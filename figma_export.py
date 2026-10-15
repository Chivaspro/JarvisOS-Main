"""Figma-importable panel exporter for the J.A.R.V.I.S. Layout Studio.

``export_panel_to_figma`` turns the elements the Studio already manages
(``main._layout_elements`` — the same spec dictionaries produced by
``ui_layout_core.default_spec`` / ``normalize_element`` and rendered live by
``ElementWidget``) into a single, self-contained SVG file. It also owns two
pieces of translation the live widget tree depends on:

* :func:`normalize_runtime_svg_fragment` — a ``QSvgGenerator`` capture is a whole
  XML document; it must become a balanced fragment before it joins the exported
  document, or the file stops being well-formed and design tools abandon it
  part-way (the original "imported SVG is incomplete" failure).
* :func:`qss_paint` / :func:`qss_border` / :func:`qss_radius` / :func:`split_color`
  — Qt's own stylesheet vocabulary (``rgba()``, ``qlineargradient``,
  per-corner radius, dashed borders) mapped onto valid SVG equivalents, resolved
  through the cascade so a widget gets *its* rule rather than a relative's.

Why SVG and not a raw ``.fig``
-------------------------------
Figma's native ``.fig`` binary format is proprietary and undocumented; there
is no supported way for a third-party tool to write one, and faking it would
either silently corrupt on import or require pretending we can do something
we can't. SVG is the actual, Figma-documented import path that produces real,
independently editable layers: rectangles/paths import as editable vector
shapes, ``<text>`` elements import as editable text objects (not flattened
outlines), and ``<g>`` groups import as named Figma groups — which is exactly
the "real editable design elements, not a picture of the panel" outcome this
feature exists to deliver.

This module intentionally imports nothing from Qt/PyQt6. It only ever reads
plain ``dict``/``str``/``int`` values out of element specs and stylesheet text,
so it can be exercised with a plain Python dict in a unit test and can never be
the reason the Layout Editor itself fails to import or open.
"""

from __future__ import annotations

import base64
import math
import mimetypes
import os
import re
import sys
from pathlib import Path
from typing import Any
# `xml.sax.saxutils` is imported LAZILY, inside these two wrappers. It pulls
# `urllib.request` (→ http.client + ssl + email, measured ~42 ms) and that cost
# landed on every J.A.R.V.I.S. start: ui_layout imports this module for its
# runtime QSS helpers, which never touch XML. Escaping only happens while an
# export is being written, which is exactly when the import now runs.
def _xml_escape(text) -> str:
    """XML-escape ``text`` (stdlib ``xml.sax.saxutils.escape``, imported on use)."""
    from xml.sax.saxutils import escape
    return escape(text)


def _xml_attr(value) -> str:
    """Quote a value for an XML attribute (stdlib ``quoteattr``, on use)."""
    from xml.sax.saxutils import quoteattr
    return quoteattr(value)

__all__ = [
    "FigmaExportError",
    "sanitize_filename",
    "downloads_directory",
    "export_panel_to_figma",
    # Qt-CSS -> SVG paint translation, shared with the Layout Studio
    "split_color",
    "parse_qt_gradient",
    "parse_qt_paint_value",
    "qss_declarations",
    "qss_property",
    "qss_paint",
    "qss_radius", "qss_border",
    # runtime capture helpers
    "normalize_runtime_svg_fragment",
    "runtime_fragment_has_content",
]


class FigmaExportError(Exception):
    """Raised for any recoverable failure exporting a panel to Figma.

    Callers (the Layout Editor UI) are expected to catch this, show the
    project's existing notification pattern, and keep running — an export
    failure must never take the editor down with it.
    """


# --------------------------------------------------------------------------- helpers
_INVALID_FILENAME_CHARS = '<>:"/\\|?*'


def sanitize_filename(name: str, fallback: str = "Panel") -> str:
    """Strip characters that are illegal in a filename on any major OS."""
    text = "".join(ch for ch in str(name or "") if ch not in _INVALID_FILENAME_CHARS
                   and ord(ch) >= 32)
    text = text.strip(" .")
    return text[:120] or fallback


def _unique_path(path: Path) -> Path:
    """Never silently clobber a previous export of the same panel."""
    if not path.exists():
        return path
    stem, suffix, parent = path.stem, path.suffix, path.parent
    for n in range(2, 1000):
        candidate = parent / f"{stem} ({n}){suffix}"
        if not candidate.exists():
            return candidate
    return path


def downloads_directory() -> Path:
    """Best-effort, portable resolution of *this* user's Downloads folder.

    Never hard-codes a username; always resolves relative to the account
    the process is running under.
    """
    if sys.platform.startswith("win"):
        found = _windows_downloads_folder()
        if found is not None:
            return found
        base = os.environ.get("USERPROFILE") or str(Path.home())
        return Path(base) / "Downloads"

    if sys.platform == "darwin":
        return Path.home() / "Downloads"

    # Linux / BSD: honour the XDG user-dirs configuration when present,
    # since many distros relocate/rename the Downloads folder.
    xdg = _xdg_download_dir()
    if xdg is not None:
        return xdg
    return Path.home() / "Downloads"


def _windows_downloads_folder() -> Path | None:
    try:
        import ctypes
        from ctypes import wintypes  # noqa: F401  (imported for platform availability check)

        class GUID(ctypes.Structure):
            _fields_ = [
                ("Data1", ctypes.c_uint32),
                ("Data2", ctypes.c_uint16),
                ("Data3", ctypes.c_uint16),
                ("Data4", ctypes.c_uint8 * 8),
            ]

        # FOLDERID_Downloads — the only correct, non-hard-coded way to find a
        # (possibly relocated) Downloads folder on modern Windows.
        folder_id = GUID(
            0x374DE290, 0x123F, 0x4565,
            (ctypes.c_uint8 * 8)(0x91, 0x64, 0x39, 0xC4, 0x92, 0x5E, 0x46, 0x7B),
        )
        buf = ctypes.c_wchar_p()
        result = ctypes.windll.shell32.SHGetKnownFolderPath(  # type: ignore[attr-defined]
            ctypes.byref(folder_id), 0, 0, ctypes.byref(buf))
        if result == 0 and buf.value:
            path = Path(buf.value)
            try:
                ctypes.windll.ole32.CoTaskMemFree(buf)  # type: ignore[attr-defined]
            except Exception:
                pass
            return path
    except Exception:
        pass
    return None


def _xdg_download_dir() -> Path | None:
    config = Path.home() / ".config" / "user-dirs.dirs"
    try:
        text = config.read_text(encoding="utf-8", errors="ignore")
    except Exception:
        return None
    for line in text.splitlines():
        line = line.strip()
        if line.startswith("XDG_DOWNLOAD_DIR"):
            try:
                value = line.split("=", 1)[1].strip().strip('"')
                value = value.replace("$HOME", str(Path.home()))
                return Path(value)
            except Exception:
                return None
    return None


def _num(value: Any, default: float = 0.0) -> float:
    try:
        if isinstance(value, bool):
            return float(default)
        return float(value)
    except (TypeError, ValueError):
        return float(default)


def _clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def _as_bool(value: Any, default: bool = False) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in ("1", "true", "yes", "on")
    if value is None:
        return default
    try:
        return bool(value)
    except Exception:
        return default


def _get_path(mapping: dict, path: str, default=None):
    node: Any = mapping
    for key in path.split("."):
        if not isinstance(node, dict) or key not in node:
            return default
        node = node[key]
    return node if node is not None else default


def _as_hex_color(value: Any, fallback: str = "#04121b") -> str:
    """Any colour Qt/CSS can express -> ``#rrggbb`` (alpha handled separately)."""
    hex_value, _alpha = split_color(value, fallback)
    return hex_value


# --------------------------------------------------------------------------- colours
_NAMED_COLORS = {
    "transparent": ("#000000", 0.0),
    "none": ("#000000", 0.0),
    "white": ("#ffffff", 1.0),
    "black": ("#000000", 1.0),
    "red": ("#ff0000", 1.0),
    "green": ("#008000", 1.0),
    "blue": ("#0000ff", 1.0),
    "cyan": ("#00ffff", 1.0),
    "magenta": ("#ff00ff", 1.0),
    "yellow": ("#ffff00", 1.0),
    "gray": ("#808080", 1.0),
    "grey": ("#808080", 1.0),
    "darkgray": ("#a9a9a9", 1.0),
    "darkgrey": ("#a9a9a9", 1.0),
    "lightgray": ("#d3d3d3", 1.0),
    "lightgrey": ("#d3d3d3", 1.0),
    "orange": ("#ffa500", 1.0),
}

_RGB_RE = re.compile(r"^rgba?\s*\(([^)]*)\)$", re.IGNORECASE)


def _parse_rgb_func(text: str) -> tuple[str, float] | None:
    """``rgb(r,g,b)`` / ``rgba(r,g,b,a)`` -> (#rrggbb, alpha).

    Qt writes rgba() for almost every colour its stylesheets produce (see
    ``ui_layout_core._rgba``), and the alpha channel there is 0-255, not 0-1.
    Dropping it is what made translucent HUD backgrounds export as a flat,
    opaque, wrong-looking fill — or vanish behind another layer.
    """
    match = _RGB_RE.match(text.strip())
    if not match:
        return None
    parts = [p.strip() for p in match.group(1).split(",") if p.strip() != ""]
    if len(parts) < 3:
        # ``rgba(0 12 20 / 250)`` slash syntax — normalise to comma form.
        parts = [p for chunk in match.group(1).replace("/", " ").split() for p in [chunk] if p]
    if len(parts) < 3:
        return None
    try:
        channels = []
        for raw in parts[:3]:
            if raw.endswith("%"):
                channels.append(int(round(_clamp(_num(raw[:-1]) * 2.55, 0, 255))))
            else:
                channels.append(int(round(_clamp(_num(raw), 0, 255))))
        alpha = 1.0
        if len(parts) >= 4:
            fourth = parts[3]
            value = _num(fourth.rstrip("%"))
            alpha = value / 100.0 if fourth.endswith("%") else (
                value / 255.0 if value > 1.0 else value)
    except (TypeError, ValueError):
        return None
    hex_value = "#{:02x}{:02x}{:02x}".format(*channels)
    return hex_value, _clamp(alpha, 0.0, 1.0)


def split_color(value: Any, fallback: str = "#04121b") -> tuple[str, float]:
    """Return ``(#rrggbb, alpha 0..1)`` for anything CSS/Qt calls a colour.

    Accepts ``#rgb``, ``#rgba``, ``#rrggbb``, ``#rrggbbaa``, ``rgb()``/``rgba()``,
    and a handful of CSS keywords. Previously only 4/7/9-character hex values
    survived and everything else silently became the dark fallback, which is a
    large part of why backgrounds and borders came out blank or wrong.
    """
    text = str(value or "").strip()
    if not text:
        return fallback, 1.0
    lowered = text.lower()
    if lowered in _NAMED_COLORS:
        return _NAMED_COLORS[lowered]
    parsed = _parse_rgb_func(text)
    if parsed:
        return parsed
    if text.startswith("#"):
        body = text[1:]
        try:
            if len(body) == 3:
                return f"#{''.join(c * 2 for c in body)}", 1.0
            if len(body) == 4:                      # #rgba
                hex_value = "#{}{}".format("".join(c * 2 for c in body[:3]),
                                            body[3].lower())
                r, g, b = hex_value[1:3], hex_value[3:5], hex_value[5:7]
                alpha = int(body[3] * 2, 16) / 255.0
                return f"#{r}{g}{b}", _clamp(alpha, 0.0, 1.0)
            if len(body) == 6:
                return f"#{body.lower()}", 1.0
            if len(body) == 8:                      # #rrggbbaa
                return f"#{body[:6].lower()}", _clamp(int(body[6:8], 16) / 255.0, 0.0, 1.0)
        except ValueError:
            return fallback, 1.0
        return f"#{body.lower()}", 1.0
    return fallback, 1.0


# --------------------------------------------------------------------------- Qt CSS -> SVG paint
_GRADIENT_RE = re.compile(r"(q(?:linear|radial|conical)gradient)\s*\((.*)\)\s*$", re.IGNORECASE | re.DOTALL)
_STOP_RE = re.compile(r"stop\s*:\s*([0-9]*\.?[0-9]+)\s*(.+)", re.IGNORECASE)


def _split_top_level(text: str, sep: str = ",") -> list[str]:
    """Split on ``sep`` but never inside parentheses (gradient stops contain them)."""
    chunks, depth, current = [], 0, []
    for ch in text:
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth = max(0, depth - 1)
        if ch == sep and depth == 0:
            chunks.append("".join(current))
            current = []
        else:
            current.append(ch)
    chunks.append("".join(current))
    return [c.strip() for c in chunks if c.strip() != ""]


def _split_ws(text: str) -> list[str]:
    """Whitespace split that keeps ``rgba(0, 12, 20, 250)`` in one piece."""
    tokens, depth, current = [], 0, []
    for ch in str(text or ""):
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth = max(0, depth - 1)
        if ch.isspace() and depth == 0:
            if current:
                tokens.append("".join(current))
                current = []
        else:
            current.append(ch)
    if current:
        tokens.append("".join(current))
    return [t for t in (token.strip() for token in tokens) if t]


def parse_qt_gradient(value: str) -> dict | None:
    """Translate a Qt ``qlineargradient``/``qradialgradient`` into an SVG gradient.

    The exporter's gradient block schema is the same one the Studio already
    stores on elements (``kind`` / ``angle`` / ``stops``), with two additions
    used only by parsed CSS: ``coords`` (exact objectBoundingBox endpoints, so
    the direction matches what Qt painted) and a third per-stop value carrying
    the stop's alpha, emitted as ``stop-opacity``.
    """
    match = _GRADIENT_RE.match(str(value or "").strip())
    if not match:
        return None
    kind_raw, body = match.group(1).lower(), match.group(2)
    tokens = _split_top_level(body)
    stops: list[list[Any]] = []
    coords: dict[str, float] = {}
    spread = "pad"
    for token in tokens:
        stop = _STOP_RE.match(token.strip())
        if stop:
            color_hex, color_alpha = split_color(stop.group(2).strip(), "#00cfff")
            stops.append([_clamp(_num(stop.group(1), 0.0), 0.0, 1.0), color_hex, color_alpha])
            continue
        if ":" in token:
            key, _, raw = token.partition(":")
            key, raw = key.strip().lower(), raw.strip()
            if key in ("x1", "y1", "x2", "y2", "cx", "cy", "r", "radius", "fx", "fy"):
                coords[key] = _num(raw, 0.0)
            elif key == "type":
                spread = raw.lower() if raw.lower() in ("pad", "repeat", "reflect") else "pad"
    if len(stops) < 2:
        return None
    stops.sort(key=lambda item: item[0])
    if kind_raw.endswith("radialgradient"):
        return {"enabled": True, "kind": "radial", "angle": 90, "spread": spread,
                "center": [coords.get("cx", 0.5), coords.get("cy", 0.5)],
                "radius": coords.get("r", coords.get("radius", 0.75)),
                "focus": [coords.get("fx", coords.get("cx", 0.5)),
                          coords.get("fy", coords.get("cy", 0.5))],
                "stops": [[pos, hex_value, alpha] for pos, hex_value, alpha in stops]}
    if kind_raw.endswith("conicalgradient"):
        # SVG 1.1 has no conic gradient; keep the colour band rather than
        # exporting nothing at all. Figma/Penpot show a plain ramp either way.
        return {"enabled": True, "kind": "linear", "angle": 90, "spread": spread,
                "coords": [0.0, 0.0, 1.0, 1.0],
                "stops": [[pos, hex_value, alpha] for pos, hex_value, alpha in stops]}
    x1, y1 = coords.get("x1", 0.0), coords.get("y1", 0.0)
    x2, y2 = coords.get("x2", 1.0), coords.get("y2", 0.0)
    return {"enabled": True, "kind": "linear", "spread": spread,
            "coords": [x1, y1, x2, y2],
            "stops": [[pos, hex_value, alpha] for pos, hex_value, alpha in stops]}


def parse_qt_paint_value(value: str) -> dict | None:
    """One Qt ``background``/``background-color`` value -> the paint block used
    by :class:`_SvgBuilder` (``color`` + ``opacity`` + optional ``gradient``)."""
    text = str(value or "").strip()
    if not text or text.lower() in ("none", "transparent"):
        return None
    gradient = parse_qt_gradient(text)
    if gradient:
        return {"color": gradient["stops"][0][1], "opacity": 100, "gradient": gradient}
    hex_value, alpha = split_color(text, "")
    if not hex_value:
        return None
    return {"color": hex_value, "opacity": int(round(alpha * 100)), "gradient": {"enabled": False}}


_COMMENT_RE = re.compile(r"/\*.*?\*/", re.DOTALL)
_QSS_RULE_RE = re.compile(r"([^{}]+)\{([^{}]*)\}", re.DOTALL)


def qss_declarations(css: str) -> list[tuple[list[str], str]]:
    """``[(selector tokens, declaration body)]`` for a Qt stylesheet.

    Qt stylesheets are cascades: a HUD panel's single ``styleSheet()`` string is
    full of rules for its *descendants* (``QPushButton{...}\nQLabel{...}``).
    Reading the first ``background:`` in that text — the old behaviour — exported
    some unrelated child's colour as the panel's own background. Returning the
    rules lets the caller match the rule that actually paints the widget.
    """
    text = _COMMENT_RE.sub(" ", str(css or ""))
    rules: list[tuple[list[str], str]] = []
    for selector, body in _QSS_RULE_RE.findall(text):
        selectors = [s.strip() for s in selector.split(",") if s.strip()]
        if selectors:
            rules.append((selectors, body.strip()))
    if not rules and text.strip():
        rules.append((["*"], text.strip()))   # a bare declaration list
    return rules


def _selector_subjects(selectors: list[str]) -> list[str]:
    subjects: list[str] = []
    for selector in selectors:
        # A rule's subject is its last compound selector; descendant/child
        # combinators only constrain ancestors, not the painted widget itself.
        compound = re.split(r"[\s>+~]+", selector.strip())[-1:] or [""]
        subjects.append(compound[0])
    return subjects


def _subject_matches(subject: str, names: set[str]) -> int:
    """Specificity score for a compound selector against a widget's identities."""
    if not subject or subject == "*":
        return 1
    score = 0
    for token in re.findall(r"[#.]?[A-Za-z0-9_-]+", subject):
        if token.startswith("#"):
            if token[1:] in names:
                score = max(score, 4)
            else:
                return 0
        elif token.startswith("."):
            if token[1:] in names:
                score = max(score, 3)
            else:
                return 0
        elif token in names:
            score = max(score, 2)
        else:
            return 0
    return score


def qss_property(css: str, names: Any, *properties: str) -> str | None:
    """Best-matching declaration value for ``properties`` on a widget.

    ``names`` is the widget's identity set (class name, objectName); the most
    specific rule wins, ties resolved by stylesheet order (Qt's own cascade).
    """
    wanted = [p.lower() for p in properties]
    name_set = {str(n) for n in (names or ()) if str(n)}
    best_rank, best_value = -1, None
    for order, (selectors, body) in enumerate(qss_declarations(css)):
        rank = max((_subject_matches(s, name_set) for s in _selector_subjects(selectors)), default=0)
        if not rank:
            continue
        for prop in _split_declaration_body(body):
            key, _, raw = prop.partition(":")
            key = key.strip().lower()
            if key in wanted:
                candidate_rank = rank * 1000 + order - wanted.index(key)
                if candidate_rank > best_rank:
                    best_rank, best_value = candidate_rank, raw.strip()
    return best_value


def _split_declaration_body(body: str) -> list[str]:
    return [chunk for chunk in _split_top_level(body, ";") if ":" in chunk]


def qss_paint(css: str, names: Any) -> dict | None:
    """The effective background paint (colour/alpha/gradient) of one widget."""
    value = qss_property(css, names, "background", "background-color")
    return parse_qt_paint_value(value) if value else None


_RADIUS_PROPS = {
    "tl": ("border-top-left-radius",),
    "tr": ("border-top-right-radius",),
    "br": ("border-bottom-right-radius",),
    "bl": ("border-bottom-left-radius",),
}


def _px(value: str | None) -> float | None:
    if value is None:
        return None
    match = re.search(r"-?\d+(?:\.\d+)?", str(value))
    return float(match.group(0)) if match else None


def qss_radius(css: str, names: Any) -> dict | None:
    """Per-corner radii from Qt CSS, shorthand and longhand both honoured."""
    radii = {corner: None for corner in ("tl", "tr", "br", "bl")}
    shorthand = qss_property(css, names, "border-radius")
    if shorthand:
        values = [_px(part) for part in shorthand.split() if _px(part) is not None]
        if len(values) == 1:
            radii = {"tl": values[0], "tr": values[0], "br": values[0], "bl": values[0]}
        elif len(values) >= 4:
            radii = {"tl": values[0], "tr": values[1], "br": values[2], "bl": values[3]}
    for corner, props in _RADIUS_PROPS.items():
        value = _px(qss_property(css, names, *props))
        if value is not None:
            radii[corner] = value
    if all(value is None for value in radii.values()):
        return None
    return {corner: max(0.0, value or 0.0) for corner, value in radii.items()}


def _border_edge(css: str, names: Any, side: str) -> tuple[str, float, float, str] | None:
    """``(#color, width, opacity, style)`` for one border edge, if it is drawn."""
    shorthand = qss_property(css, names, f"border-{side}" if side else "border")
    width = color = style = None
    if shorthand:
        for token in _split_ws(shorthand):
            lowered = token.strip().lower()
            if lowered in ("solid", "dashed", "dotted", "none", "double"):
                style = lowered
            elif re.match(r"^-?\d", token):
                width = _px(token)
            else:
                color = token
    longhand = qss_property(css, names, "border-width", "border-top-width")
    if width is None and longhand:
        width = _px(longhand.split()[0])
    longhand = qss_property(css, names, "border-color", "border-top-color")
    if color is None and longhand:
        color = longhand
    longhand = qss_property(css, names, "border-style", "border-top-style")
    if style is None and longhand:
        style = longhand.strip().lower()
    if not width or width <= 0 or (style or "solid") == "none":
        return None
    hex_value, alpha = split_color(color or "", "#00cfff")
    return hex_value, float(width), alpha, (style or "solid").capitalize()


def qss_border(css: str, names: Any) -> dict | None:
    """All four border edges of a widget, alpha preserved."""
    edges = {side: _border_edge(css, names, side) for side in
             ("", "top", "right", "bottom", "left")}
    edges = {side: value for side, value in edges.items() if value}
    if not edges:
        return None
    return {"edges": edges}


# --------------------------------------------------------------------------- runtime capture normalization
_XML_PROLOG_RE = re.compile(r"<\?xml[^>]*\?>", re.IGNORECASE)
_DOCTYPE_RE = re.compile(r"<!DOCTYPE[^>]*>", re.IGNORECASE)
_HTML_COMMENT_RE = re.compile(r"<!--.*?-->", re.DOTALL)
_TITLE_RE = re.compile(r"\s*<title\b[^>]*>.*?</title>", re.IGNORECASE | re.DOTALL)
_DESC_RE = re.compile(r"\s*<desc\b[^>]*>.*?</desc>", re.IGNORECASE | re.DOTALL)
_EMPTY_DEFS_RE = re.compile(r"\s*<defs\s*/>|\s*<defs\s*>\s*</defs>", re.IGNORECASE)
_IMAGE_TAG_RE = re.compile(r"<image\b[^>]*?/?>", re.IGNORECASE)
_HREF_RE = re.compile(r"\b(?:xlink:)?href\s*=\s*([\"'])(.*?)\1", re.IGNORECASE | re.DOTALL)
_DRAWABLE_TAGS = ("path", "image", "text", "rect", "circle", "ellipse", "line",
                  "polyline", "polygon", "use", "foreignObject")


def _tag_end_index(text: str, open_index: int) -> int:
    """Index of the ``>`` that closes the tag starting at ``open_index``."""
    depth_quote = ""
    for index in range(open_index, len(text)):
        ch = text[index]
        if depth_quote:
            if ch == depth_quote:
                depth_quote = ""
        elif ch in ('"', "'"):
            depth_quote = ch
        elif ch == ">":
            return index
    return -1


def normalize_runtime_svg_fragment(raw: Any) -> str:
    """Turn a Qt ``QSvgGenerator`` document into a *fragment* that is safe to nest.

    ``QSvgGenerator`` always returns a complete XML document::

        <?xml version="1.0" encoding="UTF-8" standalone="no"?>
        <svg width="62.7944mm" height="17.6389mm" viewBox="0 0 178 50" …>
        <title>QLabel</title><desc>Generated with Qt</desc>
        <defs>…</defs>
        <g …>…</g>
        </svg>

    The previous caller located the first ``>`` in that text, which is the end of
    the *XML declaration*, so the "fragment" it kept began with the nested
    ``<svg …>`` opening tag and — because the last ``</svg>`` was cut off — never
    closed. Every captured widget therefore injected an unbalanced ``<svg>`` into
    the exported file, the document stopped being well-formed, and Figma/Penpot
    gave up part-way: the reported "background and logos are missing/blank".

    This walks the real structure instead of guessing offsets: prolog/DOCTYPE are
    dropped, the outer ``<svg>`` element is unwrapped (its mm width/height are
    meaningless once the exporter places the group in pixel space, and the
    viewBox is pixel-identical to the widget size), Qt's ``<title>``/``<desc>``
    chrome is removed so it cannot create junk layers, and ``<image>`` hrefs get
    both the SVG2 ``href`` and the SVG1.1 ``xlink:href`` spelling that different
    design tools insist on.
    """
    text = str(raw or "")
    if not text.strip():
        return ""
    text = _XML_PROLOG_RE.sub("", text)
    text = _DOCTYPE_RE.sub("", text)
    text = _HTML_COMMENT_RE.sub("", text)

    start = text.lower().find("<svg")
    if start >= 0:
        open_end = _tag_end_index(text, start)
        end = text.lower().rfind("</svg")
        if open_end >= 0 and end > open_end:
            text = text[open_end + 1:end]
        elif open_end >= 0:
            # Unclosed root (shouldn't happen, but never emit a broken tree).
            text = text[open_end + 1:]
    text = text.strip()

    # Any *remaining* nested svg (Qt writes none today, third-party captures may)
    # becomes a plain group: an <svg> inside <g> is what breaks strict importers.
    text = re.sub(r"<svg\b([^>]*)>", lambda m: "<g" + re.sub(
        r"\s*(?:width|height|viewBox|preserveAspectRatio|baseProfile|version)\s*=\s*"
        r"(?:\"[^\"]*\"|'[^']*'|[^\s>]+)", "", m.group(1)) + ">", text, flags=re.IGNORECASE)
    text = re.sub(r"</svg\s*>", "</g>", text, flags=re.IGNORECASE)

    text = _TITLE_RE.sub("", text)
    text = _DESC_RE.sub("", text)
    text = _EMPTY_DEFS_RE.sub("", text)

    def _dual_href(match: re.Match) -> str:
        tag = match.group(0)
        href = _HREF_RE.search(tag)
        if not href:
            return tag
        quoted = f'{href.group(1)}{href.group(2)}{href.group(1)}'
        # ``xlink:href`` also ends in ``href``, so the plain spelling has to be
        # tested with the namespaced prefix excluded - otherwise a Qt capture
        # that only wrote ``xlink:href`` was left without the SVG 2 attribute.
        has_xlink = re.search(r"\bxlink:href\s*=", tag, re.IGNORECASE) is not None
        has_plain = re.search(r"(?<!xlink:)\bhref\s*=", tag, re.IGNORECASE) is not None
        if has_plain and not has_xlink:
            tag = tag.replace(f'href={quoted}', f'href={quoted} xlink:href={quoted}', 1)
        elif has_xlink and not has_plain:
            tag = tag.replace(f'xlink:href={quoted}', f'xlink:href={quoted} href={quoted}', 1)
        return tag

    text = _IMAGE_TAG_RE.sub(_dual_href, text)
    return text.strip()


def runtime_fragment_has_content(fragment: Any) -> bool:
    """Does this capture actually paint something, or is it Qt's empty scaffolding?

    ``QSvgGenerator`` emits a scaffold of ``<g>`` wrappers even for a widget that
    drew nothing, and it silently omits some raster operations. Detecting the
    difference lets the caller fall back to an embedded bitmap instead of
    exporting a blank layer — which is exactly how a pixmap logo used to disappear.
    """
    text = str(fragment or "")
    if not text.strip():
        return False
    for tag in _DRAWABLE_TAGS:
        if re.search(rf"<{tag}\b", text, re.IGNORECASE):
            return True
    return False


_TEXT_KINDS = {"Text", "Button", "Toggle", "CheckBox", "Indicator", "Input", "ProgressBar"}
_IMAGE_KINDS = {"Image"}
_OPAQUE_CONTAINER_KINDS = {"Panel", "Frame", "HUD Card"}
_UNSUPPORTED_KINDS = {"WebView", "Uiverse", "Custom"}


# --------------------------------------------------------------------------- tree building
def _build_tree(elements: dict, root_name: str, _visited: set | None = None) -> list[dict]:
    """Group the flat ``name -> spec`` map into a nested tree under ``root_name``.

    Malformed data (a missing/cyclic ``parent`` chain) degrades to "treat as a
    direct child of the root" rather than raising, per the project's
    never-crash-on-bad-data requirement.
    """
    if not isinstance(elements, dict):
        return []
    by_parent: dict[str, list[dict]] = {}
    by_name: dict[str, dict] = {}
    for name, spec in elements.items():
        if not isinstance(spec, dict):
            continue
        parent = str(spec.get("parent") or "").strip() or root_name
        node = dict(spec)
        node["_name"] = str(name)
        by_parent.setdefault(parent, []).append(node)
        by_name[str(name)] = node

    for siblings in by_parent.values():
        siblings.sort(key=lambda s: _num(s.get("z"), 0))

    attached: set[str] = {root_name}

    def attach(name: str, seen: set) -> list[dict]:
        nodes = []
        for child in by_parent.get(name, []):
            if child["_name"] in seen:
                continue  # cyclic parent chain — drop rather than loop forever
            attached.add(child["_name"])
            child_seen = seen | {child["_name"]}
            child["_children"] = attach(child["_name"], child_seen)
            nodes.append(child)
        return nodes

    tree = attach(root_name, {root_name})

    # Anything the parent chain could not reach is still a real element of this
    # panel (renamed/re-parented roots, a missing intermediate, a spec saved with
    # an empty parent). Hanging those nodes off the canvas keeps the export
    # complete instead of silently losing layers.
    for name, spec in elements.items():
        if not isinstance(spec, dict) or str(name) in attached:
            continue
        node = by_name.get(str(name)) or dict(spec, _name=str(name))
        attached.add(str(name))
        node["_children"] = attach(str(name), {root_name, str(name)})
        tree.append(node)
    tree.sort(key=lambda item: (_num(item.get("z"), 0), str(item.get("_name"))))
    return tree


# --------------------------------------------------------------------------- shape geometry
def _rounded_rect_path(width: float, height: float, radii: tuple[float, float, float, float]) -> str:
    """Independent per-corner rounded-rect path, mirroring the clamping rule
    ``ElementWidget._shape_path`` uses (each radius capped at half the
    shortest side) so the exported outline matches what the Studio painted.
    """
    width, height = max(0.0, width), max(0.0, height)
    max_r = max(0.0, min(width, height) / 2.0)
    tl, tr, br, bl = (_clamp(_num(r, 0.0), 0.0, max_r) for r in radii)
    if not any((tl, tr, br, bl)):
        return f"M 0 0 L {width:.2f} 0 L {width:.2f} {height:.2f} L 0 {height:.2f} Z"
    return (
        f"M {tl:.2f} 0 "
        f"L {width - tr:.2f} 0 "
        + (f"A {tr:.2f} {tr:.2f} 0 0 1 {width:.2f} {tr:.2f} " if tr else "")
        + f"L {width:.2f} {height - br:.2f} "
        + (f"A {br:.2f} {br:.2f} 0 0 1 {width - br:.2f} {height:.2f} " if br else "")
        + f"L {bl:.2f} {height:.2f} "
        + (f"A {bl:.2f} {bl:.2f} 0 0 1 0 {height - bl:.2f} " if bl else "")
        + f"L 0 {tl:.2f} "
        + (f"A {tl:.2f} {tl:.2f} 0 0 1 {tl:.2f} 0 " if tl else "")
        + "Z"
    )


# --------------------------------------------------------------------------- SVG assembly
class _SvgBuilder:
    """Accumulates ``<defs>`` (gradients/filters/clips) while rendering nodes."""

    def __init__(self) -> None:
        self.defs: list[str] = []
        self._counter = 0

    def _next_id(self, prefix: str) -> str:
        self._counter += 1
        return f"{prefix}{self._counter}"

    # -- fills -------------------------------------------------------------
    def _gradient_ref(self, block: dict) -> str | None:
        gradient = block.get("gradient") or {}
        if not _as_bool(gradient.get("enabled", False)):
            return None
        stops = gradient.get("stops") or []
        parsed: list[tuple[float, str, float]] = []
        for stop in stops[:8]:
            try:
                position = _clamp(_num(stop[0], 0.0), 0.0, 1.0)
            except (TypeError, IndexError, ValueError):
                continue
            hex_value, alpha = split_color(stop[1] if len(stop) > 1 else "", "#00cfff")
            if len(stop) > 2:
                # Third element = the stop's own alpha, from parsed Qt CSS.
                alpha = alpha * _clamp(_num(stop[2], 1.0), 0.0, 1.0)
            parsed.append((position, hex_value, alpha))
        if len(parsed) < 2:
            parsed = [(0.0, "#00cfff", 1.0), (1.0, "#04121b", 1.0)]
        gid = self._next_id("grad")
        stop_tags = "".join(
            f'<stop offset="{pos:.3f}" stop-color="{color}"'
            + ("" if alpha >= 0.999 else f' stop-opacity="{alpha:.3f}"') + "/>"
            for pos, color, alpha in parsed)
        spread = str(gradient.get("spread") or "pad").lower()
        spread = spread if spread in ("pad", "repeat", "reflect") else "pad"
        kind = str(gradient.get("kind") or "linear")
        if kind == "radial":
            center = gradient.get("center") or [0.5, 0.5]
            cx = _clamp(_num(center[0] if len(center) > 0 else 0.5, 0.5), 0.0, 1.0)
            cy = _clamp(_num(center[1] if len(center) > 1 else 0.5, 0.5), 0.0, 1.0)
            focus = gradient.get("focus") or [cx, cy]
            fx = _clamp(_num(focus[0] if len(focus) > 0 else cx, cx), 0.0, 1.0)
            fy = _clamp(_num(focus[1] if len(focus) > 1 else cy, cy), 0.0, 1.0)
            radius = max(0.01, _num(gradient.get("radius", 0.75), 0.75))
            self.defs.append(
                f'<radialGradient id="{gid}" gradientUnits="objectBoundingBox" '
                f'cx="{cx:.3f}" cy="{cy:.3f}" r="{radius:.3f}" '
                f'fx="{fx:.3f}" fy="{fy:.3f}" spreadMethod="{spread}">{stop_tags}</radialGradient>')
            return gid
        coords = gradient.get("coords")
        if isinstance(coords, (list, tuple)) and len(coords) == 4:
            x1 = _num(coords[0], 0.0); y1 = _num(coords[1], 0.0)
            x2 = _num(coords[2], 1.0); y2 = _num(coords[3], 0.0)
        else:
            angle = _clamp(_num(gradient.get("angle", 90), 90), 0, 360)
            rad = math.radians(angle)
            x1, y1 = (1 - math.cos(rad)) / 2.0, (1 + math.sin(rad)) / 2.0
            x2, y2 = (1 + math.cos(rad)) / 2.0, (1 - math.sin(rad)) / 2.0
        self.defs.append(
            f'<linearGradient id="{gid}" gradientUnits="objectBoundingBox" '
            f'x1="{x1:.3f}" y1="{y1:.3f}" '
            f'x2="{x2:.3f}" y2="{y2:.3f}" spreadMethod="{spread}">{stop_tags}</linearGradient>')
        return gid

    def fill_attrs(self, block: dict) -> str:
        """``fill``/``fill-opacity`` for a background block (colour *or* gradient).

        Alpha lives in two places in this project — the block's ``opacity``
        percentage and an 8-digit/``rgba()`` colour — and both are honoured now,
        so a translucent HUD surface exports translucent instead of opaque.
        """
        block = block if isinstance(block, dict) else {}
        bg_opacity = _clamp(_num(block.get("opacity", 100), 100), 0, 100)
        if bg_opacity <= 0:
            return 'fill="none"'
        gradient_id = self._gradient_ref(block)
        if gradient_id:
            return f'fill="url(#{gradient_id})" fill-opacity="{bg_opacity / 100.0:.3f}"'
        color = block.get("color") or block.get("colorscheme") or ""
        if isinstance(color, (list, tuple)):
            color = str(color[0]) if color else ""
        hex_value, alpha = split_color(color, "#04121b")
        return f'fill="{hex_value}" fill-opacity="{(bg_opacity / 100.0) * alpha:.3f}"'

    # -- shadow / glow -------------------------------------------------------
    def _effect_filter(self, shadow: dict, glow: dict) -> str | None:
        primitives = []
        if _as_bool(shadow.get("enabled", False)):
            color, alpha = split_color(shadow.get("color"), "#000000")
            primitives.append(
                f'<feDropShadow dx="{_num(shadow.get("dx", 0)):.1f}" '
                f'dy="{_num(shadow.get("dy", 0)):.1f}" '
                f'stdDeviation="{max(0.0, _num(shadow.get("blur", 0)) / 2.0):.2f}" '
                f'flood-color="{color}" '
                f'flood-opacity="{_clamp(_num(shadow.get("opacity", 60), 60) / 100.0 * alpha, 0, 1):.2f}"/>')
        if _as_bool(glow.get("enabled", False)):
            color, alpha = split_color(glow.get("color"), "#00cfff")
            # ``strength`` is the Studio's own glow intensity knob; multiplying it
            # in keeps the exported bloom as strong as the live HUD glow.
            strength = _clamp(_num(glow.get("strength", 100), 100), 0, 100) / 100.0
            primitives.append(
                f'<feDropShadow dx="0" dy="0" '
                f'stdDeviation="{max(0.0, _num(glow.get("radius", 8)) / 2.0):.2f}" '
                f'flood-color="{color}" '
                f'flood-opacity="{_clamp(_num(glow.get("opacity", 80), 80) / 100.0 * alpha * max(strength, 0.35), 0, 1):.2f}"/>')
        if not primitives:
            return None
        fid = self._next_id("fx")
        self.defs.append(
            f'<filter id="{fid}" x="-60%" y="-60%" width="220%" height="220%">'
            + "".join(primitives) + "</filter>")
        return fid

    # -- text wrapping ---------------------------------------------------
    @staticmethod
    def _wrap_text(text: str, width: float, size: float, wrap: bool) -> list[str]:
        text = str(text or "")
        if not text:
            return []
        lines_in = text.splitlines() or [text]
        if not wrap:
            return lines_in
        avg_char_w = max(1.0, size * 0.58)
        max_chars = max(1, int((max(0.0, width - 12.0)) / avg_char_w))
        wrapped: list[str] = []
        for raw_line in lines_in:
            words = raw_line.split(" ")
            current = ""
            for word in words:
                candidate = f"{current} {word}".strip()
                if len(candidate) <= max_chars or not current:
                    current = candidate
                else:
                    wrapped.append(current)
                    current = word
            wrapped.append(current)
        return wrapped or lines_in

    # -- one element -------------------------------------------------------
    def render_node(self, node: dict) -> str:
        kind = str(node.get("type") or node.get("kind") or "Panel")
        name = str(node.get("_name") or kind)
        x, y = _num(node.get("x"), 0.0), _num(node.get("y"), 0.0)
        w = max(1.0, _num(node.get("w"), 10.0))
        h = max(1.0, _num(node.get("h"), 10.0))
        visible = _as_bool(node.get("visible", True), True) and not _as_bool(node.get("hidden", False), False)
        opacity = _clamp(_num(node.get("opacity", 100), 100), 0, 100) / 100.0
        rotation = _num(node.get("rotation", 0), 0.0)

        transform = f"translate({x:.2f},{y:.2f})"
        if rotation:
            center = node.get("rotation_center") or [0.5, 0.5]
            cx = w * _clamp(_num(center[0] if len(center) > 0 else 0.5, 0.5), 0, 1)
            cy = h * _clamp(_num(center[1] if len(center) > 1 else 0.5, 0.5), 0, 1)
            transform += f" rotate({rotation:.2f},{cx:.2f},{cy:.2f})"

        attrs = (
            f'id={_xml_attr(name)} aria-label={_xml_attr(name)} '
            f'transform={_xml_attr(transform)} opacity="{opacity:.3f}"'
        )
        if not visible:
            attrs += ' display="none"'

        body: list[str] = []

        # Exact runtime captures are supplied by the Qt-aware Layout Studio for
        # widgets whose appearance cannot be reconstructed reliably from the
        # saved spec alone (custom QPainter widgets, rich text, WebEngine,
        # video, etc.).  Keep them inside the same named layer so Penpot sees a
        # real group instead of a blank/missing element.
        #
        # ``normalize_runtime_svg_fragment`` is mandatory here, not cosmetic:
        # QSvgGenerator hands back a whole XML document, and embedding it raw is
        # what turned the file into malformed XML that Figma/Penpot stopped
        # reading part-way through. A capture that normalises to *nothing*
        # drawable also falls through, so the element keeps an editable shape
        # (and its embedded bitmap, when the Studio provides one) instead of
        # exporting an invisible layer.
        runtime_svg = normalize_runtime_svg_fragment(node.get("_runtime_svg"))
        runtime_image = node.get("_runtime_image")
        has_image = isinstance(runtime_image, dict) and bool(runtime_image.get("data"))
        captured = False
        if runtime_svg and runtime_fragment_has_content(runtime_svg):
            body.append(runtime_svg)
            captured = True
        elif has_image:
            body.append(self._render_runtime_image(runtime_image, w, h))
            captured = True
        elif kind in _UNSUPPORTED_KINDS:
            body.append(self._render_unsupported_placeholder(kind, w, h))
        else:
            body.append(self._render_shape(node, kind, w, h))
            if kind in _TEXT_KINDS and str(node.get("text") or "").strip():
                body.append(self._render_text(node, w, h))
            image_path = str(_get_path(node, "image.path", "") or "")
            if kind in _IMAGE_KINDS or image_path:
                image_tag = self._render_image(node, w, h)
                if image_tag:
                    body.append(image_tag)
            elif runtime_svg:
                # Vector capture existed but painted only Qt's empty scaffold;
                # keep it as a last resort so no pixel the widget really drew is
                # lost, just behind the semantic shape.
                body.append(runtime_svg)

        if captured:
            # A captured layer is exactly what Qt painted, and QGraphicsBlur/
            # DropShadow effects do *not* survive ``widget.render()``, so the
            # glow or shadow is re-applied to the group instead of being lost.
            effect_id = self._effect_filter(node.get("shadow") or {}, node.get("glow") or {})
            if effect_id:
                attrs += f' filter="url(#{effect_id})"'

        for child in node.get("_children", []) or []:
            try:
                body.append(self.render_node(child))
            except Exception as exc:  # never let one bad child abort the export
                body.append(self._render_error_placeholder(child, exc))

        return f'<g {attrs}>\n' + "\n".join(body) + "\n</g>"

    def _render_unsupported_placeholder(self, kind: str, w: float, h: float) -> str:
        # Bullet 8: unsupported element kinds degrade to a labelled, still
        # fully editable rectangle instead of being skipped silently or
        # crashing the export — the layer survives, flattened content does not.
        label = _xml_escape(f"{kind} (not convertible — recreate manually)")
        return (
            f'<rect width="{w:.2f}" height="{h:.2f}" fill="#1a1a1a" fill-opacity="0.4" '
            f'stroke="#ff9900" stroke-width="1" stroke-dasharray="4 3"/>'
            f'<text x="6" y="16" font-family="sans-serif" font-size="10" fill="#ff9900">{label}</text>'
        )

    def _render_error_placeholder(self, node: dict, exc: Exception) -> str:
        name = _xml_escape(str(node.get("_name") or "element"))
        return f'<!-- skipped "{name}": {_xml_escape(str(exc))} -->'

    def _render_shape(self, node: dict, kind: str, w: float, h: float) -> str:
        radius = _get_path(node, "borders.radius", {}) or {}
        radii = (
            _num(radius.get("tl"), 0), _num(radius.get("tr"), 0),
            _num(radius.get("br"), 0), _num(radius.get("bl"), 0),
        )
        path_d = _rounded_rect_path(w, h, radii)

        fill_attr = self.fill_attrs(node.get("background") or {})

        stroke_attr = self._stroke_attrs(node)
        filter_id = self._effect_filter(node.get("shadow") or {}, node.get("glow") or {})
        filter_attr = f' filter="url(#{filter_id})"' if filter_id else ""

        return f'<path d="{path_d}" {fill_attr} {stroke_attr}{filter_attr}/>'

    def _stroke_attrs(self, node: dict) -> str:
        borders = node.get("borders") or {}
        sides = {side: (borders.get(side) or {}) for side in ("top", "right", "bottom", "left")}
        widths = {side: _num(block.get("width", 0), 0) for side, block in sides.items()}
        if any(width > 0 for width in widths.values()):
            uniform = len({round(w_, 2) for w_ in widths.values()}) == 1 and len(
                {(sides[s].get("color"), sides[s].get("style")) for s in sides if widths[s] > 0}) <= 1
            if uniform:
                sample = next(block for side, block in sides.items() if widths[side] > 0)
                width = widths["top"] or next(w_ for w_ in widths.values() if w_ > 0)
                color, alpha = split_color(sample.get("color"), "#00cfff")
                dash = self._dasharray(str(sample.get("style", "Solid")), width)
                opacity = (_clamp(_num(sample.get("opacity", 100), 100), 0, 100) / 100.0) * alpha
                return (f'stroke="{color}" stroke-width="{width:.2f}" '
                        f'stroke-opacity="{opacity:.3f}"{dash}')
            # Mixed per-side borders — approximate with the strongest side as a
            # visible stroke; a perfectly independent 4-side border has no
            # single-path SVG equivalent, so this keeps the shape editable
            # rather than emitting nothing.
            side = max(widths, key=lambda s: widths[s])
            block = sides[side]
            color, alpha = split_color(block.get("color"), "#00cfff")
            width = widths[side]
            dash = self._dasharray(str(block.get("style", "Solid")), width)
            opacity = (_clamp(_num(block.get("opacity", 100), 100), 0, 100) / 100.0) * alpha
            return f'stroke="{color}" stroke-width="{width:.2f}" stroke-opacity="{opacity:.3f}"{dash}'

        stroke = node.get("stroke") or {}
        if _as_bool(stroke.get("enabled", False)):
            width = max(0.5, _num(stroke.get("width", 1), 1))
            color, alpha = split_color(stroke.get("color"), "#00cfff")
            opacity = (_clamp(_num(stroke.get("opacity", 100), 100), 0, 100) / 100.0) * alpha
            return f'stroke="{color}" stroke-width="{width:.2f}" stroke-opacity="{opacity:.3f}"'
        return 'stroke="none"'

    @staticmethod
    def _dasharray(style: str, width: float) -> str:
        style = style.strip().lower()
        width = max(1.0, width)
        if style == "dashed":
            return f' stroke-dasharray="{width * 3:.1f} {width * 2:.1f}"'
        if style == "dotted":
            return f' stroke-dasharray="{width:.1f} {width * 1.6:.1f}"'
        return ""

    def _render_text(self, node: dict, w: float, h: float) -> str:
        style = node.get("text_style") or {}
        size = max(1.0, _num(style.get("size", 11), 11))
        family = _xml_escape(str(style.get("family") or "Exo 2")).replace("'", "")
        weight = "700" if _as_bool(style.get("bold", False)) else "500"
        font_style = "italic" if _as_bool(style.get("italic", False)) else "normal"
        underline = ' text-decoration="underline"' if _as_bool(style.get("underline", False)) else ""
        color, color_alpha = split_color(style.get("color"), "#e6f4ff")
        opacity = (_clamp(_num(style.get("opacity", 100), 100), 0, 100) / 100.0) * color_alpha
        letter_spacing = _num(style.get("letter_spacing", 0), 0)
        align = str(style.get("align") or "center")
        v_align = str(style.get("v_align") or "middle")
        wrap = _as_bool(style.get("wrap", True), True)

        anchor = {"left": "start", "center": "middle", "right": "end"}.get(align, "middle")
        text_x = {"left": 6.0, "center": w / 2.0, "right": w - 6.0}.get(align, w / 2.0)

        lines = self._wrap_text(str(node.get("text") or ""), w, size, wrap)
        if not lines:
            return ""
        line_height = size * 1.25
        block_height = line_height * len(lines)
        start_y = {
            "top": size,
            "middle": max(size, (h - block_height) / 2.0 + size * 0.85),
            "bottom": h - block_height + size * 0.85,
        }.get(v_align, (h - block_height) / 2.0 + size * 0.85)

        filter_id = self._effect_filter(
            _get_path(style, "shadow", {}) or {}, _get_path(style, "glow", {}) or {})
        filter_attr = f' filter="url(#{filter_id})"' if filter_id else ""

        stroke_attr = ""
        stroke = style.get("stroke") or {}
        if _as_bool(stroke.get("enabled", False)):
            s_color, s_alpha = split_color(stroke.get("color"), "#00cfff")
            s_width = max(0.1, _num(stroke.get("width", 2), 2))
            s_opacity = (_clamp(_num(stroke.get("opacity", 100), 100), 0, 100) / 100.0) * s_alpha
            stroke_attr = (f' stroke="{s_color}" stroke-width="{s_width:.2f}" '
                            f'stroke-opacity="{s_opacity:.3f}" paint-order="stroke"')

        tspans = []
        for i, line in enumerate(lines):
            dy = "0" if i == 0 else f"{line_height:.2f}"
            tspans.append(f'<tspan x="{text_x:.2f}" dy="{dy}">{_xml_escape(line)}</tspan>')

        # Quoted family + a generic fallback: unquoted multi-word names such as
        # "Exo 2" are not valid CSS font stacks and some importers drop them.
        family_attr = _xml_attr("'" + family + "', sans-serif")
        return (
            f'<text y="{start_y:.2f}" text-anchor="{anchor}" '
            f'font-family={family_attr} '
            f'font-size="{size:.2f}" font-weight="{weight}" font-style="{font_style}"{underline} '
            f'letter-spacing="{letter_spacing:.2f}" fill="{color}" fill-opacity="{opacity:.3f}"'
            f'{stroke_attr}{filter_attr}>' + "".join(tspans) + "</text>"
        )

    @staticmethod
    def _render_runtime_image(image: dict, w: float, h: float) -> str:
        data = str(image.get("data") or "")
        mime = str(image.get("mime") or "image/png")
        opacity = _clamp(_num(image.get("opacity", 100), 100), 0, 100) / 100.0
        preserve = str(image.get("preserve") or "xMidYMid meet")
        x = _num(image.get("x"), 0.0)
        y = _num(image.get("y"), 0.0)
        # An embedded capture is the widget's own pixels: when the payload knows
        # its size use it, otherwise fill the element box, so a runtime visual
        # can never be positioned at 0x0 (which reads as a missing element).
        width = _num(image.get("width"), 0.0) or max(1.0, w)
        height = _num(image.get("height"), 0.0) or max(1.0, h)
        return _image_tag(x, y, width, height, opacity, preserve, mime, data)

    def _render_image(self, node: dict, w: float, h: float) -> str | None:
        image = node.get("image") or {}
        path_str = str(image.get("path") or "").strip()
        if not path_str:
            return None
        path = Path(path_str)
        if not path.is_file():
            return (f'<!-- image not found on disk, skipped: {_xml_escape(path_str)} -->')
        try:
            data = path.read_bytes()
        except Exception as exc:
            return f'<!-- could not read image "{_xml_escape(path_str)}": {_xml_escape(str(exc))} -->'
        mime, _ = mimetypes.guess_type(path.name)
        mime = mime or "image/png"
        encoded = base64.b64encode(data).decode("ascii")
        fit = str(image.get("fit") or "Contain").lower()
        preserve = {
            "contain": "xMidYMid meet",
            "cover": "xMidYMid slice",
            "fill": "none",
            "stretch": "none",
        }.get(fit, "xMidYMid meet")
        opacity = _clamp(_num(image.get("opacity", 100), 100), 0, 100) / 100.0
        return _image_tag(0.0, 0.0, max(1.0, w), max(1.0, h), opacity, preserve, mime, encoded)


# --------------------------------------------------------------------------- public API
def _image_tag(x: float, y: float, width: float, height: float, opacity: float,
               preserve: str, mime: str, data: str) -> str:
    """One embedded raster layer, spelled for both SVG 1.1 and SVG 2 readers.

    Figma still resolves ``xlink:href``; Penpot and browser renderers prefer the
    SVG 2 ``href``. Emitting only one of them is how an asset appeared in one tool
    and stayed blank in another, so both attributes are always written.
    """
    href = _xml_attr(f"data:{mime};base64,{data}")
    return (
        f'<image x="{x:.2f}" y="{y:.2f}" width="{width:.2f}" height="{height:.2f}" '
        f'opacity="{opacity:.3f}" preserveAspectRatio={_xml_attr(preserve)} '
        f'href={href} xlink:href={href}/>'
    )


def _background_block(value: Any) -> dict:
    """Normalise the panel's background (hex/rgba/QSS paint string or paint dict)."""
    if isinstance(value, dict):
        block = dict(value)
        block.setdefault("opacity", 100)
        block.setdefault("gradient", {"enabled": False})
        return block
    parsed = parse_qt_paint_value(str(value or ""))
    if parsed:
        return parsed
    return {"color": "#04121b", "opacity": 100, "gradient": {"enabled": False}}


_SVG_TEMPLATE = """<?xml version="1.0" encoding="UTF-8"?>
<svg xmlns="http://www.w3.org/2000/svg" xmlns:xlink="http://www.w3.org/1999/xlink"
     version="1.1" width="{width}px" height="{height}px" viewBox="0 0 {width} {height}">
  <title>{name}</title>
  <desc>Exported by the J.A.R.V.I.S. Layout Studio — every layer is editable.</desc>
  <defs>
{defs}
  </defs>
  <g id={name_attr} aria-label={name_attr}>
{background}
{body}
  </g>
</svg>
"""


def export_panel_to_figma(panel_name: str, panel_size: tuple[int, int], elements: dict,
                           panel_background: Any = None,
                           output_dir: str | Path | None = None,
                           panel_runtime_svg: str | None = None) -> Path:
    """Export ``panel_name`` (and everything laid out inside it in the
    Studio) as a Figma-importable, fully editable SVG. Returns the path the
    file was written to. Raises :class:`FigmaExportError` on any failure —
    the caller is expected to catch it and never let it propagate into the
    editor's event loop.

    ``panel_background`` may be a colour string (``#hex``/``rgba()``), a Qt
    gradient expression, or an already-parsed paint block from
    :func:`qss_paint`; the canvas keeps whichever fill/opacity/ramp it describes.
    """
    if not str(panel_name or "").strip():
        raise FigmaExportError("No panel selected.")

    try:
        raw_w, raw_h = panel_size[0], panel_size[1]
        width = max(1, int(round(float(raw_w))))
        height = max(1, int(round(float(raw_h))))
    except (TypeError, ValueError, IndexError) as exc:
        raise FigmaExportError(f"Invalid panel dimensions: {exc}") from exc

    try:
        tree = _build_tree(elements if isinstance(elements, dict) else {}, str(panel_name))
    except Exception as exc:
        raise FigmaExportError(f"Could not read the panel's layout data: {exc}") from exc

    # A live panel can legitimately contain persisted elements whose parent
    # name predates the current root naming.  Never turn a populated document
    # into an empty SVG just because its parent chain cannot be resolved.  In
    # that case render the elements as direct children of the selected canvas.
    if not tree and isinstance(elements, dict) and elements:
        tree = []
        for name, spec in elements.items():
            if not isinstance(spec, dict):
                continue
            node = dict(spec)
            node["_name"] = str(name)
            node["_children"] = []
            tree.append(node)
        tree.sort(key=lambda item: _num(item.get("z"), 0))

    builder = _SvgBuilder()
    body_parts: list[str] = []
    # The panel's own painting (its stylesheet/`paintEvent` background, HUD frame
    # art) is captured by the Studio and belongs *behind* every child layer.
    panel_art = normalize_runtime_svg_fragment(panel_runtime_svg)
    if panel_art and runtime_fragment_has_content(panel_art):
        body_parts.append(panel_art)
    for node in tree:
        try:
            body_parts.append(builder.render_node(node))
        except Exception as exc:
            body_parts.append(builder._render_error_placeholder(node, exc))

    # The canvas rect is the last-resort background: it guarantees the export is
    # never a blank sheet, and it carries the real colour/alpha/gradient instead
    # of the old hard-coded dark navy that everything non-hex fell back to.
    background_rect = (
        f'    <rect id={_xml_attr(str(panel_name) + " background")} '
        f'aria-label="Background" width="{float(width):.2f}" height="{float(height):.2f}" '
        f'{builder.fill_attrs(_background_block(panel_background))}/>'
    )

    svg_text = _SVG_TEMPLATE.format(
        width=width,
        height=height,
        defs="\n".join(builder.defs),
        background=background_rect,
        body="\n".join(body_parts),
        name=_xml_escape(str(panel_name)),
        name_attr=_xml_attr(str(panel_name)),
    )

    try:
        directory = Path(output_dir) if output_dir else downloads_directory()
        directory.mkdir(parents=True, exist_ok=True)
    except Exception as exc:
        raise FigmaExportError(f"Could not access the Downloads folder: {exc}") from exc

    filename = f"{sanitize_filename(panel_name)}_Figma.svg"
    target_path = _unique_path(directory / filename)

    try:
        target_path.write_text(svg_text, encoding="utf-8")
    except PermissionError as exc:
        raise FigmaExportError(f"No permission to write to {directory}: {exc}") from exc
    except OSError as exc:
        raise FigmaExportError(f"Could not write the file: {exc}") from exc

    return target_path
