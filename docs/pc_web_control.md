# PC and web control

J.A.R.V.I.S. can act on this Windows PC and on websites through five controllers
behind **one router**. Gemini stays the brain: it asks for a structured action,
the router enforces the policy, and a named controller performs it — the model
never runs code, and every result states which controller actually ran.

## The layers

| Layer | Controller | Serves | Package |
| --- | --- | --- | --- |
| Structured web | **Playwright** | `browser_action` — elements found by role, label, placeholder, test id, text or CSS | `playwright` |
| Windows apps | **pywinauto** | `pc_control` — open/close/focus applications, native windows and controls | `pywinauto` |
| Visual computer use | **UI-TARS + Gemini** | `visual_control` — act on what is only recognisable by sight | vendored `UI-TARS/codes/ui_tars` |
| Physical input | **PyAutoGUI** | `pc_control` mouse/keyboard/screen, and the executor for visual clicks | `pyautogui` |
| Media playback | **the installed player** | `play_music` — Spotify/VLC on the user's own search URI, driven through the Windows media session | `capabilities/media.py` |
| Machine power | **Windows itself** | `power_control` — lock, sleep, hibernate, restart, shut down, sign out, Recycle Bin, Explorer, volume, brightness | built in (`shutdown.exe`, media keys, PowerShell) |
| Whole-task engines (unchanged) | **Agent TARS / Agent Reach** | `computer_control`, `browser_control`, `web_research` | `@agent-tars/cli`, AgentReach |

Selection is deterministic, not a guess:

```
browser element that the page can describe   -> Playwright        (browser_action)
Windows window or native control             -> pywinauto         (pc_control)
target only recognisable by sight            -> UI-TARS + Gemini  (visual_control)
raw pointer/keyboard input                   -> PyAutoGUI         (pc_control)
whole task you do not want to micro-manage   -> Agent TARS        (computer_control / browser_control)
music the user owns                        -> installed player  (play_music)
the machine itself (power, volume)         -> Windows           (power_control)
```

`control_plan` answers the same question on request, from the real machine — see
"One decision layer" below.

A visual click is never used when a selector or a native control is available:
Playwright and pywinauto are tried first by construction, and `visual_*` is a
separate tool the model has to choose deliberately.

## How the model asks

Three grouped tools carry an `action` enum; the enum and the parameter help are
generated from `capabilities/registry.py`, so the vocabulary the model sees can
never drift from what the router validates.

* `pc_control` — `open_application`, `close_application`, `focus_window`,
  `mouse_move`, `mouse_click`, `mouse_double_click`, `mouse_right_click`,
  `mouse_drag`, `mouse_scroll`, `keyboard_type`, `keyboard_press`,
  `keyboard_hotkey`, `screen_screenshot`
* `visual_control` — `visual_find`, `visual_click`, `visual_type`,
  `visual_scroll`, `visual_keypress`
* `browser_action` — `browser_open`, `browser_back`, `browser_forward`,
  `browser_new_tab`, `browser_switch_tab`, `browser_close_tab`, `browser_click`,
  `browser_fill`, `browser_press`, `browser_select`, `browser_scroll`,
  `browser_read`, `browser_wait`, `browser_upload`, `browser_download`,
  `browser_screenshot`
* `power_control` — `lock`, `sleep`, `hibernate`, `shutdown`, `restart`,
  `sign_out`, `cancel_shutdown`, `empty_recycle_bin`, `restart_explorer`,
  `volume_up`, `volume_down`, `mute`, `set_volume`, `set_brightness`

### The actions that end the session always ask

A power action that can end or lose the session is declared with
`gate="confirm"` in the registry (`shutdown`, `restart`, `sign_out`, `sleep`,
`hibernate`, `empty_recycle_bin`). `capabilities/router.py` then parks it behind
`security/confirm.py` — the on-screen CONFIRM banner — *before any controller
runs*, whatever the approval mode is. The token is issued by the interface after
a human press: the model can request it but cannot write it, so no tool
parameter can bypass the gate. Instantly reversible actions (`lock`,
`cancel_shutdown`, volume, brightness) are not gated, because a question there
would only make J.A.R.V.I.S. slower than the Start menu.

Every call returns one text block for Gemini ending with the real outcome:

```
[capability=browser_action backend=playwright controller=playwright status=DONE elapsed=0.42s]
evidence: action=browser_click; strategy=role-link; matches=1; page_url=https://example.com/
```

`status=NOT_DONE` plus `reason=` is used for anything that did not happen, and
`main.py` already maps those markers to FAILED / VERIFICATION_FAILED, so the
assistant cannot learn an unchecked action as a success.

## Music plays in the app the user owns

`play_music` looks at the machine before it looks at the internet:

```
installed player (capabilities/media.py: Spotify, VLC)  -> open its own search URI
                                                        -> read the OS playback state
                                                        -> only start playback if it is
                                                           not already playing
internal WebView player (Settings ▸ WEBVIEW)             -> play_web_music
the YouTube action                                       -> last resort
```

The playback check exists because the media key is a *toggle*: sending it to a
player that is already playing pauses the user's music. `media.playback_state()`
reads the Windows media session (through PowerShell, so no extra dependency),
and `start_playback()` asks that session to start — then reports the state it
actually ended in. If the platform cannot answer and the player was already
running, the key is not sent and the result says so instead of guessing.

## One decision layer

`capabilities/decide.py` is the single place that answers "which controller and
which application should serve this request", and `control_plan` exposes it to
the model:

```
python -c "from capabilities import decide; print(decide.report('play daft punk'))"
```

It executes nothing. It probes what is really installed and ready
(`media.status()`, `router.local_status()`, the Agent TARS/Reach adapters, the
Settings switches), classifies the request (media / app / web / screen /
research / chat) and returns an ordered plan where every candidate carries its
reason — including the ones that are NOT ready, so "I cannot do that" is always
specific. `tests/unit/test_control_decision.py` pins the policy offline with
injected facts.

## UI-TARS runs on Gemini — there is no UI-TARS key

`capabilities/uitars.py` uses the vendored UI-TARS SDK (its prompt and its
action parser) with **the project's existing Gemini key and the model router's
`vision` role**. The flow:

```
instruction + one screenshot
  -> COMPUTER_USE_DOUBAO prompt (UI-TARS SDK)
  -> Gemini (existing key, Settings ▸ AI ▸ MODEL ROLES ▸ vision)
  -> parse_action_to_structure_output (UI-TARS SDK, doubao/0-1000 coordinates)
  -> a structured action dict
  -> capabilities/desktop.py executes it with PyAutoGUI
```

Nothing is executed from the model's reply as code: the reply is parsed, the
position is read out as numbers, and the SDK's Python-source generator
(`parsing_response_to_pyautogui_code`) is deliberately **not** imported. A reply
that cannot be parsed is retried once with a strict format instruction and then
reported as not done — a click is never issued on an unverified position.

Coordinates are handled explicitly: the screenshot is captured at native size
with `mss`, the grounded 0–1000 point becomes a fraction of that exact image,
and `desktop.to_pointer()` applies the monitor origin and any DPI scale (the
scale and the pointer position are reported in the evidence line).

## Settings

| Switch | Turns on |
| --- | --- |
| `playwright_browser` | `browser_action` (Playwright) |
| `windows_automation` | `pc_control` application/window actions (pywinauto) |
| `uitars_visual` | `visual_control` (UI-TARS on Gemini) |
| `physical_input` | mouse/keyboard actions and the visual layer's click executor |
| `desktop_control` | master switch for everything acting on this PC |
| `agent_tars` / `web_task` / `webview` | the Agent TARS / Agent Reach engines (unchanged) |

With a switch off the matching actions are refused **by name** ("Mouse and
keyboard control (PyAutoGUI) is switched off in Settings, so I have not done
that."), and no other controller is substituted. Approval policy is unchanged
and applies here too: the consequential-verb scan runs over the action, the
target, the text and the URL, so a click on a "Submit" control or a typed
"delete" is tiered HIGH and parked behind the normal on-screen confirmation.

Other configuration (via `config/api_keys.json`, env override with the upper-case
key name):

| Key | Default | Meaning |
| --- | --- | --- |
| `automation_dir` | `data/automation` | Screenshots, downloads, browser profile |
| `browser_profile_dir` | `<automation_dir>/browser_profile` | Persistent profile — log in once, it survives |
| `browser_channel` | auto | `chrome`/`msedge` when installed, else the bundled Chromium |
| `browser_headless` | `false` | Visible by default, so the user can see what is happening |
| `browser_timeout` | `30` | Per-action page timeout (seconds) |

Playwright needs its browser once: `playwright install chromium`
(`app/installer.py` runs it when the package was missing or no browser is cached).

## Activity and narration

All three tools are `AUTOMATE` rows in the existing ReferenceDashboard, with the
real words of the request — `Opening chrome`, `Clicking Search`, `Filling Email`
— and real progress during the call: `Using Playwright`,
`Using UI-TARS visual control`, `Typing requested text`, `Completed`. Screenshots
land in the existing image panel; `browser_read` output lands in the existing
research panel. No new activity UI was added.

## Verification

```bash
# offline: registry, routing, policy, honest failures (no browser, no mouse)
QT_QPA_PLATFORM=offscreen python -m pytest tests/unit/test_pc_web_control.py -q
QT_QPA_PLATFORM=offscreen python -m pytest tests/unit -q

# what the router and its engines report right now
python -c "from capabilities import registry, router; print(registry.report()); print(router.report())"
```

Live checks performed against this machine (see the numbers in the report):
`browser_open`/`browser_read`/`browser_click`/`browser_wait` on a real page with
`controller=playwright`; `list_windows`/`find_window` on real windows through
pywinauto; a native-resolution capture with `scale=(1.0, 1.0)`; and a full
visual round trip — screenshot → Gemini (`gemini-flash-latest`) → UI-TARS parse
→ a click at the grounded point, reporting `controller=pyautogui`.

## Boundaries

* Agent TARS v0.3.0 has no pixel-level desktop controller of its own; the visual
  layer above is what provides one, on Gemini.
* pywinauto and PyAutoGUI are imported **only** in `capabilities/desktop.py`, so
  there is exactly one place that touches the user's real input devices.
* PyAutoGUI's failsafe corner stays enabled: yanking the pointer into a corner
  aborts pending input and is reported as a failed action.
* One screenshot per visual action, on demand — no polling, no continuous
  capture.
* The browser layer holds the same `browser` lock Agent TARS uses, and the PC /
  visual layers hold `desktop`, so two controllers can never drive the same
  target at once.
