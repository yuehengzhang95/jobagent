"""Scout: pulls postings from the watchlist, filters by title/level/location, stores new ones,
then has the Matcher score them."""
import re
import sys
from concurrent.futures import ThreadPoolExecutor
from typing import Callable, List, Optional

import yaml
from pydantic import BaseModel, Field

from .. import config, db, paths, sources
from . import matcher

WATCHLIST = paths.CONFIG / "watchlist.yaml"


def _rx(words: List[str]) -> re.Pattern:
    return re.compile(r"\b(" + "|".join(words) + r")\b", re.I)


def _filters(cfg: dict):
    f = cfg["scout"]
    return _rx(f["title_include"]), _rx(f["title_exclude"]), _rx(f["locations"])


def title_ok(title: str, inc: re.Pattern, exc: re.Pattern) -> bool:
    return bool(inc.search(title)) and not exc.search(title)


_US = re.compile(r"\b(US|USA|United States|U\.S\.|North America)\b", re.I)


def location_ok(location: str, loc_rx: re.Pattern) -> bool:
    if loc_rx.search(location):
        return True
    # remote counts only when US-based or remote with no other place named
    if "remote" in location.lower():
        return bool(_US.search(location)) or location.strip().lower() in ("remote", "remote (us)")
    return False


def _needs_detail_for_location(p: sources.Posting) -> bool:
    return p.source == "workday" and bool(re.search(r"\d+ Locations", p.location))


def scout(only: Optional[List[str]] = None, log: Callable[[str], None] = lambda m: print(m, file=sys.stderr)) -> dict:
    cfg = config.profile()
    watch = yaml.safe_load(WATCHLIST.read_text())
    inc, exc, loc_rx = _filters(cfg)
    targets = {"locations": cfg["locations"], "exclude_levels": cfg["seniority"]["exclude"]}
    companies = [c for c in watch["companies"] if c.get("enabled", True)
                 and (not only or c["name"].lower() in {o.lower() for o in only})]
    stats = {"companies": 0, "listed": 0, "kept": 0, "new": 0, "errors": []}

    def run(c: dict):
        adapter = sources.ADAPTERS[c["ats"]]
        try:
            if c["ats"] == "web":
                postings = adapter(c, watch["queries"], targets)
            else:
                postings = adapter(c, watch["queries"])
        except Exception as e:  # one broken board shouldn't stop the scout
            return c, [], 0, f"{c['name']}: {e!r}"[:300]
        kept = []
        for p in postings:
            if not title_ok(p.title, inc, exc):
                continue
            if _needs_detail_for_location(p):
                try:
                    p = sources.fetch_description(p)
                except Exception:
                    continue
            if location_ok(p.location, loc_rx):
                kept.append(p)
        return c, kept, len(postings), None

    with ThreadPoolExecutor(max_workers=8) as pool:
        for c, kept, listed, err in pool.map(run, companies):
            stats["companies"] += 1
            stats["listed"] += listed
            stats["kept"] += len(kept)
            if err:
                stats["errors"].append(err)
                log(f"  ! {err}")
                continue
            new = sum(db.upsert_posting(p, c.get("tier", "")) for p in kept)
            stats["new"] += new
            log(f"  {c['name']}: {listed} listed, {len(kept)} match, {new} new")
    return stats


TRIAGE_SYSTEM = """You triage job titles for one candidate before a detailed review.
For each job, rate relevance 0-3 from the title, company and location alone:
3 = squarely the candidate's lane: one of their target roles, centred on their core strengths
2 = plausible: a target role type where their strengths are likely to matter
1 = weak: the right kind of role but far from their strengths, or centred on their thin areas
0 = not a fit (outside the target role types, wrong level, customer-facing sales/support)
Use the candidate's thin areas to push roles centred on them down to 1. Keep reasons short."""


class _Rating(BaseModel):
    id: str
    relevance: int = Field(ge=0, le=3)
    reason: str


class _Ratings(BaseModel):
    ratings: List[_Rating]


def triage_pending(batch: int = 80, log: Callable[[str], None] = lambda m: print(m, file=sys.stderr)) -> int:
    """One cheap LLM call per batch of titles; relevance <= 1 becomes status 'filtered'."""
    rows = db.query("status = 'discovered' AND triage IS NULL", order="first_seen DESC", limit=5000)
    profile = paths.PROFILE_MD.read_text()
    for i in range(0, len(rows), batch):
        chunk = rows[i:i + batch]
        listing = "\n".join(f"{r['id']} | {r['company']} | {r['title']} | {r['location']}" for r in chunk)
        out = matcher.llm.structured(TRIAGE_SYSTEM, f"CANDIDATE:\n{profile}\n\nJOBS (id | company | title | location):\n{listing}",
                                     _Ratings, model=config.profile()["models"]["matcher"], label="triage")
        by_id = {r.id: r for r in out.ratings}
        for r in chunk:
            rating = by_id.get(r["id"])
            if rating is None:
                continue
            db.update(r["id"], triage=rating.relevance, triage_reason=rating.reason,
                      status="filtered" if rating.relevance <= 1 else "discovered")
        log(f"  triaged {min(i + batch, len(rows))}/{len(rows)}")
    return len(rows)


def score_pending(limit: int = 50, min_desc_chars: int = 300,
                  log: Callable[[str], None] = lambda m: print(m, file=sys.stderr)) -> int:
    rows = db.query("status = 'discovered' AND triage >= 2", order="triage DESC, first_seen DESC", limit=limit)

    def one(row):
        try:
            p = db.to_posting(row)
            if len(p.description) < min_desc_chars or p.detail_ref == "web":
                p = sources.fetch_description(p)
                db.update(row["id"], description=p.description, location=p.location, url=p.url,
                          detail_ref=p.detail_ref, posted_at=p.posted_at)
            jd = f"{p.title}\n{p.company}\n{p.location}\n{p.url}\n\n{p.description}"
            a = matcher.analyze(jd)
            db.update(row["id"], status="scored", fit_score=a.fit_score, archetype=a.archetype,
                      analysis=a.model_dump(), error=None)
            log(f"  {a.fit_score:3d}  {p.company} — {p.title}")
        except Exception as e:
            db.update(row["id"], error=f"scoring failed: {e!r}"[:500])
            log(f"  ! {row['company']} — {row['title']}: {e!r}"[:200])

    with ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(one, rows))
    return len(rows)
