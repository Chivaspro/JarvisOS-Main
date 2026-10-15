"""The terminal launch surface: an honest "optimizer" status, real progress.

``launch_ui.ConsoleUI`` is what run_jarvis.bat shows while start_jarvis.py
probes interpreters and core/startup_optimizer.py sweeps cache/temp: the
SYSTEM OPTIMIZER banner, a live percentage, the decoding status line and the
log feed. In a non-tty stream it degrades to plain ``[ pct%] LABEL`` lines —
the same information without ANSI. This locks that surface in, so a startup
that used to look frozen always shows real, moving status.
"""
from __future__ import annotations

import io
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from launch_ui import ConsoleUI, Progress, bar_text  # noqa: E402

_TRACKS = {"optimize": 0.50, "resolve": 0.28, "deps": 0.07, "launch": 0.15}


def test_the_plain_terminal_surface_reports_real_labels_and_finishes():
    prog = Progress(_TRACKS)
    out = io.StringIO()
    ui = ConsoleUI(prog, out=out)

    prog.update("optimize", 0.30, "PURGING JARVIS CACHE", "data/cache  data/tmp")
    ui._plain()
    text = out.getvalue()
    assert "PURGING JARVIS CACHE" in text
    assert "[ 15%]" in text                # 0.50 weight * 0.30 -> 15.0%

    prog.update("resolve", 1.0, "PYTHON RUNTIME LOCATED", "python.exe")
    ui._plain()
    assert "PYTHON RUNTIME LOCATED" in out.getvalue()

    ui.finish()
    final = out.getvalue()
    assert "[100%] J.A.R.V.I.S ONLINE" in final


def test_progress_is_monotonic_and_weighted():
    prog = Progress(_TRACKS)
    assert prog.percent == 0.0
    prog.update("optimize", 1.0)
    first = prog.percent
    assert first == 50.0                   # the optimize track is half the bar
    # A lower report for one track never moves the shown bar backwards.
    prog.update("optimize", 0.2)
    assert prog.percent >= first
    prog.finish()
    assert prog.percent == 100.0


def test_the_bar_fills_proportionally():
    assert bar_text(0.0, width=10).strip("░") == ""
    assert bar_text(100.0, width=10) == "█" * 10
    assert bar_text(50.0, width=10) == "█" * 5 + "░" * 5
