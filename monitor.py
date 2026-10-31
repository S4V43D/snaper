"""Check one permitted public HTML section once; persist only successful observations."""

from __future__ import annotations

import argparse
from difflib import SequenceMatcher
import hashlib
import ipaddress
import json
import os
from pathlib import Path
import smtplib
import socket
import ssl
import sys
import tempfile
import time
from datetime import datetime, timezone
from email.message import EmailMessage
from urllib.parse import urlsplit
from urllib.robotparser import RobotFileParser

import requests
from bs4 import BeautifulSoup
from soupsieve import SelectorSyntaxError

USER_AGENT = "PermittedSectionMonitor/1.0"
MAX_BYTES = 2_000_000
MAX_TEXT = 100_000
INTERVAL_SECONDS = 900


class MonitorError(Exception):
    """A safe, actionable error message (no credentials or page bodies)."""


def digest(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def load_config(path: Path) -> dict:
    config = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(config, dict):
        raise MonitorError("Configuration must be a JSON object.")
    for field, variable in (("url", "MONITOR_URL"), ("selector", "MONITOR_SELECTOR")):
        if os.environ.get(variable):
            config[field] = os.environ[variable]
        if not isinstance(config.get(field), str) or not config[field].strip():
            raise MonitorError(f"Configuration requires a nonempty {field}.")
    ignores = config.setdefault("ignore_selectors", [])
    if not isinstance(ignores, list) or any(not isinstance(s, str) for s in ignores):
        raise MonitorError("ignore_selectors must be a list of CSS selector strings.")
    return config


def validate_url(url: str) -> None:
    """Limit accidental access to local networks; this is not a DNS-rebinding firewall."""
    parsed = urlsplit(url)
    if (parsed.scheme != "https" or not parsed.hostname or parsed.username
            or parsed.password or parsed.fragment or parsed.port not in (None, 443)):
        raise MonitorError("Use a public HTTPS URL on port 443, without credentials or a fragment.")
    try:
        addresses = socket.getaddrinfo(parsed.hostname, 443, type=socket.SOCK_STREAM)
    except socket.gaierror as exc:
        raise MonitorError("The page hostname could not be resolved.") from exc
    if not addresses or any(not ipaddress.ip_address(a[4][0]).is_global for a in addresses):
        raise MonitorError("Local, private, and non-public network addresses are not allowed.")


def get_resource(session: requests.Session, url: str, *, robots: bool = False) -> str | bytes | None:
    validate_url(url)
    # No automatic retries or redirects. Reconfigure the canonical URL after reviewing it.
    with session.get(url, timeout=(10, 25), allow_redirects=False, stream=True) as response:
        if robots and response.status_code == 404:
            return None
        if response.status_code != 200:
            raise MonitorError(f"{'robots.txt' if robots else 'Page'} returned HTTP "
                               f"{response.status_code}; state was not advanced.")
        media_type = response.headers.get("Content-Type", "").split(";")[0].strip().lower()
        allowed_types = {"text/plain"} if robots else {"text/html", "application/xhtml+xml"}
        if media_type not in allowed_types:
            raise MonitorError("Unexpected content type; access may be blocked or the URL incorrect.")
        chunks = []
        size = 0
        started = time.monotonic()
        for chunk in response.iter_content(65536):
            size += len(chunk)
            if size > MAX_BYTES or time.monotonic() - started > 60:
                raise MonitorError("Download exceeded the size or time limit.")
            chunks.append(chunk)
        body = b"".join(chunks)
        if robots:
            return body.decode("utf-8-sig", errors="replace")
        # Pass bytes to Beautiful Soup so HTML encoding declarations can be detected.
        if response.encoding and "charset=" in response.headers.get("Content-Type", "").lower():
            return body.decode(response.encoding, errors="replace")
        return body


def check_robots(session: requests.Session, url: str) -> None:
    parsed = urlsplit(url)
    robots_url = f"{parsed.scheme}://{parsed.netloc}/robots.txt"
    body = get_resource(session, robots_url, robots=True)
    if body is None:  # A missing file is not permission; the user must separately confirm access.
        return
    rules = RobotFileParser()
    rules.parse(body.splitlines())
    if not rules.can_fetch(USER_AGENT, url):
        raise MonitorError("robots.txt disallows this monitor; choose a permitted page.")
    delay = rules.crawl_delay(USER_AGENT)
    rate = rules.request_rate(USER_AGENT)
    # Two requests per run: robots.txt and the page. This check is deliberately conservative.
    if delay and delay * 2 > INTERVAL_SECONDS:
        raise MonitorError("The site's crawl delay exceeds this project's request budget.")
    if rate and rate.requests / rate.seconds < 2 / INTERVAL_SECONDS:
        raise MonitorError("The site's request rate is stricter than this project's schedule.")
    if delay:
        time.sleep(delay)


def extract_text(html: str | bytes, config: dict) -> str:
    soup = BeautifulSoup(html, "html.parser")
    matches = soup.select(config["selector"])
    if len(matches) != 1:
        raise MonitorError(f"Selector must match exactly one section; found {len(matches)}.")
    section = matches[0]
    for node in section.select("script, style, noscript, template"):
        node.decompose()
    for selector in config["ignore_selectors"]:
        for node in section.select(selector):
            node.decompose()
    text = " ".join(section.get_text(separator=" ", strip=True).split())
    if not text:
        raise MonitorError("Selected section is empty; state was not advanced.")
    if len(text) > MAX_TEXT:
        raise MonitorError("Selected section is too large; use a narrower selector.")
    return text


def fetch_text(config: dict) -> str:
    with requests.Session() as session:
        session.trust_env = False  # Do not silently use .netrc credentials or environment proxies.
        session.headers.update({"User-Agent": USER_AGENT, "Accept": "text/html,text/plain;q=0.9"})
        check_robots(session, config["url"])
        session.cookies.clear()
        return extract_text(get_resource(session, config["url"]), config)


def target_id(config: dict) -> str:
    return digest(json.dumps({"url": config["url"], "selector": config["selector"],
                              "ignore_selectors": config["ignore_selectors"],
                              "normalization": "text-v1"}, sort_keys=True))


def load_state(path: Path) -> dict | None:
    if not path.exists():
        return None
    try:
        state = json.loads(path.read_text(encoding="utf-8"))
        if (not isinstance(state, dict) or state.get("version") != 1
                or not isinstance(state.get("text"), str)
                or not state["text"] or len(state["text"]) > MAX_TEXT
                or not isinstance(state.get("target_id"), str)
                or state.get("sha256") != digest(state["text"])):
            raise ValueError("Invalid state schema or checksum")
        return state
    except (ValueError, UnicodeError) as exc:
        raise MonitorError("Stored state is invalid. Review it, then explicitly reset the baseline.") from exc


def save_state(path: Path, config: dict, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    state = {"version": 1, "target_id": target_id(config), "sha256": digest(text),
             "text": text, "observed_at": datetime.now(timezone.utc).isoformat()}
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent,
                                         suffix=".tmp", delete=False) as handle:
            temporary = Path(handle.name)
            json.dump(state, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def smtp_settings() -> tuple[str, str, str]:
    sender = os.environ.get("GMAIL_USERNAME", "").strip()
    password = "".join(os.environ.get("GMAIL_APP_PASSWORD", "").split())
    recipient = os.environ.get("ALERT_TO", "").strip()
    for address in (sender, recipient):
        if (address.count("@") != 1 or any(c.isspace() for c in address)
                or any(c in address for c in ",;<>")):
            raise MonitorError("Set GMAIL_USERNAME and ALERT_TO to single, bare email addresses.")
    if not password:
        raise MonitorError("Set GMAIL_APP_PASSWORD to a Google App Password, never your normal password.")
    return sender, password, recipient


def send_email(subject: str, body: str) -> None:
    sender, password, recipient = smtp_settings()
    message = EmailMessage()
    message["From"] = sender
    message["To"] = recipient
    message["Subject"] = subject
    message.set_content(body)
    with smtplib.SMTP_SSL("smtp.gmail.com", 465, timeout=30,
                          context=ssl.create_default_context()) as smtp:
        smtp.login(sender, password)
        rejected = smtp.send_message(message)
        if rejected:
            raise MonitorError("SMTP rejected the recipient; state was not advanced.")


def format_changes(before: str, after: str) -> str:
    """Describe changed word spans only, using existing snapshots without rebaselining."""
    old_words, new_words = before.split(), after.split()
    # Strip shared edges first: even a tiny edit in a long repeated page stays small.
    start = 0
    while start < min(len(old_words), len(new_words)) and old_words[start] == new_words[start]:
        start += 1
    old_end, new_end = len(old_words), len(new_words)
    while old_end > start and new_end > start and old_words[old_end - 1] == new_words[new_end - 1]:
        old_end -= 1
        new_end -= 1
    old_words, new_words = old_words[start:old_end], new_words[start:new_end]
    removed, added = [], []
    # Repeated words remain valid anchors; the default autojunk filter would hide them.
    matcher = SequenceMatcher(None, old_words, new_words, autojunk=False)
    for operation, old_start, old_stop, new_start, new_stop in matcher.get_opcodes():
        if operation in ("delete", "replace"):
            removed.append(" ".join(old_words[old_start:old_stop]))
        if operation in ("insert", "replace"):
            added.append(" ".join(new_words[new_start:new_stop]))

    def section(label: str, spans: list[str]) -> str:
        content = "\n".join(f"- {span}" for span in spans) if spans else "(none)"
        if len(content) > 6000:
            content = content[:6000] + "\n[More changed text omitted; visit the page to review it.]"
        return f"{label}\n{content}"

    return section("REMOVED", removed) + "\n\n" + section("ADDED", added)


def run(config: dict, path: Path, *, dry_run: bool = False, reset: bool = False) -> str:
    if config.get("access_permission_confirmed") is not True:
        raise MonitorError("Review README access rules, then set access_permission_confirmed to true.")
    previous = None if reset else load_state(path)
    if previous and previous["target_id"] != target_id(config):
        raise MonitorError("URL or selectors changed. Review the new target and explicitly reset the baseline.")
    if not dry_run:
        smtp_settings()  # Catch incomplete email setup on the very first run.
    current = fetch_text(config)
    outcome = "baseline" if previous is None else ("unchanged" if previous["text"] == current else "changed")
    if dry_run:
        return f"DRY RUN: {outcome}; {len(current)} characters; no email or state write."
    if outcome == "unchanged":
        return "Unchanged; no email or state write."
    if outcome == "changed":
        send_email("[Section monitor] Page section changed",
                   f"URL: {config['url']}\nCSS selector: {config['selector']}\n"
                   f"Observed (UTC): {datetime.now(timezone.utc).isoformat()}\n\n"
                   f"{format_changes(previous['text'], current)}\n\n"
                   "Only changed text is listed. Edits show the old words as removed and the new words as added.\n"
                   "Moved text may also appear as removed and added.\n")
    # Commit the observation only after SMTP accepts the message (if needed).
    save_state(path, config, current)
    return "Baseline saved; no email sent." if outcome == "baseline" else "Change email accepted by SMTP; state saved."


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=Path("config.json"))
    parser.add_argument("--state", type=Path, default=Path(".local-state/state.json"))
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true", help="Fetch and compare without email or writes")
    mode.add_argument("--reset-baseline", action="store_true", help="Replace state without a change alert")
    mode.add_argument("--test-email", action="store_true", help="Send a test email without fetching a page")
    args = parser.parse_args()
    try:
        if args.test_email:
            send_email("[Section monitor] Email setup test", "Gmail SMTP setup is working. No webpage was fetched.\n")
            print("Test email accepted by SMTP.")
        else:
            print(run(load_config(args.config), args.state, dry_run=args.dry_run, reset=args.reset_baseline))
        return 0
    except MonitorError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
    except smtplib.SMTPException:
        print("ERROR: Gmail SMTP failed. Check App Password, recipient, account limits and connection; state not advanced.", file=sys.stderr)
    except requests.RequestException:
        print("ERROR: HTTPS request failed. Check connectivity, certificates and access; state not advanced.", file=sys.stderr)
    except SelectorSyntaxError:
        print("ERROR: Invalid CSS selector in configuration; state not advanced.", file=sys.stderr)
    except (OSError, ValueError, UnicodeError):
        print("ERROR: Check configuration/state files, filesystem permissions and network connectivity.", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
