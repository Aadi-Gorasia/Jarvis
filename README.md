# JARVIS: an always-on Gemini voice assistant for macOS

Say **"Hey Jarvis"** from anywhere. It starts at login, runs invisibly in the background (no app, no window, no Dock icon), listens locally for its wake word, then talks to Google Gemini and controls your Mac through tools.

```
mic ─▶ wake word (local, openWakeWord) ─▶ record until you pause ─▶ Gemini transcribes
                                                                          │
speaker ◀─ macOS voice / Gemini TTS ◀─ Gemini answers, calling tools ◀────┘
```

**Privacy:** the microphone stream is processed on your Mac. Audio is sent to Gemini only *after* the wake word fires, and only for that sentence. Screenshots are taken only when you ask about your screen.

## Install (macOS, Python 3.10+)

1. Get a free API key at https://aistudio.google.com
2. In Terminal:
   ```
   cd jarvis
   ./install.sh
   ```
   It builds a virtualenv, installs packages, downloads the wake-word model, asks for your key, triggers the microphone permission prompt, and registers a LaunchAgent. Jarvis is live when it finishes and returns after every restart or login.

## Try it

- "Hey Jarvis, what's the weather?" / "...what's on my screen?" / "...set a timer for ten minutes"
- "...open Spotify and pause the music" / "...find my resume" / "...turn the volume to 30"
- "...remember that my sister's name is Meera" (it remembers across restarts)
- After an answer you can keep talking for a few seconds without repeating the wake word. Say "that's all" to end.

Built-in tools: time, weather (no key needed), Google-grounded web search, open apps/URLs, volume, music control, system status, timers, notifications, clipboard, screen description (vision), file search/read/list, notes, long-term memory, and an optional confirmed-by-voice shell command.

## Manage it

```
./jarvisctl.sh status | logs | restart | stop | start
./jarvisctl.sh text     # chat by keyboard, no mic needed (good first test of your API key)
./jarvisctl.sh run      # foreground with verbose logs
./jarvisctl.sh mic      # live mic levels, to tune audio.min_rms
./uninstall.sh          # remove auto-start
```
Logs: `~/Library/Logs/jarvis.log`. Memory database and notes: `~/.jarvis/`.

## Configure (`config.yaml`, then `./jarvisctl.sh restart`)

| Want | Change |
|---|---|
| Different/newer Gemini model | `gemini.model` (any model with function calling) |
| Nicer voice | `tts.engine: gemini` (falls back to the Mac voice if it fails), or change `tts.mac_voice` |
| Triggers by accident / misses you | `wake.threshold` up / down |
| Cuts you off, or never stops listening | `audio.silence_seconds`, `audio.min_rms` |
| Home city for weather | `assistant.home_city` |
| Let it run terminal commands | `tools.allow_shell: true` (it asks you to say "yes" every time) |
| Disable a tool | `tools.disabled: [describe_screen]` |

## Add your own tool

Add a function in `jarvis/tools/` (or a new file imported in `tools/__init__.py`):

```python
from .base import tool

@tool
def lights(room: str, on: bool) -> str:
    """Turn the lights in a room on or off."""
    ...  # call your smart-home API here
    return f"{room} lights {'on' if on else 'off'}"
```
Type hints and the docstring are all Gemini needs. Restart and say "turn on the bedroom lights".

## Troubleshooting

- **Wake word never fires, log says "Microphone is returning pure silence":** macOS is blocking the mic for the background process. Re-run `./install.sh app`, which installs a small `Jarvis.app` login item that gets its own microphone permission, and approve the prompt. Also check System Settings > Privacy & Security > Microphone.
- **"describe screen" returns an error:** grant Screen Recording to the app that launched Jarvis (Terminal, or Jarvis.app).
- **Media/volume/notification tools error:** approve the Automation prompts, or enable them under Privacy & Security > Automation.
- **Model name rejected:** Google renames models often; copy a current name from https://ai.google.dev/gemini-api/docs/models into `config.yaml`.
- **Echo or false wake-ups from speakers:** raise `wake.threshold` to 0.7, or use headphones.

## Safety notes

- Text from the web, files, clipboard and screen can contain hidden instructions. The system prompt tells Gemini to treat them as data, but keep `allow_shell` off unless you need it.
- File tools are limited to `tools.allowed_roots` (your home folder by default) and refuse `.ssh`, `.aws`, `.gnupg` and Keychains.

## Layout

```
config.yaml  .env  install.sh  uninstall.sh  jarvisctl.sh  requirements.txt
jarvis/
  main.py        entry point and CLI flags
  assistant.py   wake -> listen -> think -> speak loop
  audio.py       mic stream, wake word, utterance recorder
  speech.py      text-to-speech and Gemini transcription
  brain.py       Gemini chat session, personality, tool calling
  memory.py      SQLite facts + conversation log
  tools/         system, files, web, memory tools
```
