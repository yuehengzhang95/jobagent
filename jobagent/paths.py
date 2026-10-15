import shutil
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ARTIFACTS = ROOT / "artifacts"
CONFIG = ROOT / "config"
DATA = ROOT / "data"
RUNS = ROOT / "runs"

BASE_TEX = ARTIFACTS / "main.tex"
PROFILE_MD = ARTIFACTS / "profile.md"
PROFILE_YAML = CONFIG / "profile.yaml"
ANSWERS_YAML = CONFIG / "answers.yaml"
LEARNED_YAML = CONFIG / "learned_answers.yaml"   # answers you gave in the UI, reused on later forms
BASE_RESUME_JSON = DATA / "base_resume.json"
CLAIMS_YAML = DATA / "claims.yaml"

_LOCAL_TECTONIC = ROOT / ".env" / "bin" / "tectonic"   # conda env created by environment.yml
TECTONIC = _LOCAL_TECTONIC if _LOCAL_TECTONIC.exists() else Path(shutil.which("tectonic") or "tectonic")
EXAMPLES = ROOT / "examples"
