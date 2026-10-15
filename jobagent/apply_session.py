"""Submitter, step 2: one browser session per application, driven from the web UI.

open -> fill -> screenshot -> wait for your approval -> submit -> check confirmation.
The browser window stays visible, so a CAPTCHA, an emailed security code or a field the
filler missed can be finished by hand; then mark it applied in the UI.
"""
import queue
import threading
import traceback
from datetime import datetime
from pathlib import Path

from . import config, db, mailbox
from .agents import submitter

WAIT_MINUTES = 30
_sessions: dict = {}
_lock = threading.Lock()


def active(job_id: str) -> bool:
    s = _sessions.get(job_id)
    return bool(s and s.is_alive())


def command(job_id: str, cmd: str) -> bool:
    s = _sessions.get(job_id)
    if not (s and s.is_alive()):
        return False
    s.commands.put(cmd)
    return True


def start(job_id: str) -> None:
    with _lock:
        if active(job_id):
            return
        s = _Session(job_id)
        _sessions[job_id] = s
        s.start()


class _Session(threading.Thread):
    def __init__(self, job_id: str):
        super().__init__(daemon=True, name=f"apply-{job_id}")
        self.job_id = job_id
        self.commands: queue.Queue = queue.Queue()
        row = db.get(job_id)
        self.run_dir = Path(row["run_dir"])
        self.plan = submitter.load_plan(self.run_dir)

    def _state(self, state: str, status: str = None, **extra) -> None:
        self.plan.session = {**self.plan.session, "state": state,
                             "updated": datetime.now().strftime("%H:%M:%S"), **extra}
        submitter.save_plan(self.plan, self.run_dir)
        if status:
            db.update(self.job_id, status=status)

    def _wait(self, allowed: set) -> str:
        try:
            while True:
                cmd = self.commands.get(timeout=WAIT_MINUTES * 60)
                if cmd in allowed:
                    return cmd
        except queue.Empty:
            return "timeout"

    def run(self):
        from playwright.sync_api import sync_playwright
        cfg = config.profile().get("submitter", {})
        a = config.answers()
        try:
            with sync_playwright() as p:
                # Playwright's bundled Chromium: system Chrome under automation hydrates the
                # Greenhouse form very slowly and then stalls
                browser = p.chromium.launch(headless=cfg.get("headless", False))
                page = browser.new_page(viewport={"width": 1280, "height": 900})
                self._state("filling", "applying", problems=[])
                driver = (_Greenhouse if self.plan.ats == "greenhouse" else _Generic)(self.plan, a)
                driver.open(page)
                problems = driver.fill(page)
                shot = self.run_dir / "apply_form.png"
                page.screenshot(path=str(shot), full_page=True)
                self._state("awaiting_approval", "awaiting_approval", problems=problems, screenshot=shot.name)

                cmd = self._wait({"submit", "cancel", "done"})
                if cmd == "done":
                    self._finish_manual()
                    return
                if cmd != "submit":
                    self._state("cancelled" if cmd == "cancel" else "timed_out", "ready", problems=problems)
                    return

                self._state("submitting", "submitting", problems=problems)
                clicked = mailbox.now()
                result = driver.submit(page)
                if result == "verify_code" and mailbox.configured():
                    self._state("waiting_for_code", "submitting", problems=problems)
                    code = mailbox.wait_for_code(self.plan.ats, clicked, log=lambda m: None)
                    if code:
                        result = driver.enter_code(page, code)
                shot = self.run_dir / "apply_result.png"
                page.screenshot(path=str(shot), full_page=True)
                if result == "submitted":
                    self._state("submitted", "applied", screenshot=shot.name,
                                submitted_at=datetime.now().isoformat(timespec="seconds"))
                    page.wait_for_timeout(3000)
                    return
                why = {"verify_code": "The site emailed you a security code. Enter it in the open browser window."
                                       + ("" if mailbox.configured() else
                                          " (Set up Gmail access to have this done for you; see jobagent/mailbox.py.)"),
                       "invalid": "The form rejected some fields. Fix them in the open browser window.",
                       "unknown": "No confirmation page appeared (maybe a CAPTCHA). Check the open browser window."}
                self._state("needs_manual", "needs_manual", screenshot=shot.name, problems=problems + [why[result]])
                if self._wait({"done", "cancel"}) == "done":
                    self._finish_manual()
                else:
                    self._state("closed", "needs_manual")
        except Exception as e:
            traceback.print_exc()
            self._state("failed", "needs_manual", problems=[f"{type(e).__name__}: {str(e).splitlines()[0][:300]}"])

    def _finish_manual(self):
        self._state("submitted", "applied", submitted_at=datetime.now().isoformat(timespec="seconds"),
                    manual=True)


class _Greenhouse:
    """Hand-written filler for Greenhouse hosted forms (fast, no LLM calls)."""

    def __init__(self, plan, answers):
        from .ats import greenhouse
        self.gh, self.plan, self.pick = greenhouse, plan, answers["contact"]["location_pick"]

    def open(self, page):
        self.gh.open_form(page, self.plan)

    def fill(self, page):
        return self.gh.fill(page, self.plan, self.pick)

    def submit(self, page):
        return self.gh.submit(page)

    def enter_code(self, page, code):
        return self.gh.enter_code(page, code)


class _Generic:
    """Claude reads the page, Playwright acts, Claude checks the result (any ATS)."""

    def __init__(self, plan, answers):
        from .ats import generic
        self.g, self.plan, self.pick = generic, plan, answers["contact"]["location_pick"]
        self.form = None

    def open(self, page):
        self.form = self.g.open_form(page, self.plan.form_url)
        if self.form.page != "form":
            raise RuntimeError(f"expected an application form, the page looks like: {self.form.page}")

    def _act_all(self, page, fields):
        problems = []
        for f in fields:
            q = self.g.match(self.form.questions, f.label)
            if q is None:
                if f.required:
                    problems.append(f"{f.label}: not found on the page")
                continue
            try:
                self.g.act(page, q, f.answer, self.pick if f.kind == "location" else "")
            except Exception as e:
                problems.append(f"{f.label}: {type(e).__name__}: {str(e).splitlines()[0][:120]}")
        return problems

    def fill(self, page):
        todo = [f for f in self.plan.fields if f.answer and f.source != "skip"
                and not (f.needs_approval and not f.approved)]
        problems = self._act_all(page, todo)
        intended = {f.label: (f.answer.split("/")[-1] if f.kind == "file" else f.answer) for f in todo}
        issues = self.g.check(page, intended)
        if issues:
            # one repair pass on the fields the checker flagged
            flagged = [f for f in todo if any(i.startswith(f.label[:40]) for i in issues)]
            if flagged:
                self._act_all(page, flagged)
                issues = self.g.check(page, intended)
        return problems + issues

    def submit(self, page):
        if not self.form.submit_target or page.locator(f"[data-ja='{self.form.submit_target}']").count() == 0:
            self.form = self.g.read(page)
        return self.g.submit(page, self.form)

    def enter_code(self, page, code):
        return self.g.enter_code(page, code)
