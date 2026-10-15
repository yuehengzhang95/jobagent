import re

import yaml

from . import paths


def profile() -> dict:
    return yaml.safe_load(paths.PROFILE_YAML.read_text())


def answers() -> dict:
    return yaml.safe_load(paths.ANSWERS_YAML.read_text())


def pdf_prefix() -> str:
    """Tailored PDFs are named <prefix>_<Company>.pdf, e.g. AlexChen_OpenAI.pdf."""
    c = answers()["contact"]
    return re.sub(r"[^A-Za-z0-9]+", "", (c.get("preferred_name") or c["first_name"]) + c["last_name"])
