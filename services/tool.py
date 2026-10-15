"""The ``service_hub`` tool — how the assistant answers "is that link working?".

``status`` lists every external service *and* every provider key with its honest
state (taken from the cheap, offline check), and ``test`` performs one real
request against a single link or key, exactly like the TEST button in
Settings ▸ API. A credential can be named by its row id (``openai``) or by the
config field that holds it (``openai_api_key``), so "is my OpenAI key still
good?" is one request away. The tool never holds a credential itself: it reads
the same configuration the Settings window writes and reports what came back.
"""

from __future__ import annotations

from services import hub

__all__ = ["handler"]

#: The names this tool accepts are read from the hub on every call, so the
#: declaration in main.py can list them without a second table to keep in step.


def _known_names() -> str:
    return ("Links: " + ", ".join(service.id for service in hub.SERVICES)
            + ". Keys: " + ", ".join(provider.id for provider in hub.PROVIDERS) + ".")


def handler(args: dict | None = None, player=None) -> str:
    args = dict(args or {})
    action = str(args.get("action") or "status").strip().lower()
    service_id = str(args.get("service") or "").strip().lower()
    field = str(args.get("field") or "").strip().lower()

    if action in ("status", "list", "summary"):
        lines = [hub.summary()]
        lines.append("Say 'test' for one of them and I will make a real request.")
        return "\n".join(lines)

    if action in ("test", "probe", "check"):
        target = service_id or field
        if not target:
            return "Which link shall I test, sir? " + _known_names()
        # A config field is a perfectly good way to name a credential, so resolve
        # it to the row that knows how to test it ("is my OpenAI key good?").
        if not service_id and field:
            meta = hub.key_meta(field)
            if meta.get("test"):
                target = str(meta["test"])
        result = hub.probe_target(target)
        if result.get("state") == "unknown":
            return f"{result.get('detail')}\n{_known_names()}"
        line = f"{result.get('label') or target}: {result.get('state')}"
        if result.get("elapsed_s") is not None:
            line += f" ({result['elapsed_s']}s)"
        return f"{line}.\n{result.get('detail') or ''}".strip()

    return (f"'{action}' is not a service-hub action, sir. Use 'status' to list "
            f"every link or 'test' with a service name to probe one. "
            + _known_names())
