# Agent TARS integration

J.A.R.V.I.S. uses **Agent TARS v0.3.0** (`@agent-tars/cli`) as its PC / browser
automation executor. Gemini stays the conversational and reasoning brain.

```
USER
  -> J.A.R.V.I.S. (PyQt6)
  -> Gemini  (tools: computer_control / browser_control / web_research)
  -> capabilities/router.py
        computer_control ─┐
        browser_control  ─┴─> Agent TARS v0.3.0  (Node runtime, Gemini model)
        web_research     ───> Agent Reach        (own Python environment)
  -> Windows / Browser
  -> router -> Gemini -> user
```

## Where things live

| Path | Role |
| --- | --- |
| `package.json`, `package-lock.json`, `node_modules/@agent-tars/cli` | the pinned runtime |
| `capabilities/agent_tars.py` | adapter: runtime discovery, status, `run_task` |
| `capabilities/tars_server.py` | headless server lifecycle + SSE event translation |
| `capabilities/proc.py` | owned-process runner (`spawn_argv`, `start_background`, `terminate_pid`) |
| `capabilities/router.py` | capability routing, ownership locks, approvals, honest reporting |
| `main.py` | the three Gemini tools and real-progress forwarding to the activity monitor |

## The verified interface (observed against the installed 0.3.0 package)

Nothing below is copied from older versions; each line was checked by running the
installed package.

* **Version / health** — `node node_modules/@agent-tars/cli/bin/cli.js --version`
  prints `agent-tars/0.3.0 win32-x64 node-v26.7.0`.
* **Headless server** — `agent-tars serve --headless --port <p>` with
  `--model.provider gemini --model.id <model> --toolCallEngine prompt_engineering`.
  * `GET /api/v1/health` -> `{"status":"ok"}`
  * `GET /api/v1/version` -> `{"version":"0.3.0", ...}`
  * `POST /api/v1/oneshot/query` `{"query": "..."}` ->
    `{"sessionId": "...", "result": {"type":"assistant_message","content":"...","finishReason":"stop"}}`
  * `POST /api/v1/oneshot/query/stream` -> Server-Sent Events, one JSON per
    `data:` line: `session_created`, `user_message`, `agent_run_start`,
    `assistant_streaming_tool_call`, `assistant_message`, `tool_call`,
    `tool_result`, `assistant_streaming_message`, `system`, `agent_run_end`.
  * `POST /api/v1/sessions/<id>/abort` cancels a run.
* **One-shot CLI** — `agent-tars --headless --input "<task>" --format json`
  prints a single (pretty-printed) JSON object with the same
  `{"sessionId", "result"}` envelope.
* **Model** — a native `gemini` provider exists (`GEMINI_API_KEY`,
  `@google/generative-ai`). The key is passed by environment, never on the
  command line. No second provider or credential is created.
* **Browser control** — `--browser.control dom|visual-grounding|hybrid`
  (default `hybrid`) and `--browser.cdpEndpoint <url>` to attach to a Chrome that
  is already running (signed-in profile).
* **Planner / search** — `--planner.enable`, `--search.provider
  browser_search|tavily|bing_search`.

### Discrepancies found and how they are handled

1. **Native Gemini function-calling is rejected.** With the default
   `toolCallEngine=native`, the generativelanguage API returns HTTP 400
   (`Unknown name "$schema"/"additionalProperties" ... Cannot find field`).
   Fixed by pinning `--toolCallEngine prompt_engineering`
   (`AGENT_TARS_TOOL_CALL_ENGINE`).
2. **No local pixel-level desktop GUI control.** v0.3.0's built-in local
   automation is **browser + shell commands + filesystem + search**; pixel-level
   desktop control only exists as the remote `aioSandbox` environment, which is
   not configured. Desktop requests therefore run through TARS's `run_command` /
   filesystem tools (launch and run applications). In-app GUI clicking is
   reported as unsupported rather than pretended.
3. **`run_command` blocks on GUI launches.** Running `notepad.exe` directly waits
   for the process to exit and hits TARS's 60 s tool timeout. `computer_control`
   prepends a short steer ("launch GUI applications in a non-blocking way, e.g.
   PowerShell `Start-Process`") so the first attempt succeeds.
4. **No step-limit CLI flag.** The `max_steps` tool parameter is accepted for
   schema compatibility but not forwarded; v0.3.0 governs its own loop budget.
5. **Provider capacity.** `gemini-flash-latest` returned intermittent HTTP 503 in
   testing. The adapter retries once with `AGENT_TARS_FALLBACK_MODEL`
   (`gemini-flash-lite-latest`) and remembers the model that last worked, so later
   tasks do not repeat the failed call or restart the server.

## Capability boundary

| Request | Engine | Works |
| --- | --- | --- |
| "Open my Facebook and message my friend" | browser_control | yes (browser) |
| "Check my Gmail" | browser_control | yes (browser; sign-ins are the user's) |
| "Open Roblox" | computer_control | launches the app |
| "Click a button inside a third-party desktop app" | — | **not supported by v0.3.0** (needs an external AIO sandbox) |

## Configuration

Environment variables (documented in `.env.example`, read through
`config/settings.py`; real values live in `config/api_keys.json` or the
environment): `AGENT_TARS_MODE`, `AGENT_TARS_PROVIDER`, `AGENT_TARS_MODEL`,
`AGENT_TARS_FALLBACK_MODEL`, `AGENT_TARS_TOOL_CALL_ENGINE`,
`AGENT_TARS_BROWSER_CONTROL`, `AGENT_TARS_CDP_ENDPOINT`, `AGENT_TARS_WORKSPACE`,
`AGENT_TARS_PLANNER`, `AGENT_TARS_SEARCH_PROVIDER`, `AGENT_TARS_PORT`,
`AGENT_TARS_TIMEOUT`, `AGENT_TARS_HEALTH_TIMEOUT`, `AGENT_TARS_RETRIES`, plus the
advanced `AGENT_TARS_CLI` / `AGENT_TARS_NODE` path overrides. Agent TARS reuses
`GEMINI_API_KEY`; no duplicate Gemini credential is created.

### UI TARS and the model credential (Gemini only)

`AGENT_TARS_PROVIDER` defaults to `gemini`, and Gemini is the **only** model
source this project supports for UI-TARS work. There is no UI-TARS API key: no
`ui_tars_api_key` in `config/api_keys.json`, no `UI_TARS_API_KEY` /
`ARK_API_KEY` environment variable, and no `--model.apiKey` forwarding. Both
UI-TARS surfaces run on the existing `GEMINI_API_KEY` (or the Gemini key in
Settings):

* **Agent TARS** (this document) — its own agent loop, on Gemini.
* **The visual control layer** (`capabilities/uitars.py`, see
  [pc_web_control.md](pc_web_control.md)) — the UI-TARS prompt + parser on the
  model router's `vision` role, i.e. the same Gemini configuration.

If a config file still points `agent_tars_provider` at a non-Gemini provider, the
adapter reports it as unsupported (`provider_unsupported`) rather than reaching
for another credential, and nothing is substituted for it.

### Chat-panel brain mode (Gemini ⇄ UI TARS)

The chat panel carries an icon-only toggle (a four-point sparkle for Gemini
conversation, a targeting reticle for UI TARS control). It flips
`features.ui_tars_mode`, which:

* is shown on the ReferenceDashboard **ACTIVITY** panel as `MODE: GEMINI` or
  `MODE: UI TARS`;
* is added to the live session's system instruction (act on the PC through
  `computer_control` / `browser_control` in UI TARS mode; converse normally in
  Gemini mode). The tools themselves are always registered — the mode only
  changes how J.A.R.V.I.S. is told to use them;
* takes effect through the existing live reconnect (session context preserved),
  the same path a voice or audio-device change already uses.

`JARVIS_FEATURE_UI_TARS_MODE=true|false` overrides the saved flag.

### Realtime narration while automating

Because automation runs as a child process, J.A.R.V.I.S. would otherwise go
silent for the whole task. The narrator (`voice/narrator.py`) turns each **real**
Agent TARS progress event into one short spoken line — "Sir, I'm clicking the
Stop button." — and the same hook still drives the live ACTIVITY row, so the
screen and the voice always say the same thing.

It is deliberately not a script: the sentence is derived from the tool that is
genuinely running and the progress text the engine genuinely emitted. An event it
cannot parse is either skipped or reduced to a single throttled generic line, and
completion is only ever announced from a real `SUCCESS` status. It is also
gated — one line per ~2.2 s, no repeats, a per-task cap, nothing while a reply is
already playing or while muted — so a busy task cannot turn into a monologue.
`JARVIS_FEATURE_NARRATOR=false` (Settings ► Automation) silences it without
touching the work itself.

Narration is spoken through the local voice path rather than the Live session:
injecting client content while a tool call is outstanding would collide with the
sequential tool-call flow the preview model uses.

## Running it

```bash
npm install          # once: pins @agent-tars/cli@0.3.0
python main.py       # J.A.R.V.I.S.; no separate TARS UI is launched
```

The server starts on demand and is stopped on shutdown (or by `atexit` if the
process exits without a graceful shutdown).

## Verification performed

* `python -m pytest tests/unit` — 327 passed (61 in `test_capabilities.py`).
* `python -m pytest tests/hybrid_voice` — 135 passed; Settings tabs render.
* `python -m pytest tests/ui/test_mode_status_activity.py tests/ui/test_laya_status_activity.py`
  — 13 passed (mode readout, icon toggle, persistence, ACTIVITY line).
* `python tests/live_capability_acceptance.py` — all checks passed: Agent Reach
  research, a real Agent TARS browser task, a real desktop app launch, lock
  ownership, risk tiers, and clean shutdown.
* Live browser task: "Open the page and report its main heading" on
  `<https://example.com>` -> `Example Domain`, with real `AUTOMATE` events
  ("Opening …", "Reading the page", "Task completed").
* Live desktop task: "Open the Notepad application." -> launched via PowerShell
  `Start-Process` in 10–21 s (server and one-shot modes both verified).
* No owned `node.exe` process or listening port remained after shutdown.

## Known limitations

* No pixel-level desktop GUI control on a local machine (see discrepancy 2).
* The headless server is single-run exclusive (`--server.exclusive`), so one
  automation task runs at a time — matching the existing ownership locks.
* Gemini provider capacity (503/429) can still fail a task; it is reported
  honestly and never presented as a success.
