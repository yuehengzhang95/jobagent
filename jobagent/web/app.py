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

from .. import apply_session, config, db, paths, pipeline, sources
from ..agents import matcher, scout, submitter

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
        "tier": tier, "unscored": unscored, "score_n": config.profile()["scout"].get("score_per_run", 60)})


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


@app.post("/scout")
def run_scout(include_web: Optional[str] = Form(None), score_n: int = Form(60)):
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
