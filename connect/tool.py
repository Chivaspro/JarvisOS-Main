"""The ``connect_devices`` tool — the assistant's view of JarvisOS Connect.

Actions:

* ``status``   — is the gateway listening, who is paired, what is the pairing code
* ``start``    — begin listening (the switch and the bind address are the user's)
* ``stop``     — stop listening
* ``code``     — the current short pairing code, and the address to type it into
* ``devices``  — the paired devices and when each was last seen
* ``revoke``   — revoke one device by name or id
* ``commands`` — what a paired device is allowed to ask for

Nothing here can pair a device on its own: pairing needs the code, and the code
needs the user to read it off the HUD (or set a secret), which is the point.
"""

from __future__ import annotations

from connect import commands, devices, gateway

__all__ = ["handler"]


def _switch_on() -> bool:
    try:
        from config.settings import get_feature
        return bool(get_feature("connect", True))
    except Exception:
        return True


def _refusal() -> str:
    return ("JarvisOS Connect is switched off in Settings, so the gateway stays "
            "closed and no device may pair.")


def handler(args: dict | None = None, player=None) -> str:
    args = dict(args or {})
    action = str(args.get("action") or "status").strip().lower()
    if not _switch_on() and action not in ("status",):
        return _refusal()

    if action in ("status", "report"):
        state = gateway.status()
        lines = []
        if state["running"]:
            lines.append(f"JarvisOS Connect is listening on {state['url']} with "
                         f"{state['clients']} client(s) attached, up "
                         f"{state['uptime_s']}s.")
        else:
            lines.append(f"JarvisOS Connect is not running (it would listen on "
                         f"{state['url']}).")
        paired = state.get("paired_devices") or []
        if paired:
            lines.append(f"{len(paired)} paired device(s): " + ", ".join(
                f"{d['name']} ({d['platform']})" for d in paired))
        else:
            lines.append("No device is paired yet.")
        if not state.get("has_secret"):
            lines.append("No pairing secret is set, so pairing is refused until "
                         "one is (Settings ▸ API).")
        elif state.get("pairing_code"):
            lines.append(f"Current pairing code: {state['pairing_code']} "
                         f"(valid for five minutes).")
        for line in (state.get("log") or [])[-4:]:
            lines.append(f"  {line}")
        return "\n".join(lines)

    if action in ("start", "listen", "serve"):
        ok, message = gateway.get_gateway().start()
        if ok:
            try:
                from connect import runtime
                runtime.notify("Connect: gateway listening.")
            except Exception:
                pass
        return message

    if action in ("stop", "close"):
        return gateway.stop_gateway("asked to stop")

    if action in ("code", "pair", "pairing_code"):
        secret = devices.pairing_secret()
        if not secret:
            return ("There is no pairing secret, so I cannot produce a code. Set "
                    "one on the Settings ▸ API tab first — I will not invent one "
                    "for you.")
        code = devices.pairing_code(secret)
        host = gateway.status()["host"]
        address = (gateway.status()["url"] if host not in ("0.0.0.0", "::")
                   else f"http://{gateway.local_ip()}:{gateway.status()['port']}")
        return (f"Pairing code: {code} — it is valid for five minutes.\n"
                f"On the phone, open {address} and enter that code with a name "
                f"for the device. A paired device can then ask this machine for "
                f"status, screenshots and answers, and nothing else.")

    if action in ("devices", "list"):
        paired = devices.list_devices()
        if not paired:
            return "No device has paired with this machine yet."
        import time as _time
        lines = [f"{len(paired)} paired device(s):"]
        for device in paired:
            seen = device.get("last_seen") or 0
            when = (f"{int((_time.time() - seen) // 60)} minute(s) ago"
                    if seen else "never")
            lines.append(f"- {device['name']} ({device['platform']}), id "
                         f"{device['id']}, last seen {when}")
        return "\n".join(lines)

    if action in ("revoke", "unpair"):
        reference = str(args.get("device") or args.get("name") or "").strip()
        if not reference:
            return "Which device should I revoke? Use its name or id."
        ok, message = devices.revoke_device(reference)
        return message

    if action in ("commands", "catalogue"):
        lines = ["A paired device may ask for:"]
        for spec in commands.describe_all():
            gate = []
            if spec["permission"]:
                gate.append(spec["permission"])
            if spec["confirm"]:
                gate.append("needs on-screen confirmation")
            lines.append(f"- {spec['action']}: {spec['summary']}"
                         + (f" [{', '.join(gate)}]" if gate else ""))
        return "\n".join(lines)

    return (f"'{action}' is not a Connect action, sir. Use one of: status, start, "
            f"stop, code, devices, revoke, commands.")
