#!/usr/bin/env python3
"""Scan Reddit r/ClaudeAI and Anthropic status RSS feeds for free usage/reset announcements."""
import os
import xml.etree.ElementTree as ET

import requests

FEEDS = [
    "https://www.reddit.com/r/ClaudeAI/new/.rss",
    "https://status.anthropic.com/history.rss",
]
USER_AGENT = "claude-usage-reset-scanner/1.0 (github.com/RO4449-cyber/claude-reset-usage-check)"
NTFY_URL = "https://ntfy.sh/claude_usage_reset_check"
SEEN_FILE = "seen_posts.txt"
MAX_ENTRIES = 10

BONUS_WORDS = ["free", "bonus", "gift", "everyone", "extra"]
USAGE_WORDS = ["reset", "limit", "limits", "usage", "quota", "tokens"]


def load_seen():
    if not os.path.exists(SEEN_FILE):
        return set()
    with open(SEEN_FILE, "r", encoding="utf-8") as f:
        return {line.strip() for line in f if line.strip()}


def save_seen(seen):
    with open(SEEN_FILE, "w", encoding="utf-8") as f:
        for item in sorted(seen):
            f.write(item + "\n")


def local(tag):
    return tag.rsplit("}", 1)[-1]


def child_text(elem, name):
    for c in elem:
        if local(c.tag) == name:
            return "".join(c.itertext()).strip()
    return ""


def entry_link(elem):
    for c in elem:
        if local(c.tag) == "link":
            return c.attrib.get("href") or (c.text or "").strip()
    return ""


def parse_feed(xml_bytes):
    root = ET.fromstring(xml_bytes)
    entries = [e for e in root.iter() if local(e.tag) in ("entry", "item")]
    results = []
    for e in entries[:MAX_ENTRIES]:
        title = child_text(e, "title")
        link = entry_link(e)
        eid = child_text(e, "id") or child_text(e, "guid") or link
        body = child_text(e, "content") or child_text(e, "summary") or child_text(e, "description")
        results.append({"id": eid, "title": title, "link": link, "text": f"{title} {body}"})
    return results


def matches(text):
    t = text.lower()
    return any(w in t for w in BONUS_WORDS) and any(w in t for w in USAGE_WORDS)


def send_alert(title, link):
    resp = requests.post(
        NTFY_URL,
        data=f"{title}\n{link}".encode("utf-8"),
        headers={
            "Title": "Claude Free Reset Alert!",
            "Priority": "high",
            "Tags": "gift,robot",
        },
        timeout=15,
    )
    resp.raise_for_status()


def main():
    seen = load_seen()
    for url in FEEDS:
        try:
            resp = requests.get(url, headers={"User-Agent": USER_AGENT}, timeout=20)
            resp.raise_for_status()
            entries = parse_feed(resp.content)
        except Exception as exc:
            print(f"Failed to fetch {url}: {exc}")
            continue
        for entry in entries:
            if entry["id"] in seen or not matches(entry["text"]):
                continue
            try:
                send_alert(entry["title"], entry["link"])
            except Exception as exc:
                print(f"Failed to send alert for {entry['id']}: {exc}")
                continue
            seen.add(entry["id"])
            print(f"Alerted: {entry['title']}")
    save_seen(seen)


if __name__ == "__main__":
    main()
