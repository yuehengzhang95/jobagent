"""Convert an existing resume (PDF, Word .docx, Markdown or text) into artifacts/main.tex.

Claude only restructures: it copies wording, numbers, names and dates from the original.
Code then checks that every number and every bullet comes from the source text, writes the
LaTeX in the format the other agents expect, compiles it and reports the page count.
"""
import difflib
import re
import shutil
import zipfile
from pathlib import Path
from typing import List, Optional

from pydantic import BaseModel

from . import claims, config, llm, paths, resume


class Header(BaseModel):
    name: str
    location: str = ""
    phone: str = ""
    email: str = ""
    links: List[str] = []           # e.g. linkedin.com/in/..., github.com/...


class IProject(BaseModel):
    title: str = ""                 # optional sub-heading inside a job
    bullets: List[str]


class IRole(BaseModel):
    company: str
    location: str = ""
    title: str
    dates: str
    projects: List[IProject]


class IEducation(BaseModel):
    school: str
    location: str = ""
    degree: str
    dates: str = ""


class ISection(BaseModel):
    title: str                      # e.g. Projects, Awards, Certifications
    items: List[str]


class Imported(BaseModel):
    header: Header
    summary: str
    experience: List[IRole]
    skills: List[str]
    education: List[IEducation]
    publications: List[str] = []
    other_sections: List[ISection] = []


SYSTEM = """You convert a resume into structured data. You are a copier, not a writer.

- Copy every word, number, name, title and date exactly as written. Do not rephrase,
  shorten, merge, embellish or add anything. Fix only obvious PDF-extraction artifacts
  (broken hyphenation across lines, stray page numbers, bullet symbols).
- experience: jobs in the order shown. If a job lists sub-projects with their own headings,
  put them in projects with a title; otherwise use one project with an empty title.
  Each bullet is one bullet from the original. If a bullet starts with a bold or
  colon-separated lead phrase, keep it as "**Lead phrase:** rest of bullet".
- summary: the resume's summary/objective/profile paragraph verbatim; empty string if none.
- skills: individual skills as listed (split category lines into items, keep their wording).
- Put sections that are neither experience, education, skills nor publications (projects,
  awards, certifications, leadership...) in other_sections, items verbatim.
- dates: as written, e.g. "Jan 2023 – Present"."""


def read_source(path: Path) -> str:
    suffix = path.suffix.lower()
    if suffix == ".pdf":
        from pypdf import PdfReader
        return "\n".join(p.extract_text() or "" for p in PdfReader(str(path)).pages)
    if suffix == ".docx":
        with zipfile.ZipFile(path) as z:
            xml = z.read("word/document.xml").decode("utf-8")
        paras = []
        for p in re.findall(r"<w:p[ >].*?</w:p>", xml, re.S):
            text = "".join(re.findall(r"<w:t[^>]*>([^<]*)</w:t>", p))
            if text.strip():
                paras.append(text)
        import html
        return html.unescape("\n".join(paras))
    if suffix in (".md", ".txt"):
        return path.read_text()
    raise ValueError(f"unsupported file type {suffix}: use .pdf, .docx, .md or .txt")


def _norm(s: str) -> str:
    return re.sub(r"[^a-z0-9%]+", " ", s.lower().replace("**", "")).strip()


def check(src: str, r: Imported) -> List[str]:
    """Problems: numbers or bullets that don't appear in the source."""
    problems = []
    src_numbers = claims.numbers(src)
    src_norm = _norm(src)
    texts = [r.summary] + [b for role in r.experience for p in role.projects for b in p.bullets]
    for t in texts:
        extra = claims.numbers(t) - src_numbers
        if extra:
            problems.append(f"numbers not in the original {sorted(extra)}: {t[:90]}")
    for role in r.experience:
        for p in role.projects:
            for b in p.bullets:
                nb = _norm(b)
                if nb and nb not in src_norm:
                    # allow small extraction differences; flag real rewrites
                    best = max((difflib.SequenceMatcher(None, nb, src_norm[i:i + len(nb)]).ratio()
                                for i in range(0, max(1, len(src_norm) - len(nb)), max(1, len(nb) // 4))), default=0)
                    if best < 0.85:
                        problems.append(f"bullet reworded or not found in the original: {b[:90]}")
    return problems


def _t(s: str) -> str:
    return resume.text_to_tex(s)


def to_tex(r: Imported) -> str:
    template = (paths.EXAMPLES / "artifacts" / "main.tex").read_text()
    preamble = template[:template.index("\\begin{document}")]
    h = r.header
    contact = [_t(h.location)] if h.location else []
    if h.phone:
        contact.append(f"\\faPhone\\ {_t(h.phone)}")
    if h.email:
        contact.append(f"\\faEnvelope\\ \\href{{mailto:{h.email}}}{{{_t(h.email)}}}")
    for link in h.links:
        url = link if link.startswith("http") else "https://" + link
        contact.append(f"\\href{{{url}}}{{{_t(link.replace('https://', '').replace('http://', ''))}}}")
    out = [preamble, "\\begin{document}\n\n\\begin{center}\n",
           f"{{\\Huge \\scshape {_t(h.name)}}} \\\\ \\vspace{{2pt}}\n",
           "\\small " + " \\;|\\; ".join(contact) + "\n\\end{center}\n"]
    if r.summary:
        out += ["\n%------------------------------------------------\n\\section{Summary}\n", _t(r.summary), "\n"]
    out.append("\n%------------------------------------------------\n\\section{Experience}\n\n\\resumeSubHeadingListStart\n")
    for role in r.experience:
        out.append(f"\n\\resumeSubheading\n{{{_t(role.company)}}}{{{_t(role.location)}}}\n"
                   f"{{{_t(role.title)}}}{{{_t(role.dates)}}}\n\n\\resumeItemListStart\n")
        for p in role.projects:
            if p.title:
                out.append(f"\n\\resumeProject{{{_t(p.title)}}}\n")
            for b in p.bullets:
                out.append(f"\n\\resumeItem{{{_t(b)}}}\n")
        out.append("\n\\resumeItemListEnd\n")
    out.append("\n\\resumeSubHeadingListEnd\n")
    out += ["\n%------------------------------------------------\n\\section{Skills}\n",
            "{\\small " + ", ".join(_t(s) for s in r.skills) + "}\n"]
    if r.education:
        out.append("\n%------------------------------------------------\n\\section{Education}\n\n\\resumeSubHeadingListStart\n")
        for e in r.education:
            out.append(f"\n\\resumeSubheading\n{{{_t(e.school)}}}{{{_t(e.location)}}}\n{{{_t(e.degree)}}}{{{_t(e.dates)}}}\n")
        out.append("\n\\resumeSubHeadingListEnd\n")
    for title, items in ([("Publications", r.publications)] if r.publications else []) + \
                        [(s.title, s.items) for s in r.other_sections]:
        title = title.title() if title.isupper() else title
        out.append(f"\n%------------------------------------------------\n\\section{{{_t(title)}}}\n\n"
                   "\\begin{itemize}[leftmargin=0.15in,label={}, itemsep=1pt]\n\\small\n")
        out += [f"\\item {_t(i)}\n" for i in items]
        out.append("\\end{itemize}\n")
    out.append("\n\\end{document}\n")
    return "".join(out)


def is_example(tex_path: Path) -> bool:
    return tex_path.exists() and "Alex Chen" in tex_path.read_text()


def run(src_path: Path, out_path: Optional[Path] = None, log=print) -> dict:
    src = read_source(src_path)
    if len(src.strip()) < 200:
        raise ValueError(f"only {len(src.strip())} characters of text in {src_path.name}; "
                         "is it a scanned image? Try the .docx or paste the text into a .txt file.")
    log(f"read {len(src)} characters from {src_path.name}; converting…")
    r = llm.structured(SYSTEM, f"RESUME TEXT:\n{src}", Imported,
                       model=config.profile()["models"].get("tailor", "opus"), label="import-resume")
    problems = check(src, r)
    target = out_path or paths.BASE_TEX
    if out_path is None and target.exists() and not is_example(target):
        target = target.with_name("main.imported.tex")      # never overwrite a real resume
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(to_tex(r))

    # compile and parse back, exactly as the Tailor will
    build = paths.DATA / "import_build"
    shutil.rmtree(build, ignore_errors=True)
    build.mkdir(parents=True)
    shutil.copy(target, build / "main.tex")
    base = resume.parse_base(build / "main.tex")
    rendered = resume.render(base, base, build, template_path=build / "main.tex")
    return {"tex": target, "pdf": rendered.pdf, "pages": rendered.pages, "fill": rendered.last_page_fill,
            "roles": len(base.roles), "bullets": sum(len(p.bullets) for x in base.roles for p in x.projects),
            "problems": problems}
