"""Local web UI: jobs board, job detail with tailored resume review, background tasks.

Run:  .env/bin/python -m jobagent serve   ->  http://127.0.0.1:8765
"""
import json
import threading
import traceback
import uuid
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path
from typing import Optional

import markdown
from fastapi import FastAPI, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from .. import apply_session, config, db, paths, pipeline, sources, watchlist
from ..agents import curator, matcher, scout, submitter

app = FastAPI(title="Job agent")
paths.RUNS.mkdir(parents=True, exist_ok=True)
app.mount("/runs", StaticFiles(directory=paths.RUNS), name="runs")
templates = Jinja2Templates(directory=Path(__file__).parent / "templates")

# ---------------------------------------------------------------- background tasks

_pool = ThreadPoolExecutor(max_workers=3)
_tasks: dict = {}
_tasks_lock = threading.Lock()


def _start(kind: str, label: str, fn, *args) -> str:
    tid = uuid.uuid4().hex[:8]
    log = deque(maxlen=200)
    with _tasks_lock:
        _tasks[tid] = {"id": tid, "kind": kind, "label": label, "state": "running",
                       "started": datetime.now().strftime("%H:%M:%S"), "log": log}

    def run():
        try:
            fn(*args, log=log.append)
            _tasks[tid]["state"] = "done"
        except Exception as e:
            log.append(f"failed: {e!r}")
            _tasks[tid]["state"] = "failed"
            traceback.print_exc()

    _pool.submit(run)
    return tid


def _scout_task(include_web: bool, score_n: int, log):
    watch_names = None
    if not include_web:
        import yaml
        watch = yaml.safe_load(scout.WATCHLIST.read_text())
        watch_names = [c["name"] for c in watch["companies"] if c["ats"] != "web"]
    log("scouting watchlist…")
    s = scout.scout(only=watch_names, log=log)
    log(f"scout: {s['listed']} listed, {s['kept']} matched filters, {s['new']} new")
    for e in s["errors"]:
        log(f"! {e}")
    log("triaging titles…")
    scout.triage_pending(log=log)
    if score_n:
        log(f"scoring up to {score_n} jobs…")
        scout.score_pending(limit=score_n, log=log)
    log("done")


def _score_task(limit: int, log):
    scout.score_pending(limit=limit, log=log)
    log("done")


def _score_one(job_id: str, log):
    p = db.to_posting(db.get(job_id))
    if len(p.description) < 300 or p.detail_ref == "web":
        p = sources.fetch_description(p)
        db.update(job_id, description=p.description, location=p.location, url=p.url, detail_ref=p.detail_ref)
    a = matcher.analyze(_jd_text(db.get(job_id)))
    db.update(job_id, status="scored", fit_score=a.fit_score, archetype=a.archetype, analysis=a.model_dump())
    log(f"{a.fit_score}  {p.company} — {p.title}")


def _jd_text(row) -> str:
    return f"{row['title']}\n{row['company']}\n{row['location']}\n{row['url']}\n\n{row['description'] or ''}"


def _tailor_task(job_id: str, log):
    row = db.get(job_id)
    db.update(job_id, status="tailoring", error=None)
    try:
        p = db.to_posting(row)
        if len(p.description) < 300 or p.detail_ref == "web":
            log("fetching full description…")
            p = sources.fetch_description(p)
            db.update(job_id, description=p.description, location=p.location, url=p.url, detail_ref=p.detail_ref)
            row = db.get(job_id)
        analysis = matcher.JobAnalysis(**json.loads(row["analysis"])) if row["analysis"] else None
        out_dir = paths.RUNS / pipeline.slugify(row["company"], row["title"], row["ext_id"][-8:])
        log(f"tailoring → {out_dir.name}")
        pipeline.tailor_job(_jd_text(row), out_dir=out_dir, analysis=analysis, company=row["company"])
        status = json.loads((out_dir / "status.json").read_text())["status"]
        if analysis is None:
            a = json.loads((out_dir / "analysis.json").read_text())
            db.update(job_id, fit_score=a["fit_score"], archetype=a["archetype"], analysis=a)
        db.update(job_id, status="ready" if status == "verified" else "needs_review", run_dir=str(out_dir))
        log(f"{status}")
    except Exception as e:
        db.update(job_id, status="error", error=f"{e!r}"[:1000])
        raise


# ---------------------------------------------------------------- views

TABS = {
    "review": ("To review", "status IN ('scored','discovered') AND COALESCE(triage, 2) >= 2"),
    "progress": ("In progress", "status IN ('tailoring','ready','needs_review','error','applying',"
                                "'awaiting_approval','submitting','needs_manual')"),
    "applied": ("Applied", "status = 'applied'"),
    "filtered": ("Filtered out", "status = 'filtered'"),
    "skipped": ("Skipped", "status = 'skipped'"),
    "all": ("All", "1=1"),
}


@app.get("/", response_class=HTMLResponse)
def board(request: Request, tab: str = "review", q: str = "", min_fit: int = 0, tier: str = ""):
    where, params = [TABS.get(tab, TABS["review"])[1]], []
    if q:
        where.append("(company LIKE ? OR title LIKE ? OR location LIKE ?)")
        params += [f"%{q}%"] * 3
    if min_fit:
        where.append("fit_score >= ?")
        params.append(min_fit)
    if tier:
        where.append("tier = ?")
        params.append(tier)
    rows = db.query(" AND ".join(where), tuple(params),
                    order="fit_score IS NULL, fit_score DESC, triage DESC, posted_at DESC")
    counts = {k: len(db.query(v[1], limit=100000)) for k, v in TABS.items()}
    unscored = len(db.query("status = 'discovered' AND triage >= 2", limit=100000))
    return templates.TemplateResponse(request, "board.html", {
        "rows": rows, "tab": tab, "tabs": TABS, "counts": counts, "q": q, "min_fit": min_fit,
        "tier": tier, "unscored": unscored, "score_n": config.profile()["scout"].get("score_per_run", 60),
        "tiers": sorted({c.get("tier", "") for c in watchlist.companies()} - {""}),
        "needs_setup": curator.load_prefs() is None})


@app.get("/job/{job_id:path}", response_class=HTMLResponse)
def job(request: Request, job_id: str):
    row = db.get(job_id)
    if row is None:
        return HTMLResponse("not found", status_code=404)
    analysis = json.loads(row["analysis"]) if row["analysis"] else None
    run = None
    if row["run_dir"] and Path(row["run_dir"]).exists():
        d = Path(row["run_dir"])
        pdfs = sorted(d.glob(f"{config.pdf_prefix()}_*.pdf"))
        review = (d / "review.md").read_text() if (d / "review.md").exists() else ""
        # the header block duplicates what the page already shows
        review = review.split("## Changes", 1)[-1]
        run = {"dir": d.name, "pdf": f"/runs/{d.name}/{pdfs[0].name}" if pdfs else None,
               "review_html": markdown.markdown("## Changes" + review, extensions=["fenced_code"])}
    plan = submitter.load_plan(row["run_dir"]) if run else None
    return templates.TemplateResponse(request, "job.html", {
        "job": row, "a": analysis, "run": run, "plan": plan,
        "can_apply": True, "session_open": apply_session.active(job_id)})


def _back(job_id: str):
    return RedirectResponse(f"/job/{job_id}", status_code=303)


@app.post("/job/{job_id:path}/tailor")
def tailor(job_id: str):
    db.update(job_id, status="tailoring")
    _start("tailor", db.get(job_id)["title"], _tailor_task, job_id)
    return _back(job_id)


@app.post("/job/{job_id:path}/score")
def score(job_id: str):
    _start("score", db.get(job_id)["title"], _score_one, job_id)
    return _back(job_id)


@app.post("/job/{job_id:path}/status")
def set_status(job_id: str, status: str = Form(...), next: str = Form("")):
    allowed = {"skipped", "applied", "scored", "discovered", "ready"}
    if status in allowed:
        row = db.get(job_id)
        if status == "scored" and row["fit_score"] is None:
            status = "discovered"
        db.update(job_id, status=status)
    return RedirectResponse(next or f"/job/{job_id}", status_code=303)


def _prepare_task(job_id: str, log):
    row = db.get(job_id)
    pdfs = sorted(Path(row["run_dir"]).glob(f"{config.pdf_prefix()}_*.pdf"))
    if not pdfs:
        raise RuntimeError("no tailored PDF in the run folder")
    plan = submitter.build_plan(row, pdfs[0], _jd_text(row), log=log)
    submitter.save_plan(plan, row["run_dir"])
    log(f"{len(plan.blocking)} fields need you")


@app.post("/job/{job_id:path}/apply/{cmd}")
async def apply(job_id: str, cmd: str, request: Request):
    row = db.get(job_id)
    if cmd == "prepare":
        _start("apply", f"Prepare application: {row['title']}", _prepare_task, job_id)
    elif cmd == "save":
        form = await request.form()
        plan = submitter.load_plan(row["run_dir"])
        for f in plan.fields:
            if f"a__{f.id}" in form:
                v = form.getlist(f"a__{f.id}")
                new = " | ".join(x for x in v if x) if f.kind == "multiselect" else str(v[0]).strip()
                if new != f.answer and f.source in ("needs_input", "skip") and new:
                    f.source = "learned" if f"rem__{f.id}" in form else "you"
                f.answer = new
            if f.needs_approval:
                f.approved = f"ok__{f.id}" in form
            if f"rem__{f.id}" in form and f.answer:
                submitter.remember(f.label, f.answer)
        submitter.save_plan(plan, row["run_dir"])
    elif cmd == "start":
        plan = submitter.load_plan(row["run_dir"])
        if plan and not plan.blocking:
            plan.session = {}
            submitter.save_plan(plan, row["run_dir"])
            apply_session.start(job_id)
    elif cmd in ("submit", "cancel", "done"):
        if not apply_session.command(job_id, cmd) and cmd == "done":
            db.update(job_id, status="applied")
    return RedirectResponse(f"/job/{job_id}#application", status_code=303)


# ---------------------------------------------------------------- preferences + companies

SETUP_CHOICES = {
    "seniority": ["Entry / new grad", "Mid-level", "Senior", "Staff and above", "Manager"],
    "locations": ["New York City", "San Francisco Bay Area", "Seattle area", "Los Angeles", "Boston",
                  "Austin", "Chicago", "Washington DC", "Remote (US)"],
    "company_types": ["Big tech", "AI labs", "AI-native startups", "Fintech", "Healthcare / biotech",
                      "Enterprise SaaS", "Consumer apps", "Developer tools / infra", "Hardware / robotics",
                      "Quant / trading", "E-commerce / marketplaces", "Media / entertainment"],
    "stages": ["Early (seed – Series A)", "Growth (Series B – D)", "Late / pre-IPO", "Public"],
    "list_size": ["30", "60", "100"],
}


def _setup_defaults() -> dict:
    p = config.profile()
    return {"roles": ", ".join(p.get("target_roles", [])), "seniority": ["Mid-level", "Senior"],
            "locations": [], "other_locations": "", "company_types": [], "other_types": "", "stages": [],
            "industries": "", "include": "", "exclude": "", "list_size": "60", "notes": ""}


@app.get("/setup", response_class=HTMLResponse)
def setup(request: Request, first: int = 0):
    prefs = curator.load_prefs() or {}
    values = {**_setup_defaults(), **{k: v for k, v in prefs.items() if k not in ("saved", "skipped")}}
    return templates.TemplateResponse(request, "setup.html", {
        "v": values, "choices": SETUP_CHOICES, "first": first or not prefs, "has_list": bool(watchlist.companies())})


def _propose_task(prefs: dict, log):
    curator.propose(prefs, log=log)
    log("done")


@app.post("/setup")
async def setup_save(request: Request):
    form = await request.form()
    prefs = {k: (form.getlist(k) if k in ("seniority", "locations", "company_types", "stages") else str(form.get(k, "")).strip())
             for k in ("roles", "seniority", "locations", "other_locations", "company_types", "other_types", "stages",
                       "industries", "include", "exclude", "list_size", "notes")}
    curator.save_prefs(prefs)
    curator.PROPOSAL_JSON.unlink(missing_ok=True)
    _start("curate", "Suggest companies", _propose_task, prefs)
    return RedirectResponse("/setup/review", status_code=303)


@app.post("/setup/skip")
def setup_skip():
    curator.save_prefs({**_setup_defaults(), "skipped": True})
    return RedirectResponse("/", status_code=303)


@app.get("/setup/review", response_class=HTMLResponse)
def setup_review(request: Request):
    proposal = json.loads(curator.PROPOSAL_JSON.read_text()) if curator.PROPOSAL_JSON.exists() else None
    return templates.TemplateResponse(request, "setup_review.html", {"p": proposal})


@app.post("/setup/apply")
async def setup_apply(request: Request):
    form = await request.form()
    proposal = json.loads(curator.PROPOSAL_JSON.read_text())
    curator.apply(proposal, form.getlist("add"), form.getlist("remove"), bool(form.get("update_filters")))
    curator.PROPOSAL_JSON.unlink(missing_ok=True)
    return RedirectResponse("/companies?applied=1", status_code=303)


@app.get("/companies", response_class=HTMLResponse)
def companies(request: Request, applied: int = 0, msg: str = ""):
    stats = {}
    for r in db.query("1=1", limit=1000000):
        s = stats.setdefault(r["company"].lower(), {"total": 0, "relevant": 0, "best": None, "applied": 0})
        s["total"] += 1
        s["relevant"] += r["status"] != "filtered"
        s["applied"] += r["status"] == "applied"
        if r["fit_score"] is not None:
            s["best"] = max(s["best"] or 0, r["fit_score"])
    rows = [{**c, "stats": stats.get(c["name"].lower(), {})} for c in watchlist.companies()]
    tiers = sorted({c.get("tier", "") for c in rows})
    return templates.TemplateResponse(request, "companies.html", {
        "rows": rows, "tiers": tiers, "applied": applied, "msg": msg})


@app.post("/companies/add")
def companies_add(name: str = Form(...), url: str = Form(""), tier: str = Form("other")):
    name = name.strip()
    if watchlist.find(name):
        return RedirectResponse(f"/companies?msg={name} is already on the list", status_code=303)
    d = curator.detect(name, url.strip())
    curator.confirm([d])
    entry = d["entry"] or {"name": name, "ats": "web", "careers_url": url.strip()}
    watchlist.add({"name": name, "tier": tier.strip() or "other", **{k: v for k, v in entry.items() if k != "name"}})
    jobs = f", {d['jobs']} jobs listed" if d.get("jobs") is not None else ""
    return RedirectResponse(f"/companies?msg=Added {name}: {d['how']}{jobs}", status_code=303)


@app.post("/companies/toggle")
def companies_toggle(name: str = Form(...)):
    c = watchlist.find(name)
    if c:
        watchlist.set_enabled(name, not c.get("enabled", True))
    return RedirectResponse("/companies", status_code=303)


@app.post("/companies/remove")
def companies_remove(name: str = Form(...)):
    watchlist.remove(name)
    return RedirectResponse("/companies", status_code=303)


@app.post("/scout")
def run_scout(include_web: Optional[str] = Form(None), score_n: int = Form(60)):
    if curator.load_prefs() is None:
        return RedirectResponse("/setup?first=1", status_code=303)
    _start("scout", "Scout + triage + score", _scout_task, bool(include_web), score_n)
    return RedirectResponse("/", status_code=303)


@app.post("/score")
def run_score(limit: int = Form(30)):
    _start("score", f"Score next {limit}", _score_task, limit)
    return RedirectResponse("/", status_code=303)


@app.get("/api/tasks")
def tasks():
    with _tasks_lock:
        items = sorted(_tasks.values(), key=lambda t: t["started"], reverse=True)[:8]
        return JSONResponse([{**{k: v for k, v in t.items() if k != "log"}, "log": list(t["log"])[-12:]}
                             for t in items])


@app.get("/api/job/{job_id:path}")
def job_status(job_id: str):
    row = db.get(job_id)
    return {"status": row["status"] if row else None}
