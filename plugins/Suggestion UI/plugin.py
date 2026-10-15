
PLUGIN = {
    "name": "show_suggestions",
    "description": (
        "Show interactive choices in the JARVIS chat when the user needs to choose between options. "
        "Always display one recommended option."
    ),
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "question": {"type": "STRING", "description": "Question shown above the choices."},
            "options": {"type": "ARRAY", "items": {"type": "STRING"}, "description": "Choice labels."},
            "recommended_index": {"type": "INTEGER", "description": "Zero-based recommended option; defaults to 0."}
        },
        "required": ["question", "options"]
    }
}

def run(parameters: dict, player=None, session_memory=None) -> str:
    try:
        p = parameters or {}
        q = str(p.get("question", "Which option would you like?")).strip()
        opts = [str(x).strip() for x in (p.get("options") or []) if str(x).strip()]
        if not opts: return "No suggestion choices were provided."
        try: idx = int(p.get("recommended_index", 0))
        except Exception: idx = 0
        idx = max(0, min(idx, len(opts) - 1))
        fn = getattr(player, "show_suggestions", None) if player else None
        if callable(fn): fn(q, opts, idx)
        return "I presented the choices in chat."
    except Exception as exc:
        try: player.write_log(f"ERR: Suggestion UI — {exc}")
        except Exception: pass
        return f"Suggestion UI error: {exc}"
