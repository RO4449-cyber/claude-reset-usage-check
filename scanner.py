#!/usr/bin/env python3
"""Privacy-first scanner for Anthropic status incidents about rate limits and resets.

Source: the Anthropic status RSS feed. Alerts go to ntfy. The topic and optional token come
from the environment (GitHub Secrets) and are never hardcoded or printed. Tunables live in
config.json.
"""
import calendar
import html
import json
import os
import re
import sys
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone

import requests

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_FILE = os.path.join(BASE_DIR, "config.json")
SEEN_FILE = "seen_posts.txt"
STATE_FILE = "state.json"
HEARTBEAT_FILE = "last_heartbeat.txt"

STATUS_URL = "https://status.anthropic.com/history.rss"
USER_AGENT = "claude-usage-reset-scanner/3.0"

MAX_SEEN = 200
STATUS_ENTRIES = 5
FAILURE_LIMIT = 3
SNIPPET_MAX = 220
SNIPPET_LINES = 3
MAX_ACTIVE_INCIDENTS = 20
MAX_STAT_MONTHS = 3
MAX_REPORT_MARKERS = 8
RUNS_PER_DAY = 144  # cron */10

DEFAULT_STATUS_KEYWORDS = [
    "rate limit", "rate-limit", "free tier", "reset", "compensat", "restoration", "restored",
]
RESOLVED_STATES = {"resolved", "completed"}

TAGS_STATUS = "warning,loudspeaker"
TAGS_RESOLVED = "white_check_mark,bell"
TAGS_REPORT = "bar_chart,calendar"


# --------------------------------------------------------------------------- #
# Config / state files
# --------------------------------------------------------------------------- #
def load_config():
    with open(CONFIG_FILE, "r", encoding="utf-8") as f:
        cfg = json.load(f)
    cfg.setdefault("status_keywords", DEFAULT_STATUS_KEYWORDS)
    return cfg


def load_seen():
    if not os.path.exists(SEEN_FILE):
        return []
    out, dedup = [], set()
    with open(SEEN_FILE, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line and line not in dedup:
                dedup.add(line)
                out.append(line)
    return out


def save_seen(seen):
    """Keep only the latest MAX_SEEN ids so the file never grows indefinitely."""
    with open(SEEN_FILE, "w", encoding="utf-8") as f:
        for item in seen[-MAX_SEEN:]:
            f.write(item + "\n")


def load_state():
    state = {"consecutive_failures": 0, "active_incidents": {}, "stats": {}, "reports_sent": []}
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            raw = json.load(f)
        state["consecutive_failures"] = int(raw.get("consecutive_failures", 0))
        for key in ("active_incidents", "stats"):
            if isinstance(raw.get(key), dict):
                state[key] = raw[key]
        if isinstance(raw.get("reports_sent"), list):
            state["reports_sent"] = [str(x) for x in raw["reports_sent"]]
    except (OSError, ValueError, TypeError, AttributeError):
        pass
    return state


def save_state(state):
    active = state["active_incidents"]
    while len(active) > MAX_ACTIVE_INCIDENTS:
        active.pop(next(iter(active)))
    for old in sorted(state["stats"])[:-MAX_STAT_MONTHS]:
        del state["stats"][old]
    state["reports_sent"] = state["reports_sent"][-MAX_REPORT_MARKERS:]
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)


# --------------------------------------------------------------------------- #
# Text helpers
# --------------------------------------------------------------------------- #
def clean_text(raw):
    """Strip HTML tags/entities, keeping line breaks."""
    text = re.sub(r"(?i)<br\s*/?>|</p>|</li>", "\n", raw or "")
    text = re.sub(r"<[^>]+>", "", text)
    text = html.unescape(text)
    lines = [re.sub(r"[ \t]+", " ", ln).strip() for ln in text.splitlines()]
    return "\n".join(ln for ln in lines if ln)


def snippet(raw, max_chars=SNIPPET_MAX, max_lines=SNIPPET_LINES):
    lines = clean_text(raw).splitlines()[:max_lines]
    text = "\n".join(lines)
    if len(text) > max_chars:
        text = text[: max_chars - 1].rstrip() + "…"
    return text


# --------------------------------------------------------------------------- #
# Anthropic status RSS
# --------------------------------------------------------------------------- #
def _local(tag):
    return tag.rsplit("}", 1)[-1]


def _child_text(elem, name):
    for c in elem:
        if _local(c.tag) == name:
            return "".join(c.itertext()).strip()
    return ""


def _entry_link(elem):
    for c in elem:
        if _local(c.tag) == "link":
            return c.attrib.get("href") or (c.text or "").strip()
    return ""


def parse_status(xml_bytes):
    root = ET.fromstring(xml_bytes)
    items = [e for e in root.iter() if _local(e.tag) in ("item", "entry")][:STATUS_ENTRIES]
    incidents = []
    for e in items:
        link = _entry_link(e)
        raw_desc = _child_text(e, "description") or _child_text(e, "content") or _child_text(e, "summary")
        state = _child_text(e, "status")
        if not state:
            # Statuspage feeds list updates newest-first as <strong>State</strong> - text.
            m = re.search(r"<strong>\s*([A-Za-z ]+?)\s*</strong>", raw_desc)
            state = m.group(1) if m else ""
        incidents.append({
            "id": _child_text(e, "guid") or _child_text(e, "id") or link,
            "title": _child_text(e, "title"),
            "link": link,
            "description": raw_desc,
            "status": state.strip(),
        })
    return incidents


def fetch_status():
    """GET the status feed, logging the HTTP status on failure (never the topic)."""
    try:
        resp = requests.get(STATUS_URL, headers={"User-Agent": USER_AGENT}, timeout=20)
    except Exception as exc:
        print(f"GET {STATUS_URL} failed: {type(exc).__name__}")
        raise
    if resp.status_code >= 400:
        print(f"GET {STATUS_URL} -> HTTP {resp.status_code}")
        resp.raise_for_status()
    return parse_status(resp.content)


def status_relevant(incident, cfg):
    text = (incident["title"] + " " + clean_text(incident["description"])).lower()
    return any(k.lower() in text for k in cfg["status_keywords"])


# --------------------------------------------------------------------------- #
# ntfy
# --------------------------------------------------------------------------- #
def send_ntfy(topic, token, body, title, tags, priority="high", link=None, sound=True):
    """POST to ntfy. Returns True on success. Never logs the URL (it contains the topic)."""
    headers = {"Title": title, "Priority": priority, "Tags": tags}
    if sound:
        headers["X-Sound"] = "alarm"
    if link:
        safe = link.replace(",", "%2C").replace(";", "%3B")
        headers["Actions"] = f"view, Open Post, {safe}, clear=true"
    if token:
        headers["Authorization"] = f"Bearer {token}"
    try:
        resp = requests.post(
            f"https://ntfy.sh/{topic}", data=body.encode("utf-8"), headers=headers, timeout=15
        )
        resp.raise_for_status()
        return True
    except Exception as exc:  # exception text can embed the URL/topic, so log only the type
        print(f"ntfy send failed: {type(exc).__name__}")
        return False


def build_body(title, text):
    return f"{title}\n{text}" if text else title


# --------------------------------------------------------------------------- #
# Heartbeat
# --------------------------------------------------------------------------- #
def heartbeat_due(now, cfg):
    if now.weekday() != cfg["heartbeat_day_of_week"] or now.hour != cfg["heartbeat_hour_utc"]:
        return False
    try:
        with open(HEARTBEAT_FILE, "r", encoding="utf-8") as f:
            last = f.read().strip()
    except OSError:
        last = ""
    return last != now.date().isoformat()


# --------------------------------------------------------------------------- #
# Monthly report
# --------------------------------------------------------------------------- #
def month_key(dt):
    return dt.strftime("%Y-%m")


def monthly_report_due(now, cfg, state):
    """Return (kind, marker) on the 1st ("start") or last day ("end") of the month, once."""
    if now.hour != cfg.get("monthly_report_hour_utc", cfg["heartbeat_hour_utc"]):
        return None
    if now.day == 1:
        kind = "start"
    elif (now + timedelta(days=1)).month != now.month:
        kind = "end"
    else:
        return None
    marker = f"{now.date().isoformat()}:{kind}"
    return None if marker in state["reports_sent"] else (kind, marker)


def build_monthly_report(kind, now, state):
    """Plain-text summary of the scanner's own activity."""
    if kind == "start":  # report on the month that just ended
        period_dt = now.replace(day=1) - timedelta(days=1)
        scope = "full month"
    else:
        period_dt = now
        scope = "month so far" if kind == "now" else "month end"
    key = month_key(period_dt)
    st = state["stats"].get(key, {})
    runs, fails = int(st.get("runs", 0)), int(st.get("fetch_failures", 0))
    expected = calendar.monthrange(period_dt.year, period_dt.month)[1] * RUNS_PER_DAY
    fail_pct = (100.0 * fails / runs) if runs else 0.0
    last_ok = st.get("last_success", "never")
    healthy = runs > 0 and fail_pct < 5 and state["consecutive_failures"] == 0
    verdict = "✅ OK - everything looks healthy" if healthy else "⚠️ CHECK - open the Actions tab and look at the latest runs"
    return (
        f"Claude Scanner monthly report - {key} ({scope})\n"
        f"Status: {verdict}\n\n"
        "What it does: every ~10 minutes it checks status.anthropic.com for incidents about "
        "rate limits, resets, compensation or restoration and alerts you here. It sends an "
        "'Event Ended' message when such an incident resolves, a heartbeat every Sunday, and an "
        "error alert after 3 failed scans in a row. Reddit is not tracked.\n\n"
        "Stats:\n"
        f"- Scans run: {runs} (about {expected} expected for a full month)\n"
        f"- Failed fetches: {fails} ({fail_pct:.1f}%)\n"
        f"- Incident alerts sent: {int(st.get('alerts', 0))}\n"
        f"- 'Event Ended' alerts sent: {int(st.get('resolved', 0))}\n"
        f"- Last successful scan: {last_ok}\n\n"
        "Reminder: GitHub pauses scheduled runs in a public repo after 60 days with no repo "
        "activity. If these reports stop, open Actions > Claude Usage Reset Scanner > Enable workflow."
    )


# --------------------------------------------------------------------------- #
# Main
# --------------------------------------------------------------------------- #
def main():
    topic = os.environ["NTFY_TOPIC"].strip()  # required; stored in GitHub Secrets
    if not topic:
        sys.exit("NTFY_TOPIC is empty - add it as a repository secret.")
    token = os.environ.get("NTFY_TOKEN") or None

    cfg = load_config()
    seen = load_seen()
    seen_set = set(seen)
    state = load_state()
    now = datetime.now(timezone.utc)
    failed = False
    stats = state["stats"].setdefault(month_key(now), {})
    stats["runs"] = int(stats.get("runs", 0)) + 1

    # --- Optional manual test notification (workflow_dispatch input send_test) ---
    if os.environ.get("SEND_TEST", "").lower() == "true":
        repo_url = "{}/{}".format(os.environ.get("GITHUB_SERVER_URL", "https://github.com"),
                                  os.environ.get("GITHUB_REPOSITORY", ""))
        body = ("TEST: Claude Scanner is working ✅\n"
                "If you see this on your phone, alerts reach you.\n"
                "Priority high + alarm sound + tap the button to open the run page.")
        if send_ntfy(topic, token, body, "Claude Scanner TEST", TAGS_STATUS, link=repo_url + "/actions"):
            print("Test notification sent")
        else:
            sys.exit("Test notification FAILED")

    # --- Anthropic status ---
    try:
        active = state["active_incidents"]
        for inc in fetch_status():
            key = "status:" + inc["id"]
            if inc["status"].lower() in RESOLVED_STATES:
                if inc["id"] in active:
                    body = build_body(inc["title"], snippet(inc["description"]))
                    if send_ntfy(topic, token, "Resolved / Event Ended\n" + body,
                                 "Claude Status: Event Ended", TAGS_RESOLVED,
                                 priority="default", link=inc["link"]):
                        del active[inc["id"]]
                        stats["resolved"] = int(stats.get("resolved", 0)) + 1
                        print("Alerted: incident resolved")
                continue
            if key in seen_set or not status_relevant(inc, cfg):
                continue
            body = build_body(inc["title"], snippet(inc["description"]))
            if send_ntfy(topic, token, body, "Claude Status Incident", TAGS_STATUS, link=inc["link"]):
                seen_set.add(key)
                seen.append(key)
                active[inc["id"]] = inc["title"]
                stats["alerts"] = int(stats.get("alerts", 0)) + 1
                print("Alerted: status incident")
    except Exception as exc:
        failed = True
        print(f"Status fetch failed: {type(exc).__name__}")

    # --- Consecutive failure tracking ---
    if failed:
        stats["fetch_failures"] = int(stats.get("fetch_failures", 0)) + 1
        state["consecutive_failures"] += 1
        if state["consecutive_failures"] >= FAILURE_LIMIT:
            if send_ntfy(topic, token, "⚠️ הסקריפט נתקל ב-3 שגיאות רצופות ודורש בדיקה",
                         "Claude Scanner Error", TAGS_STATUS):
                state["consecutive_failures"] = 0
    else:
        state["consecutive_failures"] = 0
        stats["last_success"] = now.strftime("%Y-%m-%d %H:%M UTC")

    # --- Weekly heartbeat ---
    if heartbeat_due(now, cfg):
        if send_ntfy(topic, token, "Claude Scanner Heartbeat: המערכת פעילה וסורקת תקין ✅",
                     "Claude Scanner Heartbeat", TAGS_RESOLVED, priority="low", sound=False):
            with open(HEARTBEAT_FILE, "w", encoding="utf-8") as f:
                f.write(now.date().isoformat())

    # --- Monthly report (1st and last day of the month; SEND_MONTHLY=true forces one now) ---
    repo_url = "{}/{}".format(os.environ.get("GITHUB_SERVER_URL", "https://github.com"),
                              os.environ.get("GITHUB_REPOSITORY", ""))
    due = monthly_report_due(now, cfg, state)
    if os.environ.get("SEND_MONTHLY", "").lower() == "true":
        if send_ntfy(topic, token, build_monthly_report("now", now, state),
                     "Claude Scanner Monthly Report", TAGS_REPORT, priority="default",
                     link=repo_url + "/actions", sound=False):
            print("Monthly report sent (manual)")
    elif due:
        kind, marker = due
        if send_ntfy(topic, token, build_monthly_report(kind, now, state),
                     "Claude Scanner Monthly Report", TAGS_REPORT, priority="default",
                     link=repo_url + "/actions", sound=False):
            state["reports_sent"].append(marker)
            print("Monthly report sent")

    save_seen(seen)
    save_state(state)
    # Always exit 0 on fetch problems: actions/cache only saves state after a successful job.


if __name__ == "__main__":
    main()
