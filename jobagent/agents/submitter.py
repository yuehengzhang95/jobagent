"""Submitter, step 1: turn an application form into an answer plan you review in the UI.

Where each answer comes from (field.source):
  profile   fixed fact from config/answers.yaml or the base resume (the LLM may only pick
            which fact a question asks about and which listed option matches it)
  learned   an answer you typed earlier for the same question (config/learned_answers.yaml)
  eeo       demographic question, answered per answers.yaml (default: decline)
  draft     essay drafted by the LLM from the claims bank; you must approve it
  consent   acknowledgement / agreement; you must tick it yourself
  needs_input  nothing known; you fill it in
  skip      optional and unknown; left blank
"""
import html
import json
import re
from datetime import datetime
from pathlib import Path
from typing import List, Literal, Optional

import httpx
import yaml
from pydantic import BaseModel

from .. import claims, config, llm, paths

Kind = Literal["text", "textarea", "select", "multiselect", "file", "location", "country",
               "combobox", "date", "radio", "checkbox"]


class Field(BaseModel):
    id: str                      # DOM id on the form
    label: str
    kind: Kind
    semantic: str = "other"      # first_name, email, resume, eeo_gender, ... (see ats/generic.py)
    required: bool = False
    options: List[str] = []
    description: str = ""
    answer: str = ""             # multiselect: options joined with " | "
    source: str = "needs_input"
    note: str = ""
    approved: bool = False       # needed for draft / consent

    @property
    def needs_approval(self) -> bool:
        return self.source in ("draft", "consent")

    @property
    def done(self) -> bool:
        if self.needs_approval and not self.approved:
            return False
        return bool(self.answer) or not self.required


class Plan(BaseModel):
    job_id: str
    ats: str
    form_url: str
    resume_pdf: str
    created: str
    fields: List[Field]
    session: dict = {}           # filled by the browser session: state, screenshot, problems

    @property
    def blocking(self) -> List[Field]:
        return [f for f in self.fields if not f.done]


def plan_path(run_dir: Path) -> Path:
    return Path(run_dir) / "application.json"


def load_plan(run_dir) -> Optional[Plan]:
    p = plan_path(run_dir)
    return Plan.model_validate_json(p.read_text()) if p.exists() else None


def save_plan(plan: Plan, run_dir) -> None:
    plan_path(run_dir).write_text(plan.model_dump_json(indent=2))


def norm_label(label: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", label.lower()).strip()


def learned() -> dict:
    p = paths.LEARNED_YAML
    return (yaml.safe_load(p.read_text()) or {}) if p.exists() else {}


def remember(label: str, answer: str) -> None:
    d = learned()
    d[norm_label(label)] = {"label": label, "answer": answer, "saved": datetime.now().strftime("%Y-%m-%d")}
    paths.LEARNED_YAML.write_text(yaml.safe_dump(d, sort_keys=True, allow_unicode=True, width=100))


# ---------------------------------------------------------------- facts the LLM may point at

def facts() -> dict:
    a = config.answers()
    c, w, comp, av, ed, emp = (a["contact"], a["work_authorization"], a["compensation"],
                               a["availability"], a["education"], a["employment"])
    yn = lambda b: "Yes" if b else "No"
    return {
        "work_authorized_us": (f"Legally authorized to work in the US: {yn(w['authorized_to_work_in_us'])} "
                               f"({w['status']}; not tied to any employer, no permit to renew).",
                               yn(w["authorized_to_work_in_us"])),
        "needs_sponsorship": (f"Requires visa sponsorship now or in the future: "
                              f"{yn(w['requires_sponsorship_now_or_future'])}.",
                              yn(w["requires_sponsorship_now_or_future"])),
        "willing_to_relocate": (f"Willing to relocate: {yn(av['willing_to_relocate'])}.",
                                yn(av["willing_to_relocate"])),
        "open_to_onsite": (f"Open to in-office / hybrid work: Yes ({av['remote_preference']}).", "Yes"),
        "current_location": (f"Currently lives in: {c['location']}.", c["location"]),
        "current_employer": (f"Current employer: {emp['current_employer']}.", emp["current_employer"]),
        "current_title": (f"Current job title: {emp['current_title']}.", emp["current_title"]),
        "restrictive_agreement": (f"Bound by a non-compete or other agreement with a current/former employer that "
                                  f"restricts working for a new company: "
                                  f"{yn(emp['bound_by_noncompete_or_restrictive_agreement'])}.",
                                  yn(emp["bound_by_noncompete_or_restrictive_agreement"])),
        "salary_expectation": (f"Salary expectation: {comp['free_text']}", comp["free_text"]),
        "highest_degree": (f"Highest degree: {ed['highest_degree']}, {ed['school']}.",
                           f"{ed['highest_degree']}, {ed['school']}"),
        "school": (f"School: {ed['school']}.", ed["school"]),
        "preferred_name": (f"Preferred first name: {c['preferred_name']}.", c["preferred_name"]),
    }


class Mapping(BaseModel):
    id: str
    category: Literal["fact", "essay", "consent", "unknown"]
    fact: str = ""               # a key from FACTS when category == fact
    option: str = ""             # for select questions: the exact option text that matches the fact
    confident: bool = True


class Mappings(BaseModel):
    items: List[Mapping]


MAP_SYSTEM = """You map job-application questions to the candidate's known facts. You never
invent facts: you only say WHICH fact a question asks about, and for multiple-choice
questions WHICH listed option states that fact.

For each question return:
- category "fact": the question is fully answered by one FACTS entry. Give its key in
  `fact`. If the question has options, copy the matching option text exactly into
  `option`. Set confident=false if no option fits the fact cleanly (e.g. options assume a
  situation that doesn't match).
- category "essay": an open-ended prompt (why this company, describe your work, ...).
- category "consent": an acknowledgement, agreement, policy confirmation or privacy notice.
- category "unknown": anything else (past interviews, referrals, non-competes, start
  date, how you heard about us, links you don't have, ...).
Watch for negations: "Will you require sponsorship?" with fact "No" -> option "No"."""


def _map_custom(questions: List[Field]) -> dict:
    if not questions:
        return {}
    fs = facts()
    listing = "\n".join(
        f"[{q.id}] {q.label}" + (f" — {q.description[:300]}" if q.description else "")
        + (f"\n    options: {json.dumps(q.options, ensure_ascii=False)}" if q.options else "")
        for q in questions)
    prompt = ("FACTS:\n" + "\n".join(f"- {k}: {v[0]}" for k, v in fs.items())
              + f"\n\nQUESTIONS:\n{listing}")
    out = llm.structured(MAP_SYSTEM, prompt, Mappings, model=config.profile()["models"]["matcher"],
                         label="submitter-map")
    return {m.id: m for m in out.items}


class Essay(BaseModel):
    answer: str
    claim_ids: List[str]


ESSAY_SYSTEM = """You draft a short answer to one job-application question for the candidate,
in first person, plain and specific, no clichés or flattery. Use only facts from the
CLAIMS (cite the ids you used). Numbers may come only from `resume` claims, copied
exactly. Do not claim anything about the company that the posting doesn't say.
Keep it to the length the question asks for; if it gives none, 80-150 words."""


def _draft_essay(q: Field, jd_text: str, bank) -> str:
    out = llm.structured(
        ESSAY_SYSTEM,
        f"QUESTION: {q.label}\n{q.description}\n\nJOB POSTING:\n{jd_text[:12000]}\n\n"
        f"CLAIMS:\n{claims.render_for_prompt(bank)}",
        Essay, model=config.profile()["models"]["tailor"], label="submitter-essay")
    allowed = set().union(*(claims.numbers(c.text) for c in bank if c.kind == "resume"))
    bad = claims.numbers(out.answer) - allowed
    if bad:
        raise llm.LLMError(f"essay draft used numbers not in the resume: {sorted(bad)}")
    return out.answer


# ---------------------------------------------------------------- Greenhouse

GH_STANDARD = {"first_name", "last_name", "preferred_name", "email", "phone", "resume", "resume_text",
               "cover_letter", "cover_letter_text", "location", "latitude", "longitude"}
_DECLINE = re.compile(r"decline|don.?t wish|not wish|prefer not|choose not", re.I)
_AI_POLICY = re.compile(r"\bAI (policy|partnership|usage|use)\b|use of (AI|artificial intelligence)", re.I)


def _strip(s: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", html.unescape(s or ""))).strip()


def _gh_kind(t: str) -> Optional[str]:
    return {"input_text": "text", "textarea": "textarea", "multi_value_single_select": "select",
            "multi_value_multi_select": "multiselect", "input_file": "file"}.get(t)


def greenhouse_fields(slug: str, job_id: str) -> List[Field]:
    url = f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs/{job_id}?questions=true"
    d = httpx.get(url, timeout=30).json()
    out = []
    for q in d.get("questions", []):
        for f in q["fields"]:
            kind = _gh_kind(f["type"])
            if kind is None or f["name"] in ("resume_text", "cover_letter_text"):
                continue
            out.append(Field(id=f["name"], label=q["label"].strip(), kind=kind, required=q["required"],
                             options=[v["label"] for v in f.get("values", [])],
                             description=_strip(q.get("description"))))
    if d.get("location_questions"):
        req = any(q["required"] for q in d["location_questions"] if q["fields"][0]["name"] == "location")
        out.append(Field(id="candidate-location", label="Location (City)", kind="location", required=req))
    for block in d.get("compliance") or []:
        for q in block["questions"]:
            for f in q["fields"]:
                out.append(Field(id=f["name"], label=q["label"], kind="select", required=q["required"],
                                 options=[v["label"] for v in f.get("values", [])], source="eeo"))
    if any(f.source == "eeo" for f in out):
        # shown on the hosted form but missing from the API; the filler picks the decline option
        for fid, label in [("hispanic_ethnicity", "Are you Hispanic/Latino?"), ("disability_status", "Disability Status")]:
            if not any(f.id == fid for f in out):
                out.append(Field(id=fid, label=label, kind="select", source="eeo", answer="Decline"))
    # the hosted form adds a country picker next to the phone field that the API doesn't list
    out.append(Field(id="country", label="Country", kind="country", required=False))
    sem = {"first_name": "first_name", "last_name": "last_name", "preferred_name": "preferred_name",
           "email": "email", "phone": "phone", "resume": "resume", "cover_letter": "cover_letter",
           "candidate-location": "location", "country": "country", "gender": "eeo_gender", "race": "eeo_race",
           "hispanic_ethnicity": "eeo_hispanic", "veteran_status": "eeo_veteran",
           "disability_status": "eeo_disability"}
    for f in out:
        f.semantic = sem.get(f.id, "other")
    return out


def generic_form_url(job_row) -> str:
    url = job_row["url"].rstrip("/")
    if job_row["source"] == "ashby":
        return url if url.endswith("/application") else url + "/application"
    if job_row["source"] == "lever":
        return url if url.endswith("/apply") else url + "/apply"
    return url


def generic_fields(url: str, log=print) -> List[Field]:
    """Open the page headless and let Claude read the form."""
    from playwright.sync_api import sync_playwright
    from ..ats import generic

    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": 1280, "height": 900})
        r = generic.open_form(page, url)
        browser.close()
    if r.page != "form" or not r.questions:
        raise RuntimeError(f"no application form found at {url} (page looks like: {r.page})")
    log(f"read {len(r.questions)} questions from the page")
    out = []
    for i, q in enumerate(r.questions):
        kind = {"combobox": "location" if q.semantic == "location" else "combobox"}.get(q.kind, q.kind)
        out.append(Field(id=f"q{i}", label=q.label, kind=kind, semantic=q.semantic, required=q.required,
                         options=q.options, description=q.description,
                         source="eeo" if q.semantic.startswith("eeo_") else "needs_input"))
    return out


def build_plan(job_row, pdf: Path, jd_text: str, log=print) -> Plan:
    if job_row["source"] == "greenhouse":
        slug, ext = job_row["id"].split(":")[1], job_row["ext_id"]
        ats, form_url = "greenhouse", f"https://job-boards.greenhouse.io/{slug}/jobs/{ext}"
        fields = greenhouse_fields(slug, ext)
    else:
        ats, form_url = "generic", generic_form_url(job_row)
        log(f"reading the form at {form_url}…")
        fields = generic_fields(form_url, log=log)
    a, mem, fs = config.answers(), learned(), facts()
    c = a["contact"]
    standard = {"first_name": c["first_name"], "last_name": c["last_name"], "preferred_name": c["preferred_name"],
                "full_name": f"{c['first_name']} {c['last_name']}", "email": c["email"], "phone": c["phone"],
                "resume": str(pdf), "location": c["location_search"], "country": c["country"]}

    custom = []
    for f in fields:
        if f.semantic in standard and not (f.semantic == "country" and f.kind == "radio"):
            f.answer, f.source = standard[f.semantic], "profile"
        elif f.semantic == "cover_letter":
            f.source = "skip" if not f.required else "needs_input"
            f.note = "No cover letter is generated yet." if f.required else ""
        elif f.source == "eeo":
            pref = a["eeo"].get({"eeo_veteran": "veteran_status", "eeo_disability": "disability_status",
                                 "eeo_gender": "gender", "eeo_race": "race_ethnicity",
                                 "eeo_hispanic": "race_ethnicity"}.get(f.semantic, ""), "decline")
            pick = [o for o in f.options if (_DECLINE.search(o) if pref == "decline" else pref.lower() in o.lower())]
            f.answer = pick[0] if pick else f.answer
            if not f.answer:
                f.source = "needs_input"
        elif norm_label(f.label) in mem and (not f.options or mem[norm_label(f.label)]["answer"] in f.options) \
                and (f.kind != "date" or re.fullmatch(r"\d{4}-\d{2}-\d{2}", mem[norm_label(f.label)]["answer"])):
            f.answer, f.source = mem[norm_label(f.label)]["answer"], "learned"
        else:
            custom.append(f)

    log(f"mapping {len(custom)} custom questions…")
    maps = _map_custom(custom)
    ai_policy = any(_AI_POLICY.search(f.label + " " + f.description) for f in fields)
    bank = claims.load()
    for f in custom:
        m = maps.get(f.id)
        if m is None or m.category == "unknown":
            f.source = "needs_input" if f.required else "skip"
        elif m.category == "fact" and m.fact in fs:
            value = fs[m.fact][1]
            if f.options:
                if m.option in f.options:
                    f.answer, f.source = m.option, "profile"
                    if not m.confident:
                        f.source, f.note = "consent", f"Closest option to: {fs[m.fact][0]} Please confirm."
                else:
                    f.source, f.note = "needs_input", f"Asks about: {fs[m.fact][0]}"
            else:
                f.answer, f.source = value, "profile"
        elif m.category == "consent":
            f.source = "consent"
            f.answer = f.options[0] if len(f.options) == 1 else ""
            f.note = "Read it on the posting and confirm."
        elif m.category == "essay":
            if ai_policy:
                f.source = "needs_input"
                f.note = "This company has an AI-use policy for applications, so no draft was written."
            elif f.required:
                log(f"drafting: {f.label[:60]}")
                try:
                    f.answer, f.source = _draft_essay(f, jd_text, bank), "draft"
                    f.note = "Drafted from your claims bank. Edit it into your own words."
                except llm.LLMError as e:
                    f.source, f.note = "needs_input", str(e)
            else:
                f.source = "skip"
        else:
            f.source = "needs_input" if f.required else "skip"
    return Plan(job_id=job_row["id"], ats=ats, form_url=form_url,
                resume_pdf=str(pdf), created=datetime.now().isoformat(timespec="seconds"), fields=fields)
