# JARVIS: an always-on Gemini voice assistant with a team of background agents (macOS)

Say **"Hey Jarvis"** from anywhere. It starts at login and lives in your **menu bar** (no Dock icon, no window). You only ever talk to Jarvis. For real work he hands jobs to specialist agents that run **in parallel in the background**, then tells you when each one finishes.

```
                         ┌────────────── you: "Hey Jarvis, ..." ──────────────┐
mic ─▶ wake word (local) ─▶ Jarvis (Gemini): chats, quick actions, delegates ─▶ voice
                                   │ delegate_task            ▲ "task 3 finished..."
                    ┌──────────────┼──────────────┐           │
                 coder        spreadsheet     researcher ... operator  (+ agents you create)
                 each = its own Gemini loop with tools: files, shell, Python, Excel, web, screen control
```

## Upgrading from an earlier version

Unzip the new version next to the old one, copy your old `.env` file into it, then run `./install.sh` again (it reuses `.env`, asks for an ElevenLabs key, pulls the local model, and re-registers the background service). Your memory and task history in `~/.jarvis/` are kept.

## Install (macOS, Python 3.10+)

1. Get a free API key at https://aistudio.google.com
2. `cd jarvis && ./install.sh` (builds the environment, downloads the wake-word model, asks for your key, asks for mic permission, registers the auto-start service).
3. Grant the permissions macOS asks for over time: Microphone, and for the agents' hands: **Accessibility** and **Screen Recording** (System Settings > Privacy & Security) for the app that launches Jarvis, plus Automation prompts the first time an agent controls an app.

## The menu bar icon

| Icon | Meaning |
|---|---|
| 🎙 | Listening for "Hey Jarvis" |
| 🔴 | Listening to you |
| 🧠 | Thinking (a number after it = agents working) |
| 🔊 | Speaking |
| ⏸ | Paused, microphone fully off |
| 🔔 | An agent is waiting for your approval |
| ⚠️ | A problem; open the menu and choose Open log |

Click it for: current status and which model is answering; **Pause listening**; **Type to Jarvis...** (a text box, handy when you can't talk); **Agents** (what each agent is doing, a Stop button per task, and one-click **Approve / Deny** when an agent needs permission); **Agent permissions** (switch between Ask, Trusted and Full, remembered across restarts); Open workspace folder; Open log; Quit.

## Voice: ElevenLabs

Put `ELEVENLABS_API_KEY=...` in `.env` (the installer asks for it). Jarvis then speaks with ElevenLabs (`tts.engine: elevenlabs`), streaming the audio so it starts almost immediately. Choose your voice by pasting its ID into `tts.elevenlabs_voice_id`. Replies longer than `tts.elevenlabs_max_chars` and any ElevenLabs failure fall back to the free Mac voice, so a quota or network problem never leaves Jarvis mute. Optional: `stt.provider: elevenlabs` uses ElevenLabs Scribe to transcribe you instead of Gemini. Test with `./jarvisctl.sh voice`.

## Which AI models Jarvis uses (and what happens when one fails)

`llm.jarvis_chain` lists the models Jarvis tries in order, by default **Gemini, then ChatGPT (OpenAI), then the on-device model**. If one hits a quota, a 503 "high demand", or a bad key or model name, Jarvis moves to the next and skips the failed one for a while (30 minutes for a used-up quota, 2 minutes for an outage), so you notice one slow answer, not a slow answer every time. The next model is given the recent conversation, so it carries on naturally. Background agents do the same through `llm.fallback_order`. Providers without a key are skipped silently.

To add ChatGPT: create an API key at platform.openai.com (this is separate from a ChatGPT Plus subscription and bills per use, so add a few dollars of credit), then put `OPENAI_API_KEY=...` in `.env`. The default model is `gpt-5.4-mini`; change `llm.openai_model` if you want another. `llm.openai_base_url` also works with other OpenAI-compatible services such as OpenRouter or Groq.

## On-device models (no rate limits)

Background agents run on a local model through [Ollama](https://ollama.com) by default (`llm.agents_provider: ollama`, model `nemotron-3-nano:4b`, about 3 GB, fine for a 16 GB Mac). Jarvis himself stays on Gemini: his chats are a handful of requests, and a small model struggles with his ~30 tools.

1. Install Ollama from ollama.com and open it once (it then runs in the menu bar).
2. `ollama pull nemotron-3-nano:4b` (the installer does this if Ollama is present).
3. `./jarvisctl.sh local` checks the model loads and can call a tool.

Know the limits: a 4B model is much weaker than Gemini at long, multi-step jobs, and a fanless laptop slows down under sustained load. Agents on the local engine run one at a time (`llm.local_parallel`). Safety nets: if Ollama is down or errors, the task automatically retries on Gemini (`llm.fallback_to_gemini`), the `operator` agent always uses Gemini (`llm.gemini_agents`), and you can say "use Gemini for this" to force it per task. If the spreadsheet agent makes mistakes locally, add `spreadsheet` to `gemini_agents`. The 30B Nemotron needs about 24 GB, too much for 16 GB. Web search and screen understanding still call Gemini, a few requests per use.

## What you can say

- "Hey Jarvis, finish the German vocabulary sheet in my Documents folder." -> spreadsheet agent finds it, inspects it, fills every row, verifies, backs the original up first.
- "Write a Python script that renames my screenshots by date, and at the same time research the best laptops for video editing, and draft a cover letter." -> three agents work at once.
- "What's everyone doing?" -> task list with each agent's latest step. "Cancel task 2."
- "Create an agent called Tutor that makes German flashcards and quizzes from any file I give it." -> permanent new agent.
- When an agent needs your OK, Jarvis announces it. Say **"approve"** or **"deny"**.
- Everything from the first version still works: weather, timers, music, volume, screen description, notes, memory ("remember that...").

Built-in agents: **coder**, **spreadsheet**, **researcher**, **writer**, **operator** (does anything a person can: scripts, AppleScript, mouse and keyboard). Agents can spawn helper agents (depth-limited). Each task's report, log and result are saved to `~/Jarvis Workspace/tasks/`.

## How much freedom the agents have (`agents.permission_mode`)

| Mode | Behaviour |
|---|---|
| `trusted` (default) | Agents work freely. Every existing file is **backed up to `~/Jarvis Workspace/backups/`** before any change, and deletes go to the Trash. They pause for your OK only on risky things: deleting outside the workspace, killing processes, uploading data, sending mail/messages, changing system settings. |
| `ask` | You approve every write, command and screen action. Good for the first few days. |
| `full` | No approval prompts at all. |

**Always blocked, in every mode:** `sudo`, disk erasing, shutdown, `curl | sh`, and anything touching SSH keys, cloud credentials, keychains, browser profiles or Jarvis's own `.env`. This is on purpose: agents read web pages and files, and those can contain hidden instructions ("prompt injection"). The hard block means even a hijacked agent cannot steal your keys or wreck the machine.

Honest limits: `run_python` and `shell` are policed by pattern checks, not a sandbox, so a determined script can get around them. If you want real isolation, run Jarvis under a separate macOS user account. Gemini can also simply be wrong, which is why edits are backed up and agents verify their work. Emergency stop for mouse/keyboard automation: slam the mouse into the top-left screen corner.

## Manage it

```
./jarvisctl.sh status | logs | restart | stop | start
./jarvisctl.sh text     # type to Jarvis instead of speaking (best first test)
./jarvisctl.sh menubar  # run the menu bar app in this terminal
./jarvisctl.sh run      # foreground without the menu bar, verbose logs (for debugging)
./jarvisctl.sh mic      # live mic levels, to tune audio.min_rms
./jarvisctl.sh voice    # hear the configured voice
./jarvisctl.sh local    # test the on-device model
./uninstall.sh          # remove auto-start
```
Logs: `~/Library/Logs/jarvis.log`. Memory, agents and task records: `~/.jarvis/`. Running tasks do not survive a restart; finished ones are kept.

## Configure (`config.yaml`, then `./jarvisctl.sh restart`)

| Want | Change |
|---|---|
| Newer Gemini model | `gemini.model`, `agents.model` |
| Different local model | `llm.ollama_model` (any Ollama model that supports tools) |
| Agents on Gemini instead of local | `llm.agents_provider: gemini` |
| More/fewer agents at once | `agents.max_concurrent` (Gemini rate limits apply; the free tier is tight) |
| Agents work longer per task | `agents.max_steps` |
| Safer / freer agents | `agents.permission_mode` |
| Nicer voice | `tts.engine: gemini` or `tts.mac_voice` |
| Wake word too eager / deaf | `wake.threshold` |

Add your own tool: a type-hinted function with a docstring and `@tool` in `jarvis/tools/`, then list its name in a group in `tools/__init__.py`. Add an agent permanently by voice (`create_agent`) or in `agents/definitions.py`.

## Troubleshooting

- **Log says "Microphone is returning pure silence":** run `./install.sh app` (installs a hidden Jarvis.app login item that gets its own mic permission).
- **Spreadsheet edits lose charts or images:** `openpyxl` only keeps cell data and formatting. Ask for "save as a copy" on such files; originals are always backed up.
- **Clicks do nothing:** grant Accessibility to the app that launched Jarvis.
- **Model name rejected / 429 errors:** names change often (https://ai.google.dev/gemini-api/docs/models); lower `max_concurrent` if you hit rate limits.

## Layout

```
config.yaml  .env  install.sh  uninstall.sh  jarvisctl.sh
jarvis/
  menubar.py     the menu bar app (how Jarvis normally runs)
  assistant.py   wake -> listen -> Jarvis -> speak loop, announcements from agents
  brain.py       Jarvis's Gemini session, personality, delegation rules
  agents/        definitions.py (roles), manager.py (parallel tasks), approvals.py
  tools/         policy.py (permissions) + power/excel/gui/web/system/file/memory/agent tools
  audio.py  speech.py  llm.py  local_llm.py  openai_llm.py  memory.py  config.py  util.py
```
