# JARVIS 3: an always-on Gemini voice assistant with a team of agents, living in your Mac's menu bar

Say **"Hey Jarvis"**. A dark halo fades in around your screen edges (that is your cue to speak), simple commands happen instantly, everything else is answered by AI as it streams in, and bigger jobs go to background agents that report back when done.

```
mic ─▶ wake word (local) ─▶ halo on ─▶ hear you (Gemini) ─┬─▶ instant command? ─▶ do it, speak (no AI call)
                                                          └─▶ AI answer streams in ─▶ speaks sentence by sentence
                                                                   │ delegate_task
                                    coder · spreadsheet · researcher · writer · operator · your own agents (parallel)
```

## Install (macOS, Python 3.10+; 3.12 recommended)

1. Keys: a Gemini key from aistudio.google.com (required). Optional: ElevenLabs (voice), OpenAI (backup brain).
2. Unzip, copy your old `.env` in (or let the installer ask), then `./install.sh`.
3. Grant the macOS prompts (Microphone now; Accessibility and Screen Recording later for agents that click).
4. Run **`./jarvisctl.sh doctor`**. It checks every part, times Gemini and ElevenLabs, and says exactly what to fix.
5. Look for 🎙 in the menu bar. It starts at every login.

## Why it is fast now

| Before | Now |
|---|---|
| Every request went through an AI model | **Instant commands** run locally in milliseconds: time, date, open apps and sites, volume, mute, music, timers and reminders, weather, battery, agent status, cancel task, approve/deny |
| Waited for the full AI answer, then spoke | **Streaming**: speech starts after the first sentence; the next sentence is synthesised while the current one plays |
| Busy Gemini could stall 60+ s (hidden SDK retries stacked up) | **12 s timeout, no hidden retries**, then instant failover to the next model (ChatGPT, then on-device) and the failed one is skipped for a while |
| Big, slow model for chat | Chat uses a fast lite model with minimal thinking; agents use the stronger one |
| New HTTPS connection per sentence | Warm connection reuse |

Every answer logs its timing: `⏱ heard->voice 1.8s (gemini)` in `~/Library/Logs/jarvis.log`.

## The halo

A soft black/grey vignette around every display while you talk to Jarvis: steady while he listens to you, slow breathing while he thinks, a quicker pulse while he speaks. It is click-through (never blocks the mouse), shows over full-screen apps and all Spaces, and fades out when idle. Tune it in `config.yaml` under `halo:` (colour, strength, thickness) or switch it off from the menu bar. If it ever fails it disables itself and Jarvis keeps working.

## The menu bar icon

🎙 listening · 🔴 hearing you · 🧠 thinking (number = agents working) · 🔊 speaking · ⏸ paused (mic fully off) · 🔔 an agent needs approval · ⚠️ problem.
Menu: status and which model is answering, **Pause listening**, **Type to Jarvis...**, **Stop speaking**, **Agents** (live task list, Stop buttons, one-click Approve/Deny), **Agent permissions**, **Screen halo**, workspace folder, log, Quit.

## What you can say

- Instant: "what time is it", "open YouTube", "open Gmail in Chrome", "volume down", "set the volume to 30", "pause the music", "timer for ten minutes", "remind me in 20 minutes to call mom", "what's the weather", "how's my battery", "what's everyone doing", "cancel task 2".
- Anything else goes to the AI, which can use tools and hand big jobs to agents: "finish the German spreadsheet in my Documents folder", "research the best laptops for video editing and write a report", "create an agent called Tutor that makes flashcards".
- When an agent needs permission you hear it and see 🔔. Say "approve" / "deny", or click in the menu.

## Models and fallbacks

`llm.jarvis_chain` (default Gemini, ChatGPT, on-device) is tried in order; a failed one is skipped (30 min for quota, 2 min for an outage). The next model gets the recent conversation and your saved facts, so it carries on naturally. Agents use `llm.fallback_order`. Providers without a key are skipped. On-device means Ollama (`nemotron-3-nano:4b`, about 3 GB); install Ollama from ollama.com and `ollama pull nemotron-3-nano:4b`. The 30B version needs about 24 GB, too much for 16 GB Macs.

## Agent safety (`agents.permission_mode`)

`trusted` (default): agents work freely, every existing file is backed up to `~/Jarvis Workspace/backups/` before any change, deletes go to the Trash, and risky actions (deleting outside the workspace, killing processes, uploading data, sending mail, system settings) pause for your OK. `ask`: approve everything. `full`: no prompts. Always blocked in every mode: `sudo`, disk erasing, `curl | sh`, and anything touching SSH keys, cloud credentials, keychains, browser profiles or Jarvis's own `.env` (a guard against prompt injection from web pages and files). Pattern checks are not a sandbox; for real isolation run Jarvis under a separate macOS user. Slam the mouse into the top-left corner to abort screen automation.

## Manage it

```
./jarvisctl.sh doctor    # check everything
./jarvisctl.sh menubar   # run the menu bar app in this terminal (to watch it)
./jarvisctl.sh status | logs | restart | stop | start
./jarvisctl.sh text      # type to Jarvis, no mic
./jarvisctl.sh mic | wake | voice | local | run
./uninstall.sh
```
Logs: `~/Library/Logs/jarvis.log` · memory, tasks, last recording (`last_utterance.wav`): `~/.jarvis/`.

## Troubleshooting

- **Icon appears but "Hey Jarvis" does nothing:** `./jarvisctl.sh doctor`. If the mic reads silence under the background service, run `./install.sh app` (a small Jarvis.app login item with its own microphone permission).
- **Heard nothing / cut off:** the halo shows when Jarvis is listening; speak after it appears. Tune `audio.min_rms` and `audio.silence_seconds`; Jarvis tracks room noise automatically.
- **Slow answers:** read the `⏱` lines in the log; `doctor` shows Gemini's current latency.
- **Odd crashes on Python 3.14:** rebuild the venv with Python 3.12 (`brew install python@3.12`).

## Layout

```
config.yaml  .env  install.sh  uninstall.sh  jarvisctl.sh
jarvis/ menubar.py (the app) · halo.py (screen halo) · assistant.py (wake→listen→answer loop) · reflex.py (instant commands)
        brain.py (streaming, model chain) · speech.py (pipelined voice, hearing) · audio.py · doctor.py
        llm.py local_llm.py openai_llm.py · memory.py · agents/ (roles, manager, approvals) · tools/ (policy + all tools)
```
