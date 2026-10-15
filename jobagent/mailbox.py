"""Read application security codes from Gmail over IMAP (read-only).

Setup (once): turn on 2-Step Verification, create an app password at
https://myaccount.google.com/apppasswords, then store it in the macOS Keychain:

    security add-generic-password -s jobagent-gmail -a <your gmail> -w

Only messages from the ATS sender that arrived after the submit click are read, and only
the code is extracted.
"""
import email
import imaplib
import re
import subprocess
import time
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from typing import Optional

from . import config

KEYCHAIN_SERVICE = "jobagent-gmail"
SENDERS = {"greenhouse": "greenhouse"}
_CODE = re.compile(r"(?:code|paste this)[^\n:]{0,80}:\s*([A-Za-z0-9]{6,10})\b", re.I)


def _password(address: str) -> Optional[str]:
    r = subprocess.run(["security", "find-generic-password", "-s", KEYCHAIN_SERVICE, "-a", address, "-w"],
                       capture_output=True, text=True)
    return r.stdout.strip() or None if r.returncode == 0 else None


def configured() -> bool:
    return _password(config.answers()["contact"]["email"]) is not None


def _text(msg) -> str:
    parts = msg.walk() if msg.is_multipart() else [msg]
    out = []
    for p in parts:
        if p.get_content_type() in ("text/plain", "text/html"):
            body = p.get_payload(decode=True) or b""
            out.append(re.sub(r"<[^>]+>", " ", body.decode(p.get_content_charset() or "utf-8", "replace")))
    return "\n".join(out)


def wait_for_code(ats: str, since: datetime, timeout_s: int = 180, log=print) -> Optional[str]:
    address = config.answers()["contact"]["email"]
    pw = _password(address)
    if not pw:
        return None
    since = since - timedelta(minutes=1)
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        with imaplib.IMAP4_SSL("imap.gmail.com") as m:
            m.login(address, pw)
            m.select("INBOX", readonly=True)
            day = (since - timedelta(days=1)).strftime("%d-%b-%Y")  # IMAP SINCE uses the server's time zone
            sender = SENDERS.get(ats)
            _, ids = m.search(None, f'(FROM "{sender}" SINCE {day})' if sender else f"(SINCE {day})")
            for mid in reversed(ids[0].split()[-15:]):
                _, data = m.fetch(mid, "(BODY.PEEK[])")
                msg = email.message_from_bytes(data[0][1])
                sent = parsedate_to_datetime(msg["Date"])
                if sent < since:
                    continue
                hit = _CODE.search(_text(msg))
                if hit:
                    log(f"security code found in “{msg['Subject']}”")
                    return hit.group(1)
        time.sleep(6)
    return None


def now() -> datetime:
    return datetime.now(timezone.utc)
