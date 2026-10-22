# ARIA

A personal desktop assistant with a face, a voice, and a memory — she lives on
your Windows PC, talks in real time, drives the computer by voice, and
remembers what you tell her across sessions.

ARIA is built on the **MARK LVI** codebase by **FatihMakes**. The engine,
avatar, HUD and phone dashboard are his; the persona, connections, routines,
scheduler, memory hardening and the rest of what makes her *ARIA* were built
on top in this fork by Alek Berger.

> **Licence: CC BY-NC 4.0** — © FatihMakes. Attribution required,
> non-commercial use only. Full text in [`LICENSE`](LICENSE).
> Upstream project: [github.com/FatihMakes/Mark-LVI](https://github.com/FatihMakes/Mark-LVI) ·
> [@FatihMakes on YouTube](https://www.youtube.com/@FatihMakes)
>
> Internal provenance: the window title still reports `MARK LVI`
> (`APP_VERSION` in `ui.py`). That string is lineage, not her name.

---

## Architecture

**Engines.** The default conversation engine is **Gemini Live** — Google's
servers handle speech-to-text, turn-taking and text-to-speech in one session.
The Live model ladder has two rungs (`gemini-3.1-flash-live-preview`, then
`gemini-2.5-flash-native-audio-preview-12-2025`) and steps down only on quota
or loss of access. One-shot Gemini calls elsewhere in the app go through
`core/gemini.py`, which owns a wider model ladder with timeouts and
per-failure cooldowns.

An optional **providers pipeline** (`providers/`) can hold the conversation
instead: faster-whisper for hearing, any OpenAI-compatible endpoint for
thinking, and Piper, your system voices, or ElevenLabs for speaking. It is
never imported unless configured. Named presets cover Ollama, LM Studio,
llama.cpp, vLLM and Jan locally, and OpenAI, Groq, DeepSeek, Mistral, xAI,
OpenRouter, Together, Cerebras and **Ollama Cloud** (`https://ollama.com/v1`)
hosted — plus a custom endpoint. Pick in ⚙ → **ENGINE & PROVIDERS**.

**Persona.** Her identity — name, whose she is, the standing rules she works
under — lives in the `[SELF]` section of `core/prompt.txt`. There is no
separate soul file; that section *is* the soul, and it is on her self-edit
watchlist like any other code.

**Memory.** A JSON store at `memory/long_term.json`, hardened in this fork:

- writes are atomic (temp file + replace) with the last five known-good
  states kept in `memory/backups/`; a file that fails to parse is restored
  from the newest good backup, the wreck is preserved, and an incident is
  recorded — corruption no longer means silent amnesia;
- values are capped at 380 characters and the store at 200,000 — a value that
  gets cut is now *marked* truncated instead of silently shortened;
- recall is hybrid: keyword search plus semantic matches from Gemini
  embeddings (`gemini-embedding-001`, vectors in `memory/embeddings.json`),
  labelled `[similar match]`. No key or no network → keyword-only, and recall
  still answers.

The same atomic-write + backup + recovery treatment covers
`config/api_keys.json` (backups in `config/backups/`).

**Failure records.** Anything a tool reports as `failed` or `unverified`,
tool exceptions, memory/config recoveries and broken self-edits are appended
to `memory/incidents.jsonl` — a ledger she (and you) can actually audit.

**Watchdog.** The old ARIA's signature death was a wedged main thread that
nothing inside the process could see. Here, the session loop and the UI
clock heartbeat into `core/watchdog.py`; a supervisor thread that neither
of them can block watches the stamps, and 30 seconds of silence from both
means wedged: it records a `watchdog_restart` incident (also kept in
`memory/watchdog.json`), then restarts her through the same relaunch path
self-edit uses. Three restarts in an hour is the cap — past that she stays
down and the incident is the evidence, rather than loop-restarting forever.
It detects and restarts; it does not move anything out of process.

**Self-heal.** Diagnostics (`run_diagnostics`, or the 🩺 button) only ever
*reports*. The separate `self_heal` tool acts, on exactly the classes that
have a designed remedy: a corrupt config or memory file is restored through
the loaders' own witnessed recovery and verified by re-reading it; rejected
action/plugin files are named and left exactly where they lie. Anything
else is reported, not touched, and every heal lands in the incident ledger.

**Backup mirror.** The rolling backups above live beside the files they
protect. Set `backup_mirror_dir` (say "set the backup mirror to …", or use
`set_backup_mirror`) to a folder your cloud drive syncs — OneDrive,
Google Drive — and every memory and config snapshot is copied there too,
verified by size. A mirror that fails costs one incident, never the backup.

---

## Capabilities

**84 tools** on a desktop install — 9 built in, 74 actions, plus
`youtube_video`, which only loads when a display is present. (A headless
registry walk counts 83.) Tools from connected MCP servers join on top of
that, namespaced per server. The in-app **⌘ COMMANDS** overlay is the source
of truth: it is rebuilt from the live registries every time it opens, so it
cannot list a tool she doesn't have. The shape of the suite:

| Area | What's there |
|---|---|
| Computer & files | `open_app`, `computer_control`, `computer_settings`, `desktop_control`, `file_controller`, `file_processor`, `file_duplicates`, `file_find_advanced`, `file_organize`, `code_helper`, `dev_agent` |
| Web & media | `web_search`, `browser_control`, `video_player`, `weather_report`, `flight_finder`, `game_updater` |
| Spotify & DJ | `spotify` (now playing from the real window title, transport, per-app volume), `dj` (saved playlist → mood → labelled search, in that order — never an invented playlist), `save_playlist` (any Spotify link form, normalised and verified) |
| Routines | `routine_record_start/stop`, `run_routine` (with `{{param}}` substitution), `list_routines`, `describe_routine`, `delete_routine`, `trust_routine`, `untrust_routine`, `export_routine_pack`, `import_routine_pack` |
| Scheduling | in-app scheduler: `set_timer`, `set_recurring_task`, `list_scheduled_tasks`, `cancel_scheduled_task`, `break_reminders` — plus `reminder`, which writes OS-level reminders |
| Connections | `gmail_setup`, `read_email`, `triage_email`, `send_email`; `calendar_setup`, `check_calendar` (read-only iCal feed; recurring events expand — RRULE, EXDATE and RDATE honoured); `github_setup`, `github_create_repo`, `github_push_file`; `provider_setup` |
| Delegation | `delegate_task` (a prompt worked in the background by her own engine, up to 4 in flight), `task_status`, `task_cancel` — records in `memory/delegated_tasks.json`, completion announced through the scheduler |
| MCP | `mcp_setup`, `mcp_connect`, `mcp_disconnect`, `mcp_list_servers`, `mcp_remove_server` — external Model Context Protocol servers (stdio or HTTP); their tools join the dispatch as `mcp_<server>_<tool>` |
| Watches | price watches (`watch_price`, `check_price_watches`, `list_price_watches`, `unwatch_price` — checks ride the scheduler), screen watches (`watch_screen`, `unwatch_screen`, `list_screen_watches`), news topic monitors (`manage_monitor`) |
| Notes & cards | `take_note`, `read_notes`, `journal_write`; `mtg_card`, `mtg_advice` (Scryfall, keyless) |
| Herself | `save_memory`, `recall_memory`, `forget_memory`, `edit_own_code`, `patch_own_code`, `self_heal`, `set_backup_mirror`, `morning_briefing`, `send_message`, `system_status`, `screen_process`, `close_camera`, `undo`, `shutdown_jarvis` |

**Startup briefing.** On launch she greets you with a briefing composed in
code — calendar, triaged inbox, weather, headlines from your monitored
topics — and presents exactly that. She asserts no plan that a source didn't
write down. `morning_briefing` runs the same composition on demand. A source
that isn't connected says so in its section; a slow source is skipped with a
note rather than hanging the greeting.

Plugins: drop a `.py` into `plugins/` (see `_template.py`) and it is
discovered next launch. No plugins ship enabled in this fork.

---

## The UI

A PyQt6 HUD. The face is real measured geometry — MediaPipe's canonical face
model (`core/face_model.obj`, 468 vertices, Apache-2.0) — rendered in
software, lip-synced from the audio's formants and the transcript. No GPU
needed.

- **Left rail (SYS MONITOR):** system readouts, and under them the
  **Spotify panel** — track and artist while something plays, quiet
  otherwise.
- **Content panel:** tabs — **News | Inbox | Calendar**. News is where
  briefings, search results and document reviews land. Inbox shows your
  latest 15 Gmail messages as per-message blocks grouped by day, with triage
  labels; Calendar shows the next 7 days. Both fetch off the UI thread, with
  a Refresh button and honest not-connected/error states. The panel header
  follows the tab: the news title on News, `Gmail Inbox - X Unread Emails`
  (a real count, once counted) on Inbox, `Calendar` on Calendar.
- **Conversation log:** the session transcript, capped at 600 blocks.
- **Overlays:** **⌘ COMMANDS** (every tool, from the registries),
  **🧠 MEMORY** (every stored fact, when she learned it, forgettable),
  **⚙ CUSTOMISE ASSISTANT** (name, voice, colour, engine panels).
- **Talking to her:** voice, typing, or push-to-talk (Ctrl+Space). The wake
  word is opt-in and still answers to **"Hey Jarvis"** — see Limitations.

---

## Phone dashboard

A FastAPI server on your local network. The dashboard itself serves
**HTTPS on port 8001 by default**, with a self-signed certificate generated
on first run into `config/certs/`; port **8000** answers plain HTTP only to
redirect hand-typed addresses to it. If a certificate cannot be generated,
it falls back to plain HTTP and says so loudly. Opt out of HTTPS with
`"dashboard_https": false` in `config/api_keys.json`.

Pairing is an **8-character key** typed on the login page, or the QR code
(one-time auto-login). The pages carry her configured name — ARIA by
default.

---

## Setup

**Windows, Python 3.12** (what CI tests; `setup.py` accepts 3.11–3.13).

```bat
py -3.12 setup.py                       :: dependencies for your OS + Playwright browsers
py -3.12 main.py                        :: run her
```

`pip install -r requirements.txt` also works if you'd rather skip the
installer. faster-whisper, Piper and the wake-word engine are optional
one-click installs from inside the app — they are deliberately not in
`requirements.txt`.

**First run** asks for a Gemini API key and stores it in
`config/api_keys.json`. That key is the only credential Gemini needs.

**Then the connection pass** — by voice, or by writing the same keys into
`config/api_keys.json` directly:

| Say | She stores | Notes |
|---|---|---|
| "Set up Gmail" | `gmail_user`, `gmail_app_password` | a Google **app password**; IMAP read/triage, SMTP send |
| "Set up my calendar" | `ical_url` | your secret iCal URL; read-only by design |
| "Set up GitHub" | `github_token`, `github_username` | storing the token does **not** enable writes — say "arm GitHub" (`github_armed`) as a separate, deliberate step |
| "Wire my Groq key" (also OpenRouter / Mistral / Ollama Cloud) | provider creds | for the providers pipeline; Gemini needs nothing here |
| "Set up an MCP server" then "connect the MCP server" | `mcp_servers` | both steps are confirmation-gated — a server is a program she runs; its tools appear in ⌘ COMMANDS once connected, namespaced `mcp_<server>_<tool>` |
| "Set the backup mirror to <folder>" | `backup_mirror_dir` | point it at a OneDrive/Google Drive folder for off-machine backup copies |

Two requirements deserve a footnote: the **MCP client** (`mcp` in
`requirements.txt`) is imported lazily — remove the package and every MCP
tool answers "MCP support is not installed" while the rest of her works
untouched. **Calendar recurrence** uses `python-dateutil`, also in
requirements; without it, recurring events show their first occurrence
only.

**Coming from the old ARIA?** Her memory imports:

```bat
py -3.12 tools\import_aria_memory.py <old ARIA workspace dir, aria_memory.db, or JSON export>
```

Idempotent; her DJ playlists land in the store's `playlist` category, where
`dj` finds them.

---

## Safety model

- **Confirmation gate.** Irreversible actions wait for a token only the UI
  can issue (`core/confirm.py`, 90-second expiry; the model cannot mint one
  and the gate fails closed). Gated today: PC restart, shutdown and Wi-Fi
  toggle; sending a message; sending email; deleting a file; a real
  file-organize run; deleting a routine; game installs (and update-with-
  shutdown); dev-agent builds; creating/changing an MCP server's spec,
  launching one, and removing one. A scheduled game update with nobody at
  the screen is the one deliberate exception — gating it would kill it
  silently.
- **Delegation inherits the gates.** A delegated task runs the same tools
  through the same dispatch; if one of its calls hits a confirmation gate,
  the task parks as *blocked* with the action named — it never bypasses,
  and delegation itself never counts as routine trust.
- **The watchdog is capped.** At most 3 watchdog restarts per hour; past
  that it leaves her down with the incident recorded, rather than
  restart-looping a machine nobody is watching.
- **GitHub is disarmed by default.** The token alone changes nothing; arming
  is separate.
- **Routines earn trust.** An untrusted routine's gated steps park for your
  confirmation and the replay stops there. `trust_routine` is how a routine
  earns the right to run them inline. Imported packs always land untrusted
  and never overwrite.
- **Full file access — deliberately.** There is no home-folder sandbox; she
  can work anywhere on the machine. The counterweights are the confirmation
  gate, the `undo` tool, and one hard refusal kept on purpose: she will not
  delete entire named user folders (Desktop, Documents, …) outright.
- **Self-editing with a net.** She watches her own source tree by hash. A
  change triggers compile → smoke-import → spoken announcement → restart.
  A broken edit is kept as `<file>.broken-<timestamp>`, the file is restored
  from git, and she restarts into the restored tree — or doesn't restart at
  all if the restore fails.
- **Outcome discipline.** Tools answer `done` only after a verifying
  observation; `unverified`, `failed` and `ask` are first-class answers,
  and failures land in the incident ledger.

---

## Known limitations

- **The wake word is still "Hey Jarvis."** No "Hey ARIA" model has been
  trained yet; until one is, wake-word use is opt-in under the old name.
- **Recurring calendar events expand**, with edges: FREQ (daily, weekly,
  monthly, yearly), INTERVAL, COUNT and UNTIL are honoured, EXDATE removes
  and RDATE adds occurrences, and generation is capped per event per
  query. A malformed rule degrades to the first occurrence for that event
  rather than failing the calendar.
- **Windows audio COM runs in this process** — pycaw volume and the SAPI
  fallback voice are in-process, the design that crashed the old ARIA four
  times. The watchdog (above) now catches that wedge and restarts her,
  incident recorded; a process-isolation cure is specced and deliberately
  shelved — if restarts become routine, that's the known fix.
- **Delegated tasks are weaker on provider rungs.** Delegation runs on
  the one-shot Gemini chain; if that chain falls through to an
  OpenAI-compatible provider rung, the rung receives no tool declarations,
  so the task answers from its prompt alone and says nothing about it.
- **MCP servers are trusted code.** Connecting one runs its command on
  this machine with her permissions — the setup/connect gates are the
  consent point. Server processes are children: disconnecting, or a
  normal quit, shuts them down cleanly; the app's hard-exit restart paths
  (self-edit, watchdog) can orphan one until it notices its parent gone.
- **Keys are plaintext at rest.** `config/api_keys.json` holds every
  credential unencrypted (DPAPI encryption is designed, not built). It is
  git-ignored — the pattern is verified to match — but anyone with your
  Windows account can read the file.
- **Backups live beside the store by default.** Memory, its backups,
  embeddings and the incident ledger are all under `memory/`. Setting a
  backup mirror (above) is what creates the off-machine copy — until
  then, one dead disk still takes the originals and the backups together.
- **The dashboard is LAN-only** with a self-signed certificate — expect a
  one-time browser warning per device.

---

## Development

Tests live in `tests/` and run the way CI runs them
(`.github/workflows/tests.yml`, Python 3.12): a ruff gate
(`--select F401`), the identity/incident suites in one process, then every
other suite file in its **own** process so import stubs can't leak between
files.

```bat
py -3.12 -m pytest tests\test_commands.py -q
```

Layout, briefly: `main.py` (session, dispatch, startup briefing),
`ui.py` (the HUD), `core/` (prompt, gemini ladders, confirm, outcome,
commands, scheduler, routines, self-edit, watchdog, self-heal, backup
mirror, delegation, MCP client, gmail/ical/github/spotify cores),
`actions/` (one file per tool, self-describing), `providers/` (the opt-in
pipeline), `memory/` (store, config, embeddings), `dashboard/` (phone UI),
`tools/` (the ARIA memory importer).

---

## Your data

Everything is on your machine — no ARIA server, no telemetry, no account.
Your voice is streamed to Gemini Live while a session is open (or to
whichever provider you configured); that is the only thing that leaves the
PC. Secrets and personal data — `config/api_keys.json`, `config/certs/`,
`memory/long_term.json` and their backups — are all in `.gitignore`,
with the patterns verified to match.

## Credits

MARK LVI engine, avatar, HUD and dashboard by **FatihMakes**, CC BY-NC 4.0.
Face geometry: MediaPipe canonical face model, Apache-2.0. This fork's ARIA
persona, connections, routines, scheduler, memory hardening and UI additions
by **Alek Berger**, under the same licence.
