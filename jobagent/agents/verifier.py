"""Verifier: deterministic checks first, then an LLM faithfulness review."""
import re
from typing import List, Literal

from pydantic import BaseModel
from pypdf import PdfReader

from .. import claims, config, llm, resume
from .matcher import JobAnalysis
from .tailor import Tailored

WIDOW_CHARS = 20  # a last line shorter than this (~2 words) wastes a line

SYSTEM = """You audit a tailored resume for faithfulness to its sources. For each bullet you
get the text and the claims it cites. Flag ONLY real problems:
- overclaiming: scope, ownership or impact stronger than the claims support
- a number attached to a different metric, system or population than in the claim
- technologies, methods or results not present in the cited claims
- meaning changes (e.g. "offline" turned into "online", "matched" into "beat")
- the summary asserting anything not supported by the resume claims
Do not flag style, word choice, ordering, or omissions. Mirroring job-posting vocabulary
is fine when it describes the same thing. If nothing is wrong, return no issues.

Severity: must_fix for anything a hiring manager could catch as inaccurate, including a
detail generalized beyond its source (one category presented as two, one product as all)
or a summary phrase not backed by resume claims. minor only for harmless wording nuance."""


class Issue(BaseModel):
    location: str
    problem: str
    fix: str
    severity: Literal["must_fix", "minor"]


class Review(BaseModel):
    issues: List[Issue]


class Report(BaseModel):
    errors: List[str]
    warnings: List[str]
    layout: List[str] = []  # cosmetic issues: sent back to the Tailor but never block "verified"
    pages: int = 0
    fill: float = 0.0

    @property
    def ok(self) -> bool:
        return not self.errors


def is_research_role(a: JobAnalysis) -> bool:
    asks_pubs = any("publication" in x.lower() or "paper" in x.lower() for x in a.must_have + a.nice_to_have)
    return a.archetype == "research" or "research" in a.title.lower() or asks_pubs


def deterministic(t: Tailored, base: resume.Resume, bank: List[claims.Claim], analysis: JobAnalysis) -> Report:
    cfg = config.profile()
    by_id = {c.id: c for c in bank}
    errors, warnings = [], []

    base_ids = [r.id for r in base.roles]
    got_ids = [r.role_id for r in t.roles]
    if got_ids != base_ids:
        errors.append(f"Roles must be exactly {base_ids} in that order; got {got_ids}.")
    for r in t.roles:
        if not any(p.bullets for p in r.projects):
            errors.append(f"Role {r.role_id} has no bullets.")

    def check(location: str, text: str, ids: List[str]):
        unknown = [i for i in ids if i not in by_id]
        if unknown:
            errors.append(f"{location}: unknown claim ids {unknown}.")
        allowed = set()
        for i in ids:
            if i in by_id and by_id[i].kind == "resume":
                allowed |= claims.numbers(by_id[i].text)
                if by_id[i].project:
                    allowed |= claims.numbers(by_id[i].project)
        extra = claims.numbers(text) - allowed
        if extra:
            errors.append(f"{location}: numbers {sorted(extra)} do not appear in the cited resume "
                          f"claims {ids}. Copy numbers exactly from cited claims or remove them.")
        if "\\" in text:
            errors.append(f"{location}: contains a backslash; write plain text, not LaTeX.")

    optional_pubs = {c.id for c in bank if c.role_id == "publications"}
    unknown_pubs = set(t.include_publications) - optional_pubs
    if unknown_pubs:
        errors.append(f"include_publications has unknown ids {sorted(unknown_pubs)}.")
    if t.include_publications and not is_research_role(analysis):
        errors.append("include_publications must be empty: this is not a research-oriented role.")
    excluded = optional_pubs - set(t.include_publications)
    if excluded & set(t.summary_claim_ids):
        errors.append(f"Summary cites {sorted(excluded & set(t.summary_claim_ids))} but that publication "
                      "is not included; drop it from the summary or include it.")

    all_resume_ids = [c.id for c in bank if c.kind == "resume" and c.id not in excluded]
    check("summary", t.summary, t.summary_claim_ids or all_resume_ids)
    for r in t.roles:
        for pi, p in enumerate(r.projects):
            if p.title:
                check(f"{r.role_id} project header '{p.title}'", p.title,
                      [i for b in p.bullets for i in b.claim_ids])
            for bi, b in enumerate(p.bullets):
                check(f"{r.role_id} project {pi + 1} bullet {bi + 1}", b.text, b.claim_ids)
                cross = [i for i in b.claim_ids if i in by_id and by_id[i].role_id not in (r.role_id,)]
                if cross:
                    errors.append(f"{r.role_id} bullet {bi + 1} cites claims from another role: {cross}.")

    allowed_skills = {s.lower() for s in cfg["allowed_skills"]}
    bad = [s for s in t.skills if s.lower() not in allowed_skills]
    if bad:
        errors.append(f"Skills not in ALLOWED SKILLS: {bad}.")
    if not 4 <= len(t.skills) <= 12:
        warnings.append(f"{len(t.skills)} skills listed; aim for 6-10.")
    return Report(errors=errors, warnings=warnings)


def page_check(report: Report, rendered: resume.RenderResult) -> Report:
    report.pages, report.fill = rendered.pages, rendered.last_page_fill
    if rendered.pages > 1:
        report.errors.append(f"Resume runs to {rendered.pages} pages (second page {rendered.last_page_fill:.0%} "
                             f"full). Cut text until it fits on one page.")
    elif rendered.last_page_fill < 0.85:
        report.warnings.append(f"Page is only {rendered.last_page_fill:.0%} full; room for more relevant detail.")
    return report


def _alnum(s: str) -> str:
    return re.sub(r"[^a-z0-9]", "", s.replace("**", "").lower())


def layout_check(report: Report, t: Tailored, pdf_path: str) -> Report:
    """Flag paragraphs whose last rendered line holds only a word or two."""
    lines = [l.strip() for p in PdfReader(pdf_path).pages for l in (p.extract_text() or "").splitlines()]
    paragraphs = [("summary", t.summary)] + [
        (f"{r.role_id} project {pi + 1} bullet {bi + 1}", b.text)
        for r in t.roles for pi, p in enumerate(r.projects) for bi, b in enumerate(p.bullets)]
    for line in lines:
        key = _alnum(line)
        if not key or len(line) >= WIDOW_CHARS:
            continue
        for location, text in paragraphs:
            full = _alnum(text)
            if full.endswith(key) and len(full) > len(key) * 3:
                report.layout.append(
                    f"{location}: the last rendered line is only \"{line}\" ({len(line)} chars). Trim about "
                    f"{len(line) + 5} characters from this paragraph so it ends on the previous line; "
                    "keep every number and fact.")
                break
    return report


def review(t: Tailored, bank: List[claims.Claim]) -> Review:
    cfg = config.profile()
    by_id = {c.id: c for c in bank}
    lines = [f"SUMMARY: {t.summary}\n  sources: all resume claims"]
    for r in t.roles:
        for pi, p in enumerate(r.projects):
            for bi, b in enumerate(p.bullets):
                src = "\n".join(f"    [{i}] {by_id[i].text}" for i in b.claim_ids if i in by_id)
                lines.append(f"{r.role_id} project {pi + 1} bullet {bi + 1}: {b.text}\n  sources:\n{src}")
    resume_claims = "\n".join(f"[{c.id}] {c.text}" for c in bank if c.kind == "resume")
    prompt = f"ALL RESUME CLAIMS (for the summary):\n{resume_claims}\n\nTAILORED RESUME:\n" + "\n\n".join(lines)
    return llm.structured(SYSTEM, prompt, Review, model=cfg["models"]["verifier"], label="verifier")
