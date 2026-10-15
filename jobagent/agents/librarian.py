"""Librarian: builds data/claims.yaml from main.tex (verified) + artifacts (details)."""
import re
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import List

from pydantic import BaseModel
from pypdf import PdfReader

from .. import claims, config, llm, paths, resume

SYSTEM = """You extract resume-ready facts from a candidate's work documents.

The candidate's resume is VERIFIED and authoritative. The documents you read add
qualitative detail (methods, architectures, tools, design decisions, scope) that a
tailored resume may draw on. Rules:
- Each fact is one atomic, specific statement, written as a resume fragment in English,
  past tense, first person implied (e.g. "Used a two-step Claude Sonnet annotator as the
  teacher: first span extraction, then document-level label assignment").
- Every fact must be directly supported by the document. No inference, no embellishment.
- Do NOT include metrics, percentages, latencies, counts or other numbers. The resume is
  the only source of numbers. Model/version names (ModernBERT, GPT-3.5) are fine.
- Skip interview-prep speculation, known bugs, weaknesses, future plans, open questions,
  and anything describing teammates' work rather than the candidate's.
- Skip facts that merely restate a resume bullet; add detail beyond it.
- Documents may be in Chinese; always write facts in English.
- Assign role_id (one of the resume role ids) and a short project label."""


class Fact(BaseModel):
    role_id: str
    project: str
    text: str


class Facts(BaseModel):
    facts: List[Fact]


def _artifact_text(path: Path) -> str:
    if path.suffix == ".pdf":
        return "\n".join(p.extract_text() or "" for p in PdfReader(str(path)).pages)
    return path.read_text()


def _resume_claims(base: resume.Resume) -> List[claims.Claim]:
    out = [claims.Claim(id="summary", kind="resume", role_id="summary", text=base.summary,
                        source="main.tex")]
    for role in base.roles:
        n = 0
        for p in role.projects:
            for b in p.bullets:
                n += 1
                out.append(claims.Claim(id=f"{role.id}.{n}", kind="resume", role_id=role.id,
                                        project=p.title, text=b, source="main.tex"))
    return out


def _slug(path: Path) -> str:
    return re.sub(r"[^a-z0-9]+", "-", path.stem.lower()).strip("-")[:24]


def build() -> List[claims.Claim]:
    cfg = config.profile()
    base = resume.parse_base()
    paths.DATA.mkdir(parents=True, exist_ok=True)
    paths.BASE_RESUME_JSON.write_text(base.model_dump_json(indent=2))
    resume_claims = _resume_claims(base)

    sources = [p for p in sorted(paths.ARTIFACTS.iterdir())
               if p.suffix in (".md", ".pdf") and p.name not in cfg.get("librarian_skip", [])
               and p != paths.BASE_TEX and not p.name.startswith((config.pdf_prefix(), "."))]
    role_ids = ", ".join(r.id for r in base.roles)
    context = (f"Resume role ids: {role_ids}\n\nVERIFIED RESUME BULLETS:\n"
               + claims.render_for_prompt(resume_claims))

    def extract(path: Path):
        prompt = f"{context}\n\n=== DOCUMENT: {path.name} ===\n{_artifact_text(path)}"
        print(f"  librarian: reading {path.name}", file=sys.stderr)
        return path, llm.structured(SYSTEM, prompt, Facts, model=cfg["models"]["librarian"],
                                    label=f"librarian:{path.name}")

    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(extract, sources))

    details = []
    valid_roles = {r.id for r in base.roles}
    for path, facts in results:
        for i, f in enumerate(facts.facts, 1):
            # numbers belong to the resume only; drop facts that carry metrics anyway
            if f.role_id not in valid_roles or not _only_names(f.text):
                continue
            details.append(claims.Claim(id=f"d.{_slug(path)}.{i}", kind="detail", role_id=f.role_id,
                                        project=f.project, text=f.text, source=path.name))
    all_claims = resume_claims + details
    claims.save(all_claims)
    return all_claims


def _only_names(text: str) -> bool:
    """True if every number in text is part of a name like 'GPT-3.5', 'v2', 'P99', 'Recall@5'."""
    stripped = re.sub(r"[A-Za-z]+[-@ ]?v?\d[\d.]*", "", text)
    return not claims.numbers(stripped)
