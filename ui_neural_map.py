"""The Neural Map: an interactive graph of what J.A.R.V.I.S. remembers.

The visual language is deliberate — a dark field, thin glowing edges, and nodes
whose size and colour carry meaning — but the *data* rule matters more: every
node is a real note, tag or folder, and every edge is a relationship that exists
in memory (a wiki link, a backlink, a tag, a folder membership). Nothing is drawn
to fill the screen.

The graph follows the **active memory backend**: with Obsidian Vault active it is
built from the vault's own links and tags; with the original memory active it is
built from that store's memory graph
(:meth:`memory.manager.BrainMemory.graph`). Switching the backend in Settings
changes the source, which is what makes the map a view of memory rather than a
second, decorative copy of it.

The canvas is drawn by hand rather than assembled from scene items: the graph is
a few hundred positioned points and lines, and a direct painter is both simpler
and far faster to update every simulation tick. Layout is a small force
simulation stepped on a timer, and the glow work is dropped above a node-count
threshold so a large vault stays smooth.
"""

from __future__ import annotations

import math
import os
import random
import time
from pathlib import Path

from PyQt6.QtCore import QPointF, Qt, QTimer
from PyQt6.QtGui import (
    QBrush, QColor, QFont, QPainter, QPen, QRadialGradient,
)
from PyQt6.QtWidgets import (
    QApplication, QCheckBox, QComboBox, QDialog, QFrame, QHBoxLayout, QLabel,
    QLineEdit, QMenu, QPushButton, QVBoxLayout, QWidget,
)

import ui_theme as _t
from ui_theme import (
    ACCENT, GREEN, AMBER, TEXT, TEXT_BRIGHT, TEXT_DIM, TEXT_FAINT, TEXT_MID,
    SURFACE, BORDER_SOFT, RADIUS_MD, FONT_UI, FONT_HEAD, qcolor as _qcolor,
    rgba as _rgba,
)

__all__ = ["build_graph", "graph_source", "DEFAULT_LAYERS"]

#: Which relationship layers exist, and their defaults. Semantic links are
#: offered but off: they only render when an embedding source is wired, and a
#: toggle that silently does nothing would be a lie.
DEFAULT_LAYERS = {
    "links": True,
    "backlinks": True,
    "tags": True,
    "folders": True,
    "semantic": False,
}

#: Above this many nodes the glow and gradient work is dropped: the graph stays
#: readable and the interface stays responsive.
_EFFECT_LIMIT = 140

#: Per-type node colour, so the eye can separate notes from tags from folders.
_NODE_COLORS = {
    "note": ACCENT,
    "tag": GREEN,
    "folder": AMBER,
    "person": "#7FD1FF",
    "project": "#B58CFF",
    "concept": "#66E0C0",
    "conversation": "#FFB86B",
    "memory": ACCENT,
    "task": AMBER,
    "core": "#FF6B9D",
}

_EDGE_ALPHA = {
    "link": 130, "backlink": 110, "tag": 80, "folder": 60,
    "semantic": 70, "related": 90, "has_memory": 60, "owns_task": 70,
}


#: How many notes the vault graph may carry. Large enough for a real vault,
#: bounded so the force layout stays inside its performance budget. The live
#: value follows the Performance profile's node budget.
_VAULT_NODE_LIMIT = 2500


def _vault_node_limit() -> int:
    try:
        from ui_perfomance import effective
        return max(50, int(effective().get("neural_max_nodes") or _VAULT_NODE_LIMIT))
    except Exception:
        return _VAULT_NODE_LIMIT


def graph_source() -> dict:
    """Which store the map should read, and the honest reason why.

    A vault wins in the two cases the user configured it: the Obsidian memory
    backend is active, or a vault folder is selected (in Settings or the Mind
    panel). Otherwise the brain store is the source. A selected folder that is
    not there is reported on the source, never silently replaced by the brain
    store: the user must see which path failed.
    """
    try:
        from memory import backend as memory_backend
        if memory_backend.active_backend_name() == "obsidian":
            from memory.vault_store import get_index
            index = get_index()
            return {"kind": "vault", "path": str(index.path),
                    "name": index.path.name or str(index.path),
                    "reason": "The Obsidian memory backend is active."}
    except Exception:
        pass
    try:
        from config.settings import get
        configured = str(get("obsidian_vault_path", "") or "").strip()
        if configured:
            candidate = Path(configured)
            from memory.vault_store import get_index
            index = get_index()
            try:
                index.use_path(candidate)
            except Exception:
                pass
            source = {"kind": "vault", "path": str(index.path),
                      "name": index.path.name or str(index.path),
                      "reason": "A vault folder is selected; the map shows it."}
            # A selection that points at nothing is reported, not silently
            # swapped for the brain store: the user must see which path failed.
            if not candidate.is_dir():
                source["error"] = f"the vault folder does not exist: {candidate}"
            return source
    except Exception:
        pass
    return {"kind": "brain", "path": "", "name": "",
            "reason": "No vault selected — the brain store is the source."}


def build_graph(layers: dict | None = None) -> dict:
    """The graph for the active source: the selected vault, or the brain store.

    Never raises. A vault that cannot be read returns an empty graph with the
    reason attached, so the panel says what is wrong instead of drawing
    something invented.
    """
    layer_values = {**DEFAULT_LAYERS, **(layers or {})}
    source = graph_source()
    if source.get("kind") == "vault":
        if source.get("error"):
            # A selected folder that is not there is reported as-is: drawing
            # whatever rows the index still holds would misstate the vault.
            return {"nodes": [], "links": [], "source": source,
                    "error": source["error"]}
        try:
            from memory.vault_store import get_index
            index = get_index()
            try:
                index.ensure_fresh()
            except Exception:
                pass
            graph = index.graph(layers=layer_values, limit=_vault_node_limit())
            _add_attachments(graph, index)
            graph["source"] = source
            if not graph.get("nodes"):
                reason = source.get("error") or str(
                    getattr(index, "last_error", "") or "")
                if reason:
                    graph["error"] = reason
            return graph
        except Exception as exc:
            return {"nodes": [], "links": [], "source": source,
                    "error": f"the vault graph could not be built: {exc}"}
    graph = _brain_graph()
    graph["source"] = source
    return graph


def _brain_graph() -> dict:
    """The brain store's graph, with beliefs and dreams as their own nodes.

    Beliefs are real rows in the store, so they are drawn with an edge to the
    core; a dream keeps the evidence link the store recorded (a memory or a
    task) when that node is on screen.
    """
    try:
        from memory.manager import get_brain_memory
        brain = get_brain_memory()
        raw = brain.graph()
        nodes = []
        for node in raw.get("nodes", []):
            kind = str(node.get("category") or "").lower()
            nodes.append({
                "id": str(node.get("id")),
                "label": str(node.get("label") or ""),
                "type": ("core" if kind == "core"
                         else "task" if kind == "tasks" else "memory"),
                "body_head": str(node.get("content") or ""),
                "tags": [],
                "mtime": float(node.get("updated_at") or 0),
            })
        links = [{"s": str(edge.get("s")), "t": str(edge.get("t")),
                  "type": str(edge.get("type") or "related"),
                  "weight": float(edge.get("weight") or 0.5)}
                 for edge in raw.get("links", [])]
        known = {node["id"] for node in nodes}
        try:
            for belief in brain.beliefs(limit=200):
                node_id = f"belief:{belief.get('id')}"
                if node_id in known:
                    continue
                known.add(node_id)
                nodes.append({
                    "id": node_id,
                    "label": str(belief.get("statement") or "")[:60],
                    "type": "belief",
                    "body_head": str(belief.get("statement") or ""),
                    "tags": [],
                    "mtime": float(belief.get("updated_at") or 0),
                })
                links.append({"s": "0", "t": node_id, "type": "has_belief",
                              "weight": 0.5})
        except Exception:
            pass
        try:
            for dream in brain.dreams(limit=200):
                node_id = f"dream:{dream.get('id')}"
                if node_id in known:
                    continue
                known.add(node_id)
                nodes.append({
                    "id": node_id,
                    "label": str(dream.get("title") or "")[:60],
                    "type": "dream",
                    "body_head": str(dream.get("narrative") or ""),
                    "tags": [],
                    "mtime": float(dream.get("updated_at") or 0),
                })
                source = str(dream.get("source") or "")
                target = ""
                if source.startswith("memory:"):
                    target = source.split(":", 1)[1]
                elif source.startswith("task:"):
                    target = f"task_{source.split(':', 1)[1]}"
                if target and target in known:
                    links.append({"s": node_id, "t": target,
                                  "type": "dream_source", "weight": 0.6})
                else:
                    links.append({"s": "0", "t": node_id, "type": "has_dream",
                                  "weight": 0.5})
        except Exception:
            pass
        return {"nodes": nodes, "links": links}
    except Exception:
        return {"nodes": [], "links": []}


def _vault_files(root: Path, limit: int = 6000) -> dict:
    """name -> path for every non-markdown file in the vault (bounded).

    Built only when a bare-name link needs resolving — Obsidian finds
    ``![[map.png]]`` anywhere in the vault, so the map does too. The walk is
    capped so a pathological folder cannot stall the graph build.
    """
    out: dict[str, Path] = {}
    try:
        for count, item in enumerate(root.rglob("*")):
            if count >= limit:
                break
            try:
                if item.is_file() and item.suffix.lower() != ".md":
                    out.setdefault(item.name.lower(), item)
            except Exception:
                continue
    except Exception:
        pass
    return out


def _add_attachments(graph: dict, index) -> None:
    """Links to real non-markdown files become attachment nodes.

    Obsidian draws attachments only when asked; the same rule applies here. A
    link whose target genuinely exists in the vault is a real relationship, so
    it earns a node — a link with no file behind it is left out entirely.
    Relative targets resolve against the vault and the linking note's folder;
    a bare ``![[map.png]]`` resolves against the vault file list, the way
    Obsidian resolves it.
    """
    nodes = graph.get("nodes") or []
    links = graph.get("links") or []
    by_name: dict[str, str] = {}
    for node in nodes:
        if node.get("type") != "note":
            continue
        path = str(node.get("path") or "")
        base = path.rsplit("/", 1)[-1].removesuffix(".md").lower()
        if base:
            by_name.setdefault(base, str(node.get("id")))
        label = str(node.get("label") or "").strip().lower()
        if label:
            by_name.setdefault(label, str(node.get("id")))
    try:
        notes = list(index.notes())
    except Exception:
        return
    files: dict[str, Path] | None = None
    seen: set[str] = set()
    checked = 0
    for note in notes:
        if len(seen) >= 300:
            break
        path = str(note.get("path") or "")
        parent = path.rsplit("/", 1)[0] if "/" in path else ""
        for target in list(note.get("links") or [])[:200]:
            name = str(target).strip()
            if not name or name.lower() in by_name:
                continue
            if checked >= 400:
                break
            checked += 1
            if os.path.splitext(name)[1].lower() in ("", ".md"):
                continue
            candidates = [index.path / name]
            if parent:
                candidates.append(index.path / parent / name)
            found = next((item for item in candidates if item.is_file()), None)
            if found is None and "/" not in name.replace("\\", "/"):
                if files is None:
                    files = _vault_files(index.path)
                found = files.get(name.lower())
            if found is None:
                continue
            try:
                rel = found.relative_to(index.path).as_posix()
            except Exception:
                continue
            node_id = f"attachment:{rel}"
            if node_id in seen:
                continue
            seen.add(node_id)
            try:
                mtime = found.stat().st_mtime
            except Exception:
                mtime = 0.0
            nodes.append({"id": node_id, "label": found.name,
                          "type": "attachment", "path": rel,
                          "folder": parent, "mtime": mtime})
            links.append({"s": f"note:{path}", "t": node_id,
                          "type": "attachment", "weight": 0.3})
