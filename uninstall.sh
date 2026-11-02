#!/usr/bin/env bash
set -u
LABEL="com.jarvis.assistant"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
launchctl bootout "gui/$(id -u)" "$PLIST" 2>/dev/null
rm -f "$PLIST"
pkill -f "jarvis.main" 2>/dev/null
osascript -e 'tell application "System Events" to delete (every login item whose name is "Jarvis")' 2>/dev/null
rm -rf "$HOME/Applications/Jarvis.app"
echo "Jarvis auto-start removed. Project folder, ~/.jarvis memory and logs were left in place."
