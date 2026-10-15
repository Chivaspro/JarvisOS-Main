"""The structured action registry for PC / visual / browser control.

One table holds every action Gemini may request, and nothing else in the project
hardcodes an action name. Each row declares:

* which **tool** carries it (``pc_control``, ``visual_control``, ``browser_action``)
* which **controller** runs it (reported back to the model in every result)
* the **permission** switch that gates it (an existing Settings feature flag)
* which parameters are required, and which of the shared parameter superset the
  model may supply
* the controller chain tried in order (pywinauto → pyautogui, and so on)

Why a registry instead of one declaration per action: the Live session already
carries forty tool declarations, so three grouped tools with an explicit
``action`` enum keep Gemini's schema small while still offering the exact action
vocabulary. Validation happens here, before any engine is touched, so an
unknown action or a missing parameter can never reach a controller.

No action executes code. There is no ``exec``/``eval`` path in this layer, and
the UI-TARS SDK's pyautogui-code generator is deliberately not imported — a
model reply is parsed into a structured action and dispatched through the
handlers below, nothing more.
"""

from __future__ import annotations

from dataclasses import dataclass, field

# ── tool names (declared in main.py) ──────────────────────────────────────────

TOOL_PC = "pc_control"
TOOL_VISUAL = "visual_control"
TOOL_BROWSER = "browser_action"
TOOL_POWER = "power_control"
#: Deep Windows orchestration: processes/settings and window arrangement are
#: separate tools from pc_control because they carry their own permission
#: switches and their own controllers (psutil / Win32), and because a refusal
#: has to name the switch that stopped them.
TOOL_SYSTEM = "system_control"
TOOL_WINDOW = "window_control"
#: The clipboard sentry is a registry tool too, so its actions are validated and
#: permission-gated through exactly the same path as every other local action.
TOOL_CLIPBOARD = "clipboard_sentry"
#: The document architect writes REAL files (Word, Excel, PowerPoint, PDF) from
#: a declarative spec, so it carries its own switch: a user may happily let
#: J.A.R.V.I.S. type in Word without letting him write files anywhere.
TOOL_DOCUMENTS = "documents"
#: Smart-home devices and the user's own Instagram account: each is a separate
#: relationship with a separate switch, and neither is desktop control —
#: revoking the right to drive this PC must not revoke a lamp.
TOOL_SMART_HOME = "smart_home"
TOOL_INSTAGRAM = "instagram"
#: The HoloGlobe only draws: placement is resolved by `capabilities/geospatial`
#: and rendered by a local page in the HUD. Its permission is `desktop_control`
#: because that is exactly what it is — a surface on this machine — so revoking
#: desktop control takes the globe with it.
TOOL_GLOBE = "globe"

TOOLS = (TOOL_PC, TOOL_VISUAL, TOOL_BROWSER, TOOL_POWER, TOOL_SYSTEM,
         TOOL_WINDOW, TOOL_CLIPBOARD, TOOL_DOCUMENTS, TOOL_SMART_HOME,
         TOOL_INSTAGRAM, TOOL_GLOBE)

#: The shared parameter superset. Gemini sees one flat object per tool (a
#: union schema is not expressible in the Live function-declaration dialect),
#: and each action declares the subset it actually reads. Fields never named by
#: any action of a tool are omitted from that tool's declaration.
PARAM_HELP: dict[str, tuple[str, str]] = {
    "app": ("STRING", "Application to act on, e.g. 'notepad', 'chrome', "
                       "'explorer'. A name, never a path fragment."),
    "title": ("STRING", "Window title or native control name to match, e.g. "
                         "'Untitled - Notepad' or 'Save as'."),
    "target": ("STRING", "What to act on: a page element for browser actions "
                          "(visible text, label, role or CSS) and a described "
                          "element for visual actions."),
    "selector": ("STRING", "An explicit CSS/DOM selector when you already know "
                            "it. Prefer 'target' otherwise."),
    "url": ("STRING", "Absolute URL, including the scheme."),
    "text": ("STRING", "Text to type or fill in."),
    "key": ("STRING", "A single key name, e.g. 'enter', 'tab', 'esc'."),
    "keys": ("STRING", "A hotkey combination, e.g. 'ctrl c' or 'alt tab'."),
    "x": ("INTEGER", "Absolute screen X in pixels."),
    "y": ("INTEGER", "Absolute screen Y in pixels."),
    "to_x": ("INTEGER", "Drag destination X in pixels."),
    "to_y": ("INTEGER", "Drag destination Y in pixels."),
    "amount": ("INTEGER", "Scroll amount or repeat count (default 3)."),
    "direction": ("STRING", "One of: up, down, left, right."),
    "path": ("STRING", "Absolute file path (upload source, download folder or "
                        "screenshot destination)."),
    "tab": ("STRING", "Tab to switch to: an index ('1'), a title fragment or a URL."),
    "instruction": ("STRING", "What the user wants, in their own words, for the "
                               "visual layer to ground on the screen."),
    "timeout_s": ("INTEGER", "How long to wait, in seconds."),
    "clear": ("BOOLEAN", "Clear the field before filling it (default true)."),
    "button": ("STRING", "Mouse button for a click: left, right or middle."),
    "percent": ("INTEGER", "A percentage between 0 and 100 (volume, brightness)."),
    "pid": ("INTEGER", "Process id, when you know it."),
    "name": ("STRING", "Process image name to match (e.g. 'chrome', 'excel')."),
    "level": ("STRING", "Priority level: idle, low, below_normal, normal, "
                         "above_normal, high."),
    "theme": ("STRING", "Theme to apply: dark, light or toggle."),
    "position": ("STRING", "Snap position, e.g. top-left, right-half, full."),
    "monitor": ("INTEGER", "Display number, 1-based (or 'next' where supported)."),
    "limit": ("INTEGER", "How many rows to return (default 20)."),
    "language": ("STRING", "Target language for a translation."),
    "content": ("STRING", "The document content as a JSON object, written as a "
                          "string in this field. The shape is described in the "
                          "tool description; it is the whole document, not a "
                          "file path."),
    "save_as": ("STRING", "Where to write the generated file (absolute path, or "
                           "a name inside the Jarvis documents folder). Omit it "
                           "and J.A.R.V.I.S. names the file himself."),
    "paths": ("STRING", "Several file paths at once, separated by commas or new "
                         "lines."),
    "pages": ("STRING", "Page numbers, e.g. '1-3,5'."),
    "host": ("STRING", "Address of a device on this network (IP or hostname)."),
    "user": ("STRING", "An account name, e.g. an Instagram username."),
    "tag": ("STRING", "A hashtag, without the leading '#'."),
    "caption": ("STRING", "Caption text for a post."),
    "media": ("STRING", "A post identifier, shortcode or link."),
    "place": ("STRING", "A place name ('Tokyo', 'Paris, France') or an exact "
                         "'latitude, longitude' pair."),
    "places": ("STRING", "Several place names at once, separated by commas. "
                          "They are plotted in order, and the globe draws a "
                          "route between them."),
    "focus": ("STRING", "A place already on the globe that should face the "
                         "camera. Omit it to leave the view where it is."),
}


@dataclass(frozen=True)
class Action:
    """One requestable action, its controller chain and its policy."""

    name: str
    tool: str
    controller: str                 # what the result reports as the controller
    permission: str                 # Settings feature flag gating this action
    summary: str                    # one line, used in docs/activity rows
    required: tuple[str, ...] = ()
    optional: tuple[str, ...] = ()
    #: Controllers tried in order, most structured first. The first entry is the
    #: preferred one; the run reports which actually did the work.
    chain: tuple[str, ...] = ()
    #: Verb phrase for the activity row ("Opening", "Clicking", "Typing").
    verb: str = ""
    #: "confirm" when the action can end or lose the user's session, and must
    #: therefore always wait for the on-screen confirmation the model cannot
    #: forge (see capabilities/router.py). Empty means the normal approval
    #: policy applies.
    gate: str = ""


_A = Action

#: Every action, in declaration order. Keep the names stable: they are the
#: vocabulary the model is told about and what the router validates against.
ACTIONS: tuple[Action, ...] = (
    # ── PC control: applications and windows (pywinauto, then pyautogui) ──────
    _A("open_application", TOOL_PC, "pywinauto", "desktop_control",
       "Launch an application on this PC.",
       required=("app",), chain=("pywinauto",), verb="Opening"),
    _A("close_application", TOOL_PC, "pywinauto", "desktop_control",
       "Close an application window.",
       required=(), optional=("app", "title"), chain=("pywinauto",),
       verb="Closing"),
    _A("focus_window", TOOL_PC, "pywinauto", "desktop_control",
       "Bring an application window to the front.",
       required=(), optional=("app", "title"), chain=("pywinauto",),
       verb="Focusing"),

    # ── PC control: mouse and keyboard (pyautogui) ────────────────────────────
    _A("mouse_move", TOOL_PC, "pyautogui", "physical_input",
       "Move the pointer to a screen position.",
       required=("x", "y"), chain=("pyautogui",), verb="Moving the pointer"),
    _A("mouse_click", TOOL_PC, "pyautogui", "physical_input",
       "Left-click at a screen position (or wherever the pointer is).",
       optional=("x", "y"), chain=("pyautogui",), verb="Clicking"),
    _A("mouse_double_click", TOOL_PC, "pyautogui", "physical_input",
       "Double-click at a screen position (or wherever the pointer is).",
       optional=("x", "y"), chain=("pyautogui",), verb="Double-clicking"),
    _A("mouse_right_click", TOOL_PC, "pyautogui", "physical_input",
       "Right-click at a screen position (or wherever the pointer is).",
       optional=("x", "y"), chain=("pyautogui",), verb="Right-clicking"),
    _A("mouse_drag", TOOL_PC, "pyautogui", "physical_input",
       "Drag from one screen position to another.",
       required=("to_x", "to_y"), optional=("x", "y"), chain=("pyautogui",),
       verb="Dragging"),
    _A("mouse_scroll", TOOL_PC, "pyautogui", "physical_input",
       "Scroll the mouse wheel at a screen position.",
       required=("direction",), optional=("amount", "x", "y"),
       chain=("pyautogui",), verb="Scrolling"),
    _A("keyboard_type", TOOL_PC, "pyautogui", "physical_input",
       "Type text into the focused field.",
       required=("text",), chain=("pyautogui",), verb="Typing"),
    _A("keyboard_press", TOOL_PC, "pyautogui", "physical_input",
       "Press one key, e.g. enter, tab or esc.",
       required=("key",), optional=("amount",), chain=("pyautogui",),
       verb="Pressing"),
    _A("keyboard_hotkey", TOOL_PC, "pyautogui", "physical_input",
       "Press a key combination, e.g. 'ctrl c'.",
       required=("keys",), chain=("pyautogui",), verb="Pressing"),
    _A("screen_screenshot", TOOL_PC, "pyautogui", "screen_understanding",
       "Capture the screen and show it.",
       optional=("path",), chain=("pyautogui",), verb="Capturing the screen"),

    # ── Visual computer use (UI-TARS prompt + Gemini vision → pyautogui) ──────
    _A("visual_find", TOOL_VISUAL, "uitars", "uitars_visual",
       "Locate an element on the current screen visually, without clicking.",
       required=(), optional=("instruction", "target"), chain=("uitars",),
       verb="Looking for"),
    _A("visual_click", TOOL_VISUAL, "uitars", "uitars_visual",
       "Find an element on screen visually and click it.",
       required=(), optional=("instruction", "target", "x", "y"),
       chain=("uitars", "pyautogui"), verb="Clicking"),
    _A("visual_type", TOOL_VISUAL, "uitars", "uitars_visual",
       "Click a visually-located field, then type text into it.",
       required=("text",), optional=("instruction", "target"),
       chain=("uitars", "pyautogui"), verb="Typing"),
    _A("visual_scroll", TOOL_VISUAL, "uitars", "uitars_visual",
       "Scroll the screen, optionally at a visually-located element.",
       required=("direction",), optional=("instruction", "target", "amount"),
       chain=("uitars", "pyautogui"), verb="Scrolling"),
    _A("visual_keypress", TOOL_VISUAL, "uitars", "uitars_visual",
       "Press a key after confirming the screen state visually.",
       required=("key",), optional=("instruction"), chain=("uitars", "pyautogui"),
       verb="Pressing"),

    # ── Browser automation (Playwright, semantic selectors only) ──────────────
    _A("browser_open", TOOL_BROWSER, "playwright", "playwright_browser",
       "Open a URL in the automation browser.",
       required=("url",), chain=("playwright",), verb="Opening"),
    _A("browser_back", TOOL_BROWSER, "playwright", "playwright_browser",
       "Go back one page.",
       chain=("playwright",), verb="Going back"),
    _A("browser_forward", TOOL_BROWSER, "playwright", "playwright_browser",
       "Go forward one page.",
       chain=("playwright",), verb="Going forward"),
    _A("browser_new_tab", TOOL_BROWSER, "playwright", "playwright_browser",
       "Open a new tab, optionally at a URL.",
       optional=("url",), chain=("playwright",), verb="Opening a tab"),
    _A("browser_switch_tab", TOOL_BROWSER, "playwright", "playwright_browser",
       "Switch to another open tab.",
       required=("tab",), chain=("playwright",), verb="Switching tab"),
    _A("browser_close_tab", TOOL_BROWSER, "playwright", "playwright_browser",
       "Close a tab (the current one by default).",
       optional=("tab",), chain=("playwright",), verb="Closing a tab"),
    _A("browser_click", TOOL_BROWSER, "playwright", "playwright_browser",
       "Click a page element located by role, label, text, test id or CSS.",
       required=("target",), optional=("selector", "button"),
       chain=("playwright",), verb="Clicking"),
    _A("browser_fill", TOOL_BROWSER, "playwright", "playwright_browser",
       "Fill a page field with text.",
       required=("target", "text"), optional=("selector", "clear"),
       chain=("playwright",), verb="Filling"),
    _A("browser_press", TOOL_BROWSER, "playwright", "playwright_browser",
       "Press a key in the page (the focused element by default).",
       required=("key",), optional=("target", "selector"),
       chain=("playwright",), verb="Pressing"),
    _A("browser_select", TOOL_BROWSER, "playwright", "playwright_browser",
       "Choose an option in a page <select>.",
       required=("target", "text"), optional=("selector",),
       chain=("playwright",), verb="Selecting"),
    _A("browser_scroll", TOOL_BROWSER, "playwright", "playwright_browser",
       "Scroll the page, optionally to a located element.",
       required=("direction",), optional=("amount", "target", "selector"),
       chain=("playwright",), verb="Scrolling"),
    _A("browser_read", TOOL_BROWSER, "playwright", "playwright_browser",
       "Read the page text (or one element's text).",
       optional=("target", "selector"), chain=("playwright",), verb="Reading"),
    _A("browser_wait", TOOL_BROWSER, "playwright", "playwright_browser",
       "Wait for an element, text or URL to appear.",
       optional=("target", "selector", "url", "timeout_s"),
       chain=("playwright",), verb="Waiting"),
    _A("browser_upload", TOOL_BROWSER, "playwright", "playwright_browser",
       "Upload a local file through a page's file input.",
       required=("path",), optional=("target", "selector"),
       chain=("playwright",), verb="Uploading"),
    _A("browser_download", TOOL_BROWSER, "playwright", "playwright_browser",
       "Click a download link and save the file.",
       required=("target",), optional=("selector", "path"),
       chain=("playwright",), verb="Downloading"),
    _A("browser_screenshot", TOOL_BROWSER, "playwright", "playwright_browser",
       "Capture the page as an image.",
       optional=("path",), chain=("playwright",), verb="Capturing the page"),

    # ── Power and session control (one switch; the machine-level actions) ────
    # Actions that can end the session or destroy something carry gate="confirm",
    # so they always wait for a HUMAN press on the HUD however the approval mode
    # is configured. Instantly reversible actions (lock, cancel, volume,
    # brightness) do not, because a question there would only make J.A.R.V.I.S.
    # slower than doing it by hand.
    _A("lock", TOOL_POWER, "power", "power_control",
       "Lock this PC.", chain=("power",), verb="Locking"),
    _A("sleep", TOOL_POWER, "power", "power_control",
       "Put this PC to sleep.", gate="confirm", chain=("power",),
       verb="Suspending"),
    _A("hibernate", TOOL_POWER, "power", "power_control",
       "Hibernate this PC, saving the session to disk.", gate="confirm",
       chain=("power",), verb="Hibernating"),
    _A("shutdown", TOOL_POWER, "power", "power_control",
       "Shut this PC down.", gate="confirm", chain=("power",),
       verb="Shutting down"),
    _A("restart", TOOL_POWER, "power", "power_control",
       "Restart this PC.", gate="confirm", chain=("power",), verb="Restarting"),
    _A("sign_out", TOOL_POWER, "power", "power_control",
       "Sign the current user out.", gate="confirm", chain=("power",),
       verb="Signing out"),
    _A("cancel_shutdown", TOOL_POWER, "power", "power_control",
       "Cancel a pending shutdown or restart.", chain=("power",),
       verb="Cancelling the shutdown"),
    _A("empty_recycle_bin", TOOL_POWER, "power", "power_control",
       "Permanently empty the Recycle Bin.", gate="confirm", chain=("power",),
       verb="Emptying the Recycle Bin"),
    _A("restart_explorer", TOOL_POWER, "power", "power_control",
       "Restart Windows Explorer (taskbar and desktop).", chain=("power",),
       verb="Restarting Explorer"),
    _A("volume_up", TOOL_POWER, "power", "power_control",
       "Turn the system volume up.", chain=("power",), verb="Turning the volume up"),
    _A("volume_down", TOOL_POWER, "power", "power_control",
       "Turn the system volume down.", chain=("power",),
       verb="Turning the volume down"),
    _A("mute", TOOL_POWER, "power", "power_control",
       "Mute or unmute system audio (a toggle).", chain=("power",),
       verb="Toggling mute"),
    _A("set_volume", TOOL_POWER, "power", "power_control",
       "Set the system volume to an exact percentage.",
       required=("percent",), chain=("power",), verb="Setting the volume"),
    _A("set_brightness", TOOL_POWER, "power", "power_control",
       "Set the screen brightness to an exact percentage.",
       required=("percent",), chain=("power",), verb="Setting the brightness"),

    # ── process manager (psutil + ntdll; kill always confirms) ──────────────
    _A("list_processes", TOOL_SYSTEM, "psutil", "process_control",
       "List running processes, heaviest first.",
       optional=("name", "limit"), chain=("psutil",), verb="Listing processes"),
    _A("kill_process", TOOL_SYSTEM, "psutil", "process_control",
       "End a running process.", optional=("pid", "name"),
       gate="confirm", chain=("psutil",), verb="Ending"),
    _A("suspend_process", TOOL_SYSTEM, "psutil", "process_control",
       "Freeze a process in memory (reversible with resume_process).",
       optional=("pid", "name"), chain=("psutil", "ntdll"), verb="Freezing"),
    _A("resume_process", TOOL_SYSTEM, "psutil", "process_control",
       "Resume a process frozen with suspend_process.",
       optional=("pid", "name"), chain=("psutil", "ntdll"), verb="Resuming"),
    _A("set_process_priority", TOOL_SYSTEM, "psutil", "process_control",
       "Change a process's scheduling priority.",
       required=("level",), optional=("pid", "name"),
       chain=("psutil", "win32"), verb="Reprioritising"),

    # ── system settings (netsh / registry / power.py) ──────────────────────
    _A("wifi_status", TOOL_SYSTEM, "windows", "system_settings",
       "Report the Wi-Fi state and network name.",
       chain=("netsh",), verb="Checking Wi-Fi"),
    _A("wifi_on", TOOL_SYSTEM, "windows", "system_settings",
       "Turn the Wi-Fi adapter on.", chain=("netsh",), verb="Turning Wi-Fi on"),
    _A("wifi_off", TOOL_SYSTEM, "windows", "system_settings",
       "Turn the Wi-Fi adapter off.", chain=("netsh",), verb="Turning Wi-Fi off"),
    _A("set_volume", TOOL_SYSTEM, "power", "system_settings",
       "Set the system volume to an exact percentage.",
       required=("percent",), chain=("power",), verb="Setting the volume"),
    _A("mute", TOOL_SYSTEM, "power", "system_settings",
       "Mute or unmute system audio (a toggle).",
       chain=("power",), verb="Toggling mute"),
    _A("set_brightness", TOOL_SYSTEM, "power", "system_settings",
       "Set the screen brightness to an exact percentage.",
       required=("percent",), chain=("power",), verb="Setting the brightness"),
    _A("set_theme", TOOL_SYSTEM, "windows", "system_settings",
       "Switch between the dark and light Windows themes.",
       required=("theme",), chain=("registry",), verb="Setting the theme"),
    _A("system_metrics", TOOL_SYSTEM, "psutil", "system_settings",
       "Report live CPU, memory and battery figures.",
       chain=("psutil",), verb="Reading the metrics"),

    # ── window arrangement across monitors (Win32) ─────────────────────────
    _A("list_windows", TOOL_WINDOW, "win32", "window_control",
       "List the visible top-level windows.",
       chain=("pywinauto",), verb="Listing windows"),
    _A("minimize", TOOL_WINDOW, "win32", "window_control",
       "Minimize a window.", optional=("app", "title"),
       chain=("win32",), verb="Minimising"),
    _A("maximize", TOOL_WINDOW, "win32", "window_control",
       "Maximize a window.", optional=("app", "title"),
       chain=("win32",), verb="Maximising"),
    _A("restore", TOOL_WINDOW, "win32", "window_control",
       "Restore a minimized or maximized window.", optional=("app", "title"),
       chain=("win32",), verb="Restoring"),
    _A("tile_left", TOOL_WINDOW, "win32", "window_control",
       "Tile a window into the left half of its display.",
       optional=("app", "title", "monitor"), chain=("win32",), verb="Tiling"),
    _A("tile_right", TOOL_WINDOW, "win32", "window_control",
       "Tile a window into the right half of its display.",
       optional=("app", "title", "monitor"), chain=("win32",), verb="Tiling"),
    _A("snap_grid", TOOL_WINDOW, "win32", "window_control",
       "Snap a window to a quadrant, half or the full work area.",
       required=("position",), optional=("app", "title", "monitor"),
       chain=("win32",), verb="Snapping"),
    _A("move_to_monitor", TOOL_WINDOW, "win32", "window_control",
       "Move a window to another display.",
       required=("monitor",), optional=("app", "title"),
       chain=("win32",), verb="Moving"),
    _A("arrange_all", TOOL_WINDOW, "win32", "window_control",
       "Tile every visible window in a grid.",
       optional=("monitor",), chain=("win32",), verb="Arranging"),
    _A("list_monitors", TOOL_WINDOW, "win32", "window_control",
       "Report the connected displays and their work areas.",
       chain=("win32",), verb="Listing displays"),

    # ── clipboard sentry (pyperclip; text analysis via the active provider) ─
    _A("read", TOOL_CLIPBOARD, "clipboard", "clipboard_sentry",
       "Read the clipboard out loud.", chain=("clipboard",), verb="Reading the clipboard"),
    _A("write", TOOL_CLIPBOARD, "clipboard", "clipboard_sentry",
       "Put text on the clipboard.", required=("text",),
       chain=("clipboard",), verb="Copying"),
    _A("clear", TOOL_CLIPBOARD, "clipboard", "clipboard_sentry",
       "Empty the clipboard.", chain=("clipboard",), verb="Clearing the clipboard"),
    _A("analyze", TOOL_CLIPBOARD, "clipboard", "clipboard_sentry",
       "Summarise what the clipboard text is and what to notice about it.",
       optional=("text",), chain=("clipboard", "model"), verb="Analysing the clipboard"),
    _A("explain", TOOL_CLIPBOARD, "clipboard", "clipboard_sentry",
       "Explain the clipboard text.", optional=("text",),
       chain=("clipboard", "model"), verb="Explaining the clipboard"),
    _A("translate", TOOL_CLIPBOARD, "clipboard", "clipboard_sentry",
       "Translate the clipboard text.", optional=("text", "language"),
       chain=("clipboard", "model"), verb="Translating the clipboard"),
    _A("format", TOOL_CLIPBOARD, "clipboard", "clipboard_sentry",
       "Reformat the clipboard text as clean Markdown.", optional=("text",),
       chain=("clipboard", "model"), verb="Formatting the clipboard"),
    _A("fix", TOOL_CLIPBOARD, "clipboard", "clipboard_sentry",
       "Correct the spelling and grammar of the clipboard text.",
       optional=("text",), chain=("clipboard", "model"), verb="Correcting the clipboard"),

    # ── document architect (Word · Excel · PowerPoint · PDF) ────────────────
    # Every one of these writes a real file, so the result always names the
    # path it wrote and a missing optional package is reported by name — never
    # a silent substitution and never a claim that a file exists.
    _A("create_docx", TOOL_DOCUMENTS, "python-docx", "document_architect",
       "Write a Word document: headings, paragraphs, bullets and tables.",
       required=("content",), optional=("save_as",),
       chain=("python-docx",), verb="Writing"),
    _A("create_xlsx", TOOL_DOCUMENTS, "openpyxl", "document_architect",
       "Build an Excel workbook: several sheets, header rows and charts.",
       required=("content",), optional=("save_as",),
       chain=("openpyxl",), verb="Building the workbook"),
    _A("create_pptx", TOOL_DOCUMENTS, "python-pptx", "document_architect",
       "Build a PowerPoint deck: title slide, bullets and speaker notes.",
       required=("content",), optional=("save_as",),
       chain=("python-pptx",), verb="Building the deck"),
    _A("pdf_from_text", TOOL_DOCUMENTS, "reportlab", "document_architect",
       "Render a titled text document to PDF (built-in renderer without "
       "reportlab).", required=("content",), optional=("save_as",),
       chain=("reportlab", "builtin-pdf"), verb="Rendering the PDF"),
    _A("pdf_merge", TOOL_DOCUMENTS, "pypdf", "document_architect",
       "Combine several PDFs into one file.", required=("paths",),
       optional=("save_as",), chain=("pypdf",), verb="Merging PDFs"),
    _A("pdf_extract", TOOL_DOCUMENTS, "pypdf", "document_architect",
       "Pull pages out of a PDF into a new file and return their text.",
       required=("path", "pages"), optional=("save_as",),
       chain=("pypdf",), verb="Extracting pages"),
    _A("list_documents", TOOL_DOCUMENTS, "filesystem", "document_architect",
       "List the documents J.A.R.V.I.S. has generated, newest first.",
       optional=("limit",), chain=("filesystem",), verb="Listing documents"),
    _A("engines", TOOL_DOCUMENTS, "filesystem", "document_architect",
       "Report which document engines are installed and where files are saved.",
       chain=("filesystem",), verb="Checking the document engines"),

    # ── smart home (python-kasa, this network only) ──────────────────────────
    _A("list_devices", TOOL_SMART_HOME, "kasa", "smart_home",
       "List the configured smart plugs and bulbs, with their live state.",
       chain=("kasa",), verb="Listing devices"),
    _A("discover", TOOL_SMART_HOME, "kasa", "smart_home",
       "Broadcast on this network to find Kasa/Tapo devices.",
       optional=("timeout_s",), chain=("kasa",), verb="Discovering devices"),
    _A("status", TOOL_SMART_HOME, "kasa", "smart_home",
       "Report a device's real state (on/off, brightness).",
       optional=("host", "name"), chain=("kasa",), verb="Checking"),
    _A("turn_on", TOOL_SMART_HOME, "kasa", "smart_home",
       "Switch a device on.", optional=("host", "name"),
       chain=("kasa",), verb="Switching on"),
    _A("turn_off", TOOL_SMART_HOME, "kasa", "smart_home",
       "Switch a device off.", optional=("host", "name"),
       chain=("kasa",), verb="Switching off"),
    _A("toggle", TOOL_SMART_HOME, "kasa", "smart_home",
       "Switch a device to the opposite of its current state.",
       optional=("host", "name"), chain=("kasa",), verb="Toggling"),
    _A("set_brightness", TOOL_SMART_HOME, "kasa", "smart_home",
       "Dim a bulb or dimmer to an exact percentage.",
       required=("percent",), optional=("host", "name"),
       chain=("kasa",), verb="Dimming"),
    _A("energy", TOOL_SMART_HOME, "kasa", "smart_home",
       "Read a device's energy usage figures.", optional=("host", "name"),
       chain=("kasa",), verb="Reading energy"),

    # ── Instagram (instagrapi; the irreversible actions always confirm) ─────
    _A("status", TOOL_INSTAGRAM, "instagrapi", "instagram",
       "Report whether Instagram is connected, without logging in.",
       chain=("instagrapi",), verb="Checking Instagram"),
    _A("profile", TOOL_INSTAGRAM, "instagrapi", "instagram",
       "Read an account's public profile.", required=("user",),
       chain=("instagrapi",), verb="Reading the profile"),
    _A("recent_posts", TOOL_INSTAGRAM, "instagrapi", "instagram",
       "List an account's latest posts.", required=("user",),
       optional=("limit",), chain=("instagrapi",), verb="Reading posts"),
    _A("hashtag", TOOL_INSTAGRAM, "instagrapi", "instagram",
       "List the top posts under a hashtag.", required=("tag",),
       optional=("limit",), chain=("instagrapi",), verb="Reading the hashtag"),
    _A("like", TOOL_INSTAGRAM, "instagrapi", "instagram",
       "Like one post by identifier, shortcode or link.", required=("media",),
       chain=("instagrapi",), verb="Liking"),
    _A("follow", TOOL_INSTAGRAM, "instagrapi", "instagram",
       "Follow an account.", required=("user",),
       chain=("instagrapi",), verb="Following"),
    # Publishing, messaging and anything else another person sees always needs
    # the on-screen confirmation, whatever the approval policy is set to.
    _A("post_photo", TOOL_INSTAGRAM, "instagrapi", "instagram",
       "Publish an image to the user's own feed.", required=("path",),
       optional=("caption",), gate="confirm", chain=("instagrapi",),
       verb="Publishing"),
    _A("direct_message", TOOL_INSTAGRAM, "instagrapi", "instagram",
       "Send a direct message to an account.", required=("user", "text"),
       gate="confirm", chain=("instagrapi",), verb="Messaging"),

    # ── HoloGlobe (geospatial resolver → local three.js page) ───────────────
    _A("show", TOOL_GLOBE, "globe", "desktop_control",
       "Plot one or more places on the interactive globe and bring it up.",
       required=("place",), optional=("places", "focus"),
       chain=("gazetteer", "geocoding service"), verb="Plotting"),
    _A("focus", TOOL_GLOBE, "globe", "desktop_control",
       "Turn a place that is already plotted back to face the camera.",
       required=("place",), chain=("globe",), verb="Focusing"),
    _A("clear", TOOL_GLOBE, "globe", "desktop_control",
       "Remove every marker from the globe.", chain=("globe",), verb="Clearing"),
    _A("list", TOOL_GLOBE, "gazetteer", "desktop_control",
       "List the places the globe can place without a network lookup.",
       chain=("gazetteer",), verb="Listing places"),
    _A("status", TOOL_GLOBE, "gazetteer", "desktop_control",
       "Report how a place would be resolved and what the globe can do now.",
       chain=("gazetteer",), verb="Checking the globe"),
)

_BY_KEY: dict[tuple[str, str], Action] = {(a.tool, a.name): a for a in ACTIONS}
_BY_NAME: dict[str, list[Action]] = {}
for _action in ACTIONS:
    _BY_NAME.setdefault(_action.name, []).append(_action)


# ── lookups ───────────────────────────────────────────────────────────────────

def get(tool: str, action: str) -> Action | None:
    """The action declaration for (*tool*, *action*), or None when unknown."""
    return _BY_KEY.get((str(tool or "").strip(), str(action or "").strip()))


def for_tool(tool: str) -> list[Action]:
    """Every action declared for one tool, in declaration order."""
    name = str(tool or "").strip()
    return [a for a in ACTIONS if a.tool == name]


def names(tool: str = "") -> list[str]:
    """Action names, for one tool or for the whole registry."""
    return [a.name for a in (for_tool(tool) if tool else ACTIONS)]


def find(action: str) -> list[Action]:
    """Every declaration carrying this action name (names are unique in practice)."""
    return list(_BY_NAME.get(str(action or "").strip(), []))


# ── validation ────────────────────────────────────────────────────────────────

def validate(tool: str, args: dict) -> tuple[Action | None, str]:
    """Resolve and validate one request.

    Returns ``(action, "")`` when the call may proceed, else ``(None, reason)``
    where *reason* is a sentence for the model. Nothing else in the layer needs
    to know the parameter rules.
    """
    args = dict(args or {})
    name = str(args.get("action") or "").strip()
    if not name:
        return None, (f"{tool} needs an 'action' naming what to do "
                      f"(one of: {', '.join(names(tool))}).")
    action = get(tool, name)
    if action is None:
        return None, (f"'{name}' is not an action of {tool}. Valid actions: "
                      f"{', '.join(names(tool))}.")
    missing = [p for p in action.required if not _present(args.get(p))]
    if missing:
        return None, (f"{name} needs {', '.join(repr(m) for m in missing)} "
                      f"before it can run.")
    for key in action.optional:
        if key in args and args[key] in ("", None):
            args.pop(key, None)
    return action, ""


def _present(value) -> bool:
    if value is None:
        return False
    if isinstance(value, str):
        return bool(value.strip())
    if isinstance(value, (list, tuple, dict)):
        return bool(value)
    return True


# ── tool declarations ─────────────────────────────────────────────────────────

def parameters(tool: str) -> dict:
    """The flat parameter object declared for one tool.

    ``action`` is a required enum of that tool's action names; the rest is the
    subset of the shared superset the tool's actions actually read.
    """
    actions = for_tool(tool)
    used: list[str] = []
    for a in actions:
        for key in (*a.required, *a.optional):
            if key not in used:
                used.append(key)
    props: dict[str, dict] = {
        "action": {"type": "STRING",
                   "description": ("The operation to perform. One of: "
                                   + ", ".join(a.name for a in actions) + "."),
                   "enum": [a.name for a in actions]},
    }
    for key in used:
        kind, help_text = PARAM_HELP.get(key, ("STRING", ""))
        props[key] = {"type": kind, "description": help_text}
    return {"type": "OBJECT", "properties": props, "required": ["action"]}


def summary(tool: str) -> str:
    """The action list for one tool, as a compact line for its description."""
    return "; ".join(f"{a.name} — {a.summary}" for a in for_tool(tool))


# ── activity rows ─────────────────────────────────────────────────────────────

def activity_detail(tool: str, args: dict) -> str:
    """Live activity text for one request, e.g. ``Opening Chrome``.

    Only real request fields are used — nothing here invents progress.
    """
    args = dict(args or {})
    action = get(tool, str(args.get("action") or ""))
    if action is None:
        return ""
    target = _first(args, "app", "title", "target", "selector", "url", "path",
                    "save_as", "instruction", "text", "key", "keys", "tab",
                    "direction")
    head = action.verb or action.summary.split(" ", 1)[0].rstrip(".")
    if not target:
        return head
    return f"{head} {_short(target)}"


def _first(args: dict, *keys: str) -> str:
    for key in keys:
        value = args.get(key)
        if isinstance(value, (list, tuple)):
            value = " ".join(str(v) for v in value)
        if _present(value):
            return str(value).strip()
    return ""


def _short(value: str, limit: int = 60) -> str:
    text = " ".join(str(value).split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def report() -> str:
    """Human/Gemini-readable action sheet for skill_query and diagnostics."""
    lines = ["STRUCTURED ACTIONS"]
    for tool in TOOLS:
        actions = for_tool(tool)
        controllers = sorted({a.controller for a in actions})
        permissions = sorted({a.permission for a in actions})
        lines.append(f"- {tool}: {len(actions)} actions, controller "
                     f"{'/'.join(controllers)}, permission "
                     f"{'/'.join(permissions)}")
        for a in actions:
            chain = " → ".join(a.chain or (a.controller,))
            required = f" (needs {', '.join(a.required)})" if a.required else ""
            lines.append(f"    {a.name}{required}: {a.summary} [{chain}]")
    return "\n".join(lines)


__all__ = [    "Action", "ACTIONS", "PARAM_HELP", "TOOLS", "TOOL_BROWSER", "TOOL_CLIPBOARD",
    "TOOL_DOCUMENTS", "TOOL_GLOBE", "TOOL_INSTAGRAM", "TOOL_PC", "TOOL_POWER",
    "TOOL_SMART_HOME", 
    "TOOL_SYSTEM", "TOOL_VISUAL", "TOOL_WINDOW", "activity_detail", "find",
    "for_tool", "get", "names", "parameters", "report", "summary", "validate",
]
