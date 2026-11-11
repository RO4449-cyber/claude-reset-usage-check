#!/bin/bash
# Installs the local scanner as a macOS login item (launchd) so it starts automatically.
# Run from this folder:  bash install_macos.sh      (undo: bash install_macos.sh uninstall)
set -e
LABEL="com.claude-reset-scanner"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
DIR="$(cd "$(dirname "$0")" && pwd)"

if [ "$1" = "uninstall" ]; then
  launchctl unload "$PLIST" 2>/dev/null || true
  rm -f "$PLIST"
  echo "Removed $LABEL"
  exit 0
fi

PY="$(command -v python3 || true)"
[ -n "$PY" ] || { echo "python3 not found. Install it from python.org first."; exit 1; }
[ -f "$DIR/.env" ] || { echo "Create $DIR/.env first (copy .env.example and set NTFY_TOPIC)."; exit 1; }

mkdir -p "$HOME/Library/LaunchAgents"
cat > "$PLIST" <<PLISTEOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>$LABEL</string>
  <key>ProgramArguments</key><array><string>$PY</string><string>$DIR/scanner_local.py</string></array>
  <key>WorkingDirectory</key><string>$DIR</string>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>StandardOutPath</key><string>$DIR/scanner.log</string>
  <key>StandardErrorPath</key><string>$DIR/scanner.log</string>
</dict></plist>
PLISTEOF
launchctl unload "$PLIST" 2>/dev/null || true
launchctl load "$PLIST"
echo "Installed. It now runs in the background and starts at login. Log: $DIR/scanner.log"
