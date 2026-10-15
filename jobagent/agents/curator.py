"""Curator: turns your answers about what you want into watchlist changes.

1. Claude proposes companies to add (and current ones to drop) from your preferences.
2. Code finds each company's job board by probing the public Greenhouse / Ashby / Lever APIs
   (or reading its careers page) and verifies it lists jobs.
3. You tick what to keep; only then is config/watchlist.yaml edited.
"""
import json
import re
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from typing import List, Optional

import httpx
import yaml
from pydantic import BaseModel

from .. import config, llm, paths, watchlist

PREFS_YAML = paths.CONFIG / "search_prefs.yaml"
PROPOSAL_JSON = paths.DATA / "watchlist_proposal.json"
_UA = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 Chrome/141.0 Safari/537.36"}


# ---------------------------------------------------------------- preferences

def load_prefs() -> Optional[dict]:
    return yaml.safe_load(PREFS_YAML.read_text()) if PREFS_YAML.exists() else None


def save_prefs(prefs: dict) -> None:
    prefs = {**prefs, "saved": datetime.now().isoformat(timespec="seconds")}
    PREFS_YAML.write_text(yaml.safe_dump(prefs, sort_keys=False, allow_unicode=True, width=100))


# ---------------------------------------------------------------- job-board detection

_PATTERNS = [
    ("greenhouse", re.compile(r"(?:job-)?boards(?:-api)?\.greenhouse\.io/(?:v1/boards/|embed/job_board\?for=)?([A-Za-z0-9_-]+)")),
    ("lever", re.compile(r"jobs\.lever\.co/([A-Za-z0-9_-]+)")),
    ("ashby", re.compile(r"jobs\.ashbyhq\.com/([A-Za-z0-9_.%-]+)")),
    ("workday", re.compile(r"([a-z0-9-]+)\.(wd\d+)\.myworkdayjobs\.com/(?:[a-z]{2}-[A-Z]{2}/)?([A-Za-z0-9_-]+)")),
]
_NOT_SLUGS = {"embed", "v1", "jobs", "job_app", "api", "careers", "search"}


def _count(ats: str, slug: str) -> Optional[dict]:
    """Jobs listed on a board, or None if the board doesn't exist."""
    try:
        if ats == "greenhouse":
            meta = httpx.get(f"https://boards-api.greenhouse.io/v1/boards/{slug}", timeout=15)
            if meta.status_code != 200:
                return None
            jobs = httpx.get(f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs", timeout=20).json()["jobs"]
            return {"board_name": meta.json().get("name", ""), "jobs": len(jobs),
                    "sample": [f"{j['title']} ({(j.get('location') or {}).get('name', '')})" for j in jobs[:8]]}
        if ats == "ashby":
            r = httpx.get(f"https://api.ashbyhq.com/posting-api/job-board/{slug}", timeout=20)
            if r.status_code != 200:
                return None
            jobs = r.json().get("jobs") or []
            return {"board_name": "", "jobs": len(jobs),
                    "sample": [f"{j['title']} ({j.get('location', '')})" for j in jobs[:8]]}
        if ats == "lever":
            r = httpx.get(f"https://api.lever.co/v0/postings/{slug}?mode=json", timeout=20)
            if r.status_code != 200 or not isinstance(r.json(), list):
                return None
            jobs = r.json()
            return {"board_name": "", "jobs": len(jobs),
                    "sample": [f"{j['text']} ({j.get('categories', {}).get('location', '')})" for j in jobs[:8]]} if jobs else None
    except (httpx.HTTPError, ValueError, KeyError):
        return None
    return None


def _from_text(text: str) -> Optional[dict]:
    for ats, rx in _PATTERNS:
        for m in rx.finditer(text):
            if ats == "workday":
                tenant, wd, site = m.groups()
                return {"ats": "workday", "host": f"{tenant}.{wd}.myworkdayjobs.com", "tenant": tenant, "site": site}
            slug = m.group(1)
            if slug.lower() not in _NOT_SLUGS:
                return {"ats": ats, "slug": slug}
    return None


def _slug_guesses(name: str) -> List[str]:
    base = re.sub(r"\b(inc|llc|ltd|corp|corporation|co|technologies|labs?)\b\.?", "", name, flags=re.I).strip()
    words = re.findall(r"[A-Za-z0-9]+", base)
    out = ["".join(words).lower(), "-".join(words).lower(), words[0].lower() if words else ""]
    out += [o + "hq" for o in out[:1]] + ["".join(re.findall(r"[A-Za-z0-9]+", name)).lower()]
    return [g for g in dict.fromkeys(out) if g]


def _name_ok(company: str, board_name: str) -> bool:
    if not board_name:
        return True
    a, b = re.sub(r"[^a-z0-9]", "", company.lower()), re.sub(r"[^a-z0-9]", "", board_name.lower())
    return a[:5] in b or b[:5] in a


def _scan_page(url: str) -> Optional[dict]:
    """Look for a job-board link on a careers page: plain download first, then a rendered page."""
    try:
        page = httpx.get(url, headers=_UA, follow_redirects=True, timeout=20)
        found = _from_text(str(page.url)) or _from_text(page.text)
        if found:
            return found
    except httpx.HTTPError:
        pass
    try:
        from playwright.sync_api import sync_playwright
        with sync_playwright() as pw:
            browser = pw.chromium.launch()
            pg = browser.new_page()
            pg.goto(url, wait_until="networkidle", timeout=30000)
            pg.wait_for_timeout(1500)
            text = " ".join([pg.url, pg.content()] + [f.url for f in pg.frames])
            browser.close()
        return _from_text(text)
    except Exception:
        return None


class _Careers(BaseModel):
    careers_url: str


def _careers_url(name: str) -> str:
    try:
        return llm.structured("Give the official careers page URL of the company. Only the URL.",
                              name, _Careers, model="haiku", label="careers-url").careers_url
    except llm.LLMError:
        return ""


def _verified(name: str, found: Optional[dict], how: str) -> Optional[dict]:
    if not found:
        return None
    if found["ats"] == "workday":
        return {"entry": {"name": name, **found}, "jobs": None, "sample": [], "how": how + " (Workday)"}
    c = _count(found["ats"], found["slug"])
    return {"entry": {"name": name, **found}, **c, "how": how} if c else None


def detect(name: str, url: str = "") -> dict:
    """Find a company's job board. Returns {entry, jobs, sample, how}; entry is None if not found."""
    if url:
        hit = _verified(name, _from_text(url) or _scan_page(url), "linked from the careers page")
        if hit:
            return hit
    for slug in _slug_guesses(name):
        for ats in ("greenhouse", "ashby", "lever"):
            c = _count(ats, slug)
            if c and c["jobs"] and _name_ok(name, c["board_name"]):
                return {"entry": {"name": name, "ats": ats, "slug": slug}, **c, "how": f"{ats} board “{slug}”",
                        "guessed": True}
    if not url:
        guess = _careers_url(name)
        hit = _verified(name, _scan_page(guess), f"linked from {guess}") if guess else None
        if hit:
            return hit
    return {"entry": None, "jobs": None, "sample": [],
            "how": "no public job board found; can fall back to web search (~$0.10 per scout)"}


class _Same(BaseModel):
    name: str
    same_company: bool
    reason: str


class _SameList(BaseModel):
    items: List[_Same]


def confirm(found: List[dict], context: dict = None) -> None:
    """Boards found by guessing a slug can belong to another company with the same name, or be
    a test account. Claude checks the sample jobs; rejected boards get entry=None."""
    todo = [d for d in found if d.get("guessed") and d["entry"]]
    if not todo:
        return
    context = context or {}
    listing = "\n".join(f"- {d['entry']['name']} ({context.get(d['entry']['name'], '')}): {d['jobs']} jobs, e.g. "
                        + "; ".join(d["sample"]) for d in todo)
    out = llm.structured(
        "Each line is a company and some jobs from a job board found by guessing its name. Say whether "
        "the board really belongs to that company: the jobs should fit what the company does and where it "
        "operates. Placeholder/test titles or an unrelated business mean same_company=false.",
        listing, _SameList, model=config.profile()["models"].get("matcher", "sonnet"), label="board-check")
    verdict = {v.name.lower(): v for v in out.items}
    for d in todo:
        v = verdict.get(d["entry"]["name"].lower())
        if v and not v.same_company:
            d["how"] = f"guessed {d['entry']['ats']} board “{d['entry'].get('slug')}” looks like another company: {v.reason}"
            d["entry"], d["jobs"], d["sample"] = None, None, []


_BROAD = {"us", "usa", "u.s.", "united states", "america", "remote", "north america", "anywhere"}


# ---------------------------------------------------------------- proposals

class AddCompany(BaseModel):
    name: str
    tier: str            # short snake_case group, e.g. big_tech, ai_lab, startup, fintech, healthcare
    reason: str
    careers_url: str = ""


class DropCompany(BaseModel):
    name: str
    reason: str


class Proposal(BaseModel):
    add: List[AddCompany]
    remove: List[DropCompany]
    target_roles: List[str]
    location_terms: List[str]    # words that appear in posting locations, for the scout filter
    summary: str


SYSTEM = """You help a job seeker decide which companies to watch for openings.
You get their stated preferences, their profile, and the companies already on the list.

- add: real companies that fit the preferences and are likely hiring for the target roles in
  the target locations. Respect the requested company types, stages and industries, and any
  companies they named. Do not repeat companies already on the list. Give the official
  careers page URL when you know it. Aim for the number of companies they asked for, counting
  the ones already on the list that still fit.
- remove: companies on the current list that clearly conflict with the preferences (excluded
  by name, wrong type or stage, no presence in the target locations). Don't remove for weak reasons.
- tier: a short snake_case group name; reuse existing tiers where they fit.
- target_roles: role titles to search for, from the preferences.
- location_terms: short strings that appear in job-posting locations for the target places
  (city names, metro names, state names, abbreviations like "NYC", ", CA"). Include suburbs
  that big employers use (e.g. Redmond, Bellevue for Seattle; Menlo Park, Sunnyvale for the Bay Area).
  Never include country names or "Remote": they would match every posting (remote US roles are
  handled separately).
- summary: one or two sentences on what you changed and why."""


def propose(prefs: dict, log=print) -> dict:
    current = watchlist.companies()
    listing = "\n".join(f"- {c['name']} ({c.get('tier', '')})" for c in current)
    profile_md = paths.PROFILE_MD.read_text() if paths.PROFILE_MD.exists() else ""
    log("asking Claude for company suggestions…")
    p = llm.structured(SYSTEM, f"PREFERENCES:\n{yaml.safe_dump(prefs, allow_unicode=True)}\n\n"
                               f"PROFILE:\n{profile_md[:6000]}\n\nCURRENT LIST:\n{listing}",
                       Proposal, model=config.profile()["models"].get("curator", "opus"), label="curator")
    names = {c["name"].lower() for c in current}
    adds = [a for a in p.add if a.name.lower() not in names]
    log(f"finding job boards for {len(adds)} companies…")
    with ThreadPoolExecutor(max_workers=8) as ex:
        found = list(ex.map(lambda a: detect(a.name, a.careers_url), adds))
    confirm(found, {a.name: a.reason for a in adds})
    rows = []
    for a, d in zip(adds, found):
        entry = d["entry"] or {"name": a.name, "ats": "web", "careers_url": a.careers_url}
        entry = {"name": a.name, "tier": a.tier, **{k: v for k, v in entry.items() if k != "name"}}
        rows.append({"entry": entry, "reason": a.reason, "jobs": d["jobs"], "sample": d["sample"],
                     "how": d["how"], "verified": d["entry"] is not None})
    out = {"created": datetime.now().isoformat(timespec="seconds"), "summary": p.summary,
           "add": rows, "remove": [r.model_dump() for r in p.remove if r.name.lower() in names],
           "target_roles": p.target_roles, "location_terms": p.location_terms}
    PROPOSAL_JSON.write_text(json.dumps(out, indent=2, ensure_ascii=False))
    log(f"{sum(r['verified'] for r in rows)}/{len(rows)} job boards verified")
    return out


def apply(proposal: dict, add_names: List[str], remove_names: List[str], update_filters: bool) -> None:
    keep = {n.lower() for n in add_names}
    for r in proposal["add"]:
        if r["entry"]["name"].lower() in keep:
            watchlist.add(r["entry"])
    for n in remove_names:
        watchlist.remove(n)
    if update_filters:
        _update_profile(proposal["target_roles"], proposal["location_terms"])


def _update_profile(roles: List[str], terms: List[str]) -> None:
    from ruamel.yaml import YAML
    y = YAML()
    y.preserve_quotes = True
    y.width = 4096
    y.indent(mapping=2, sequence=4, offset=2)
    doc = y.load(paths.PROFILE_YAML.read_text())
    if roles:
        doc["target_roles"] = roles
    terms = [t for t in terms if t.strip().lower() not in _BROAD]
    if terms:
        doc["scout"]["locations"] = [re.escape(t) if not t.startswith(",") else r",\s*" + re.escape(t[1:].strip())
                                     for t in terms]
    with paths.PROFILE_YAML.open("w") as f:
        y.dump(doc, f)
