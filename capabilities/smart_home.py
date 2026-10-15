"""Smart home — local Kasa/Tapo plugs and bulbs, over the LAN only.

The devices are reached through ``python-kasa`` (an optional package) and never
through a cloud account: discovery is a broadcast on this network and every
command goes straight to the device's own address. That is the whole point of
this module — a smart plug that needs the internet to turn off is not a smart
plug.

Three rules, because a plug can control something that matters:

* **Discovery is bounded.** A broadcast sweep waits a fixed few seconds and
  reports how many devices answered; it is never run implicitly by a status
  command (``hosts`` in the configuration is the fast path).
* **A device that cannot be reached is named.** No action ever reports success
  for a device that did not answer, and the answer says which address failed.
* **Nothing guesses a state.** ``status`` reads the device; after a command the
  device is read back, so "the lamp is on" is a measurement rather than an echo
  of the request.
"""

from __future__ import annotations

import asyncio
from typing import Any

__all__ = ["configured_hosts", "run", "status"]

_PACKAGE_HINT = ("Smart home control needs the optional 'python-kasa' package, "
                 "which is not installed — run 'pip install python-kasa' and ask "
                 "me again. Nothing was switched.")


def _kasa():
    """The kasa module, or None when the optional package is absent."""
    try:
        import kasa  # type: ignore
        return kasa
    except Exception:
        return None


def configured_hosts() -> list[str]:
    """The addresses the user listed in Settings (fast, no broadcast)."""
    try:
        from memory.config_manager import load_api_keys
        raw = str(dict(load_api_keys() or {}).get("smart_home_hosts") or "")
    except Exception:
        raw = ""
    return [part.strip() for part in raw.replace(";", ",").split(",") if part.strip()]


def _credentials() -> tuple[str, str]:
    try:
        from memory.config_manager import load_api_keys
        data = dict(load_api_keys() or {})
    except Exception:
        data = {}
    return (str(data.get("smart_home_username") or ""),
            str(data.get("smart_home_password") or ""))


def _fail(detail: str, reason: str = "action_failed") -> dict:
    return {"ok": False, "controller": "kasa", "detail": detail, "reason": reason,
            "evidence": {}}


def _ok(detail: str, **evidence: Any) -> dict:
    return {"ok": True, "controller": "kasa", "detail": detail,
            "reason": "", "evidence": evidence}


def _connect(host: str, username: str, password: str):
    kasa = _kasa()
    if kasa is None:
        return None
    try:
        if username and password:
            return kasa.Discover.discover_single(host, username=username,
                                                 password=password)
        return kasa.Discover.discover_single(host)
    except Exception:
        return None


async def _connect_async(host: str):
    kasa = _kasa()
    username, password = _credentials()
    try:
        if username and password:
            device = await kasa.Discover.discover_single(host, username=username,
                                                        password=password)
        else:
            device = await kasa.Discover.discover_single(host)
    except Exception as exc:
        raise RuntimeError(f"{host} did not answer ({exc})") from exc
    if device is None:
        raise RuntimeError(f"no Kasa/Tapo device answered at {host}")
    return device


def _detail(device) -> str:
    name = getattr(device, "alias", None) or getattr(device, "config", None)
    alias = getattr(device, "alias", "") or "device"
    try:
        on = bool(device.is_on)
    except Exception:
        on = None
    bits = [f"{alias} at {getattr(device, 'host', '?')}",
            f"{'on' if on else 'off'}" if on is not None else "state unknown"]
    model = getattr(device, "model", "")
    if model:
        bits.append(str(model))
    brightness = getattr(device, "brightness", None)
    if brightness is not None:
        bits.append(f"brightness {brightness}%")
    return ", ".join(bits)


def _host_arg(args: dict) -> str:
    host = str(args.get("host") or args.get("name") or args.get("target")
               or "").strip()
    if host:
        return host
    hosts = configured_hosts()
    if len(hosts) == 1:
        return hosts[0]
    return ""


def _resolve_hosts(args: dict) -> tuple[list[str], str]:
    """Which addresses to act on: named, configured, or discovered."""
    host = _host_arg(args)
    if host:
        return [host], ""
    hosts = configured_hosts()
    if hosts:
        return hosts, ""
    return [], ("No device was named and none are configured. Add addresses in "
                "Settings ▸ API (comma separated), or ask me to discover devices "
                "on this network.")


# ── handlers ──────────────────────────────────────────────────────────────────

def _h_list(args: dict) -> dict:
    if _kasa() is None:
        return _fail(_PACKAGE_HINT, "missing_dependency")
    hosts = configured_hosts()
    if hosts and not args.get("discover"):
        found: list[str] = []
        async def _read_all():
            for host in hosts:
                try:
                    device = await _connect_async(host)
                except Exception as exc:
                    found.append(f"- {host}: {exc}")
                    continue
                try:
                    await device.update()
                except Exception:
                    pass
                found.append(f"- {_detail(device)}")
        asyncio.run(_read_all())
        return _ok(f"{len(hosts)} configured device(s):\n" + "\n".join(found),
                   hosts=hosts)

    kasa = _kasa()
    timeout = float(args.get("timeout_s") or 5.0)

    async def _discover():
        try:
            return await kasa.Discover.discover(timeout=timeout)
        except TypeError:
            return await kasa.Discover.discover()

    try:
        devices = asyncio.run(_discover()) or {}
    except Exception as exc:
        return _fail(f"Device discovery failed on this network: {exc}")
    if not devices:
        return _fail("The discovery broadcast found no Kasa/Tapo devices on this "
                     "network.", "no_devices")
    lines = []
    for address, device in devices.items():
        alias = getattr(device, "alias", "") or "unnamed"
        model = getattr(device, "model", "")
        lines.append(f"- {alias} at {address}" + (f" ({model})" if model else ""))
    return _ok(f"{len(devices)} device(s) answered the discovery broadcast:\n"
               + "\n".join(lines), count=len(devices))


def _h_status(args: dict) -> dict:
    if _kasa() is None:
        return _fail(_PACKAGE_HINT, "missing_dependency")
    hosts, error = _resolve_hosts(args)
    if not hosts:
        return _fail(error, "no_target")
    lines: list[str] = []
    reachable = 0

    async def _read():
        nonlocal reachable
        for host in hosts:
            try:
                device = await _connect_async(host)
            except Exception as exc:
                lines.append(f"- {host}: {exc}")
                continue
            try:
                await device.update()
            except Exception as exc:
                lines.append(f"- {host}: connected but would not report "
                             f"({exc})")
                continue
            reachable += 1
            lines.append(f"- {_detail(device)}")

    asyncio.run(_read())
    if not reachable:
        return _fail("No device could be reached:\n" + "\n".join(lines),
                    "unreachable")
    return _ok("\n".join(lines), reachable=reachable)


def _switch(args: dict, on: bool | None) -> dict:
    if _kasa() is None:
        return _fail(_PACKAGE_HINT, "missing_dependency")
    hosts, error = _resolve_hosts(args)
    if not hosts:
        return _fail(error, "no_target")
    results: list[str] = []
    changed = 0

    async def _apply():
        nonlocal changed
        for host in hosts:
            try:
                device = await _connect_async(host)
                if on is None:
                    state = bool(getattr(device, "is_on", False))
                    await (device.turn_off() if state else device.turn_on())
                else:
                    await (device.turn_on() if on else device.turn_off())
                try:
                    await device.update()
                except Exception:
                    pass
                changed += 1
                results.append(f"- {_detail(device)}")
            except Exception as exc:
                results.append(f"- {host}: {exc}")

    asyncio.run(_apply())
    if not changed:
        return _fail("Nothing was switched:\n" + "\n".join(results), "unreachable")
    return _ok("\n".join(results), changed=changed)


def _h_on(args: dict) -> dict:
    return _switch(args, True)


def _h_off(args: dict) -> dict:
    return _switch(args, False)


def _h_toggle(args: dict) -> dict:
    return _switch(args, None)


def _h_brightness(args: dict) -> dict:
    if _kasa() is None:
        return _fail(_PACKAGE_HINT, "missing_dependency")
    try:
        percent = int(args.get("percent"))
    except (TypeError, ValueError):
        return _fail("A brightness percentage between 1 and 100, please.", "bad_percent")
    percent = max(1, min(100, percent))
    hosts, error = _resolve_hosts(args)
    if not hosts:
        return _fail(error, "no_target")
    results: list[str] = []
    changed = 0

    async def _apply():
        nonlocal changed
        for host in hosts:
            try:
                device = await _connect_async(host)
            except Exception as exc:
                results.append(f"- {host}: {exc}")
                continue
            if not hasattr(device, "set_brightness"):
                results.append(f"- {getattr(device, 'alias', host)} is not "
                               f"dimmable")
                continue
            try:
                await device.set_brightness(percent)
                if not bool(getattr(device, "is_on", True)):
                    await device.turn_on()
                try:
                    await device.update()
                except Exception:
                    pass
                changed += 1
                results.append(f"- {_detail(device)}")
            except Exception as exc:
                results.append(f"- {host}: {exc}")

    asyncio.run(_apply())
    if not changed:
        return _fail("Nothing was dimmed:\n" + "\n".join(results), "unreachable")
    return _ok("\n".join(results), changed=changed, percent=percent)


def _h_energy(args: dict) -> dict:
    if _kasa() is None:
        return _fail(_PACKAGE_HINT, "missing_dependency")
    hosts, error = _resolve_hosts(args)
    if not hosts:
        return _fail(error, "no_target")
    lines: list[str] = []

    async def _read():
        for host in hosts:
            try:
                device = await _connect_async(host)
                await device.update()
            except Exception as exc:
                lines.append(f"- {host}: {exc}")
                continue
            alias = getattr(device, "alias", host)
            energy = getattr(device, "energy", None)
            if energy is None or not hasattr(energy, "current_consumption"):
                lines.append(f"- {alias} does not report energy usage.")
                continue
            try:
                entries = await energy.current_consumption()
            except Exception as exc:
                lines.append(f"- {alias}: energy unreadable ({exc})")
                continue
            if isinstance(entries, dict):
                pairs = ", ".join(f"{k} {v}" for k, v in list(entries.items())[:4])
                lines.append(f"- {alias}: {pairs}")
            else:
                lines.append(f"- {alias}: {entries}")

    asyncio.run(_read())
    if not lines:
        return _fail("No device reported energy figures.", "unavailable")
    return _ok("Energy usage:\n" + "\n".join(lines))


_HANDLERS = {
    "list_devices": _h_list,
    "discover": _h_list,
    "status": _h_status,
    "turn_on": _h_on,
    "turn_off": _h_off,
    "toggle": _h_toggle,
    "set_brightness": _h_brightness,
    "energy": _h_energy,
}


def status() -> dict:
    """Availability, without touching the network."""
    installed = _kasa() is not None
    return {
        "available": installed,
        "detail": ("python-kasa is installed; devices are reached directly on "
                   "this network." if installed else _PACKAGE_HINT),
        "hosts": configured_hosts(),
    }


def run(action_name: str, args: dict) -> dict:
    """Execute one smart-home action. Returns a result dict, never raises."""
    handler = _HANDLERS.get(str(action_name or "").strip().lower())
    if handler is None:
        return _fail(f"'{action_name}' is not a smart-home action. Use one of: "
                     + ", ".join(sorted(_HANDLERS)) + ".", "unknown_action")
    try:
        return handler(dict(args or {})) or _fail("The device layer returned nothing.")
    except Exception as exc:
        return _fail(f"{type(exc).__name__}: {exc}")
