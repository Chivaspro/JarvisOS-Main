"""Real icons for MCP servers — never an emoji.

Every provider shown in Settings → MCP gets a recognisable vector mark, and an
unknown server gets a polished generic node/rack mark rather than a guess at a
brand. The files are SVG and themeable: each one draws with ``currentColor``, and
the loader substitutes the active theme's accent before rendering, so an icon
matches whatever theme the interface is wearing instead of carrying its own
palette.

Qt is imported lazily, inside the render function: this module is imported by the
manager and the tool layer too, and neither of those may pull in a GUI
toolkit just to know an icon's filename.
"""

from __future__ import annotations

from pathlib import Path

__all__ = ["ICON_NAMES", "icon_for_spec", "path_for", "pixmap", "svg_text"]

#: The icons shipped with the project, keyed by provider.
ICON_NAMES = ("composio", "github", "filesystem", "database", "server",
              "system", "web", "vault", "terminal")

_ROOT = Path(__file__).resolve().parent.parent / "assets" / "mcp"

#: Provider aliases → the icon that represents them.
_ALIASES = {
    "composio": "composio",
    "github": "github", "gh": "github", "github-mcp-server": "github",
    "filesystem": "filesystem", "fs": "filesystem", "files": "filesystem",
    "database": "database", "db": "database", "postgres": "database",
    "sqlite": "database", "mysql": "database",
    "system": "system", "system_tools": "system", "sys": "system", "os": "system",
    "web": "web", "web_tools": "web", "fetch": "web", "browser": "web",
    "vault": "vault", "memory_vault": "vault", "notes": "vault", "obsidian": "vault",
    "terminal": "terminal", "dev_tools": "terminal", "developer": "terminal", "cmd": "terminal",
}


def path_for(name: str) -> Path:
    """The SVG file for *provider*, falling back to the generic mark."""
    key = _ALIASES.get(str(name or "").strip().lower(), str(name or "").strip().lower())
    candidate = _ROOT / f"{key}.svg"
    if candidate.exists():
        return candidate
    return _ROOT / "server.svg"


def svg_text(name: str, color: str = "") -> str:
    """The raw SVG with ``currentColor`` resolved to *color*.

    Qt's SVG renderer has no cascade to inherit from, so the token is replaced
    textually. An empty colour leaves ``currentColor`` alone (a caller that sets
    it on a wrapping element still gets what it expected).
    """
    try:
        text = path_for(name).read_text(encoding="utf-8")
    except Exception:
        return ""
    if color:
        text = text.replace("currentColor", str(color))
    return text


def pixmap(name: str, color: str, size: int = 24):
    """Render one icon to a ``QPixmap`` at *size*, or ``None`` if Qt cannot.

    Returns a pixmap so callers can draw it in a paint event and cache it; a
    missing Qt SVG plugin is reported by ``None``, never by an exception, so an
    absent icon degrades to a plain card instead of breaking a panel.
    """
    try:
        from PyQt6.QtCore import QByteArray, Qt
        from PyQt6.QtGui import QPixmap
        from PyQt6.QtSvg import QSvgRenderer
        from PyQt6.QtGui import QPainter
    except Exception:
        return None
    text = svg_text(name, color)
    if not text:
        return None
    try:
        renderer = QSvgRenderer(QByteArray(text.encode("utf-8")))
        if not renderer.isValid():
            return None
        pixmap = QPixmap(int(size), int(size))
        pixmap.fill(Qt.GlobalColor.transparent)
        painter = QPainter(pixmap)
        renderer.render(painter)
        painter.end()
        return pixmap
    except Exception:
        return None


def icon_for_spec(spec) -> str:
    """Which icon represents this server.

    The provider hint wins; otherwise the name, URL and command are matched
    against the known aliases — a server called "GitHub MCP" with a
    githubcopilot.com URL is a GitHub server no matter how it was configured,
    and anything unrecognised is the generic MCP mark rather than a wrong logo.
    """
    provider = str(getattr(spec, "provider", "") or "").strip().lower()
    if provider in _ALIASES:
        return _ALIASES[provider]
    haystack = " ".join([
        str(getattr(spec, "name", "") or ""),
        str(getattr(spec, "url", "") or ""),
        " ".join(str(part) for part in (getattr(spec, "command", []) or [])),
    ]).lower()
    for alias, icon in _ALIASES.items():
        if alias and alias in haystack:
            return icon
    return "server"
