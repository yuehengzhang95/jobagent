"""CLI.

  python -m jobagent init                      # copy the examples into artifacts/ and config/
  python -m jobagent import-resume <file>      # PDF/Word resume -> artifacts/main.tex
  python -m jobagent librarian                 # (re)build data/claims.yaml
  python -m jobagent analyze  --jd <file|url|->
  python -m jobagent tailor   --jd <file|url|-> [--out runs/foo]
  python -m jobagent scout    [--web] [--score N]   # pull watchlist, triage, score top N
  python -m jobagent score    [--limit N]
  python -m jobagent serve    [--port 8765]          # web UI
"""
import argparse
import sys
from pathlib import Path


def read_jd(src: str) -> str:
    if src == "-":
        return sys.stdin.read()
    if src.startswith("http"):
        import httpx
        from bs4 import BeautifulSoup

        html = httpx.get(src, follow_redirects=True, timeout=30,
                         headers={"User-Agent": "Mozilla/5.0"}).text
        soup = BeautifulSoup(html, "html.parser")
        for tag in soup(["script", "style", "nav", "footer", "header"]):
            tag.decompose()
        text = "\n".join(line.strip() for line in soup.get_text("\n").splitlines() if line.strip())
        if len(text) < 500:
            sys.exit(f"Fetched only {len(text)} chars from {src} (JS-rendered page?). "
                     "Paste the description into a file and pass that instead.")
        return text
    return Path(src).read_text()


def main() -> None:
    ap = argparse.ArgumentParser(prog="jobagent")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("init")
    im = sub.add_parser("import-resume", help="convert a PDF / Word / text resume into artifacts/main.tex")
    im.add_argument("file")
    im.add_argument("--out", help="where to write the .tex (default: artifacts/main.tex)")
    sub.add_parser("librarian")
    a = sub.add_parser("analyze")
    a.add_argument("--jd", required=True)
    t = sub.add_parser("tailor")
    t.add_argument("--jd", required=True)
    t.add_argument("--out")
    s = sub.add_parser("scout")
    s.add_argument("--web", action="store_true", help="also search companies without a public API (Apple) via web search")
    s.add_argument("--score", type=int, default=None)
    sc = sub.add_parser("score")
    sc.add_argument("--limit", type=int, default=30)
    sv = sub.add_parser("serve")
    sv.add_argument("--port", type=int, default=8765)
    args = ap.parse_args()

    if args.cmd == "init":
        import shutil
        from . import paths
        pairs = [(paths.EXAMPLES / "artifacts", paths.ARTIFACTS)] + [
            (paths.EXAMPLES / "config" / f"{n}.example.yaml", paths.CONFIG / f"{n}.yaml") for n in ("profile", "answers", "watchlist")]
        paths.CONFIG.mkdir(exist_ok=True)
        for src, dst in pairs:
            if dst.exists():
                print(f"keep   {dst.relative_to(paths.ROOT)} (already exists)")
            elif src.is_dir():
                shutil.copytree(src, dst)
                print(f"create {dst.relative_to(paths.ROOT)}/")
            else:
                shutil.copy(src, dst)
                print(f"create {dst.relative_to(paths.ROOT)}")
        print("Next: put your resume in artifacts/main.tex (from a PDF or Word file: "
              "`python -m jobagent import-resume <file>`), replace artifacts/profile.md, edit config/*.yaml, "
              "then run `python -m jobagent librarian`.")
    elif args.cmd == "import-resume":
        from . import importer
        r = importer.run(Path(args.file), Path(args.out) if args.out else None)
        print(f"wrote {r['tex']}: {r['roles']} roles, {r['bullets']} bullets, {r['pages']} page(s), "
              f"last page {r['fill']:.0%} full")
        print(f"preview: {r['pdf']}")
        if r["tex"].name == "main.imported.tex":
            print("artifacts/main.tex already holds a resume, so it was not overwritten. "
                  "Review main.imported.tex and rename it when you're happy.")
        if r["pages"] > 1:
            print("It runs over one page. Shorten it (the Tailor keeps resumes to one page).")
        for p in r["problems"]:
            print(f"! check: {p}")
        print("Next: read the preview against your original, fix anything in the .tex, then run "
              "`python -m jobagent librarian`.")
    elif args.cmd == "librarian":
        from .agents import librarian
        cs = librarian.build()
        print(f"{len(cs)} claims ({sum(c.kind == 'detail' for c in cs)} details) -> data/claims.yaml")
    elif args.cmd == "analyze":
        from .agents import matcher
        print(matcher.analyze(read_jd(args.jd)).model_dump_json(indent=2))
    elif args.cmd == "tailor":
        from . import pipeline
        out = pipeline.tailor_job(read_jd(args.jd), Path(args.out) if args.out else None)
        print(out / "review.md")
    elif args.cmd == "scout":
        import yaml
        from . import config
        from .agents import scout
        names = None
        if not args.web:
            names = [c["name"] for c in yaml.safe_load(scout.WATCHLIST.read_text())["companies"] if c["ats"] != "web"]
        stats = scout.scout(only=names)
        print({k: v for k, v in stats.items() if k != "errors"})
        scout.triage_pending()
        n = args.score if args.score is not None else config.profile()["scout"].get("score_per_run", 60)
        if n:
            scout.score_pending(limit=n)
    elif args.cmd == "score":
        from .agents import scout
        scout.score_pending(limit=args.limit)
    elif args.cmd == "serve":
        import uvicorn
        uvicorn.run("jobagent.web.app:app", host="127.0.0.1", port=args.port)


if __name__ == "__main__":
    main()
