#!/usr/bin/env python3
"""Self-hosted Reddit scanner for Claude usage-reset announcements.

Runs on YOUR computer (home IPs are not blocked by Reddit the way GitHub's servers are).
Checks r/ClaudeAI (and any other subreddits in local_config.json) every few minutes and
sends a push to your ntfy topic when a post looks like a limit reset / free usage news.

Standard library only - nothing to install.   Usage:
    python3 scanner_local.py --test    send one test notification and exit
    python3 scanner_local.py --once    scan once and exit (for cron)
    python3 scanner_local.py           scan forever, every `interval_seconds`
"""
import argparse
import html
import json
import os
import random
import re
import sys
import time
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
CONFIG_FILE = os.path.join(HERE, "local_config.json")
ENV_FILE = os.path.join(HERE, ".env")
SEEN_FILE = os.path.join(HERE, "seen_local.txt")
STATE_FILE = os.path.join(HERE, "state_local.json")

USER_AGENTS = [
    "claude-reset-scanner-local/1.0 (personal notifier)",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) "
    "Version/17.4 Safari/605.1.15",
]
# (url template, kind). JSON has upvote scores; RSS does not.
ENDPOINTS = [
    ("https://www.reddit.com/r/{sub}/new.json?limit=25&raw_json=1", "json"),
    ("https://old.reddit.com/r/{sub}/new.json?limit=25&raw_json=1", "json"),
    ("https://old.reddit.com/r/{sub}/new/.rss?limit=25", "rss"),
]
MAX_SEEN = 300
FAILURE_LIMIT = 3
SNIPPET_MAX = 220
SNIPPET_LINES = 3
HIGH_WEIGHT = 10
MEDIUM_WEIGHT = 5


def log(msg):
    print(f"{datetime.now().strftime('%Y-%m-%d %H:%M:%S')} {msg}", flush=True)


# --------------------------------------------------------------------------- #
# Config, .env, state
# --------------------------------------------------------------------------- #
def load_env(path=ENV_FILE):
    """Read KEY=VALUE lines from .env (never overrides real environment variables)."""
    if not os.path.exists(path):
        return
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def load_config():
    with open(CONFIG_FILE, "r", encoding="utf-8") as f:
        return json.load(f)


def load_seen():
    if not os.path.exists(SEEN_FILE):
        return []
    with open(SEEN_FILE, "r", encoding="utf-8") as f:
        return list(dict.fromkeys(line.strip() for line in f if line.strip()))


def save_seen(seen):
    with open(SEEN_FILE, "w", encoding="utf-8") as f:
        for item in seen[-MAX_SEEN:]:
            f.write(item + "\n")


def load_state():
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            raw = json.load(f)
        return {"consecutive_failures": int(raw.get("consecutive_failures", 0)),
                "last_heartbeat": str(raw.get("last_heartbeat", ""))}
    except (OSError, ValueError, TypeError, AttributeError):
        return {"consecutive_failures": 0, "last_heartbeat": ""}


def save_state(state):
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2)


# --------------------------------------------------------------------------- #
# HTTP (urllib only)
# --------------------------------------------------------------------------- #
def http_get(url, user_agent, timeout=20):
    req = urllib.request.Request(url, headers={"User-Agent": user_agent, "Accept": "*/*"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, resp.read()
    except urllib.error.HTTPError as exc:
        return exc.code, b""


def http_post(url, data, headers, timeout=15):
    req = urllib.request.Request(url, data=data, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status
    except urllib.error.HTTPError as exc:
        return exc.code


def notify(topic, token, body, title, tags, priority="high", link=None, sound=True):
    """Send an ntfy push. Returns True on success. Never prints the topic."""
    headers = {"Title": title, "Priority": priority, "Tags": tags}
    if sound:
        headers["X-Sound"] = "alarm"
    if link:
        safe = link.replace(",", "%2C").replace(";", "%3B")
        headers["Actions"] = f"view, Open Post, {safe}, clear=true"
    if token:
        headers["Authorization"] = f"Bearer {token}"
    try:
        status = http_post(f"https://ntfy.sh/{topic}", body.encode("utf-8"), headers)
    except Exception as exc:
        log(f"ntfy send failed: {type(exc).__name__}")
        return False
    if status >= 400:
        log(f"ntfy send failed: HTTP {status}")
        return False
    return True


# --------------------------------------------------------------------------- #
# Reddit parsing
# --------------------------------------------------------------------------- #
def clean_text(raw):
    text = re.sub(r"(?i)<br\s*/?>|</p>|</li>", "\n", raw or "")
    text = html.unescape(re.sub(r"<[^>]+>", "", text))
    lines = [re.sub(r"[ \t]+", " ", ln).strip() for ln in text.splitlines()]
    return "\n".join(ln for ln in lines if ln)


def snippet(raw):
    text = "\n".join(clean_text(raw).splitlines()[:SNIPPET_LINES])
    return text if len(text) <= SNIPPET_MAX else text[: SNIPPET_MAX - 1].rstrip() + "…"


def parse_json_listing(body):
    posts = []
    for child in json.loads(body)["data"]["children"]:
        d = child["data"]
        link = "https://www.reddit.com" + d["permalink"] if d.get("permalink") else d.get("url", "")
        posts.append({"id": d["id"], "title": d.get("title", ""), "selftext": d.get("selftext", ""),
                      "url": link, "score": int(d.get("score", 0)), "flair": d.get("link_flair_text") or ""})
    return posts


def _local(tag):
    return tag.rsplit("}", 1)[-1]


def _child_text(elem, name):
    for c in elem:
        if _local(c.tag) == name:
            return "".join(c.itertext()).strip()
    return ""


def parse_rss(body):
    posts = []
    for e in [x for x in ET.fromstring(body).iter() if _local(x.tag) == "entry"]:
        link = next((c.attrib.get("href", "") for c in e if _local(c.tag) == "link"), "")
        content = _child_text(e, "content")
        m = re.search(r'<div class="md">(.*?)</div>', content, re.S)
        posts.append({"id": re.sub(r"^t3_", "", _child_text(e, "id") or link),
                      "title": _child_text(e, "title"), "selftext": m.group(1) if m else "",
                      "url": link, "score": None, "flair": ""})  # RSS has no upvote counts
    return posts


def fetch_subreddit(sub):
    """Try JSON then RSS endpoints; log the HTTP status of every failure."""
    last = None
    for template, kind in ENDPOINTS:
        url = template.format(sub=sub)
        label = url.split("?")[0]
        for ua in USER_AGENTS:
            try:
                status, body = http_get(url, ua)
            except Exception as exc:
                log(f"GET {label} failed: {type(exc).__name__}")
                last = exc
                break
            if status == 200:
                try:
                    return parse_json_listing(body) if kind == "json" else parse_rss(body)
                except Exception as exc:
                    log(f"GET {label}: unreadable response ({type(exc).__name__})")
                    last = exc
                    break
            log(f"GET {label} -> HTTP {status}")
            last = RuntimeError(f"HTTP {status}")
            if status != 403:  # 429 etc.: don't hammer, go to the next endpoint
                break
    raise RuntimeError(f"all endpoints failed for r/{sub}") from last


# --------------------------------------------------------------------------- #
# Scoring
# --------------------------------------------------------------------------- #
def score_title(title, cfg):
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


def should_alert(post, cfg):
    score, high_count = score_title(post["title"], cfg)
    if score is None or score < cfg["score_threshold"]:
        return False
    upvotes = post["score"]  # None when only RSS was reachable
    return upvotes is None or upvotes >= cfg["min_upvotes"] or high_count >= 2


# --------------------------------------------------------------------------- #
# One scan cycle
# --------------------------------------------------------------------------- #
def heartbeat_due(now, cfg, state):
    return (now.weekday() == cfg["heartbeat_day_of_week"] and now.hour == cfg["heartbeat_hour_local"]
            and state["last_heartbeat"] != now.date().isoformat())


def run_once(topic, token, cfg, state, seen, now=None):
    now = now or datetime.now()
    seen_set = set(seen)
    failed = False
    for sub in cfg["subreddits"]:
        try:
            posts = fetch_subreddit(sub)
        except Exception as exc:
            failed = True
            log(f"r/{sub} failed: {exc}")
            continue
        for post in posts:
            key = "reddit:" + post["id"]
            if key in seen_set or not should_alert(post, cfg):
                continue
            head = f"[{post['flair']}] {post['title']}" if post["flair"] else post["title"]
            text = snippet(post["selftext"])
            if notify(topic, token, head + (("\n" + text) if text else ""),
                      "Claude Free Reset Alert!", "gift,rocket", link=post["url"]):
                seen.append(key)
                seen_set.add(key)
                log(f"ALERT sent: r/{sub} {post['id']}")
        time.sleep(random.uniform(1, 3))  # be polite between subreddits

    if failed:
        state["consecutive_failures"] += 1
        if state["consecutive_failures"] >= FAILURE_LIMIT:
            if notify(topic, token, "⚠️ Local Claude scanner failed 3 scans in a row - please check it",
                      "Claude Local Scanner Error", "warning,loudspeaker"):
                state["consecutive_failures"] = 0
    else:
        state["consecutive_failures"] = 0

    if heartbeat_due(now, cfg, state):
        if notify(topic, token, "Local Claude scanner is alive and scanning Reddit ✅",
                  "Claude Local Scanner Heartbeat", "white_check_mark,bell", priority="low", sound=False):
            state["last_heartbeat"] = now.date().isoformat()
    save_seen(seen)
    save_state(state)
    return not failed


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--once", action="store_true", help="scan once and exit")
    parser.add_argument("--test", action="store_true", help="send a test notification and exit")
    args = parser.parse_args()

    load_env()
    topic = os.environ["NTFY_TOPIC"].strip()  # set in local/.env or the environment
    if not topic:
        sys.exit("NTFY_TOPIC is empty - put NTFY_TOPIC=your-topic in local/.env")
    token = os.environ.get("NTFY_TOKEN") or None
    cfg = load_config()

    if args.test:
        ok = notify(topic, token, "TEST: local Claude scanner works ✅\nIf you see this on your phone, alerts reach you.",
                    "Claude Local Scanner TEST", "gift,rocket")
        print("Test notification sent" if ok else "Test notification FAILED")
        sys.exit(0 if ok else 1)

    state, seen = load_state(), load_seen()
    log("Local scanner started")
    while True:
        try:
            run_once(topic, token, cfg, state, seen)
        except Exception as exc:  # never die on a surprise; log and keep going
            log(f"unexpected error: {type(exc).__name__}")
        if args.once:
            return
        time.sleep(cfg["interval_seconds"] + random.uniform(0, 30))


if __name__ == "__main__":
    main()
