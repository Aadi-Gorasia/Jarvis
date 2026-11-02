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

## Never frozen: interrupt him, keep talking, watch tasks live

- **Interrupt him any time.** Talk over him and he stops mid-word (within a fraction of a second) and listens. Saying his name always interrupts; "stop" or "never mind" cancels what he's saying or thinking about, and if nothing is being said it cancels the newest running task. With **headphones** it is instant. With laptop speakers his own voice reaches the mic, so Jarvis measures how loud that is and only reacts to you being clearly louder: speak up a little, or say "Jarvis, stop". Switch off with `interrupt.enabled: false`, or use the menu bar's **Stop speaking**.
- **He listens while he thinks.** Listening, thinking and speaking run on separate threads, so he never goes deaf. Ask "what time is it?" while he is still working on something else and you get the answer immediately. Add to or change your request ("actually, just the weather") and the old answer is dropped and the new one is told what you had asked before.
- **Live timer.** Whenever something runs, a small panel appears under the menu bar on the right: a spinner, the elapsed time, and what each job is doing right now, e.g. `⠹ 1:12  github #3 · auditing my-site`. The menu bar icon carries the timer too (`🧠 1:12`), and the Agents menu shows a live clock per task. Agents call `report_progress` at each phase ("Auditing repository 4 of 12"). Ask "what are they doing?" and he reads it out. Disable the panel with `hud.enabled: false`.

## Hands-free: no "Hey Jarvis" needed

Just talk. Jarvis tells for himself whether a sentence was meant for him: "Wake up, daddy's here", "So where were we", "open my downloads", "can you remind me at five" all work, while "yeah I think we should move the meeting to Thursday" and "hey mum, what's for dinner" are ignored.

- **How he decides:** on your Mac, in microseconds. Built-in knowledge (his name, summons, requests, questions, how recently he last spoke, how long you talked, whether you addressed someone else, meetings, background media) gives a good score on day one. On top of it sits a small personal model that **learns from you**: every "Hey Jarvis ..." teaches it how you sound when you're talking to him, and corrections teach it strongly.
- **Corrections:** say "I wasn't talking to you" (he apologises and learns), or "Jarvis, I was talking to you" (he answers the sentence he skipped and learns). "Go to sleep" / "be quiet" makes him ignore everything except his name or a clear summons ("wake up") for ten minutes.
- **Borderline sentences** can be checked by a fast AI model (`always_listen.use_ai_judge`); only that one sentence is sent.
- **Privacy:** speech is understood on your Mac with Whisper and **thrown away unless it was for him**; nothing you say to other people is stored or uploaded, and ignored sentences are never logged. That is why hands-free needs `faster-whisper` (installer tries it). Without it, only the wake word works.
- **Sensible limits:** he pauses hands-free if lots of speech is going on around him (a TV, a podcast, a long call), when a video-call app is in front (then only his name works), and ignores his own voice coming back through the mic. Honest limit: a TV character saying "what's the weather" can still fool him now and then; raise or lower `always_listen.sensitivity`, or use the menu bar toggle **Hands-free (no wake word)**.

## Light on your Mac

Jarvis is built to stay out of your way: he runs at lower priority than your apps, math libraries and speech recognition are capped at 2 threads, the halo draws 24 frames a second (15 on battery) with almost no maths, hands-free only does any work when someone is speaking, background checks (messages, imports, learning) wait while the Mac is busy, and when it is busy or on battery agents use the cloud model instead of the on-device one. The on-device model unloads five minutes after use. The menu bar shows his live CPU and memory use. Tune it under `performance:` in `config.yaml`.

## The GitHub agent

"Clean my GitHub" hands the job to the **github agent**. One-time setup: install the GitHub CLI and sign in (`brew install gh`, then `gh auth login`), or put `GITHUB_TOKEN=...` in `.env`.

It inventories your repositories, audits each one (README, LICENSE, .gitignore, junk or secrets committed, big files, stale branches, uncommitted or unpushed work), looks at the GitHub pages themselves, writes a plan, then fixes things in your local clones on a `jarvis/cleanup-<date>` branch: proper READMEs built only from what is really in the code, a LICENSE, a .gitignore, junk removed, a tidy profile README, repository descriptions and topics. It commits, pushes the branch and opens pull requests.

Safeguards: pushes and changes on GitHub ask for your OK once per task (one "yes" covers the same kind of action for the rest of the task, so it doesn't nag per repository). Never allowed in any mode: deleting repositories, force pushes, rewriting history, changing visibility, and touching keys, secrets, webhooks or collaborators. If a secret is committed it tells you the file and line (never the value) and asks you to rotate it.

## The Mac cleaner

"Clean my Mac" hands the job to the **cleaner agent**. It scans your home folder (caches, old downloads, installers, old screenshots, stale `node_modules` and virtualenvs, duplicates, huge files), keeps one copy of any duplicate, and writes a report. Everything is reversible: files only ever move to the **Trash**, never get deleted; one approval covers the whole batch; every batch writes an undo manifest, and **"undo the cleanup"** puts everything back. It never touches system files, settings, Library (other than caches and logs), your main folders themselves, git repositories' own files, or anything you changed in the last week. Anything it isn't sure about goes into a "needs your decision" list instead.

## Why it is fast

| What | Change |
|---|---|
| Waiting for you to finish | A pause of 0.5 s ends your sentence (was 0.8), and transcription **starts during the pause**, so it's usually done the moment you stop |
| Hearing you | **On-device Whisper** when installed (`pip install faster-whisper`, the installer tries it): no network trip, about a second faster. Falls back to Gemini automatically |
| Simple requests | **Instant commands** run locally with no AI call: time, date, open apps and sites, search Google/YouTube/Spotify, volume, mute, music, timers and reminders, weather, battery, messages, agent status |
| Waiting for the AI | Chat uses a fast lite model; the answer **streams**, and speech starts after the first sentence |
| Slow or stuck model | If it hasn't started answering in 10 s the next model takes over (the stuck one is skipped for a minute) |
| Starting to speak | Voice plays **while it downloads** (first bytes, not the whole sentence); connections are kept warm |
| Long jobs | Agents default to the **fast cloud model** with low thinking (the on-device model is private but slow on a laptop). \"Clean my GitHub\" and \"clean my Mac\" start **instantly** with no AI call; batch tools (`github_inventory`, `audit_all_repos`, `mac_scan(\"all\")`) do in one parallel step what used to take dozens of sequential calls; and \"go ahead with everything\" skips approval waits |
| Still waiting | If an answer takes over 1.4 s Jarvis says a short "one moment" so you're never left in silence |

Every answer logs a breakdown, e.g. `⏱ heard->voice 1.9s (AI first words 1.2s, voice start 0.7s, gemini)`, in `~/Library/Logs/jarvis.log`. Run `./jarvisctl.sh doctor` to see what is slowing you down.

## The halo (it moves with your voice)

A dark vignette hugs every screen edge while you talk to Jarvis, with silver and graphite light that **swells inward and ripples in step with how loud you are**. When Jarvis answers, it follows *his* voice instead. While he thinks it swirls calmly, and every time a new phase starts (you begin, he begins) it surges briefly. It is click-through, shows over full-screen apps and all Spaces, and fades out when idle. Under `halo:` in `config.yaml` you can set colour, strength, reach (`thickness`), ripple `speed`, and `sensitivity` (raise it if you speak softly). Switch it off from the menu bar. If drawing ever fails it turns itself off and Jarvis keeps working.

## Context and history: what Jarvis knows about you

Three layers, all local on your Mac in `~/.jarvis/`:

1. **Your ChatGPT and Gemini history (imported).** Neither service lets other programs read your chats live, so this works from the exports they give you:
   - **ChatGPT:** Settings > Data controls > Export data. You get an email with a zip.
   - **Gemini:** takeout.google.com, deselect everything, select **My Activity**, then in its options choose only **Gemini Apps**, format JSON. (Gemini Apps Activity must be switched on in your Google account, or there is nothing to export. Google can repeat the export every two months.)
   - **Import:** drop the zip in your Downloads folder (Jarvis notices within five minutes), or use the menu bar's **Import ChatGPT / Gemini export...**, or `./jarvisctl.sh import-history <zip or folder>`. Re-importing a newer export is safe: duplicates are skipped.
2. **Automatic context in every answer.** When you ask something, Jarvis quietly searches that history *and his own past conversations with you* (instantly, on your Mac). If a past passage genuinely relates (it must share at least two meaningful words with your request) it is added to the question as background, so "what was my plan for the school website CMS?" is answered from what you actually discussed. Unrelated questions get nothing added. Turn it off with `memory.inject_history: false`. The `search_history` tool also lets the AI look things up deliberately.
3. **Things he learns about you.** After each conversation Jarvis saves a few lasting facts (projects, people and how they relate to you, preferences, routines), never health, money or private matters. Ask "what do you know about me?", say "forget [topic]" to remove one, or "remember that..." to add one. Turn off with `memory.auto_learn: false`. `./jarvisctl.sh distill-history` can also propose facts from your imported history for you to approve.

Conversations also carry across sessions: if you spoke within the last 12 hours, a new session starts knowing the last exchange, and when Jarvis switches AI model mid-conversation the new model gets the recent turns.

Privacy: history and facts stay on your Mac. Only the few passages Jarvis finds relevant, and the conversation excerpts used for learning, are sent to the AI that answers you, like anything else you say to it. Exports are your data from the services; nothing is fetched from them without you.

## Your messages, from everywhere (WhatsApp, Instagram, Telegram, Slack...)

There is no official way to read a personal WhatsApp or Instagram account, and unofficial clients get accounts banned. So Jarvis reads the same thing you do: **the web chats you already have open.** Pin WhatsApp Web, Instagram, Telegram Web and so on in your browser and stay signed in. Every 45 seconds Jarvis reads the visible text of those tabs, spots what is new, has the AI work out who wrote and what, and tells you ("New messages: 2 from Rahul on WhatsApp, 1 on Instagram"). No notifications, no Full Disk Access, no logging in on your behalf, and nothing extra is requested from the services, because it is the page you already have open.

- **Set up once:** in your browser turn on *Allow JavaScript from Apple Events* (Chrome/Brave/Edge: View > Developer. Safari: Settings > Advanced > Show features for web developers, then Develop > Developer Settings). The menu item **Set up message reading...** walks you through it, and macOS asks once whether Jarvis may control the browser. `./jarvisctl.sh doctor` shows which chat tabs it can read.
- **Ask:** "any messages?", "catch me up" (instant), or "summarise my messages", "what did Rahul say?" (the AI reads your inbox).
- **Not while you're looking:** if the browser is the frontmost app, nothing is announced. Quiet hours hold messages for "catch me up". `notifications.read_aloud` chooses how much he says (counts, who, or who and what).
- **Read-only:** Jarvis never types, clicks, replies or sends. Message text is treated as data, never instructions.
- **Limits and privacy:** it sees what is on the page, so keep chat tabs open (a pinned tab in a spare window is fine). The *new* lines from your chats are sent to Gemini to work out who wrote what. The macOS-notification reader still exists as an option (`notifications.enabled: true`, needs Full Disk Access), as do Dock-badge counts (`social:`).

## Automatic imports

Request your ChatGPT export and a Google Takeout (Gemini Apps activity; Google can schedule it to repeat every two months). When the zip lands in **Downloads** (or `~/Jarvis Workspace/exports`), Jarvis recognises it within five minutes, imports it, and says so. No command needed. See "Your ChatGPT and Gemini history" above for what each service exports.

## The menu bar icon

🎙 listening · 🔴 hearing you · 🧠 thinking (number = agents working) · 🔊 speaking · ⏸ paused (mic fully off) · 🔔 an agent needs approval · ⚠️ problem.
Menu: status and which model is answering, **Check messages now**, **Message updates** on/off, **Pause listening**, **Type to Jarvis...**, **Stop speaking**, **Agents** (live task list, Stop buttons, one-click Approve/Deny), **Agent permissions**, **Screen halo**, workspace folder, log, Quit.

## What you can say

- Say "stop", "never mind" or just talk over him; ask "what are they doing?" while a job runs.
- Instant: "what time is it", "open YouTube", "open Gmail in Chrome", "volume down", "set the volume to 30", "pause the music", "timer for ten minutes", "remind me in 20 minutes to call mom", "what's the weather", "how's my battery", "what's everyone doing", "cancel task 2".
- "Clean my GitHub", "clean my Mac", "undo the cleanup"; or just say anything that's meant for him, no name needed.
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
./jarvisctl.sh import-history <export> | distill-history
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
        history.py (ChatGPT/Gemini import + search) · webinbox.py (chat tabs) · notifications.py (shared inbox) · social.py · permissions.py
        llm.py local_llm.py openai_llm.py · memory.py · agents/ (roles, manager, approvals) · tools/ (policy + all tools)
```
