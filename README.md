# claude-reset-usage-check
## Setup

Add these under **Settings → Secrets and variables → Actions → Repository secrets**:

- `NTFY_TOPIC` (required): your private ntfy topic name.
- `REDDIT_CLIENT_ID` and `REDDIT_CLIENT_SECRET` (recommended): from a free "script" app at reddit.com/prefs/apps. Without them the scanner falls back to Reddit RSS, which GitHub runners usually can't reach.
- `NTFY_TOKEN` (optional): ntfy access token, sent as a Bearer token.

Tune keywords, thresholds and the heartbeat time in `config.json`; no code changes needed.
