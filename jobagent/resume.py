"""Resume data model, main.tex <-> structured conversion, and PDF rendering.

Bullet text is stored as plain Unicode with **bold** markup so the LLM never
writes LaTeX. Only the summary, the Experience section and the Skills line are
tailorable; everything else (header, education, publications, preamble) is
copied verbatim from main.tex so the look stays identical.
"""
import re
import shutil
import subprocess
from pathlib import Path
from typing import List, Optional

from pydantic import BaseModel
from pypdf import PdfReader

from . import paths


class Project(BaseModel):
    title: Optional[str] = None
    bullets: List[str]


class Role(BaseModel):
    id: str
    org: str
    location: str
    title: str
    dates: str
    projects: List[Project]


class Resume(BaseModel):
    summary: str
    roles: List[Role]
    skills: List[str]
    extra_publications: List[str] = []  # raw LaTeX items, prepended to Publications


# ---------------------------------------------------------------- LaTeX <-> text

# (latex, unicode) pairs; order matters for tex -> text
_TEX_TEXT = [
    (r"$\rightarrow$", "→"),
    (r"$-$", "−"),
    (r"$\kappa$", "κ"),
    (r"vs.\ ", "vs. "),
    (r"\%", "%"),
    (r"\&", "&"),
    (r"\#", "#"),
    (r"\$", "$"),
    (r"\_", "_"),
    ("--", "–"),
]


def tex_to_text(s: str) -> str:
    s = re.sub(r"\\textbf\{([^{}]*)\}", r"**\1**", s)
    for tex, txt in _TEX_TEXT:
        s = s.replace(tex, txt)
    return re.sub(r"\s+", " ", s).strip()


def text_to_tex(s: str) -> str:
    out = s.replace("\\", r"\textbackslash{}")
    for ch in "%&#$_":
        out = out.replace(ch, "\\" + ch)
    out = out.replace("~", r"\textasciitilde{}").replace("^", r"\textasciicircum{}")
    out = (out.replace("→", r"$\rightarrow$").replace("−", "$-$").replace("κ", r"$\kappa$")
              .replace("–", "--").replace("—", "---").replace("≈", r"$\approx$")
              .replace("×", r"$\times$").replace("≥", r"$\geq$").replace("≤", r"$\leq$"))
    out = re.sub(r"\bvs\. ", r"vs.\\ ", out)
    out = re.sub(r"\*\*(.+?)\*\*", r"\\textbf{\1}", out)
    return out


# ---------------------------------------------------------------- parsing

def _read_group(s: str, i: int):
    """s[i] must be '{'; return (content, index after closing brace)."""
    assert s[i] == "{", s[i:i + 30]
    depth, j = 0, i
    while True:
        if s[j] == "\\":
            j += 2
            continue
        if s[j] == "{":
            depth += 1
        elif s[j] == "}":
            depth -= 1
            if depth == 0:
                return s[i + 1:j], j + 1
        j += 1


def _read_args(s: str, i: int, n: int):
    args = []
    for _ in range(n):
        while s[i].isspace():
            i += 1
        a, i = _read_group(s, i)
        args.append(a)
    return args, i


def _strip_comments(tex: str) -> str:
    return "\n".join(re.sub(r"(?<!\\)%.*", "", line) for line in tex.splitlines())


def _section_body(tex: str, name: str) -> str:
    m = re.search(r"\\section\{" + name + r"\}(.*?)(?=\\section\{|\\end\{document\})", tex, re.S)
    return m.group(1)


def _slug(org: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", org.lower().split()[0])


def parse_base(tex_path: Path = paths.BASE_TEX) -> Resume:
    tex = _strip_comments(tex_path.read_text())
    summary = tex_to_text(_section_body(tex, "Summary"))
    skills_raw = _section_body(tex, "Skills").strip()
    skills_raw = re.sub(r"^\{\\small\s*(.*)\}$", r"\1", skills_raw, flags=re.S)
    skills = [s.strip() for s in re.split(r",(?![^()]*\))", tex_to_text(skills_raw)) if s.strip()]

    body = _section_body(tex, "Experience")
    roles: List[Role] = []
    for m in re.finditer(r"\\resume(Subheading|Project|Item)(?![A-Za-z])", body):
        kind = m.group(1)
        if kind == "Subheading":
            (org, loc, title, dates), _ = _read_args(body, m.end(), 4)
            roles.append(Role(id=_slug(org), org=org, location=loc, title=title,
                              dates=tex_to_text(dates), projects=[]))
        elif kind == "Project":
            (title,), _ = _read_args(body, m.end(), 1)
            roles[-1].projects.append(Project(title=tex_to_text(title), bullets=[]))
        else:
            (item,), _ = _read_args(body, m.end(), 1)
            if not roles[-1].projects:
                roles[-1].projects.append(Project(title=None, bullets=[]))
            roles[-1].projects[-1].bullets.append(tex_to_text(item))
    return Resume(summary=summary, roles=roles, skills=skills)


# ---------------------------------------------------------------- rendering

def _experience_tex(resume: Resume, base: Resume) -> str:
    headers = {r.id: r for r in base.roles}
    parts = ["\n\\resumeSubHeadingListStart\n"]
    for role in resume.roles:
        h = headers[role.id]  # employer/title/dates always come from the base resume
        parts.append(f"\n\\resumeSubheading\n{{{h.org}}}{{{h.location}}}\n"
                     f"{{{h.title}}}{{{text_to_tex(h.dates)}}}\n\n\\resumeItemListStart\n")
        for p in role.projects:
            if p.title:
                parts.append(f"\n\\resumeProject{{{text_to_tex(p.title)}}}\n")
            for b in p.bullets:
                parts.append(f"\n\\resumeItem{{{text_to_tex(b)}}}\n")
        parts.append("\n\\resumeItemListEnd\n")
    parts.append("\n\\resumeSubHeadingListEnd\n\n")
    return "".join(parts)


def to_tex(resume: Resume, base: Resume, template_path: Path = paths.BASE_TEX) -> str:
    tex = template_path.read_text()

    def replace_section(tex: str, name: str, new_body: str) -> str:
        pat = re.compile(r"(\\section\{" + name + r"\})(.*?)(?=%-{10,}|\\section\{|\\end\{document\})", re.S)
        assert pat.search(tex), name
        return pat.sub(lambda m: m.group(1) + new_body, tex, count=1)

    tex = replace_section(tex, "Summary", "\n" + text_to_tex(resume.summary) + "\n\n")
    tex = replace_section(tex, "Experience", _experience_tex(resume, base))
    tex = replace_section(tex, "Skills", "\n{\\small " + text_to_tex(", ".join(resume.skills)) + "}\n\n")
    if resume.extra_publications:
        start = tex.index("\\section{Publications}")
        at = tex.index("\\small\n", start) + len("\\small\n")
        items = "".join(f"\\item {p}\n" for p in resume.extra_publications)
        tex = tex[:at] + items + tex[at:]
    # report how full the last page is, read back from the log by render()
    return tex.replace("\\end{document}",
                       "\\par\\typeout{RESUMEFILL \\the\\pagetotal\\space\\the\\textheight}\n\\end{document}")


class RenderResult(BaseModel):
    pdf: str
    pages: int
    last_page_fill: float  # fraction of the last page's text height used


def render(resume: Resume, base: Resume, out_dir: Path, stem: str = "resume") -> RenderResult:
    out_dir.mkdir(parents=True, exist_ok=True)
    tex_path = out_dir / f"{stem}.tex"
    tex_path.write_text(to_tex(resume, base))
    tectonic = str(paths.TECTONIC) if paths.TECTONIC.exists() else shutil.which("tectonic")
    if not tectonic:
        raise RuntimeError("tectonic (LaTeX engine) not found. Create the environment with "
                           "`conda env create -p ./.env -f environment.yml` or install tectonic on your PATH.")
    proc = subprocess.run([tectonic, "--chatter", "minimal", "--keep-logs", "--outdir", str(out_dir), str(tex_path)],
                          capture_output=True, text=True)
    if proc.returncode != 0:
        raise RuntimeError(f"tectonic failed:\n{proc.stderr[-3000:]}")
    pdf = out_dir / f"{stem}.pdf"
    reader = PdfReader(str(pdf))
    m = re.search(r"RESUMEFILL ([\d.]+)pt ([\d.]+)pt", (out_dir / f"{stem}.log").read_text())
    fill = round(float(m.group(1)) / float(m.group(2)), 3) if m else 0.0
    return RenderResult(pdf=str(pdf), pages=len(reader.pages), last_page_fill=fill)
