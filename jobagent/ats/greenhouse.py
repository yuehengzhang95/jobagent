"""Fill and submit a Greenhouse hosted application form (job-boards.greenhouse.io) with Playwright."""
import re
from typing import List

from playwright.sync_api import Page

from ..agents.submitter import _DECLINE, Field, Plan

CONFIRMED = re.compile(r"thank you for (applying|your application)|application (has been )?(received|submitted)"
                       r"|we.ve received your application", re.I)
VERIFY_CODE = re.compile(r"security code|verification code|enter the code", re.I)


def open_form(page: Page, plan: Plan) -> None:
    page.goto(plan.form_url, wait_until="networkidle")
    if page.locator("#first_name").count() == 0:
        # companies that host the board on their own site still serve the embed form
        slug, ext = plan.job_id.split(":")[1], plan.job_id.split(":")[2]
        page.goto(f"https://job-boards.greenhouse.io/embed/job_app?for={slug}&token={ext}", wait_until="networkidle")
    if page.locator("#first_name").count() == 0:
        raise RuntimeError(f"no Greenhouse application form found at {page.url}")
    _wait_interactive(page)


def _wait_interactive(page: Page, timeout_s: int = 45) -> None:
    """The form is server-rendered; until React hydrates, clicks and uploads silently do
    nothing. React tags hydrated DOM nodes with __react* properties."""
    page.wait_for_function(
        "() => { const e = document.querySelector('#first_name');"
        " return !!e && Object.keys(e).some(k => k.startsWith('__react')); }", timeout=timeout_s * 1000)
    page.wait_for_timeout(500)


def _choose(page: Page, fid: str, option: str, typed: str = "", attempts: int = 2) -> bool:
    for _ in range(attempts):
        if _choose_once(page, fid, option, typed):
            return True
        page.wait_for_timeout(1000)
    return False


def _choose_once(page: Page, fid: str, option: str, typed: str) -> bool:
    page.click(f"#{fid}")
    if typed:
        page.fill(f"#{fid}", "")
    if typed:
        page.keyboard.type(typed, delay=40)
    opts = page.locator(f"[id^='react-select-{fid}-option']")
    try:
        opts.first.wait_for(timeout=8000)
    except Exception:
        page.keyboard.press("Escape")
        return False
    want = option.lower().strip()
    texts = [t.strip() for t in opts.all_inner_texts()]
    idx = next((i for i, t in enumerate(texts) if t.lower() == want), None)
    if idx is None:
        idx = next((i for i, t in enumerate(texts) if t.lower().startswith(want)), None)
    if idx is None and want == "decline":
        idx = next((i for i, t in enumerate(texts) if _DECLINE.search(t)), None)
    if idx is None:
        page.keyboard.press("Escape")
        return False
    opts.nth(idx).click()
    return True


def fill(page: Page, plan: Plan, location_pick: str) -> List[str]:
    """Fill every answered field. Returns problems (missing fields, options not found)."""
    problems = []
    for f in plan.fields:
        if not f.answer or f.source == "skip" or (f.needs_approval and not f.approved):
            continue
        el = page.locator(f"#{f.id}")
        if el.count() == 0:
            if f.required:
                problems.append(f"{f.label}: field not on the page")
            continue
        try:
            ok = _fill_one(page, f, location_pick)
        except Exception as e:
            ok = False
            problems.append(f"{f.label}: {type(e).__name__}: {str(e).splitlines()[0][:120]}")
            continue
        if not ok:
            problems.append(f"{f.label}: upload not confirmed" if f.kind == "file"
                            else f"{f.label}: could not select “{f.answer}”")
    return problems


def _fill_one(page: Page, f: Field, location_pick: str) -> bool:
    sel = f"#{f.id}"
    if f.kind in ("text", "textarea"):
        page.fill(sel, f.answer)
        return True
    if f.kind == "file":
        page.set_input_files(sel, f.answer)
        name = f.answer.split("/")[-1]
        try:
            page.get_by_text(name).first.wait_for(timeout=10000)
            return True
        except Exception:
            return False
    if f.kind == "select":
        return _choose(page, f.id, f.answer)
    if f.kind == "multiselect":
        return all(_choose(page, f.id, a.strip()) for a in f.answer.split("|") if a.strip())
    if f.kind == "country":
        return _choose(page, f.id, f.answer, typed=f.answer)
    if f.kind == "location":
        return _choose(page, f.id, location_pick, typed=f.answer)
    return False


def submit(page: Page) -> str:
    """Click submit. Returns 'submitted', 'verify_code', 'invalid' or 'unknown'."""
    page.get_by_role("button", name=re.compile(r"submit application", re.I)).click()
    return _outcome(page)


def enter_code(page: Page, code: str) -> str:
    """Type the emailed security code and resubmit. Same return values as submit()."""
    boxes = page.locator("input[maxlength='1']:visible")
    if boxes.count() >= len(code):
        for i, ch in enumerate(code):
            boxes.nth(i).fill(ch)
    else:
        field = page.locator("input[id*='security' i]:visible, input[name*='security' i]:visible, "
                             "input[id*='code' i]:visible, input[aria-label*='code' i]:visible").first
        field.fill(code)
    page.get_by_role("button", name=re.compile(r"submit", re.I)).last.click()
    return _outcome(page, code_entered=True)


def _outcome(page: Page, code_entered: bool = False) -> str:
    for _ in range(30):
        page.wait_for_timeout(1000)
        body = page.inner_text("body")
        if "confirmation" in page.url or CONFIRMED.search(body):
            return "submitted"
        if VERIFY_CODE.search(body) and not code_entered:
            return "verify_code"
    if page.locator("[aria-invalid='true'], .helper-text--error").count():
        return "invalid"
    return "unknown"
