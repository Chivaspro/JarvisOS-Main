"""Housekeeping tests — logs rotate, caches expire, bytecode is pruned.

These pin the behaviour the setup wizard and the `diagnostics` tool rely on:
a bounded log directory, an expiring cache, and safe ``__pycache__`` pruning
that never wanders into vendored trees (node_modules, .venv, models, …).
"""
from __future__ import annotations

import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from core import maintenance  # noqa: E402

DAY = 86400


def _write(path: Path, text: str = "x", age_days: float = 0.0) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    if age_days:
        old = time.time() - age_days * DAY
        os.utime(path, (old, old))
    return path


def test_prune_logs_drops_old_and_keeps_the_newest(tmp_path):
    old = _write(tmp_path / "jarvis-20250101.jsonl", age_days=30)
    new = _write(tmp_path / "jarvis-20990101.jsonl", age_days=0)
    out = maintenance.prune_logs(logs_dir=tmp_path, retention_days=14)
    assert out["removed"] == 1
    assert old.exists() is False
    assert new.exists() is True                 # the live log is never touched
    assert out["kept"] >= 1


def test_prune_logs_never_deletes_the_only_file(tmp_path):
    only = _write(tmp_path / "jarvis-20250101.jsonl", age_days=999)
    out = maintenance.prune_logs(logs_dir=tmp_path, retention_days=1)
    assert out["removed"] == 0
    assert only.exists() is True


def test_prune_logs_enforces_the_size_ceiling(tmp_path):
    big = "x" * (64 * 1024)
    for i in range(4):
        _write(tmp_path / f"jarvis-2025010{i}.jsonl", big, age_days=0.1 * i)
    new = _write(tmp_path / "jarvis-20990101.jsonl", big, age_days=0)
    out = maintenance.prune_logs(logs_dir=tmp_path, retention_days=999,
                                 max_total_mb=0)
    assert new.exists() is True                 # newest always survives
    assert out["removed"] >= 1


def test_prune_pycache_removes_bytecode_but_skips_vendored_trees(tmp_path):
    mine = _write(tmp_path / "pkg" / "__pycache__" / "mod.cpython-312.pyc")
    vendored = _write(tmp_path / "node_modules" / "dep" / "__pycache__" / "x.pyc")
    loose = _write(tmp_path / "stray.pyc")

    out = maintenance.prune_pycache(root=tmp_path)

    assert mine.exists() is False
    assert loose.exists() is False
    assert vendored.exists() is True            # node_modules is not ours
    assert out["removed"] >= 1


def test_run_maintenance_reports_and_formats(tmp_path, monkeypatch):
    _write(tmp_path / "logs" / "jarvis-20250101.jsonl", age_days=30)
    monkeypatch.setattr(maintenance, "_logs_dir", lambda: tmp_path / "logs")
    report = maintenance.run_maintenance(prune_bytecode=False)
    assert set(report) >= {"logs", "cache", "pycache", "reclaimed_bytes"}
    text = maintenance.format_report(report)
    assert "MAINTENANCE" in text and "TOTAL RECLAIMED" in text