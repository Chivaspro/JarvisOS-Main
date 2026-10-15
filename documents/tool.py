"""The ``documents`` tool — Office and PDF deliverables.

Actions:

* ``create_docx`` / ``create_xlsx`` / ``create_pptx`` — a Word file, a workbook
  and a deck, from the declarative spec the model sends
* ``pdf_from_text`` — a titled text document rendered to PDF
* ``pdf_merge`` — several PDFs combined into one
* ``pdf_extract`` — pages pulled out of a PDF, plus their text
* ``list_documents`` — what has already been generated, newest first
* ``engines`` — which builders are installed, and where files are written

Every answer names the real file it wrote. A missing optional package produces a
sentence naming the package and its pip command — never a traceback, never a
half-written file, and never a claim that something was created.
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any, Callable

from documents import builders

__all__ = ["run", "status", "workspace_dir"]

_MAX_LIST = 60


# ── helpers ───────────────────────────────────────────────────────────────────

def workspace_dir() -> Path:
    return builders.workspace_dir()


def _fail(detail: str, reason: str = "action_failed") -> dict:
    return {"ok": False, "controller": "documents", "detail": detail,
            "reason": reason, "evidence": {}}


def _from_build(result: dict, **evidence: Any) -> dict:
    """Translate a builder result into the tool result shape.

    Anything the builder reports besides the file path (page numbers, sheet
    names, the fallback renderer) becomes evidence, so the model always reads
    the real measurements rather than guessing at them.
    """
    ok = bool(result.get("ok"))
    path = str(result.get("path") or "")
    detail = str(result.get("detail") or ("Done." if ok else "Nothing was written."))
    if ok and path:
        detail = f"{detail.rstrip('.')} Saved as: {path}"
    extra = {key: value for key, value in result.items()
             if key not in ("ok", "path", "detail", "reason") and value not in (None, "")}
    payload: dict[str, Any] = {
        "ok": ok,
        "controller": "documents",
        "detail": detail,
        "reason": "" if ok else str(result.get("reason") or "write_failed"),
        "evidence": {"path": path, **extra, **evidence},
    }
    return payload


def _content(args: dict) -> Any:
    return args.get("content")


def _save_as(args: dict) -> str | None:
    value = str(args.get("save_as") or args.get("path") or "").strip()
    return value or None


def _paths(args: dict) -> list[str]:
    """Merge sources: a list, or one string of comma/newline separated paths."""
    value = args.get("paths")
    if isinstance(value, (list, tuple)):
        items = [str(v) for v in value]
    else:
        items = []
        for chunk in str(value or "").replace(";", ",").splitlines():
            items.extend(chunk.split(","))
    return [item.strip().strip('"') for item in items if item.strip()]


def _human_size(size: int) -> str:
    value = float(size)
    for unit in ("B", "KB", "MB", "GB"):
        if value < 1024 or unit == "GB":
            return f"{value:.0f} {unit}" if unit == "B" else f"{value:.1f} {unit}"
        value /= 1024
    return f"{value:.1f} GB"


# ── handlers ──────────────────────────────────────────────────────────────────

def _h_create_docx(args: dict) -> dict:
    return _from_build(builders.create_docx(_content(args), _save_as(args)))


def _h_create_xlsx(args: dict) -> dict:
    return _from_build(builders.create_xlsx(_content(args), _save_as(args)))


def _h_create_pptx(args: dict) -> dict:
    return _from_build(builders.create_pptx(_content(args), _save_as(args)))


def _h_pdf_from_text(args: dict) -> dict:
    return _from_build(builders.pdf_from_text(_content(args), _save_as(args)))


def _h_pdf_merge(args: dict) -> dict:
    sources = _paths(args)
    if not sources:
        return _fail("Merging needs the paths of the PDFs to combine, sir. Pass "
                     "them in 'paths', separated by commas or new lines.",
                     "no_sources")
    return _from_build(builders.pdf_merge(sources, _save_as(args)),
                       sources=len(sources))


def _h_pdf_extract(args: dict) -> dict:
    source = str(args.get("path") or "").strip()
    if not source:
        return _fail("I need the path of the PDF to extract from, sir.", "no_source")
    return _from_build(builders.pdf_extract(source, str(args.get("pages") or ""),
                                            _save_as(args)))


def _h_list_documents(args: dict) -> dict:
    limit = int(args.get("limit") or 15)
    base = workspace_dir()
    try:
        files = [p for p in base.rglob("*") if p.is_file()]
    except Exception as exc:
        return _fail(f"I could not read the documents folder: {exc}", "read_failed")
    files.sort(key=lambda p: p.stat().st_mtime if p.exists() else 0.0, reverse=True)
    if not files:
        return {"ok": True, "controller": "documents",
                "detail": (f"I have not generated any documents yet, sir. New ones "
                           f"are saved in {base}."),
                "reason": "", "evidence": {"path": str(base)}}
    lines = [f"{len(files)} document(s) in {base}, newest first:"]
    for item in files[:min(limit, _MAX_LIST)]:
        try:
            stamp = time.strftime("%Y-%m-%d %H:%M",
                                  time.localtime(item.stat().st_mtime))
            size = _human_size(item.stat().st_size)
        except OSError:
            continue
        lines.append(f"- {item.name} — {size}, {stamp}")
    return {"ok": True, "controller": "documents", "detail": "\n".join(lines),
            "reason": "", "evidence": {"path": str(base), "count": len(files)}}


def _h_engines(_args: dict) -> dict:
    return {"ok": True, "controller": "documents",
            "detail": status(), "reason": "",
            "evidence": {"path": str(workspace_dir())}}


_HANDLERS: dict[str, Callable[[dict], dict]] = {
    "create_docx": _h_create_docx,
    "create_xlsx": _h_create_xlsx,
    "create_pptx": _h_create_pptx,
    "pdf_from_text": _h_pdf_from_text,
    "pdf_merge": _h_pdf_merge,
    "pdf_extract": _h_pdf_extract,
    "list_documents": _h_list_documents,
    "engines": _h_engines,
}


def status() -> str:
    """Which document engines are installed, in plain English."""
    state = builders.available()
    labels = {
        "docx": "Word (.docx) — python-docx",
        "openpyxl": "Excel (.xlsx) — openpyxl",
        "pptx": "PowerPoint (.pptx) — python-pptx",
        "pypdf": "PDF merge/extract — pypdf",
        "reportlab": "PDF from text — reportlab",
    }
    lines = [f"Generated documents are saved in {workspace_dir()}."]
    for module, label in labels.items():
        lines.append(f"- {label}: {'ready' if state.get(module) else 'not installed'}")
    if not state.get("reportlab"):
        lines.append("Without reportlab, PDFs are written by the built-in "
                     "single-font renderer — install it with 'pip install reportlab' "
                     "for full typography.")
    return "\n".join(lines)


def run(action_name: str, args: dict) -> dict:
    """Execute one document action. Returns a result dict, never raises."""
    handler = _HANDLERS.get(str(action_name or "").strip().lower())
    if handler is None:
        return _fail(f"'{action_name}' is not a document action, sir. Use one of: "
                     + ", ".join(sorted(_HANDLERS)) + ".")
    try:
        return handler(dict(args or {})) or _fail("The builder returned no result.")
    except Exception as exc:
        return _fail(f"{type(exc).__name__}: {exc}")
