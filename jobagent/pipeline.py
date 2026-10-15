"""Tailor <-> Verifier loop for one job. Writes everything to runs/<slug>/."""
import difflib
import json
import re
import shutil
import sys
from pathlib import Path
from typing import Optional

from . import claims, config, paths, resume
from .agents import matcher, tailor, verifier


def slugify(*parts: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", "-".join(parts).lower()).strip("-")[:60]


def load_base() -> resume.Resume:
    return resume.parse_base()


def _log(msg: str) -> None:
    print(msg, file=sys.stderr)


def _as_text(r: resume.Resume) -> list:
    lines = [f"SUMMARY: {r.summary}"]
    for role in r.roles:
        lines.append(f"== {role.id}")
        for p in role.projects:
            if p.title:
                lines.append(f"  # {p.title}")
            lines += [f"  - {b}" for b in p.bullets]
    lines.append("SKILLS: " + ", ".join(r.skills))
    return lines


def tailor_job(jd_text: str, out_dir: Optional[Path] = None,
               analysis: Optional[matcher.JobAnalysis] = None, company: Optional[str] = None) -> Path:
    cfg = config.profile()
    base = load_base()
    bank = claims.load()

    if analysis is None:
        _log("matcher: analyzing posting")
        analysis = matcher.analyze(jd_text)
    out_dir = out_dir or paths.RUNS / slugify(analysis.company, analysis.title)
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "jd.md").write_text(jd_text)
    (out_dir / "analysis.json").write_text(analysis.model_dump_json(indent=2))
    _log(f"  {analysis.company} — {analysis.title}: fit {analysis.fit_score} ({analysis.archetype})")

    prev, feedback, budget = None, None, tailor.char_count(base)
    t, rep, passed, polishing = None, None, None, False
    for rnd in range(1, cfg["max_tailor_rounds"] + 1):
        _log(f"tailor: round {rnd}" + (" (wording polish)" if polishing else ""))
        t = tailor.tailor(jd_text, analysis, base, bank, previous=prev, feedback=feedback,
                          budget=budget, polish=polishing)
        rep = verifier.deterministic(t, base, bank, analysis)
        draft = tailor.to_resume(t)
        if [r.role_id for r in t.roles] == [r.id for r in base.roles]:
            rendered = resume.render(draft, base, out_dir / "build")
            rep = verifier.page_check(rep, rendered)
            if rendered.pages == 1:
                rep = verifier.layout_check(rep, t, rendered.pdf)
            if rendered.pages > 1:
                budget = int(tailor.char_count(draft) * 0.92)
                rep.errors.append(f"New length budget: {budget} characters.")
        if rep.ok:
            _log("verifier: deterministic checks passed; reviewing faithfulness")
            review = verifier.review(t, bank)
            rep.errors += [f"{i.location}: {i.problem} Fix: {i.fix}" for i in review.issues
                           if i.severity == "must_fix"]
            rep.warnings += [f"{i.location}: {i.problem}" for i in review.issues if i.severity == "minor"]
        (out_dir / f"round{rnd}.json").write_text(json.dumps(
            {"tailored": t.model_dump(), "report": rep.model_dump()}, indent=2, ensure_ascii=False))
        _log(f"  errors={len(rep.errors)} layout={len(rep.layout)} warnings={len(rep.warnings)} "
             f"pages={rep.pages} fill={rep.fill:.0%}")
        if rep.ok and not rep.layout:
            # a clean pass; keep it (and its PDF) in case a wording polish makes things worse
            passed = (t, rep)
            shutil.copy(out_dir / "build" / "resume.pdf", out_dir / "build" / "passed.pdf")
            if not rep.warnings:
                break
            polishing = True
            prev, feedback = t, [f"Wording (minor): {w}" for w in rep.warnings]
            continue
        if polishing:
            _log("  wording polish did not pass cleanly; keeping the previous draft")
            break
        prev, feedback = t, rep.errors + rep.layout
    if passed and passed[0] is not t:
        t, rep = passed
        shutil.copy(out_dir / "build" / "passed.pdf", out_dir / "build" / "resume.pdf")
    rep.warnings += rep.layout

    final = tailor.to_resume(t)
    (out_dir / "resume.json").write_text(json.dumps(t.model_dump(), indent=2, ensure_ascii=False))
    company = re.sub(r"[^A-Za-z0-9]+", "", company or analysis.company) or "Company"
    pdf = out_dir / f"{config.pdf_prefix()}_{company}.pdf"
    built = out_dir / "build" / "resume.pdf"
    if built.exists():
        shutil.copy(built, pdf)

    diff = "\n".join(difflib.unified_diff(_as_text(base), _as_text(final), "base", "tailored", lineterm="", n=0))
    status = "verified" if rep.ok else "needs_review"
    (out_dir / "status.json").write_text(json.dumps({"status": status, "pdf": str(pdf)}, indent=2))
    (out_dir / "review.md").write_text(
        f"# {analysis.company} — {analysis.title}\n\n"
        f"**Status:** {status} · **Fit:** {analysis.fit_score}/100 ({analysis.archetype}) · "
        f"**Pages:** {rep.pages} ({rep.fill:.0%} full)\n\n"
        f"**Why it fits:** {analysis.fit_rationale}\n\n"
        f"**Gaps:** {'; '.join(analysis.gaps) or 'none'}\n\n"
        f"**Red flags:** {'; '.join(analysis.red_flags) or 'none'}\n\n"
        "## Changes\n" + "\n".join(f"- {c}" for c in t.change_log) + "\n\n"
        + ("## Unresolved errors\n" + "\n".join(f"- {e}" for e in rep.errors) + "\n\n" if rep.errors else "")
        + ("## Warnings\n" + "\n".join(f"- {w}" for w in rep.warnings) + "\n\n" if rep.warnings else "")
        + f"## Diff vs base resume\n```diff\n{diff}\n```\n")
    _log(f"{status}: {pdf}")
    return out_dir
