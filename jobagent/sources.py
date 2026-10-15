"""Job sources. Each adapter lists postings for one company from config/watchlist.yaml.

Deterministic adapters (free, structured): greenhouse, lever, ashby, eightfold, workday.
`web` uses Claude with WebSearch for career sites without a public API (Google, Meta, Apple).
Descriptions that are not in the listing response are fetched lazily by `fetch_description`.
"""
import html as htmllib
import re
import time
from datetime import datetime, timezone
from typing import Callable, Dict, List, Optional
from urllib.parse import quote

import httpx
from bs4 import BeautifulSoup
from pydantic import BaseModel

from . import llm

UA = {"User-Agent": "Mozilla/5.0 (job-search-agent; personal use)", "Accept": "application/json"}
TIMEOUT = 30


class Posting(BaseModel):
    source: str
    company: str
    ext_id: str
    title: str
    location: str
    url: str
    posted_at: str = ""
    description: str = ""
    detail_ref: str = ""  # adapter-specific pointer used to fetch the description later

    @property
    def id(self) -> str:
        return f"{self.source}:{re.sub(r'[^a-z0-9]+', '', self.company.lower())}:{self.ext_id}"


def html_to_text(s: str) -> str:
    soup = BeautifulSoup(htmllib.unescape(s or ""), "html.parser")
    return "\n".join(l.strip() for l in soup.get_text("\n").splitlines() if l.strip())


def _iso(ts) -> str:
    if ts in (None, ""):
        return ""
    if isinstance(ts, (int, float)):
        ts = ts / 1000 if ts > 1e11 else ts
        return datetime.fromtimestamp(ts, tz=timezone.utc).date().isoformat()
    return str(ts)[:10]


def _get(url: str, pause: float = 0.0, **kw):
    for attempt in range(4):
        if pause:
            time.sleep(pause)
        r = httpx.get(url, headers=UA, timeout=TIMEOUT, follow_redirects=True, **kw)
        if r.status_code == 429 and attempt < 3:
            time.sleep(10 * (attempt + 1))
            continue
        r.raise_for_status()
        return r.json()


# ---------------------------------------------------------------- adapters

def greenhouse(c: dict, _queries: List[str]) -> List[Posting]:
    data = _get(f"https://boards-api.greenhouse.io/v1/boards/{c['slug']}/jobs?content=true")
    return [Posting(source="greenhouse", company=c["name"], ext_id=str(j["id"]), title=j["title"].strip(),
                    location=j.get("location", {}).get("name", ""), url=j["absolute_url"],
                    posted_at=_iso(j.get("first_published") or j.get("updated_at")),
                    description=html_to_text(j.get("content", "")))
            for j in data.get("jobs", [])]


def lever(c: dict, _queries: List[str]) -> List[Posting]:
    data = _get(f"https://api.lever.co/v0/postings/{c['slug']}?mode=json")
    out = []
    for j in data:
        cat = j.get("categories", {})
        locs = cat.get("allLocations") or [cat.get("location", "")]
        desc = "\n\n".join([j.get("descriptionPlain", "")] +
                           [f"{l.get('text', '')}\n{html_to_text(l.get('content', ''))}" for l in j.get("lists", [])] +
                           [j.get("additionalPlain", "")])
        out.append(Posting(source="lever", company=c["name"], ext_id=j["id"], title=j["text"].strip(),
                           location="; ".join(filter(None, locs)), url=j["hostedUrl"],
                           posted_at=_iso(j.get("createdAt")), description=desc.strip()))
    return out


def ashby(c: dict, _queries: List[str]) -> List[Posting]:
    data = _get(f"https://api.ashbyhq.com/posting-api/job-board/{c['slug']}?includeCompensation=true")
    out = []
    for j in data.get("jobs", []):
        if not j.get("isListed", True):
            continue
        locs = [j.get("location", "")] + [s.get("location", "") for s in j.get("secondaryLocations", [])]
        if j.get("isRemote"):
            locs.append("Remote")
        desc = j.get("descriptionPlain") or html_to_text(j.get("descriptionHtml", ""))
        comp = (j.get("compensation") or {}).get("compensationTierSummary")
        if comp:
            desc += f"\n\nCompensation: {comp}"
        out.append(Posting(source="ashby", company=c["name"], ext_id=j["id"], title=j["title"].strip(),
                           location="; ".join(filter(None, locs)), url=j["jobUrl"],
                           posted_at=_iso(j.get("publishedAt")), description=desc))
    return out


def eightfold(c: dict, queries: List[str]) -> List[Posting]:
    """Eightfold career sites (Microsoft, Netflix). Description fetched lazily."""
    host, domain = c["host"], c["domain"]
    seen: Dict[str, Posting] = {}
    try:
        _eightfold_pages(c, queries, host, domain, seen)
    except httpx.HTTPStatusError:
        if not seen:  # rate-limited partway: keep what we have
            raise
    return list(seen.values())


def _eightfold_pages(c: dict, queries: List[str], host: str, domain: str, seen: Dict[str, Posting]) -> None:
    for q in queries:
        for loc in c.get("locations", [""]):
            for start in range(0, c.get("max_results", 100), 10):
                if c.get("api") == "v2":
                    url = (f"https://{host}/api/apply/v2/jobs?domain={domain}&query={quote(q)}"
                           f"&location={quote(loc)}&start={start}&num=10")
                    rows = _get(url, pause=2.0).get("positions", [])
                else:
                    url = (f"https://{host}/api/pcsx/search?domain={domain}&query={quote(q)}"
                           f"&location={quote(loc)}&start={start}")
                    rows = _get(url, pause=2.0).get("data", {}).get("positions", [])
                for j in rows:
                    pid = str(j["id"])
                    link = j.get("canonicalPositionUrl") or f"https://{host}{j.get('positionUrl', '')}"
                    seen[pid] = Posting(source="eightfold", company=c["name"], ext_id=pid,
                                        title=j.get("name", "").strip(),
                                        location="; ".join(j.get("standardizedLocations") or j.get("locations") or []),
                                        url=link, posted_at=_iso(j.get("postedTs") or j.get("t_create")),
                                        detail_ref=f"{c.get('api', 'pcsx')}|{host}|{domain}|{pid}")
                if len(rows) < 10:
                    break


def workday(c: dict, queries: List[str]) -> List[Posting]:
    base = f"https://{c['host']}/wday/cxs/{c['tenant']}/{c['site']}"
    seen: Dict[str, Posting] = {}
    for q in queries:
        for offset in range(0, c.get("max_results", 100), 20):
            r = httpx.post(f"{base}/jobs", headers=UA, timeout=TIMEOUT,
                           json={"appliedFacets": {}, "limit": 20, "offset": offset, "searchText": q})
            r.raise_for_status()
            rows = r.json().get("jobPostings", [])
            for j in rows:
                path = j["externalPath"]
                seen[path] = Posting(source="workday", company=c["name"], ext_id=path.rsplit("_", 1)[-1],
                                     title=j["title"].strip(), location=j.get("locationsText", ""),
                                     url=f"https://{c['host']}/{c['site']}{path}",
                                     detail_ref=f"{base}{path}")
            if len(rows) < 20:
                break
    return list(seen.values())


class _WebJob(BaseModel):
    title: str
    location: str
    url: str
    posted_at: str = ""
    summary: str


class _WebJobs(BaseModel):
    jobs: List[_WebJob]


WEB_SYSTEM = """You find open job postings on one company's official careers site.
Only return postings you actually saw on the company's own careers domain in this session,
with the exact title, the location(s) as listed, and the official posting URL. Never guess
URLs. summary = 2-4 sentences on the team and core requirements, from the posting."""


def web(c: dict, queries: List[str], targets: Optional[dict] = None) -> List[Posting]:
    targets = targets or {}
    prompt = (f"Company: {c['name']} (careers site: {c.get('careers_url', 'official site')}).\n"
              f"Find up to {c.get('max_results', 15)} currently open roles matching any of: {queries}.\n"
              f"Locations: {targets.get('locations')}.\n"
              f"Exclude these levels: {targets.get('exclude_levels')}.")
    found = llm.structured(WEB_SYSTEM, prompt, _WebJobs, model="sonnet", web=True, label=f"scout:{c['name']}")
    return [Posting(source="web", company=c["name"], ext_id=re.sub(r"\W+", "", j.url)[-40:], title=j.title,
                    location=j.location, url=j.url, posted_at=j.posted_at, description=j.summary,
                    detail_ref="web")
            for j in found.jobs]


ADAPTERS: Dict[str, Callable] = {
    "greenhouse": greenhouse, "lever": lever, "ashby": ashby,
    "eightfold": eightfold, "workday": workday, "web": web,
}


# ---------------------------------------------------------------- lazy details

class _JD(BaseModel):
    description: str


def fetch_description(p: Posting) -> Posting:
    """Fill in description (and location for Workday) when the listing didn't include it."""
    if p.source == "eightfold":
        api, host, domain, pid = p.detail_ref.split("|")
        if api == "v2":
            d = _get(f"https://{host}/api/apply/v2/jobs/{pid}?domain={domain}")
            p.description = html_to_text(d.get("job_description", ""))
        else:
            d = _get(f"https://{host}/api/pcsx/position_details?position_id={pid}&domain={domain}&hl=en")["data"]
            p.description = html_to_text(d.get("jobDescription", ""))
            p.url = d.get("publicUrl") or p.url
    elif p.source == "workday":
        d = _get(p.detail_ref)["jobPostingInfo"]
        p.description = html_to_text(d.get("jobDescription", ""))
        p.location = "; ".join([d.get("location", "")] + d.get("additionalLocations", []))
        p.posted_at = d.get("startDate", p.posted_at)
    elif p.source == "web" and p.detail_ref == "web":
        jd = llm.structured("Fetch the job posting URL and return its full description text verbatim "
                            "(responsibilities, qualifications, compensation). No commentary.",
                            p.url, _JD, model="sonnet", web=True, label="scout:fetch-jd")
        p.description, p.detail_ref = jd.description, "web:fetched"
    return p
