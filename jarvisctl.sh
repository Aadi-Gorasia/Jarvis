#!/usr/bin/env bash
cd "$(dirname "$0")"
LABEL="com.jarvis.assistant"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
G="gui/$(id -u)"
case "${1:-status}" in
  start)   if [ -f "$PLIST" ]; then launchctl bootstrap "$G" "$PLIST" 2>/dev/null || launchctl kickstart -k "$G/$LABEL"
           else open "$HOME/Applications/Jarvis.app"; fi ;;
  stop)    launchctl bootout "$G" "$PLIST" 2>/dev/null; pkill -f "jarvis[.](main|menubar)" ;;
  restart) "$0" stop; sleep 1; "$0" start ;;
  status)  pgrep -fl "jarvis[.](main|menubar)" || echo "Jarvis is not running"; tail -n 5 "$HOME/Library/Logs/jarvis.log" 2>/dev/null ;;
  logs)    tail -f "$HOME/Library/Logs/jarvis.log" ;;
  run)     "$0" stop; exec ./venv/bin/python -m jarvis.main -v ;;   # foreground without the menu bar, for debugging
  menubar) "$0" stop; exec ./venv/bin/python -m jarvis.menubar ;;     # menu bar app in this terminal
  text)    exec ./venv/bin/python -m jarvis.main --text ;;          # type instead of speak
  local)   exec ./venv/bin/python -m jarvis.main --test-local ;;       # check the on-device model
  voice)   exec ./venv/bin/python -m jarvis.main --test-voice ;;       # hear the configured voice
  doctor)  exec ./venv/bin/python -m jarvis.main --doctor ;;          # check everything, say what to fix
  wake)    exec ./venv/bin/python -m jarvis.main --test-wake ;;        # watch wake-word scores live
  mic)     exec ./venv/bin/python -m jarvis.main --test-mic ;;
  *)       echo "usage: $0 {start|stop|restart|status|logs|run|menubar|text|mic|wake|doctor|local|voice}" ;;
esac
