"""Visual computer use: the UI-TARS action format, grounded by Gemini.

UI-TARS is the visual computer-use layer, and **Gemini is its model**. There is
no UI-TARS credential anywhere in this project: the same Gemini key and the same
configured Gemini model that J.A.R.V.I.S. already runs on produce the GUI
actions, and the UI-TARS SDK's own prompt and parser give that reply its
structure. Real flow:

    instruction + one screenshot
        → the vendored ``ui_tars`` COMPUTER_USE prompt
        → the existing Gemini client (existing key, existing model)
        → ``parse_action_to_structure_output``  (the real UI-TARS parser)
        → a structured action dict
        → capabilities/desktop.py executes it (pyautogui, failsafe on)

The SDK is used from its vendored location (``UI-TARS/codes/ui_tars``). It is
pure Python with no dependencies, so it loads by file path without being
installed. If that folder is absent the layer reports ``uitars_sdk_missing``
instead of silently substituting a hand-rolled imitation.

Two rules matter and are enforced here rather than trusted:

* The model's reply is **parsed**, never executed. The SDK's
  ``parsing_response_to_pyautogui_code`` (which emits Python source) is
  deliberately not imported — coordinates are read out of the parsed action and
  handed to the audited executor as numbers.
* One screenshot per action, captured on demand. There is no timer and no
  polling loop in this module.

The parser is asked for ``doubao``-style output, which is UI-TARS's native
format: a point is written ``<point>x y</point>`` with x and y normalised to
0–1000, then converted here into the captured screen's pixels.
"""

from __future__ import annotations

import importlib.util
import re
import threading
import time
from pathlib import Path

_lock = threading.RLock()
_sdk: dict = {"loaded": False, "error": "", "prompt": None, "parser": None,
              "path": ""}
_client = {"impl": None, "key": "", "model": ""}

#: Pointer actions with a grounded position → the local executor's action.
#: (The remaining UI-TARS action types carry text or a destination instead, and
#: are handled explicitly in :func:`execute`.)
_POINTER_ACTIONS = {
    "click": "mouse_click",
    "left_single": "mouse_click",
    "left_double": "mouse_double_click",
    "right_single": "mouse_right_click",
    "hover": "mouse_move",
}


def _log(message: str) -> None:
    try:
        from observability.logger import log_event
        log_event("INFO", "capabilities", "ui_tars_visual", detail=message[:400])
        return
    except Exception:
        pass
    try:
        print(f"[UI-TARS] {message}")
    except Exception:
        pass


# ── the vendored SDK ──────────────────────────────────────────────────────────

def sdk_dir() -> Path:
    try:
        from capabilities import config
        base = Path(config.BASE_DIR)
    except Exception:
        base = Path(__file__).resolve().parent.parent
    return base / "UI-TARS" / "codes" / "ui_tars"


def _load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(f"jarvis_uitars_{name}", path)
    if spec is None or spec.loader is None:
        raise ImportError(f"could not load {path.name}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _load_sdk() -> dict:
    """Load the UI-TARS prompt + parser once. Never raises."""
    with _lock:
        if _sdk["loaded"]:
            return _sdk
        folder = sdk_dir()
        _sdk["path"] = str(folder)
        try:
            prompt = _load_module("prompt", folder / "prompt.py")
            parser = _load_module("action_parser", folder / "action_parser.py")
            _sdk.update({"loaded": True, "error": "", "prompt": prompt,
                         "parser": parser})
            _log(f"UI-TARS SDK loaded from {folder}")
        except Exception as exc:
            _sdk.update({"loaded": False, "error": f"{type(exc).__name__}: {exc}"})
            _log(f"UI-TARS SDK unavailable: {_sdk['error']}")
        return _sdk


def available() -> bool:
    sdk = _load_sdk()
    return bool(sdk["loaded"] and _gemini_key())


def status() -> dict:
    """Real probe for diagnostics and Settings (no API call)."""
    sdk = _load_sdk()
    key = _gemini_key()
    missing = []
    if not sdk["loaded"]:
        missing.append(f"the UI-TARS SDK is not readable at {sdk['path']}")
    if not key:
        missing.append("no Gemini API key is configured (the visual layer's model)")
    model = _model_name()
    return {
        "name": "uitars_visual",
        "sdk": bool(sdk["loaded"]),
        "sdk_path": sdk["path"],
        "model": model,
        "model_source": "gemini (model_router role 'vision')",
        "credential": "gemini_api_key",
        "available": not missing,
        "missing": missing,
        "detail": (f"UI-TARS prompt + parser ready on Gemini/{model}"
                   if not missing else "; ".join(missing)),
    }


# ── the model (the existing Gemini key, nothing else) ─────────────────────────

def _gemini_key() -> str:
    try:
        from capabilities import config
        return config.gemini_api_key() or ""
    except Exception:
        return ""


def _model_name() -> str:
    """The project's configured Gemini model for images.

    Resolved through the existing model router's ``vision`` role, so the user's
    Settings ▸ AI ▸ MODEL ROLES override decides which Gemini model grounds the
    screen — this layer never picks or changes a model on its own.
    """
    try:
        from app import model_router
        return model_router.resolve("vision")
    except Exception:
        return "gemini-flash-latest"


def _client_impl():
    key = _gemini_key()
    model = _model_name()
    with _lock:
        if _client["impl"] is not None and _client["key"] == key:
            return _client["impl"]
        from google import genai
        impl = genai.Client(api_key=key, http_options={"api_version": "v1beta"})
        _client.update({"impl": impl, "key": key, "model": model})
        return impl


def _call_model(prompt: str, png: bytes) -> str:
    """One grounded Gemini call, through the project's existing model router.

    ``role="vision"`` keeps this inside the same resilient path every other
    image call in the project uses (health-based model switching and the user's
    own model-role override). Raises RuntimeError with a readable reason.
    """
    from google.genai import types as gtypes
    from app import model_router
    client = _client_impl()
    model = _model_name()
    try:
        response = model_router.generate(
            client,
            contents=[gtypes.Part.from_bytes(data=png, mime_type="image/png"),
                      prompt],
            role="vision",
            config=gtypes.GenerateContentConfig(temperature=0.0,
                                                max_output_tokens=600),
        )
    except Exception as exc:
        raise RuntimeError(
            f"the Gemini vision request failed on the '{model}' vision model: {exc}"
        ) from exc
    text = (getattr(response, "text", "") or "").strip()
    if not text:
        raise RuntimeError(f"Gemini model '{model}' returned an empty answer")
    return text


# ── prompt + parse ────────────────────────────────────────────────────────────

def _prompt(instruction: str, history: str = "") -> str:
    sdk = _load_sdk()
    template = getattr(sdk["prompt"], "COMPUTER_USE_DOUBAO", "") if sdk["loaded"] else ""
    if not template:
        raise RuntimeError("the UI-TARS prompt template is unavailable")
    text = (template.replace("{language}", "English")
                    .replace("{instruction}", instruction.strip()))
    if history.strip():
        text += ("\n\n## Action history\n"
                 + history.strip()[:1200]
                 + "\nContinue from that history; the screenshot shows the "
                   "current state.")
    return text


def _parse(text: str, width: int, height: int) -> dict:
    """Run the real UI-TARS parser over the model's reply."""
    sdk = _load_sdk()
    parser = sdk["parser"]
    # factor=1000 + model_type="doubao" is UI-TARS's native convention: the
    # point is normalised 0-1000 and the parser returns it as a 0-1 fraction, so
    # no image resize is involved and the numbers map straight onto the
    # captured screen's pixels.
    actions = parser.parse_action_to_structure_output(
        text,
        factor=1000,
        origin_resized_height=int(height),
        origin_resized_width=int(width),
        model_type="doubao",
    )
    if not actions:
        raise ValueError("the reply contained no action")
    first = dict(actions[0] or {})
    first["all"] = [a.get("action_type") for a in actions]
    return first


def _numbers(value) -> list[float]:
    """Read numbers out of the parser's stringified list — no eval()."""
    text = str(value or "")
    return [float(m) for m in re.findall(r"-?\d+(?:\.\d+)?", text)]


def _point(action_inputs: dict, width: int, height: int) -> tuple[int, int] | None:
    """The action's screen position, in captured-screen pixels."""
    box = action_inputs.get("start_box") or action_inputs.get("start_point")
    values = _numbers(box)
    if len(values) < 2:
        return None
    if len(values) >= 4:
        cx = (values[0] + values[2]) / 2.0
        cy = (values[1] + values[3]) / 2.0
    else:
        cx, cy = values[0], values[1]
    if cx > 1.5 or cy > 1.5:          # already absolute pixels
        px, py = cx, cy
    else:                              # normalised 0-1 fractions
        px, py = cx * float(width), cy * float(height)
    px = max(0, min(int(width) - 1, int(round(px))))
    py = max(0, min(int(height) - 1, int(round(py))))
    return px, py


_HISTORY_LIMIT = 6


class Session:
    """A short-lived visual task: the history the prompt is told about.

    The model is stateless between calls, so the previous real actions are fed
    back in text form. Nothing else is retained, and the object is created per
    user request — not per screenshot.
    """

    def __init__(self, instruction: str, max_steps: int = 8) -> None:
        self.instruction = str(instruction or "").strip()
        self.max_steps = max(1, int(max_steps or 8))
        self.steps: list[str] = []
        self.created = time.time()

    def history(self) -> str:
        if not self.steps:
            return ""
        return "\n".join(f"{i + 1}. {line}"
                         for i, line in enumerate(self.steps[-_HISTORY_LIMIT:]))

    def note(self, line: str) -> None:
        if line:
            self.steps.append(str(line)[:160])

    @property
    def exhausted(self) -> bool:
        return len(self.steps) >= self.max_steps


# ── grounding + execution ─────────────────────────────────────────────────────

def next_action(instruction: str, history: str = "", png: bytes | None = None,
                info: dict | None = None, session: Session | None = None) -> dict:
    """Ask Gemini (through the UI-TARS prompt) for the next GUI action.

    Returns a dict with the parsed action, the model used and the grounded
    point in captured-screen pixels. Never executes anything.
    """
    from capabilities import desktop

    sdk = _load_sdk()
    if not sdk["loaded"]:
        return {"ok": False, "controller": "uitars",
                "detail": ("The UI-TARS SDK is not readable at "
                           f"{sdk['path']}, so the visual layer is unavailable. "
                           "Nothing else was substituted."),
                "evidence": {"reason": "uitars_sdk_missing"}}
    if not _gemini_key():
        return {"ok": False, "controller": "uitars",
                "detail": ("The visual layer needs the Gemini API key (its model "
                           "provider) and no key is configured. Add the Gemini key "
                           "in Settings ▸ API; there is no separate UI-TARS key."),
                "evidence": {"reason": "no_gemini_key", "credential": "gemini_api_key"}}
    if not str(instruction or "").strip():
        return {"ok": False, "controller": "uitars",
                "detail": "The visual layer needs an instruction describing what "
                          "to do on screen.",
                "evidence": {"reason": "no_instruction"}}

    try:
        if png is None:
            png, info = desktop.grab_png()
        info = info or {}
        width, height = int(info.get("width") or 0), int(info.get("height") or 0)
    except Exception as exc:
        return {"ok": False, "controller": "uitars",
                "detail": f"The screen could not be captured: {exc}",
                "evidence": {"reason": "capture_failed"}}

    history_text = history or (session.history() if session else "")
    model = _model_name()
    try:
        prompt = _prompt(instruction, history_text)
    except RuntimeError as exc:
        return {"ok": False, "controller": "uitars", "detail": str(exc),
                "evidence": {"reason": "uitars_sdk_missing"}}

    started = time.monotonic()
    raw = ""
    try:
        raw = _call_model(prompt, png)
    except RuntimeError as exc:
        return {"ok": False, "controller": "uitars", "detail": str(exc),
                "evidence": {"reason": "model_error", "model": model}}

    parsed, parse_error = _parse_with_retry(raw, instruction, png, width, height)
    if parsed is None:
        return {"ok": False, "controller": "uitars",
                "detail": ("Gemini answered but the reply was not a readable "
                           f"UI-TARS action ({parse_error}). Nothing was clicked."),
                "evidence": {"reason": "unparsed_action", "model": model,
                             "raw": raw[:400]}}

    action_type = str(parsed.get("action_type") or "")
    action_inputs = dict(parsed.get("action_inputs") or {})
    point = _point(action_inputs, width, height)
    return {
        "ok": True,
        "controller": "uitars",
        "detail": f"{action_type or 'action'} grounded via the UI-TARS format",
        "model": model,
        "thought": str(parsed.get("thought") or "")[:400],
        "action_type": action_type,
        "action_inputs": action_inputs,
        "point": point,
        "raw": raw[:600],
        "elapsed_s": round(time.monotonic() - started, 2),
        "evidence": {"model": model, "screen": {"width": width, "height": height},
                     "point": point, "action": action_type,
                     "scale": (info.get("scale_x"), info.get("scale_y")),
                     "sdk": str(sdk["path"])},
    }


def _parse_with_retry(raw: str, instruction: str, png: bytes,
                      width: int, height: int):
    """Parse the reply; on a malformed answer ask once for the exact format."""
    try:
        return _parse(raw, width, height), ""
    except Exception as first:
        _log(f"unparsed reply: {first}")
    repair = ("Your previous answer could not be parsed.\n"
              "Reply with exactly one line in this format and nothing else:\n"
              "Action: click(point='<point>x y</point>')\n"
              f"where x and y are numbers between 0 and 1000 for: {instruction}")
    try:
        second = _call_model(repair, png)
        return _parse(second, width, height), ""
    except Exception as second:
        return None, f"{type(second).__name__}: {second}"


def execute(parsed: dict, info: dict | None = None) -> dict:
    """Run one parsed UI-TARS action through the local PC executor.

    The action dict is data, never code: each field is read by name and passed
    to ``capabilities.desktop``, which owns every pyautogui call in the project.
    """
    from capabilities import desktop

    action_type = str(parsed.get("action_type") or "")
    inputs = dict(parsed.get("action_inputs") or {})
    point = parsed.get("point")
    info = info or {}
    width = int(info.get("width") or 0)
    height = int(info.get("height") or 0)

    if action_type == "finished":
        return {"ok": True, "controller": "uitars",
                "detail": str(inputs.get("content") or "Task complete."),
                "evidence": {"finished": True}}
    if action_type in ("wait", ""):
        if action_type == "wait":
            time.sleep(2.0)
        return {"ok": True, "controller": "uitars",
                "detail": "Waited for the screen to update",
                "evidence": {"action": "wait"}}

    if action_type in ("drag", "select"):
        target = _numbers(inputs.get("end_box") or inputs.get("end_point"))
        if len(target) < 2:
            return {"ok": False, "controller": "uitars",
                    "detail": "The drag had no destination, so nothing moved.",
                    "evidence": {"action": action_type}}
        ex, ey = _abs_point(target, width, height)
        args = {"to_x": ex, "to_y": ey}
        if point:
            args.update({"x": point[0], "y": point[1]})
        return desktop.run_physical("mouse_drag", args)

    if action_type == "scroll":
        direction = str(inputs.get("direction") or "down").strip().lower()
        direction = direction if direction in ("up", "down", "left", "right") else "down"
        args = {"direction": direction, "amount": 3}
        if point:
            args.update({"x": point[0], "y": point[1]})
        return desktop.run_physical("mouse_scroll", args)

    if action_type == "type":
        text = _unescape(str(inputs.get("content") or ""))
        if not text:
            return {"ok": False, "controller": "uitars",
                    "detail": "The reply asked to type, but carried no text.",
                    "evidence": {"action": action_type}}
        return desktop.run_physical("keyboard_type", {"text": text})

    if action_type == "hotkey":
        combo = str(inputs.get("key") or inputs.get("hotkey") or "")
        return desktop.run_physical("keyboard_hotkey", {"keys": combo})

    if action_type in ("press", "keydown"):
        key = str(inputs.get("key") or inputs.get("press") or "")
        return desktop.run_physical("keyboard_press", {"key": key})

    if action_type in _POINTER_ACTIONS:
        if point is None:
            return {"ok": False, "controller": "uitars",
                    "detail": (f"The reply asked to {action_type} but carried no "
                               "usable position, so nothing was clicked."),
                    "evidence": {"action": action_type}}
        return desktop.run_physical(_POINTER_ACTIONS[action_type],
                                    {"x": point[0], "y": point[1]})

    return {"ok": False, "controller": "uitars",
            "detail": f"I do not know how to perform the action '{action_type}', "
                      f"so nothing was done.",
            "evidence": {"action": action_type}}


def _abs_point(values: list[float], width: int, height: int) -> tuple[int, int]:
    if len(values) >= 4:
        cx = (values[0] + values[2]) / 2.0
        cy = (values[1] + values[3]) / 2.0
    else:
        cx, cy = values[0], values[1]
    if cx > 1.5 or cy > 1.5:
        px, py = cx, cy
    else:
        px, py = cx * float(width), cy * float(height)
    return max(0, int(round(px))), max(0, int(round(py)))


def _unescape(text: str) -> str:
    """Undo the escape characters the prompt asks the model to use."""
    out = text.replace("\\'", "'").replace('\\"', '"')
    out = out.replace("\\n", "\n").rstrip("\n")
    return out


__all__ = ["Session", "available", "execute", "next_action", "sdk_dir", "status"]
