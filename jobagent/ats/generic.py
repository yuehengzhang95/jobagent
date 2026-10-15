"""AI-driven form agent for any ATS (Ashby, Lever, Workday, custom sites).

Claude reads a cleaned snapshot of the page and says what the questions are and which
element answers each one; Playwright performs the actions; Claude then re-reads the page to
check the result. Answers still come only from the plan you approved.
"""
import difflib
import re
from typing import Dict, List, Literal, Optional

from playwright.sync_api import Page
from pydantic import BaseModel

from .. import config, llm

SNAPSHOT_JS = r"""
() => {
  const KEEP = new Set(['id','name','type','role','placeholder','value','for','aria-label','aria-required','required',
    'aria-checked','aria-expanded','aria-selected','aria-pressed','checked','multiple','accept','data-ja','data-state',
    'href','maxlength','disabled']);
  let n = 0;
  document.querySelectorAll('[data-ja]').forEach(e => { n = Math.max(n, +e.dataset.ja.slice(1) + 1); });
  const SEL = 'input, textarea, select, button, a[href], [role=combobox], [role=radio], [role=checkbox], [role=option], [role=button], [contenteditable=true]';
  document.querySelectorAll(SEL).forEach(e => { if (!e.dataset.ja) e.dataset.ja = 'j' + (n++); });
  const live = new Map();
  document.querySelectorAll('[data-ja]').forEach(e => live.set(e.dataset.ja, e));
  const forms = [...document.querySelectorAll('form')].filter(f => f.querySelector('input, textarea, select'));
  const root = forms.length === 1 ? forms[0] : document.body;
  const clone = root.cloneNode(true);
  clone.querySelectorAll('script, style, svg, noscript, iframe, img, path, link, meta, header, footer, nav').forEach(e => e.remove());
  clone.querySelectorAll('[data-ja]').forEach(c => {
    const e = live.get(c.dataset.ja);
    if (!e) return;
    if (e.offsetParent === null && e.type !== 'file' && e.type !== 'checkbox' && e.type !== 'radio') c.setAttribute('data-state', 'hidden');
    if (e.matches('input, textarea, select') && e.type !== 'file') c.setAttribute('value', e.value);
    if (e.type === 'checkbox' || e.type === 'radio') c.setAttribute('checked', e.checked);
    if (e.type === 'file' && e.files && e.files.length) c.setAttribute('value', e.files[0].name);
    if (/(selected|active|checked|pressed)/i.test(e.className && e.className.baseVal === undefined ? e.className : ''))
      c.setAttribute('data-state', 'selected');
  });
  clone.querySelectorAll('*').forEach(e => { for (const a of [...e.attributes]) if (!KEEP.has(a.name)) e.removeAttribute(a.name); });
  let html = clone.outerHTML.replace(/\s+/g, ' ');
  for (let i = 0; i < 6; i++)
    html = html.replace(/<(div|span)>\s*<\/\1>/g, '').replace(/<(div|span)>\s*(<(div|span)>)/g, '$2').replace(/(<\/(div|span)>)\s*<\/(div|span)>/g, '$1');
  return html;
}
"""

Semantic = Literal["first_name", "last_name", "full_name", "preferred_name", "email", "phone", "resume",
                   "cover_letter", "location", "country", "linkedin", "github", "website", "eeo_gender",
                   "eeo_race", "eeo_hispanic", "eeo_veteran", "eeo_disability", "other"]


class Question(BaseModel):
    label: str
    description: str = ""
    semantic: Semantic
    kind: Literal["text", "textarea", "file", "select", "combobox", "date", "radio", "checkbox", "multiselect"]
    required: bool
    target: str = ""                  # data-ja of the input / select / combobox / file input
    options: List[str] = []
    option_targets: List[str] = []    # parallel to options, for radio / checkbox / button choices


class FormRead(BaseModel):
    page: Literal["form", "job_page_with_apply_button", "submitted", "verify_code", "error", "other"]
    apply_target: str = ""           # when page == job_page_with_apply_button
    submit_target: str = ""
    code_target: str = ""            # when page == verify_code: the code input (first box if split)
    errors: List[str] = []           # validation errors shown on the page
    questions: List[Question] = []


READ_SYSTEM = """You read a cleaned HTML snapshot of a job-application page. Interactive
elements carry data-ja ids (j0, j1, ...). Report what the page is and, if it is an
application form, every question on it in page order:

- label: the question text exactly as shown; description: helper text under it, short.
- semantic: what standard field it is, else "other". "Legal name"/"Full name" -> full_name.
- kind: text | textarea | file | select (native <select>) | combobox (type-to-search or
  custom dropdown) | date | radio (pick one: radio inputs OR Yes/No style buttons) |
  checkbox (a single tick box, e.g. an acknowledgement) | multiselect (several tick boxes).
- target: data-ja of the element to type into / upload to / open. For radio, checkbox and
  multiselect leave target empty and give options + option_targets (the data-ja to click
  for each option, in the same order). For a single checkbox: options = [its label text].
- required: from required / aria-required / an asterisk.
Ignore resume-autofill widgets, search boxes, cookie banners and navigation.
data-state="hidden" elements are not visible; skip them unless they are the real file input.
submit_target: the button that submits the application.
If the page shows a job description with an Apply button/link instead of a form, page =
job_page_with_apply_button and give apply_target. If it confirms the application was
received, page = submitted. If it asks for an emailed security/verification code, page =
verify_code and give code_target. List any validation errors shown in `errors`."""


def snapshot(page: Page) -> str:
    return page.evaluate(SNAPSHOT_JS)


def read(page: Page, purpose: str = "") -> FormRead:
    html = snapshot(page)
    return llm.structured(READ_SYSTEM, f"URL: {page.url}\n{purpose}\n\nSNAPSHOT:\n{html[:120000]}", FormRead,
                          model=config.profile()["models"]["matcher"], label="form-read")


def open_form(page: Page, url: str) -> FormRead:
    page.goto(url, wait_until="networkidle")
    page.wait_for_timeout(1500)
    for _ in range(3):
        r = read(page)
        if r.page == "job_page_with_apply_button" and r.apply_target:
            page.locator(f"[data-ja='{r.apply_target}']").first.click()
            page.wait_for_load_state("networkidle")
            page.wait_for_timeout(1500)
            continue
        return r
    return r


def norm(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", s.lower()).strip()


def match(questions: List[Question], label: str) -> Optional[Question]:
    exact = [q for q in questions if norm(q.label) == norm(label)]
    if exact:
        return exact[0]
    best = max(questions, key=lambda q: difflib.SequenceMatcher(None, norm(q.label), norm(label)).ratio(), default=None)
    if best and difflib.SequenceMatcher(None, norm(best.label), norm(label)).ratio() > 0.85:
        return best
    return None


def _el(page: Page, ja: str):
    return page.locator(f"[data-ja='{ja}']").first


def _click_choice(page: Page, ja: str) -> None:
    el = _el(page, ja)
    try:
        el.click(timeout=3000)
    except Exception:
        el_id = el.get_attribute("id")
        if el_id:
            page.locator(f"label[for='{el_id}']").first.click(timeout=3000)
        else:
            el.check(force=True, timeout=3000)


def act(page: Page, q: Question, answer: str, location_pick: str = "") -> None:
    if q.kind in ("text", "textarea"):
        _el(page, q.target).fill(answer)
    elif q.kind == "date":
        # answers are stored as YYYY-MM-DD (the UI uses a date picker); US forms want MM/DD/YYYY
        m = re.fullmatch(r"(\d{4})-(\d{2})-(\d{2})", answer.strip())
        if not m:
            raise ValueError(f"“{answer}” is not a date; pick one in the answer table")
        el = _el(page, q.target)
        el.click()
        el.fill(f"{m[2]}/{m[3]}/{m[1]}")
        page.keyboard.press("Enter")
        page.keyboard.press("Escape")
    elif q.kind == "file":
        _el(page, q.target).set_input_files(answer)
        page.wait_for_timeout(2000)
    elif q.kind == "select":
        _el(page, q.target).select_option(label=answer)
    elif q.kind == "combobox":
        el = _el(page, q.target)
        el.click()
        el.fill("")
        el.type(answer, delay=40)
        page.wait_for_timeout(1500)
        want = (location_pick or answer).lower()
        opts = page.locator("[role=option]:visible")
        texts = [t.strip().lower() for t in opts.all_inner_texts()]
        idx = next((i for i, t in enumerate(texts) if t.startswith(want) or want in t), None)
        if idx is not None:
            opts.nth(idx).click()
        else:
            page.keyboard.press("ArrowDown")
            page.keyboard.press("Enter")
    elif q.kind in ("radio", "checkbox", "multiselect"):
        wanted = [a.strip() for a in answer.split("|")] if q.kind == "multiselect" else [answer]
        if q.kind == "checkbox" and q.options:
            wanted = [q.options[0]]
        for w in wanted:
            i = next((i for i, o in enumerate(q.options) if norm(o) == norm(w)), None)
            if i is None or i >= len(q.option_targets):
                raise ValueError(f"option “{w}” not on the page")
            _click_choice(page, q.option_targets[i])


class Check(BaseModel):
    label: str
    ok: bool
    problem: str = ""


class Checks(BaseModel):
    items: List[Check]


CHECK_SYSTEM = """You check a filled job-application form. For each INTENDED answer, look at
the page snapshot (value=, checked=, data-state="selected", chosen option text, uploaded
file name) and say whether the form now shows that answer. Be literal: an empty value or a
different option is not ok. Also add an item (ok=false) for any REQUIRED question on the page
that is still empty and is not in the intended list, and for any validation error shown."""


def check(page: Page, intended: Dict[str, str]) -> List[str]:
    html = snapshot(page)
    listing = "\n".join(f"- {k}: {v[:200]}" for k, v in intended.items())
    out = llm.structured(CHECK_SYSTEM, f"INTENDED:\n{listing}\n\nSNAPSHOT:\n{html[:120000]}", Checks,
                         model=config.profile()["models"]["matcher"], label="form-check")
    return [f"{c.label}: {c.problem}" for c in out.items if not c.ok]


def submit(page: Page, r: FormRead) -> str:
    _el(page, r.submit_target).click()
    return outcome(page)


def outcome(page: Page, code_entered: bool = False) -> str:
    from .greenhouse import CONFIRMED, VERIFY_CODE
    for i in range(20):
        page.wait_for_timeout(1500)
        body = page.inner_text("body")
        if CONFIRMED.search(body):
            return "submitted"
        if VERIFY_CODE.search(body) and not code_entered:
            return "verify_code"
    r = read(page, "The application was just submitted. What does the page show now?")
    return {"submitted": "submitted", "verify_code": "verify_code" if not code_entered else "unknown",
            "error": "invalid"}.get(r.page, "unknown")


def enter_code(page: Page, code: str) -> str:
    r = read(page, "Find the security-code input and the button that submits it.")
    boxes = page.locator("input[maxlength='1']:visible")
    if boxes.count() >= len(code):
        for i, ch in enumerate(code):
            boxes.nth(i).fill(ch)
    else:
        _el(page, r.code_target).fill(code)
    if r.submit_target:
        _el(page, r.submit_target).click()
    else:
        page.keyboard.press("Enter")
    return outcome(page, code_entered=True)
