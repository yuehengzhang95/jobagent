"""Matcher: reads a job description and scores fit against the candidate profile."""
from typing import List, Literal

from pydantic import BaseModel, Field

from .. import config, llm, paths

Archetype = Literal["eval_llm_quality", "applied_nlp_ie", "llm_agents", "mle_infra", "research", "other"]

SYSTEM = """You are a candid technical recruiter screening one job posting for one candidate.
Read the posting and the candidate profile, then extract what the role needs and score fit.

Scoring (fit_score, 0-100): 85+ = strong match on core skills and level; 70-84 = good match
with minor gaps; 50-69 = stretch; <50 = poor fit. Penalize roles centred on the candidate's
listed thin areas, roles outside target seniority (staff/principal/manager/intern/new grad),
and locations outside the targets. Be honest; do not inflate.

keywords: the 10-20 exact terms an ATS or hiring manager will scan for (tools, methods,
domains), copied in the posting's own wording.
archetype: eval_llm_quality (LLM evaluation, quality, safety), applied_nlp_ie (NER, extraction,
classification, search/NLP modelling), llm_agents (agents, RAG, LLM product engineering),
mle_infra (training/serving infra, pipelines, platform), research (publications-driven
research), other."""


class JobAnalysis(BaseModel):
    company: str
    title: str
    location: str
    seniority: str
    archetype: Archetype
    must_have: List[str]
    nice_to_have: List[str]
    keywords: List[str]
    fit_score: int = Field(ge=0, le=100)
    fit_rationale: str
    gaps: List[str]
    red_flags: List[str] = Field(description="dealbreakers: level, location, clearance, degree field, etc.")


def analyze(jd_text: str) -> JobAnalysis:
    cfg = config.profile()
    prompt = (f"CANDIDATE PROFILE:\n{paths.PROFILE_MD.read_text()}\n\n"
              f"TARGETS: roles={cfg['target_roles']}; locations={cfg['locations']}; "
              f"exclude levels={cfg['seniority']['exclude']}\n\n"
              f"JOB POSTING:\n{jd_text}")
    return llm.structured(SYSTEM, prompt, JobAnalysis, model=cfg["models"]["matcher"], label="matcher")
