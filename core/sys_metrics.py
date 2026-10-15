"""The one system-metrics sampler for this process.

The HUD shows CPU / RAM / network / GPU / temperature a couple of times a
second, and every reading is native work (psutil, NVML, WMI), so it cannot run
on the UI thread.

It must not run *many* times either. The sampler used to be started by a
module-level ``_SysMetrics()`` inside ``ui.py`` — and ``ui.py`` is executed more
than once in a single process: the layout editor loads it, and the test tree
loads the file by path under an alias, once per test module. Each execution
created its own sampler thread, every thread polled psutil/NVML/WMI every
1.5 seconds, and none of them ever stopped. A test session ended up with
hundreds of them firing thousands of native calls a second, which reads as lag
and eventually ends the process with a native fail-fast (exit ``0xC0000409``,
no traceback, no summary) that took a whole test run down with it.

``shared_metrics()`` returns exactly one sampler per process: repeated
executions of ``ui.py`` share this module through ``sys.modules``, so they share
its instance and its thread.

``actions/system_monitor.py`` reads the same numbers for the ``system_status``
tool and owns the COM-safe, cached WMI probe used here.

psutil is optional on purpose. The HUD readouts are not the reason J.A.R.V.I.S
starts, so a missing psutil must never be the reason it fails to: without it
every reading keeps its neutral default (CPU/RAM 0, GPU/temperature -1, uptime
0), ``snapshot()`` reports ``available: False`` and the HUD shows dashes. This
used to be an unguarded ``import psutil`` that could abort the whole app during
startup on an interpreter without the project's packages.
"""
from __future__ import annotations

import platform
import threading
import time

try:
    import psutil
except Exception:  # pragma: no cover - requirements.txt ships it; never fatal
    psutil = None


class _NetCounters:
    """Stand-in for psutil's network counters when psutil is unavailable."""

    bytes_sent = 0
    bytes_recv = 0

_OS = platform.system()
_POLL_SECONDS = 1.5

_nvml_lib: object = None
_nvml_ok: object = None

#: The pynvml probe runs ONCE per process. Its failure is final — GPU NVML is a
#: native library: either it is present for this driver or it is not, and no
#: amount of re-probing on a 1.5 s poll changes that. Before this flag, every
#: poll re-ran ``pynvml.nvmlInit()`` (loading the native library each time) just
#: to swallow the same exception, and repeated native init/teardown on a
#: mismatched driver is exactly the kind of call that ends a process with an
#: access violation instead of a Python error.
_pynvml_ok: bool | None = None
_pynvml_lock = threading.Lock()


def _pynvml_gpu() -> float:
    """GPU utilisation via pynvml, probed at most once per process."""
    global _pynvml_ok
    if _pynvml_ok is False:
        return -1.0
    with _pynvml_lock:
        if _pynvml_ok is False:
            return -1.0
        try:
            import pynvml  # type: ignore
            pynvml.nvmlInit()
            h = pynvml.nvmlDeviceGetHandleByIndex(0)
            value = float(pynvml.nvmlDeviceGetUtilizationRates(h).gpu)
            _pynvml_ok = True
            return value
        except Exception:
            _pynvml_ok = False
            return -1.0


def _nvml_gpu_windows() -> float:
    """GPU utilisation in percent, or -1.0 when NVML is unavailable.

    A machine without the NVIDIA library is remembered in ``_nvml_ok`` so the
    loader is only ever tried once per process.
    """
    global _nvml_lib, _nvml_ok
    if _nvml_ok is False:
        return -1.0
    try:
        import ctypes

        class _Util(ctypes.Structure):
            _fields_ = [("gpu", ctypes.c_uint), ("memory", ctypes.c_uint)]

        if _nvml_lib is None:
            for dll_name in ("nvml", r"C:\Windows\System32\nvml.dll"):
                try:
                    lib = ctypes.WinDLL(dll_name)
                    lib.nvmlInit_v2()
                    _nvml_lib = lib
                    break
                except Exception:
                    continue

        if _nvml_lib is None:
            import pynvml  # type: ignore
            pynvml.nvmlInit()
            h = pynvml.nvmlDeviceGetHandleByIndex(0)
            _nvml_ok = True
            return float(pynvml.nvmlDeviceGetUtilizationRates(h).gpu)

        dev = ctypes.c_void_p()
        _nvml_lib.nvmlDeviceGetHandleByIndex_v2(0, ctypes.byref(dev))
        util = _Util()
        _nvml_lib.nvmlDeviceGetUtilizationRates(dev, ctypes.byref(util))
        _nvml_ok = True
        return float(util.gpu)
    except Exception:
        _nvml_ok = False
        return -1.0


class SysMetrics:
    """Background sampler for the HUD readouts.

    Build one through :func:`shared_metrics`; the constructor starts the poll
    thread, so a second instance is a second thread by definition.
    """

    def __init__(self) -> None:
        self.cpu = 0.0
        self.mem = 0.0
        self.net = 0.0
        self.net_down = 0.0
        self.net_up = 0.0
        self.gpu = -1.0
        self.tmp = -1.0
        self.disk = 0.0
        self.batt = -1.0
        self.plugged = False
        self.uptime = 0
        self.procs = 0
        self._lock = threading.Lock()
        self._last_net = (psutil.net_io_counters() if psutil is not None
                          else _NetCounters())
        self._last_net_t = time.time()
        self._running = True
        t = threading.Thread(target=self._loop, name="jarvis-sys-metrics",
                             daemon=True)
        t.start()

    def _loop(self) -> None:
        while self._running:
            try:
                self._update()
            except Exception:
                pass
            time.sleep(_POLL_SECONDS)

    def _update(self) -> None:
        if psutil is None:
            # Nothing to sample with. Every field keeps its neutral default and
            # the HUD renders dashes instead of the app dying at import time.
            return

        cpu = psutil.cpu_percent(interval=None)
        mem = psutil.virtual_memory().percent

        nc = psutil.net_io_counters()
        now = time.time()
        dt = now - self._last_net_t
        if dt > 0:
            sent = (nc.bytes_sent - self._last_net.bytes_sent) / dt
            recv = (nc.bytes_recv - self._last_net.bytes_recv) / dt
            net_down = recv / (1024 * 1024)
            net_up = sent / (1024 * 1024)
            net = (sent + recv) / (1024 * 1024)
        else:
            net = net_down = net_up = 0.0
        self._last_net = nc
        self._last_net_t = now

        gpu = self._get_gpu()
        tmp = self._get_temp()

        try:
            disk = float(psutil.disk_usage('C:\\' if _OS == "Windows" else '/').percent)
        except Exception:
            disk = 0.0
        try:
            batt_info = psutil.sensors_battery()
            batt = float(batt_info.percent) if batt_info else -1.0
            plugged = bool(batt_info.power_plugged) if batt_info else False
        except Exception:
            batt, plugged = -1.0, False
        try:
            uptime = int(time.time() - psutil.boot_time())
        except Exception:
            uptime = 0
        try:
            procs = len(psutil.pids())
        except Exception:
            procs = 0

        with self._lock:
            self.cpu = cpu
            self.mem = mem
            self.net = net
            self.net_down = net_down
            self.net_up = net_up
            self.gpu = gpu
            self.tmp = tmp
            self.disk = disk
            self.batt = batt
            self.plugged = plugged
            self.uptime = uptime
            self.procs = procs

    def _get_gpu(self) -> float:
        try:
            value = _pynvml_gpu()
            if value >= 0.0:
                return value
        except Exception:
            pass

        if _OS == "Windows":
            return _nvml_gpu_windows()

        try:
            import ctypes
            _lib = "libnvidia-ml.so.1" if _OS == "Linux" else "libnvidia-ml.dylib"

            class _Util(ctypes.Structure):
                _fields_ = [("gpu", ctypes.c_uint), ("memory", ctypes.c_uint)]

            nv = ctypes.CDLL(_lib)
            nv.nvmlInit_v2()
            dev = ctypes.c_void_p()
            nv.nvmlDeviceGetHandleByIndex_v2(0, ctypes.byref(dev))
            u = _Util()
            nv.nvmlDeviceGetUtilizationRates(dev, ctypes.byref(u))
            return float(u.gpu)
        except Exception:
            pass

        return -1.0

    def _get_temp(self) -> float:
        try:
            temps = psutil.sensors_temperatures()
            for name in ["coretemp", "k10temp", "cpu_thermal", "acpitz",
                         "cpu-thermal", "zenpower", "it8688"]:
                if name in temps and temps[name]:
                    return temps[name][0].current
            for entries in temps.values():
                if entries:
                    return entries[0].current
        except Exception:
            pass

        # Windows thermal zones are deliberately NOT read here. The only way to
        # them is win32com/WMI, and calling that from this sampler thread ends
        # the process natively (exit 0xC0000409 — a fail-fast, so there is no
        # exception to catch). It is read in snapshot() instead, on the thread
        # doing the asking. The psutil call above returns nothing on Windows
        # because the installed build has no sensors_temperatures at all.
        return -1.0

    def snapshot(self) -> dict:
        with self._lock:
            data = {
                "available": psutil is not None,
                "cpu": self.cpu,
                "mem": self.mem,
                "net": self.net,
                "net_down": self.net_down,
                "net_up": self.net_up,
                "gpu": self.gpu,
                "tmp": self.tmp,
                "disk": self.disk,
                "batt": self.batt,
                "plugged": self.plugged,
                "uptime": self.uptime,
                "procs": self.procs,
            }
        # Windows: the ACPI thermal zone is a win32com/WMI read, which the
        # sampler thread must never make (see _get_temp). Asking thread instead
        # — the HUD asks from the UI thread — is safe, and system_monitor caches
        # the value and remembers a missing sensor, so this costs one native
        # read per 15 s at most.
        if _OS == "Windows":
            try:
                from actions.system_monitor import wmi_thermal_temp
                data["tmp"] = float(wmi_thermal_temp())
            except Exception:
                pass
        return data


_shared: "SysMetrics | None" = None
_shared_lock = threading.Lock()


def shared_metrics() -> SysMetrics:
    """The process-wide sampler, created on first use and never duplicated."""
    global _shared
    if _shared is None:
        with _shared_lock:
            if _shared is None:
                _shared = SysMetrics()
    return _shared
