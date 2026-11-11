# Local Claude reset scanner (free, runs on your own computer)

GitHub's servers are blocked by Reddit, so this version runs **on your computer**, where Reddit
normally lets requests through. It checks r/ClaudeAI and r/Anthropic every ~10 minutes and pushes
an alert to your phone (via ntfy) when a post looks like a usage-limit reset or free usage news.

It uses only Python's standard library. No accounts, no API keys, no cost.

## Setup (macOS / Linux)
```bash
git clone https://github.com/RO4449-cyber/claude-reset-usage-check
cd claude-reset-usage-check/local
cp .env.example .env          # then open .env and set NTFY_TOPIC to your private topic
python3 scanner_local.py --test   # you should get a push on your phone
python3 scanner_local.py          # scans forever (Ctrl+C to stop)
```

### Start automatically at login (macOS)
```bash
bash install_macos.sh             # remove later with: bash install_macos.sh uninstall
```
Log file: `local/scanner.log`.

### Linux / cron alternative
`*/10 * * * * cd /path/to/claude-reset-usage-check/local && python3 scanner_local.py --once`

## Good to know
- It only works while the computer is **awake and online**. A sleeping laptop scans nothing.
  For 24/7 coverage use a machine that stays on (home PC, Raspberry Pi). Free cloud servers usually
  have IPs that Reddit blocks, just like GitHub's.
- Tune keywords, thresholds, subreddits and the interval in `local_config.json`.
- Use a **new random topic** in `.env` (the topic is like a password; never commit `.env`).
- You get a heartbeat push on Sundays at 10:00 (local time) and an error push after 3 failed scans.
- The GitHub workflow keeps watching the Anthropic status page; this one covers Reddit.
- Reddit RSS has no upvote counts. If only RSS works, the upvote filter is skipped.
