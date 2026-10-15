# jobagent

A local, multi-agent job-application assistant for ML / AI roles. It finds openings at the
companies you care about, scores each against your background, writes a one-page resume
tailored to the posting (and checks it for accuracy), and fills in the application form,
pausing for your approval before anything is submitted.

Everything runs on your machine. Your resume, answers and job history stay in folders that
git ignores.

```
Scout ──► Triage ──► Matcher ──► Tailor ⇄ Verifier ──► Submitter
public job     cheap title   fit score     resume rewrite    form agent fills the
boards         filter        0–100         + accuracy check  form, you approve, submit
```

| Agent | What it does |
|---|---|
| **Librarian** | Turns your resume and project notes (`artifacts/`) into a *claims bank*: the only facts the other agents may use. |
| **Scout** | Pulls postings from Greenhouse, Lever, Ashby, Workday and Eightfold boards (free public APIs); uses Claude web search for sites without one. Filters by title, level and location. |
| **Triage / Matcher** | Rates titles cheaply, then reads promising postings in full and scores fit, gaps and red flags. |
| **Tailor + Verifier** | Rewrites summary, bullets and skills for one posting. A second model checks every claim against the claims bank; code checks numbers, skills, page length and short last lines. Failing drafts are sent back, up to 3 rounds. |
| **Submitter** | Reads the application form (Greenhouse via its API; any other site by letting Claude read the page), fills answers from your config, checks the result, waits for your approval, submits, and can fetch the emailed security code from Gmail. |

## Ground rules the code enforces

- **Numbers only come from your resume.** Every number in a tailored resume must already be
  in `artifacts/main.tex`. Your other notes contribute wording only.
- **Employer, title and dates are never rewritten.** Skills must come from your `allowed_skills` list.
- **Form facts never come from the model.** Work authorization, sponsorship, salary, current
  employer and similar answers come from `config/answers.yaml`. The model only decides which
  fact a question asks about.
- **Nothing is submitted without you.** Essays drafted by the model and legal acknowledgements
  must be approved one by one; the filled form is shown as a screenshot before you click Submit.
  If a company says applications must not be written with AI, no essay draft is written.

## Requirements

- macOS or Linux, [conda](https://docs.conda.io/) (or another way to get Python 3.12 and
  [tectonic](https://tectonic-typesetting.github.io/)).
- Access to Claude, either:
  - [Claude Code](https://claude.com/claude-code) installed and logged in (the default; calls go
    through `claude -p`), or
  - an `ANTHROPIC_API_KEY` environment variable (used for everything except web search).
- Your resume as a LaTeX file (see below).

## Setup

```bash
git clone <this repo> jobagent && cd jobagent
conda env create -p ./.env -f environment.yml
./.env/bin/playwright install chromium
./.env/bin/python -m jobagent init
```

`init` copies a fictional example (Alex Chen) into `artifacts/` and `config/`. Replace it with
your own material:

### 1. Your resume: `artifacts/main.tex`

The resume must use the macros from the example (based on Jake's resume template):

- Sections named `Summary`, `Experience`, `Skills`, plus any others (Education, Publications…),
  which are kept as they are.
- Each job: `\resumeSubheading{Company}{Location}{Title}{Dates}`, then
  `\resumeItemListStart … \resumeItemListEnd`.
- Optional project headers inside a job: `\resumeProject{Project name}`.
- Bullets: `\resumeItem{\textbf{Lead phrase:} sentence…}`.
- It should fit on **one page**; the Tailor keeps it there.

The easiest path is to copy your content into `examples/artifacts/main.tex`'s structure.
Check it with `./.env/bin/tectonic artifacts/main.tex`.

**This file is the source of truth.** Put only numbers you can defend in an interview.

### 2. Your background: `artifacts/profile.md` and project notes

- `profile.md`: a short profile (targets, locations, education, strengths, **thin areas**).
  The Scout and Matcher use it for scoring.
- Any other `.md` or `.pdf` in `artifacts/` (design docs, retros, paper drafts, write-ups) adds
  detail the Tailor can use for wording. Name files you want skipped in `librarian_skip`.

### 3. Preferences: `config/profile.yaml`

- `target_roles`, `seniority`, `locations`: what you're looking for.
- `scout.title_include` / `title_exclude` / `locations`: the free regex filter applied
  before any model call.
- `allowed_skills`: the only skills the Tailor may list. Keep it honest.
- `optional_publications`: papers the Tailor may add for research roles only.
- `models`: which Claude model each agent uses.

### 4. Application answers: `config/answers.yaml`

Contact details, work authorization, sponsorship, salary text, current employer and title,
non-compete status, relocation, education, and EEO preferences (default: decline). Answers
you type in the UI with "remember" ticked are saved to `config/learned_answers.yaml`.

### 5. Companies: `config/watchlist.yaml`

About 70 big-tech companies, AI labs and startups are included. Add or remove entries; leave
out your current employer. Board slugs are the part after `boards.greenhouse.io/`,
`jobs.lever.co/` or `jobs.ashbyhq.com/`.

### 6. Build the claims bank, then start the UI

```bash
./.env/bin/python -m jobagent librarian      # once, and again after you change artifacts/
./.env/bin/python -m jobagent serve          # http://127.0.0.1:8765
```

## Using it

1. **Run scout** on the board. Tick "include Google / Meta / Apple" to search sites without a
   public API (extra cost). New jobs are triaged and the best ones scored.
2. Open a job: see why it fits, its gaps and red flags. Click **Tailor resume**. After 1–3
   minutes you get the PDF, the change log, any warnings, and a diff against your base resume.
3. Click **Prepare application**. Rows in red need you: answer, approve drafts, tick
   acknowledgements. **Save answers**.
4. **Open browser & fill form**: a Chromium window opens and fills the form. Check the
   screenshot, then **Submit application**. If a CAPTCHA or anything else gets in the way,
   finish in that window and click **I submitted it myself**.

### Optional: let it read emailed security codes (Gmail)

Some forms email a one-time code after you submit. To have it entered automatically, turn on
2-Step Verification, create an app password at <https://myaccount.google.com/apppasswords>, and
store it in the macOS Keychain:

```bash
security add-generic-password -s jobagent-gmail -a you@gmail.com -w
```

The inbox is opened read-only, and only messages that arrive after the submit click are read.

## Command line

```bash
python -m jobagent init                      # copy the examples into artifacts/ and config/
python -m jobagent librarian                 # rebuild data/claims.yaml
python -m jobagent scout [--web] [--score N] # pull boards, triage, score top N
python -m jobagent score [--limit N]         # score more triaged jobs
python -m jobagent analyze --jd <file|url>   # fit analysis for one posting
python -m jobagent tailor  --jd <file|url>   # tailored resume for one posting
python -m jobagent serve [--port 8765]       # web UI
```

## Costs (approximate, API pricing)

| Step | Cost |
|---|---|
| Librarian (once) | ~$1 for a dozen documents |
| Triage | ~$0.25 per 250 titles |
| Fit score | ~$0.04 per job |
| Tailored resume | ~$0.5–1.1 per job |
| Prepare application | ~$0.03 (Greenhouse), ~$0.10 (other sites) |
| Fill + check (non-Greenhouse) | ~$0.15 |
| Web-search scout (Google/Meta/Apple) | ~$0.30 per run |

## What stays private

`.gitignore` excludes `artifacts/`, `config/profile.yaml`, `config/answers.yaml`,
`config/learned_answers.yaml`, `data/` (claims bank, job database) and `runs/` (tailored
resumes, screenshots). Check `git status` before you commit.

## Limitations

- The resume parser expects the macros above. Other LaTeX templates need `jobagent/resume.py` changes.
- Workday, LinkedIn Easy Apply and sites behind a login are not automated.
- Web-search scouting is incomplete; big-tech sites often return few results.
- Forms change. If a fill looks wrong, fix it in the browser window before submitting.
- You are responsible for what you submit. Read every answer and the resume first.

## License

MIT
