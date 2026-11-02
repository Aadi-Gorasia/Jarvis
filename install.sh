#!/usr/bin/env bash
# Usage: ./install.sh [launchd|app]
#   launchd (default): background service, starts at login, auto-restarts if it crashes
#   app:               tiny Jarvis.app login item (use this if the mic returns silence under launchd)
set -euo pipefail
cd "$(dirname "$0")"
ROOT="$(pwd)"
MODE="${1:-launchd}"
LABEL="com.jarvis.assistant"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
APP="$HOME/Applications/Jarvis.app"
UIDN="$(id -u)"

[ "$(uname)" = "Darwin" ] || { echo "This installer is for macOS."; exit 1; }

PY=""
for c in python3.12 python3.11 python3.10 python3; do
  if command -v "$c" >/dev/null 2>&1 && "$c" -c 'import sys; sys.exit(0 if sys.version_info >= (3,10) else 1)' 2>/dev/null; then PY="$c"; break; fi
done
[ -n "$PY" ] || { echo "Python 3.10+ is required:  brew install python@3.12"; exit 1; }

echo "==> Creating virtualenv and installing packages (a few minutes the first time)"
[ -d venv ] || "$PY" -m venv venv
./venv/bin/pip install --quiet --upgrade pip
./venv/bin/pip install --quiet -r requirements.txt
echo "==> Downloading the 'Hey Jarvis' wake-word model"
./venv/bin/python -c "import openwakeword; openwakeword.utils.download_models()"

if [ ! -f .env ]; then
  read -r -s -p "Paste your Gemini API key (from aistudio.google.com): " KEY; echo
  printf 'GEMINI_API_KEY=%s\n' "$KEY" > .env
  chmod 600 .env
fi

if ! grep -q '^ELEVENLABS_API_KEY=' .env; then
  read -r -s -p "ElevenLabs API key for Jarvis's voice (press Enter to skip and use the Mac voice): " EK; echo
  if [ -n "$EK" ]; then printf 'ELEVENLABS_API_KEY=%s\n' "$EK" >> .env; fi
fi

if ! grep -q '^OPENAI_API_KEY=' .env; then
  read -r -s -p "OpenAI (ChatGPT API) key as a backup when Gemini is down (Enter to skip): " OK; echo
  if [ -n "$OK" ]; then printf 'OPENAI_API_KEY=%s\n' "$OK" >> .env; fi
fi

MODEL="$(./venv/bin/python -c 'from jarvis.config import load_config; print(load_config().llm.ollama_model)')"
if command -v ollama >/dev/null 2>&1; then
  echo "==> Downloading the on-device model ($MODEL), about 3 GB"
  ollama pull "$MODEL" || echo "   (could not pull it now; run: ollama pull $MODEL)"
else
  echo "==> Ollama is not installed. Get it from https://ollama.com, open it once, then run: ollama pull $MODEL"
fi

echo "==> Microphone permission: speak for a few seconds and approve the macOS prompt if it appears"
./venv/bin/python -m jarvis.main --test-mic || true

# clear any previous install so the two modes never run at once
launchctl bootout "gui/$UIDN" "$PLIST" 2>/dev/null || true
pkill -f "jarvis[.](main|menubar)" 2>/dev/null || true
osascript -e 'tell application "System Events" to delete (every login item whose name is "Jarvis")' 2>/dev/null || true

if [ "$MODE" = "launchd" ]; then
  rm -rf "$APP"
  mkdir -p "$HOME/Library/LaunchAgents"
  cat > "$PLIST" <<PLISTEOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>$LABEL</string>
  <key>ProgramArguments</key><array>
    <string>$ROOT/venv/bin/python</string><string>-m</string><string>jarvis.menubar</string>
  </array>
  <key>WorkingDirectory</key><string>$ROOT</string>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>ThrottleInterval</key><integer>10</integer>
  <key>ProcessType</key><string>Interactive</string>
  <key>StandardOutPath</key><string>$HOME/Library/Logs/jarvis.launchd.log</string>
  <key>StandardErrorPath</key><string>$HOME/Library/Logs/jarvis.launchd.log</string>
  <key>EnvironmentVariables</key><dict>
    <key>PATH</key><string>/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin</string>
  </dict>
</dict></plist>
PLISTEOF
  launchctl bootstrap "gui/$UIDN" "$PLIST"
  echo "==> Installed as a LaunchAgent. Jarvis is running and will start at every login."
else
  rm -f "$PLIST"
  mkdir -p "$HOME/Applications"
  rm -rf "$APP"
  TMP="$(mktemp -t jarvis).applescript"
  printf 'do shell script "cd %s && nohup %s/venv/bin/python -m jarvis.menubar >/dev/null 2>&1 &"\n' "'$ROOT'" "'$ROOT'" > "$TMP"
  osacompile -o "$APP" "$TMP"
  defaults write "$APP/Contents/Info" LSUIElement -bool true
  defaults write "$APP/Contents/Info" NSMicrophoneUsageDescription -string "Jarvis listens for its wake word."
  codesign --force --deep --sign - "$APP" 2>/dev/null || true
  osascript -e "tell application \"System Events\" to make login item at end with properties {path:\"$APP\", hidden:true}"
  open "$APP"
  echo "==> Installed Jarvis.app as a hidden login item. Approve the microphone prompt for 'Jarvis'."
fi

echo
./venv/bin/python -m jarvis.main --doctor || true
echo "Look for the microphone icon in your menu bar, then say 'Hey Jarvis'. Manage it with ./jarvisctl.sh  (status | logs | restart | stop | text | mic)"
