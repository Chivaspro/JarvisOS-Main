"""JARVIS Performance & Lag-Fix Engine (master spec §27/§28).

Answers "why is my computer slow?" with MEASURE → IDENTIFY → DIAGNOSE —
and stops there: nothing on the machine is changed, no process is killed,
no setting is touched. Safe optimizations are *suggested*, with an owner,
so the assistant (or the user) decides.

Usage:
    from observability.performance import handle_performance
    handle_performance({"mode": "scan"})        # full diagnosis
    handle_performance({"mode": "scan", "scope": "processes"})
    handle_performance({"mode": "scan", "scope": "jarvis"})

All heavy work is synchronous but bounded (~1–3 s); callers keep it off the
UI/event-loop threads (the tool dispatch already runs tools in an executor).
"""
from __future__ import annotations

import os
import platform
import threading
import time
from collections import Counter, deque

import psutil

# ── JARVIS self-scan ─────────────────────────────────────────────────────────
_JARVIS_PROCESS_NAMES = ("jarvis", "python", "pythonw")
_proc = psutil.Process()

_JARVIS_SUBSTRINGS = ("jarvis", "vision", "camera", "tts", "wake", "brain",
                      "gemini", "main.py", "mark-lii", "mark_lii")

# How much CPU/RAM a JARVIS part may use before it is reported as a suspect.
_JARVIS_CPU_LIMIT = 15.0    # % of one core-equivalent... actually % of total
_JARVIS_RAM_LIMIT_MB = 900

# History for the "was this always slow?" question.
_recent: deque = deque(maxlen=12)   # (time, bottleneck, headline)
_recent_lock = threading.Lock()


def _mb(n: int) -> float:
    return round(n / (1024 * 1024), 1)


def _gb(n: int) -> float:
    return round(n / (1024 ** 3), 1)


# ── measurement helpers ──────────────────────────────────────────────────────

def _gpu_percent() -> float:
    """GPU load via the existing monitor helpers (NVML/ctypes), -1 if unknown."""
    try:
        from actions.system_monitor import _get_gpu_usage
        return float(_get_gpu_usage())
    except Exception:
        return -1.0


def _disk_busy() -> tuple[float, float]:
    """(busy_percent_estimate, read+write MB/s) over a 250 ms window."""
    try:
        d0 = psutil.disk_io_counters()
        t0 = time.perf_counter()
        time.sleep(0.25)
        d1 = psutil.disk_io_counters()
        dt = max(1e-6, time.perf_counter() - t0)
        if not d0 or not d1:
            return -1.0, -1.0
        rb = (d1.read_bytes - d0.read_bytes) / dt / (1024 * 1024)
        wb = (d1.write_bytes - d0.write_bytes) / dt / (1024 * 1024)
        # busy% from the weighted (active) time delta, best-effort across platforms
        busy_ms = (d1.busy_time - d0.busy_time) if (d0.busy_time is not None and d1.busy_time is not None) else None
        if busy_ms is None:
            return -1.0, round(rb + wb, 1)
        busy_pct = busy_ms / (dt * 1000.0) * 100.0
        return round(min(100.0, max(0.0, busy_pct)), 1), round(rb + wb, 1)
    except Exception:
        return -1.0, -1.0


def _net_rate() -> tuple[float, float]:
    """(down MB/s, up MB/s) over a 250 ms window."""
    try:
        n0 = psutil.net_io_counters()
        t0 = time.perf_counter()
        time.sleep(0.25)
        n1 = psutil.net_io_counters()
        dt = max(1e-6, time.perf_counter() - t0)
        down = max(0.0, (n1.bytes_recv - n0.bytes_recv)) / dt / (1024 * 1024)
        up = max(0.0, (n1.bytes_sent - n0.bytes_sent)) / dt / (1024 * 1024)
        return round(down, 2), round(up, 2)
    except Exception:
        return -1.0, -1.0


def _swap() -> dict:
    try:
        s = psutil.swap_memory()
        return {"percent": round(s.percent, 1), "used_gb": _gb(s.used), "total_gb": _gb(s.total)}
    except Exception:
        return {"percent": -1.0, "used_gb": -1.0, "total_gb": -1.0}


# ── process scan ─────────────────────────────────────────────────────────────

def _scan_processes(top: int = 6) -> dict:
    """Top CPU + top RAM consumers with streak tracking (not just one sample).

    CPU% here is percent of *total* machine capacity, matching Task Manager.
    """
    samples: list[tuple[int, str, float, float]] = []
    handles = []
    for p in psutil.process_iter(["pid", "name"]):
        try:
            p.cpu_percent(None)          # prime the counter
            handles.append(p)
        except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
            continue
    time.sleep(0.35)                     # real measurement window
    ncpu = max(1, psutil.cpu_count() or 1)
    for p in handles:
        try:
            with p.oneshot():
                info = p.as_dict(attrs=["pid", "name", "memory_info", "cpu_percent", "status"])
            name = (info.get("name") or "").lower()
            cpu_total = float(info.get("cpu_percent") or 0.0) / ncpu
            rss = info.get("memory_info").rss if info.get("memory_info") else 0
            samples.append((info["pid"], name, cpu_total, rss))
        except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
            continue
    by_cpu = sorted(samples, key=lambda s: s[2], reverse=True)[:top + 2]
    # Windows reports idle time as a fake process — never a consumer.
    by_cpu = [r for r in by_cpu if r[0] != 0 and "system idle" not in r[1]][:top]
    by_ram = sorted(samples, key=lambda s: s[3], reverse=True)[:top]
    return {
        "by_cpu": by_cpu,
        "by_ram": by_ram,
        "cpu_line": _format_proc_list(by_cpu, limit=2),
        "ram_line": _format_proc_list(by_ram, limit=2, ram_mode=True),
    }


def _format_proc_list(rows, limit: int = 4, ram_mode: bool = False) -> str:
    lines = []
    for pid, name, cpu, rss in rows:
        # Windows' "System Idle Process" reports idle time, not load —
        # filtered at the presentation boundary so every listing is clean.
        if pid == 0 or "system idle" in name:
            continue
        if ram_mode:
            lines.append(f"{name} (pid {pid}) — {_mb(rss)} MB")
        else:
            lines.append(f"{name} (pid {pid}) — {cpu:.0f}% CPU, {_mb(rss)} MB")
        if len(lines) >= limit:
            break
    return "; ".join(lines) if lines else "none"


def _is_jarvis(name: str) -> bool:
    n = name.lower()
    return any(s in n for s in _JARVIS_SUBSTRINGS)


# ── bottleneck classification (§28) ──────────────────────────────────────────

def _classify(m: dict) -> tuple[str, str]:
    """Pick the dominant bottleneck. Returns (bottleneck, explanation)."""
    cpu, ram = m["cpu"], m["ram"]
    gpu, disk = m["gpu"], m["disk_busy"]
    swap = m["swap"]["percent"]

    # RAM exhaustion outranks CPU: swapping makes everything slow at once.
    if ram >= 92 or swap >= 60:
        why = f"RAM {ram:.0f}% used"
        if swap > 0:
            why += f", pagefile {swap:.0f}% used"
        why += " — the machine is at its memory limit"
        return "RAM", why
    if disk >= 97:
        return "DISK", f"disk {disk:.0f}% busy — everything waits on storage"
    if cpu >= 85:
        return "CPU", f"CPU {cpu:.0f}% — compute-bound"
    if gpu >= 95:
        return "GPU", f"GPU {gpu:.0f}% — rendering/compute saturated"
    if disk >= 85:
        return "DISK", f"disk {disk:.0f}% busy with {m['disk_rate']:.0f} MB/s traffic"
    if ram >= 80:
        return "RAM", f"RAM {ram:.0f}% — getting tight but not exhausted"
    if cpu >= 65:
        return "CPU", f"CPU {cpu:.0f}% — moderately busy"
    return "NONE", "no single resource is saturated"


def _headline(m: dict, bottleneck: str, why: str, procs: dict | None) -> str:
    if bottleneck == "NONE":
        # User still perceives lag — say what was checked and offer the likely
        # non-resource causes instead of dismissing the report.
        disk_txt = (f"disk {m['disk_busy']:.0f}% busy"
                    if m["disk_busy"] >= 0 else "disk OK")
        return ("No single resource is saturated "
                f"(CPU {m['cpu']:.0f}%, RAM {m['ram']:.0f}%, {disk_txt}). The lag "
                "may come from a specific app, thermal throttling, or the "
                "network — say 'scan processes' and I will look at the busiest "
                "applications.")
    part = f" — busiest: {procs['cpu_line']}" if (bottleneck == "CPU" and procs) else ""
    part = f" — largest: {procs['ram_line']}" if (bottleneck == "RAM" and procs) else part
    return f"{bottleneck} bottleneck: {why}{part}"


# ── JARVIS self-scan (§28 "JARVIS SCAN") ─────────────────────────────────────

def _jarvis_scan() -> tuple[list[str], list[str]]:
    """(suspect_lines, harmless_lines)."""
    suspects, harmless = [], []
    try:
        cur = _proc
        with cur.oneshot():
            rss = cur.memory_info().rss
            cpu = cur.cpu_percent(None) / max(1, psutil.cpu_count() or 1)
        # children: vision workers, wake-word thread process, UI threads
        kids = cur.children(recursive=True)
        for k in kids:
            try:
                with k.oneshot():
                    name = (k.name() or "").lower()
                    kcpu = k.cpu_percent(None) / max(1, psutil.cpu_count() or 1)
                    krss = k.memory_info().rss
                tag = f"JARVIS {name} (pid {k.pid})"
                if kcpu > _JARVIS_CPU_LIMIT:
                    suspects.append(f"{tag} — {kcpu:.0f}% CPU")
                elif krss > _JARVIS_RAM_LIMIT_MB * 1024 * 1024:
                    suspects.append(f"{tag} — {_mb(krss)} MB RAM")
                else:
                    harmless.append(tag)
            except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
                continue
        if rss > _JARVIS_RAM_LIMIT_MB * 1024 * 1024:
            suspects.append(f"JARVIS main process — {_mb(rss)} MB RAM (limit {_JARVIS_RAM_LIMIT_MB} MB)")
        if cpu > _JARVIS_CPU_LIMIT:
            suspects.append(f"JARVIS main process — {cpu:.0f}% CPU at idle-check time")
        if not suspects and kids:
            harmless.append(f"JARVIS main process — {_mb(rss)} MB, {len(kids)} worker(s)")
    except Exception as exc:
        suspects.append(f"JARVIS self-scan incomplete ({type(exc).__name__})")
    return suspects, harmless


# ── report assembly ──────────────────────────────────────────────────────────

def _measure() -> dict:
    cpu = psutil.cpu_percent(interval=0.3)
    ram = psutil.virtual_memory()
    disk_busy, disk_rate = _disk_busy()
    down, up = _net_rate()
    m = {
        "cpu": round(cpu, 1),
        "ram": round(ram.percent, 1),
        "ram_used_gb": _gb(ram.used),
        "ram_total_gb": _gb(ram.total),
        "swap": _swap(),
        "gpu": _gpu_percent(),
        "disk_busy": disk_busy,
        "disk_rate": disk_rate,
        "down": down, "up": up,
        "procs": len(psutil.pids()),
        "loadavg": "",
    }
    # Unmeasurable counters must read as unavailable, not as -1.
    if disk_busy < 0:
        m["disk_busy"] = -1.0
    return m
    try:
        la = os.getloadavg()  # POSIX only
        m["loadavg"] = f", loadavg {la[0]:.1f}/{la[1]:.1f}/{la[2]:.1f}"
    except (AttributeError, OSError):
        pass
    return m


def scan(scope: str = "all") -> dict:
    """Run one diagnosis. scope: all | quick | processes | jarvis."""
    scope = (scope or "all").strip().lower()
    t0 = time.perf_counter()

    if scope == "jarvis":
        suspects, harmless = _jarvis_scan()
        return {
            "scope": "jarvis",
            "suspects": suspects,
            "harmless": harmless,
            "elapsed_s": round(time.perf_counter() - t0, 2),
        }

    m = _measure()
    result: dict = {"scope": scope, "metrics": m}

    if scope == "processes":
        procs = _scan_processes()
        result["processes"] = procs
        result["text"] = (
            "PROCESS SCAN\n"
            f"Busiest by CPU: {_format_proc_list(procs['by_cpu'])}\n"
            f"Largest by RAM: {_format_proc_list(procs['by_ram'], ram_mode=True)}"
        )
        with _recent_lock:
            _recent.append((time.strftime("%H:%M:%S"), "PROCESS", "process scan"))
        return result

    # full / quick — classify the bottleneck
    bottleneck, why = _classify(m)
    procs = None
    suspects = []
    if scope == "all":
        procs = _scan_processes()
        result["processes"] = procs
        js, jh = _jarvis_scan()
        suspects = js
        result["jarvis"] = {"suspects": js, "harmless": jh}

    headline = _headline(m, bottleneck, why, procs)

    # suggestions: only safe ones, each with an owner — nothing is auto-applied
    sugg: list[str] = []
    if bottleneck in ("RAM", "DISK", "CPU") and procs:
        top_cpu_name = procs["by_cpu"][0][1] if procs["by_cpu"] else ""
        top_ram_name = procs["by_ram"][0][1] if procs["by_ram"] else ""
        if bottleneck == "RAM" and top_ram_name and not _is_jarvis(top_ram_name):
            sugg.append(f"Close or restart {top_ram_name} — it holds the most memory "
                        f"({_mb(procs['by_ram'][0][3])} MB). I have not touched it.")
        elif bottleneck == "RAM":
            sugg.append("JARVIS itself is holding the most memory — restarting "
                        "JARVIS would free it. I have not done anything.")
        if bottleneck in ("CPU", "RAM", "DISK") and top_cpu_name and not _is_jarvis(top_cpu_name):
            sugg.append(f"{top_cpu_name} is the busiest process "
                        f"({procs['by_cpu'][0][2]:.0f}% CPU). Say the word and I "
                        "will list what it is; closing it is your call.")
    if m["swap"]["percent"] >= 40:
        sugg.append("The pagefile is heavily used — closing memory-hungry apps "
                    "will help more than any setting change.")
    if m["gpu"] > 90:
        sugg.append("GPU is saturated — lower the app/game resolution or close "
                    "GPU-heavy windows.")
    if suspects:
        sugg.append("JARVIS self-scan: " + "; ".join(suspects) +
                    " — restarting JARVIS may help.")
    if not sugg:
        sugg.append("No safe optimization stands out right now — the numbers "
                    "look normal for this machine.")

    result["bottleneck"] = bottleneck
    result["why"] = why
    result["headline"] = headline
    result["suggestions"] = sugg
    result["elapsed_s"] = round(time.perf_counter() - t0, 2)

    with _recent_lock:
        _recent.append((time.strftime("%H:%M:%S"), bottleneck, headline))
    return result


def format_report(res: dict) -> str:
    """Text the assistant can read out — every number is a real measurement."""
    scope = res.get("scope", "all")
    if scope == "jarvis":
        s, h = res.get("suspects", []), res.get("harmless", [])
        if s:
            return "JARVIS SELF-SCAN — suspects:\n" + "\n".join(f"- {x}" for x in s)
        return ("JARVIS SELF-SCAN — clean. " +
                ("; ".join(h) if h else "No workers running."))
    if scope == "processes":
        return res.get("text", "process scan produced nothing")

    m = res["metrics"]
    lines = ["PERFORMANCE SCAN"]
    lines.append(
        f"CPU {m['cpu']}%  |  RAM {m['ram']}% ({m['ram_used_gb']}/{m['ram_total_gb']} GB)"
        + (f"  |  swap {m['swap']['percent']}%" if m["swap"]["percent"] >= 0 else "")
    )
    gpu_txt = "—" if m["gpu"] < 0 else format(m["gpu"], ".0f") + "%"
    disk_txt = (f"{m['disk_busy']:.0f}% busy ({m['disk_rate']:.0f} MB/s)"
                if m["disk_busy"] >= 0 else "n/a on this system")
    lines.append(
        f"GPU {gpu_txt}  |  disk {disk_txt}"
        f"  |  net ↓{m['down']:.1f} ↑{m['up']:.1f} MB/s"
        f"  |  {m['procs']} processes{m['loadavg']}"
    )
    lines.append(f"VERDICT: {res['headline']}")
    sugg = res.get("suggestions") or []
    if sugg:
        lines.append("SAFE NEXT STEPS (nothing has been changed):")
        lines.extend(f"- {x}" for x in sugg)
    if res.get("processes"):
        p = res["processes"]
        lines.append(f"Busiest by CPU: {_format_proc_list(p['by_cpu'], limit=3)}")
        lines.append(f"Largest by RAM: {_format_proc_list(p['by_ram'], limit=3, ram_mode=True)}")
    return "\n".join(lines)


def history() -> list[tuple[str, str, str]]:
    with _recent_lock:
        return list(_recent)


def handle_performance(args: dict | None = None) -> str:
    """Single entry for the performance_scan tool. Never raises."""
    a = dict(args or {})
    mode = str(a.get("mode") or "scan").strip().lower()
    scope = str(a.get("scope") or "all").strip().lower()
    try:
        if mode in ("scan", "all", "full", "diagnose", "lag", "why slow"):
            if scope in ("quick",):
                scope = "quick"
            return format_report(scan(scope))
        if mode in ("processes", "process", "apps"):
            return format_report(scan("processes"))
        if mode in ("jarvis", "self", "self_scan", "self-scan"):
            return format_report(scan("jarvis"))
        if mode in ("history", "recent"):
            items = history()
            if not items:
                return "No performance scans have run yet in this session."
            return "RECENT SCANS\n" + "\n".join(f"- {t} — {b}: {h}" for t, b, h in items[-8:])
        return ("Unknown performance mode. Use scan, processes, jarvis or history.")
    except Exception as exc:
        return f"Performance scan failed: {type(exc).__name__}: {exc}"


__all__ = ["scan", "format_report", "handle_performance", "history"]
