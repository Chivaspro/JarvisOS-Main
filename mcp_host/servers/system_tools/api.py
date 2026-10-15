"""Tool implementations for the System Tools MCP server."""

from __future__ import annotations

import os
import platform
import socket
import sys
import time
from typing import Any

from mcp_host.server import Tool

try:
    import psutil
except ImportError:
    psutil = None


def _get_system_metrics(args: dict) -> dict:
    """CPU, memory, disk, and OS statistics."""
    info: dict[str, Any] = {
        "platform": platform.platform(),
        "python_version": sys.version.split()[0],
        "processor": platform.processor() or platform.machine(),
        "hostname": socket.gethostname(),
    }
    if psutil:
        try:
            info["cpu_percent"] = psutil.cpu_percent(interval=0.1)
            info["cpu_count_logical"] = psutil.cpu_count(logical=True)
            info["cpu_count_physical"] = psutil.cpu_count(logical=False)
            
            mem = psutil.virtual_memory()
            info["memory_total_gb"] = round(mem.total / (1024 ** 3), 2)
            info["memory_used_gb"] = round(mem.used / (1024 ** 3), 2)
            info["memory_available_gb"] = round(mem.available / (1024 ** 3), 2)
            info["memory_percent"] = mem.percent

            disk = psutil.disk_usage(os.path.abspath(os.sep))
            info["disk_total_gb"] = round(disk.total / (1024 ** 3), 2)
            info["disk_used_gb"] = round(disk.used / (1024 ** 3), 2)
            info["disk_free_gb"] = round(disk.free / (1024 ** 3), 2)
            info["disk_percent"] = disk.percent

            boot_time = psutil.boot_time()
            uptime_s = time.time() - boot_time
            info["uptime_hours"] = round(uptime_s / 3600, 2)

            battery = psutil.sensors_battery()
            if battery:
                info["battery_percent"] = battery.percent
                info["power_plugged"] = battery.power_plugged
        except Exception as exc:
            info["metric_error"] = str(exc)
    return info


def _list_processes(args: dict) -> list[dict]:
    """List active running processes with resource usage."""
    if not psutil:
        return [{"error": "psutil library not available"}]
    limit = max(1, min(int(args.get("limit", 20)), 100))
    filter_name = str(args.get("filter_name", "")).strip().lower()
    sort_by = str(args.get("sort_by", "memory")).strip().lower()

    procs = []
    for p in psutil.process_iter(["pid", "name", "memory_info", "cpu_percent"]):
        try:
            pinfo = p.info
            name = str(pinfo.get("name") or "")
            if filter_name and filter_name not in name.lower():
                continue
            mem_bytes = getattr(pinfo.get("memory_info"), "rss", 0) if pinfo.get("memory_info") else 0
            procs.append({
                "pid": pinfo.get("pid"),
                "name": name,
                "memory_mb": round(mem_bytes / (1024 * 1024), 1),
                "cpu_percent": pinfo.get("cpu_percent") or 0.0,
            })
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            pass

    if sort_by == "cpu":
        procs.sort(key=lambda x: x["cpu_percent"], reverse=True)
    else:
        procs.sort(key=lambda x: x["memory_mb"], reverse=True)

    return procs[:limit]


def _kill_process(args: dict) -> dict:
    """Terminate a process by PID."""
    if not psutil:
        return {"ok": False, "error": "psutil library not available"}
    try:
        pid = int(args.get("pid", 0))
    except (ValueError, TypeError):
        return {"ok": False, "error": "Invalid PID"}
    if pid <= 4:
        return {"ok": False, "error": "Cannot terminate system process PID <= 4"}
    try:
        p = psutil.Process(pid)
        p_name = p.name()
        p.terminate()
        return {"ok": True, "message": f"Terminated process {p_name} (PID {pid})"}
    except psutil.NoSuchProcess:
        return {"ok": False, "error": f"No process found with PID {pid}"}
    except psutil.AccessDenied:
        return {"ok": False, "error": f"Access denied terminating PID {pid}"}
    except Exception as exc:
        return {"ok": False, "error": str(exc)}


def _get_clipboard(args: dict) -> dict:
    """Read the current system clipboard text."""
    try:
        import win32clipboard  # type: ignore[import-not-found]
        win32clipboard.OpenClipboard()
        try:
            if win32clipboard.IsClipboardFormatAvailable(win32clipboard.CF_UNICODETEXT):
                data = win32clipboard.GetClipboardData(win32clipboard.CF_UNICODETEXT)
                return {"text": data, "length": len(data)}
            return {"text": "", "detail": "Clipboard does not contain text"}
        finally:
            win32clipboard.CloseClipboard()
    except Exception:
        pass

    try:
        import ctypes
        user32 = ctypes.windll.user32
        kernel32 = ctypes.windll.kernel32
        if user32.OpenClipboard(None):
            try:
                handle = user32.GetClipboardData(13)  # CF_UNICODETEXT = 13
                if handle:
                    kernel32.GlobalLock.restype = ctypes.c_wchar_p
                    text = kernel32.GlobalLock(handle)
                    kernel32.GlobalUnlock(handle)
                    return {"text": text or "", "length": len(text or "")}
                return {"text": "", "detail": "Clipboard empty or not text"}
            finally:
                user32.CloseClipboard()
    except Exception as exc:
        return {"error": f"Failed reading clipboard: {exc}"}
    return {"text": "", "detail": "Clipboard reader unavailable"}


def _set_clipboard(args: dict) -> dict:
    """Write text to the system clipboard."""
    text = str(args.get("text", ""))
    try:
        import ctypes
        user32 = ctypes.windll.user32
        kernel32 = ctypes.windll.kernel32
        if user32.OpenClipboard(None):
            try:
                user32.EmptyClipboard()
                encoded = text.encode("utf-16le") + b"\x00\x00"
                h_mem = kernel32.GlobalAlloc(0x0042, len(encoded))  # GMEM_MOVEABLE | GMEM_ZEROINIT
                if h_mem:
                    p_mem = kernel32.GlobalLock(h_mem)
                    ctypes.memmove(p_mem, encoded, len(encoded))
                    kernel32.GlobalUnlock(h_mem)
                    user32.SetClipboardData(13, h_mem)  # CF_UNICODETEXT
                    return {"ok": True, "length": len(text)}
            finally:
                user32.CloseClipboard()
    except Exception as exc:
        return {"ok": False, "error": str(exc)}
    return {"ok": False, "error": "Could not access clipboard"}


def _show_notification(args: dict) -> dict:
    """Display a system desktop notification."""
    title = str(args.get("title", "J.A.R.V.I.S.")).strip()
    message = str(args.get("message", "")).strip()
    if not message:
        return {"ok": False, "error": "Notification message cannot be empty"}
    try:
        import subprocess
        ps_script = (
            f'[reflection.assembly]::loadwithpartialname("System.Windows.Forms");'
            f'$notify = New-Object System.Windows.Forms.NotifyIcon;'
            f'$notify.Icon = [System.Drawing.SystemIcons]::Information;'
            f'$notify.Visible = $true;'
            f'$notify.ShowBalloonTip(5000, "{title}", "{message}", [System.Windows.Forms.ToolTipIcon]::Info);'
        )
        subprocess.Popen(["powershell", "-NoProfile", "-Command", ps_script],
                         creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
        return {"ok": True, "detail": "Notification dispatched"}
    except Exception as exc:
        return {"ok": False, "error": str(exc)}


def _get_network_info(args: dict) -> dict:
    """Return local host IP and network details."""
    hostname = socket.gethostname()
    ips = []
    try:
        ips.append(socket.gethostbyname(hostname))
    except Exception:
        pass
    adapters = []
    if psutil:
        try:
            addrs = psutil.net_if_addrs()
            for iface, addr_list in addrs.items():
                for addr in addr_list:
                    if addr.family == socket.AF_INET:
                        adapters.append({"interface": iface, "ip": addr.address, "netmask": addr.netmask})
        except Exception:
            pass
    return {
        "hostname": hostname,
        "default_ip": ips[0] if ips else "127.0.0.1",
        "interfaces": adapters,
    }


TOOLS: list[Tool] = [
    Tool(
        name="getSystemMetrics",
        description="Retrieve CPU percent, memory usage, disk space, battery and system uptime.",
        input_schema={"type": "object", "properties": {}},
        handler=_get_system_metrics,
    ),
    Tool(
        name="listProcesses",
        description="List active running processes with PID, memory (MB), and CPU percentage.",
        input_schema={
            "type": "object",
            "properties": {
                "limit": {"type": "integer", "description": "Maximum number of processes to return (default 20)"},
                "sort_by": {"type": "string", "enum": ["memory", "cpu"], "description": "Sort order (default 'memory')"},
                "filter_name": {"type": "string", "description": "Optional name filter for process name"},
            },
        },
        handler=_list_processes,
    ),
    Tool(
        name="killProcess",
        description="Terminate a running process by its PID. Destructive operation.",
        input_schema={
            "type": "object",
            "properties": {
                "pid": {"type": "integer", "description": "Process ID to terminate"},
            },
            "required": ["pid"],
        },
        handler=_kill_process,
    ),
    Tool(
        name="getClipboard",
        description="Read the current text from the Windows clipboard.",
        input_schema={"type": "object", "properties": {}},
        handler=_get_clipboard,
    ),
    Tool(
        name="setClipboard",
        description="Copy text into the Windows system clipboard.",
        input_schema={
            "type": "object",
            "properties": {
                "text": {"type": "string", "description": "Text to set on the clipboard"},
            },
            "required": ["text"],
        },
        handler=_set_clipboard,
    ),
    Tool(
        name="showNotification",
        description="Display a desktop toast / balloon notification.",
        input_schema={
            "type": "object",
            "properties": {
                "title": {"type": "string", "description": "Notification title"},
                "message": {"type": "string", "description": "Notification message body"},
            },
            "required": ["message"],
        },
        handler=_show_notification,
    ),
    Tool(
        name="getNetworkInfo",
        description="Get hostname, local IP addresses, and network interface adapters.",
        input_schema={"type": "object", "properties": {}},
        handler=_get_network_info,
    ),
]
