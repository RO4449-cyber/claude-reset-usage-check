# claude-reset-usage-check
## Setup

Add these under **Settings → Secrets and variables → Actions → Repository secrets**:

- `NTFY_TOPIC` (required): your private ntfy topic name.
- `NTFY_TOKEN` (optional): ntfy access token, sent as a Bearer token.

Tune keywords, thresholds and the heartbeat time in `config.json`; no code changes needed.
