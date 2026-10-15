"""Read and edit config/watchlist.yaml without losing its comments or layout."""
from typing import List, Optional

from ruamel.yaml import YAML
from ruamel.yaml.comments import CommentedMap

from . import paths

WATCHLIST = paths.CONFIG / "watchlist.yaml"


def _yaml() -> YAML:
    y = YAML()
    y.preserve_quotes = True
    y.width = 4096
    y.indent(mapping=2, sequence=4, offset=2)
    return y


def load():
    return _yaml().load(WATCHLIST.read_text())


def save(doc) -> None:
    with WATCHLIST.open("w") as f:
        _yaml().dump(doc, f)


def companies() -> List[dict]:
    return [dict(c) for c in load()["companies"]]


def find(name: str) -> Optional[dict]:
    return next((c for c in companies() if c["name"].lower() == name.lower()), None)


def add(entry: dict) -> None:
    doc = load()
    if any(c["name"].lower() == entry["name"].lower() for c in doc["companies"]):
        return
    m = CommentedMap(entry)
    m.fa.set_flow_style()          # one line per company, like the hand-written entries
    doc["companies"].append(m)
    save(doc)


def remove(name: str) -> None:
    doc = load()
    seq = doc["companies"]
    for i in reversed(range(len(seq))):
        if seq[i]["name"].lower() == name.lower():
            del seq[i]                 # in place, so section comments stay attached
    save(doc)


def set_enabled(name: str, enabled: bool) -> None:
    doc = load()
    for c in doc["companies"]:
        if c["name"].lower() == name.lower():
            if enabled:
                c.pop("enabled", None)
            else:
                c["enabled"] = False
    save(doc)
