"""Tailor: rewrites the resume for one job using only claims from the claims bank."""
import json
from typing import List, Optional

from pydantic import BaseModel, Field

from .. import claims, config, llm, resume
from .matcher import JobAnalysis

SYSTEM = """You tailor a one-page resume for a specific job. You are an expert technical
resume writer for ML/AI roles, and you never misrepresent the candidate.

HARD RULES (a verifier enforces these; violations get sent back):
1. Every bullet cites the claim ids it is built from (claim_ids). Use only facts in those claims.
2. Numbers come ONLY from cited `resume` claims and must be copied exactly (same value,
   precision and unit; e.g. "1,208", "κ = 0.90", "240+"). Never round, combine, or move a
   number onto a different metric. `detail` claims contribute wording only.
3. Do not inflate scope or ownership: keep the verbs the claims support (Built, Designed,
   Distilled...). No "led a team", "owned", "architected org-wide" unless a claim says so.
   Do not claim technologies that no cited claim mentions.
   You may borrow the posting's vocabulary for lead phrases and the summary, but keep the
   claim's count and mechanism: one evaluator is not "metrics"; a mitigation or an
   offline/async detector is not a "guardrail" (which implies blocking outputs at runtime).
4. Keep every role (by role_id) in the given order; each role keeps at least one bullet.
   Employer, title and dates are fixed and not part of your output.
5. Bullet format: "**Short lead phrase:** sentence(s)". Plain Unicode text, no LaTeX.
   Use →, −, κ, % as in the claims.
6. Skills: choose 6-10 from ALLOWED SKILLS only, most relevant first. Copy spelling exactly.
7. OPTIONAL PUBLICATIONS (claim ids starting with "pub."): list an id in
   include_publications ONLY if the role is research-oriented (research title, research
   archetype, or the posting asks for publications) AND the paper strengthens the
   application. Otherwise leave include_publications empty. The summary may cite a pub
   claim only if it is included; bullets never cite pub claims.

WHAT TO OPTIMIZE:
- Put the most job-relevant work first within each role (you may reorder projects and bullets,
  rename project headers, merge or drop low-relevance bullets).
- Mirror the posting's terminology where it truthfully describes the same thing (e.g. the
  posting says "offline evaluation" and the claim describes an offline eval framework).
- Fold in relevant `detail` claims when they show a must-have skill the bullets don't.
- Summary: 2-3 sentences aimed at this role and its must-haves; truthful about the actual
  title and experience in the base resume; may end with the highest degree as the base
  summary states it.
- LENGTH: the page is already full. Keep total characters across summary + bullets within
  the budget given. Adding something means cutting something.

change_log: 3-8 short notes on what you changed and why (for the candidate's review)."""


class TBullet(BaseModel):
    claim_ids: List[str] = Field(min_length=1)
    text: str


class TProject(BaseModel):
    title: Optional[str] = None
    bullets: List[TBullet]


class TRole(BaseModel):
    role_id: str
    projects: List[TProject]


class Tailored(BaseModel):
    summary: str
    summary_claim_ids: List[str]
    roles: List[TRole]
    skills: List[str]
    include_publications: List[str] = Field(default_factory=list)
    change_log: List[str]


def char_count(r: resume.Resume) -> int:
    return len(r.summary) + sum(len(b) for role in r.roles for p in role.projects for b in p.bullets)


def to_resume(t: Tailored) -> resume.Resume:
    pubs = {p["id"]: p["latex"] for p in config.profile().get("optional_publications", [])}
    return resume.Resume(
        summary=t.summary,
        skills=t.skills,
        extra_publications=[pubs[i] for i in t.include_publications if i in pubs],
        roles=[resume.Role(id=r.role_id, org="", location="", title="", dates="",
                           projects=[resume.Project(title=p.title, bullets=[b.text for b in p.bullets])
                                     for p in r.projects])
               for r in t.roles],
    )


def tailor(jd_text: str, analysis: JobAnalysis, base: resume.Resume, bank: List[claims.Claim],
           previous: Optional[Tailored] = None, feedback: Optional[List[str]] = None,
           budget: Optional[int] = None, polish: bool = False) -> Tailored:
    cfg = config.profile()
    budget = budget or char_count(base)
    parts = [
        f"JOB POSTING:\n{jd_text}",
        f"JOB ANALYSIS:\n{analysis.model_dump_json(indent=1)}",
        f"BASE RESUME (role ids and current content):\n{base.model_dump_json(indent=1)}",
        f"CLAIMS BANK:\n{claims.render_for_prompt(bank)}",
        f"ALLOWED SKILLS: {json.dumps(cfg['allowed_skills'], ensure_ascii=False)}",
        f"LENGTH BUDGET: at most {budget} characters across summary + all bullets "
        f"(base resume uses {char_count(base)}).",
    ]
    if previous is not None:
        parts.append(f"YOUR PREVIOUS DRAFT:\n{previous.model_dump_json(indent=1)}")
        parts.append("VERIFIER FEEDBACK - fix every item, change nothing else unnecessarily:\n- "
                     + "\n- ".join(feedback or []))
        if polish:
            parts.append("This draft already passed verification. Change ONLY the flagged words or "
                         "phrases; keep every other bullet, the skills and the publications exactly "
                         "as they are, and keep the length within a few characters of the draft.")
    return llm.structured(SYSTEM, "\n\n".join(parts), Tailored, model=cfg["models"]["tailor"],
                          effort="high", label="tailor")
