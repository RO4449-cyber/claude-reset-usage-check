#!/usr/bin/env python3
"""Privacy-first scanner for Claude usage-limit reset announcements.

Sources: Reddit r/ClaudeAI (JSON API) and the Anthropic status RSS feed.
Alerts go to ntfy. The topic and optional token come from the environment
(GitHub Secrets) and are never hardcoded or printed. Tunables live in config.json.
"""
import html
import json
import os
import re
import sys
import xml.etree.ElementTree as ET
from datetime import datetime, timezone

import requests

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_FILE = os.path.join(BASE_DIR, "config.json")
SEEN_FILE = "seen_posts.txt"
STATE_FILE = "state.json"
HEARTBEAT_FILE = "last_heartbeat.txt"

# old.reddit.com RSS first (least bot-blocking), then the standard RSS endpoint.
REDDIT_FEEDS = [
    "https://old.reddit.com/r/ClaudeAI/new/.rss?limit=15",
    "https://www.reddit.com/r/ClaudeAI/new/.rss?limit=15",
]
STATUS_URL = "https://status.anthropic.com/history.rss"
USER_AGENT = "claude-usage-reset-scanner/2.0"
BROWSER_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)
REDDIT_ENTRIES = 15

MAX_SEEN = 200
STATUS_ENTRIES = 5
FAILURE_LIMIT = 3
SNIPPET_MAX = 220
SNIPPET_LINES = 3
MAX_ACTIVE_INCIDENTS = 20
HIGH_WEIGHT = 10
MEDIUM_WEIGHT = 5

DEFAULT_STATUS_KEYWORDS = [
    "rate limit", "rate-limit", "free tier", "reset", "compensat", "restoration", "restored",
]
RESOLVED_STATES = {"resolved", "completed"}

TAGS_REDDIT = "gift,rocket"
TAGS_STATUS = "warning,loudspeaker"
TAGS_RESOLVED = "white_check_mark,bell"


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
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            raw = json.load(f)
        failures = int(raw.get("consecutive_failures", 0))
        active = raw.get("active_incidents", {})
        if not isinstance(active, dict):
            active = {}
    except (OSError, ValueError, TypeError, AttributeError):
        failures, active = 0, {}
    return {"consecutive_failures": failures, "active_incidents": active}


def save_state(state):
    active = state["active_incidents"]
    while len(active) > MAX_ACTIVE_INCIDENTS:
        active.pop(next(iter(active)))
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
# Reddit scoring
# --------------------------------------------------------------------------- #
def score_title(title, cfg):
    """Return (relevance_score, high_confidence_match_count); score is None if blocked."""
    t = title.lower()
    if any(p.lower() in t for p in cfg["negative_phrases"]):
        return None, 0
    high = [p for p in cfg["high_confidence_phrases"] if p.lower() in t]
    score = HIGH_WEIGHT if high else 0
    if any(k.lower() in t for k in cfg["medium_keywords_group1"]):
        score += MEDIUM_WEIGHT
    if any(k.lower() in t for k in cfg["medium_keywords_group2"]):
        score += MEDIUM_WEIGHT
    return score, len(high)


def reddit_should_alert(post, cfg):
    score, high_count = score_title(post["title"], cfg)
    if score is None or score < cfg["score_threshold"]:
        return False
    # RSS carries no upvote count (score is None); the title score gate above still applies.
    upvotes = post["score"]
    return upvotes is None or upvotes >= cfg["min_upvotes"] or high_count >= 2


def get_logged(url, user_agent):
    """GET with explicit failure logging (status code, Retry-After, short body excerpt)."""
    label = re.sub(r"\?.*$", "", url)
    try:
        resp = requests.get(
            url,
            headers={"User-Agent": user_agent, "Accept": "application/rss+xml, application/xml, */*"},
            timeout=20,
        )
    except Exception as exc:
        print(f"GET {label} failed: {type(exc).__name__}")
        raise
    if resp.status_code >= 400:
        body = re.sub(r"\s+", " ", clean_text(resp.text))[:200]
        retry = resp.headers.get("Retry-After")
        extra = f", Retry-After={retry}" if retry else ""
        print(f"GET {label} -> HTTP {resp.status_code}{extra}; body: {body!r}")
        resp.raise_for_status()
    return resp


def parse_reddit_atom(xml_bytes):
    root = ET.fromstring(xml_bytes)
    posts = []
    for e in [x for x in root.iter() if _local(x.tag) in ("entry", "item")][:REDDIT_ENTRIES]:
        link = _entry_link(e)
        raw_id = _child_text(e, "id") or _child_text(e, "guid") or link
        content = _child_text(e, "content") or _child_text(e, "description")
        m = re.search(r'<div class="md">(.*?)</div>', content, re.S)  # drop "submitted by" footer
        posts.append({
            "id": re.sub(r"^t3_", "", raw_id),
            "title": _child_text(e, "title"),
            "selftext": m.group(1) if m else "",
            "url": link,
            "score": None,  # not available in RSS
            "flair": "",
        })
    return posts


def fetch_reddit():
    last_exc = None
    for url in REDDIT_FEEDS:
        try:
            return parse_reddit_atom(get_logged(url, BROWSER_USER_AGENT).content)
        except Exception as exc:
            last_exc = exc
            print(f"Reddit endpoint failed ({type(exc).__name__}); trying next")
    raise RuntimeError("all Reddit endpoints failed") from last_exc


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
    return parse_status(get_logged(STATUS_URL, USER_AGENT).content)


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


def build_body(title, text, flair=""):
    head = f"[{flair}] {title}" if flair else title
    return f"{head}\n{text}" if text else head


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

    def mark(key):
        if key not in seen_set:
            seen_set.add(key)
            seen.append(key)

    # --- Reddit ---
    try:
        for post in fetch_reddit():
            key = "reddit:" + post["id"]
            if key in seen_set or not reddit_should_alert(post, cfg):
                continue
            body = build_body(post["title"], snippet(post["selftext"]), post["flair"])
            if send_ntfy(topic, token, body, "Claude Free Reset Alert!", TAGS_REDDIT, link=post["url"]):
                mark(key)
                print("Alerted: reddit post")
    except Exception as exc:
        failed = True
        print(f"Reddit fetch failed: {type(exc).__name__}")

    # --- Anthropic status ---
    try:
        active = state["active_incidents"]
        for inc in fetch_status():
            key = "status:" + inc["id"]
            resolved = inc["status"].lower() in RESOLVED_STATES
            if resolved:
                if inc["id"] in active:
                    body = build_body(inc["title"], snippet(inc["description"]))
                    if send_ntfy(topic, token, "Resolved / Event Ended\n" + body,
                                 "Claude Status: Event Ended", TAGS_RESOLVED,
                                 priority="default", link=inc["link"]):
                        del active[inc["id"]]
                        print("Alerted: incident resolved")
                continue
            if key in seen_set or not status_relevant(inc, cfg):
                continue
            body = build_body(inc["title"], snippet(inc["description"]))
            if send_ntfy(topic, token, body, "Claude Status Incident", TAGS_STATUS, link=inc["link"]):
                mark(key)
                active[inc["id"]] = inc["title"]
                print("Alerted: status incident")
    except Exception as exc:
        failed = True
        print(f"Status fetch failed: {type(exc).__name__}")

    # --- Consecutive failure tracking ---
    if failed:
        state["consecutive_failures"] += 1
        if state["consecutive_failures"] >= FAILURE_LIMIT:
            if send_ntfy(topic, token, "⚠️ הסקריפט נתקל ב-3 שגיאות רצופות ודורש בדיקה",
                         "Claude Scanner Error", TAGS_STATUS):
                state["consecutive_failures"] = 0
    else:
        state["consecutive_failures"] = 0

    # --- Weekly heartbeat ---
    if heartbeat_due(now, cfg):
        if send_ntfy(topic, token, "Claude Scanner Heartbeat: המערכת פעילה וסורקת תקין ✅",
                     "Claude Scanner Heartbeat", TAGS_RESOLVED, priority="low", sound=False):
            with open(HEARTBEAT_FILE, "w", encoding="utf-8") as f:
                f.write(now.date().isoformat())

    save_seen(seen)
    save_state(state)
    # Always exit 0 on fetch problems: actions/cache only saves state after a successful job.


if __name__ == "__main__":
    main()
