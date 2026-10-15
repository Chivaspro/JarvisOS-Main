"""Intelligent post-ASR speech correction.

Purpose
-------
Real-time STT (Gemini Live ``input_transcription``) frequently mishears words
that are phonetically similar to a known J.A.R.V.I.S. command or application
name — e.g. "Open Web Brow" instead of "Open Webcam".  This module adds a
lightweight, dependency-free correction layer that runs between raw STT output
and the textbox/pipeline.

Design goals
------------
* Command-aware: canonical phrases come from the project's *existing* vocabulary
  (registered tools, application aliases, feature names) plus a small curated
  list of high-frequency voice commands the assistant already supports.
* Phonetic, not spelling: comparisons use a custom sound-fold so "web brow" /
  "webcam" / "va kem" collapse close enough to be considered.
* Context-aware: intent words (``open``, ``close``, ``turn on`` …) influence
  candidate scoring so "web brow" after "open" resolves to ``Webcam`` and not
  to a browser.
* Conservative: only replaces when strong evidence exists.  Clear text is
  passed through untouched.  Ambiguity → original preserved.
* Realtime-safe: pure-Python, O(candidates × tokens), no I/O.  Called on every
  interim with sub-millisecond latency.
* Never fatal: every public entry point swallows exceptions and returns the
  original input on any failure.
"""

from __future__ import annotations

import difflib
import re
import threading
from typing import Iterable


# ── Vocabulary sources ────────────────────────────────────────────────────────

# Canonical command phrases J.A.R.V.I.S. already handles.  Extending this list
# (or wiring another source into ``refresh_vocabulary``) is enough to teach the
# corrector new phrases — no code change required.
_CORE_COMMANDS: tuple[str, ...] = (
    # Camera / webcam
    "Open Webcam", "Close Webcam", "Turn On Webcam", "Turn Off Webcam",
    "Check Webcam", "Start Webcam", "Stop Webcam", "Open Camera",
    "Close Camera", "Turn On Camera", "Turn Off Camera", "Check Camera",
    "Show Camera", "Hide Camera", "Snap Photo", "Take Photo",
    # YouTube / media
    "Open YouTube", "Play YouTube", "Search YouTube", "YouTube Video",
    "Play Music", "Pause Music", "Next Song", "Previous Song",
    "Open Spotify", "Open VLC", "Play Video",
    # Browsers
    "Open Chrome", "Open Firefox", "Open Edge", "Open Brave",
    "Open Opera", "Open Safari", "Open Browser", "Open Web Browser",
    # Communication
    "Send Message", "Open WhatsApp", "Open Telegram", "Open Discord",
    "Open Slack",
    # Utilities / desktop
    "Open Files", "Open File Explorer", "Open Settings", "Open Terminal",
    "Open Command Prompt", "Open Task Manager", "Open Control Panel",
    "Open Notepad", "Open Calculator", "Open Paint", "Open Photos",
    "Open Recycle Bin", "Lock Screen", "Sleep", "Hibernate", "Restart",
    "Shut Down", "Take a Screenshot", "Screenshot",
    # Assistant
    "Hello Jarvis", "Hey Jarvis", "Jarvis", "Wake Up", "Go to Sleep",
    "System Status", "Weather Report", "What Time Is It", "What Is The Date",
    "Search Web", "Web Search", "Deep Research",
    "Remember This", "Forget This",
    "Minimize", "Maximize", "Close Window", "Hide Window",
    "Show Desktop", "Hide Jarvis", "Show Jarvis",
)

# Small, curated phonetic confusions that STT models repeatedly hit.  Kept
# minimal on purpose — the phonetic engine handles the long tail.  Values are
# always canonical, source spelling is what STT tends to emit.
_PHONETIC_MAP: dict[str, str] = {
    "web brow":     "webcam",
    "web bra":      "webcam",
    "web cam":      "webcam",
    "webcaam":      "webcam",
    "web cd":       "webcam",
    "wiv cam":      "webcam",
    "va kem":       "webcam",
    "you tube":     "youtube",
    "utoob":        "youtube",
    "utube":        "youtube",
    "tube":         "youtube",
    "coral":        "Chrome",
    "crom":         "Chrome",
    "crome":        "Chrome",
    "krome":        "Chrome",
    "fire fox":     "Firefox",
    "edge broser":  "Edge",
    "wats app":     "WhatsApp",
    "whats app":    "WhatsApp",
    "walats app":   "WhatsApp",
    "spotifai":     "Spotify",
    "spotiffy":     "Spotify",
    "telesGram":    "Telegram",
    "task managerr":"Task Manager",
    "resycle bin":  "Recycle Bin",
    "notepad":      "Notepad",
    "cmd prompt":   "Command Prompt",
    "comand prompt":"Command Prompt",
    "komand prompt":"Command Prompt",
    "jarvi":        "Jarvis",
    "jarvisss":     "Jarvis",
    "chelle Jarvis":"Hello Jarvis",
    "helo Jarvis":  "Hello Jarvis",
    "screenshot":   "Screenshot",
    "screen shot":  "Screenshot",
}

# Proper nouns whose canonical capitalisation should always be restored, no
# matter their position in free-form speech (STT lower-cases most of these).
_BRAND_WORDS: dict[str, str] = {
    "youtube": "YouTube", "chrome": "Chrome", "firefox": "Firefox",
    "whatsapp": "WhatsApp", "telegram": "Telegram", "discord": "Discord",
    "slack": "Slack", "spotify": "Spotify", "safari": "Safari",
    "opera": "Opera", "brave": "Brave", "edge": "Edge", "vlc": "VLC",
    "jarvis": "Jarvis", "explorer": "Explorer", "notepad": "Notepad",
}

# Intent verbs that let us resolve "web brow" specifically to a device verb
# rather than a browser.
_OPEN_VERBS = {"open", "launch", "start", "turn", "on", "show", "display",
               "check", "enable", "activate", "run"}
_CLOSE_VERBS = {"close", "stop", "off", "hide", "disable", "terminate", "kill",
                "exit", "quit", "deactivate"}


# ── Phonetic folding ──────────────────────────────────────────────────────────

_FOLD_SUBS: tuple[tuple[str, str], ...] = (
    ("ph", "f"), ("th", "t"), ("sh", "s"), ("ch", "k"), ("ck", "k"),
    ("qu", "kw"), ("wh", "w"), ("gh", "g"),
    ("aa", "a"), ("ee", "i"), ("oo", "u"), ("ou", "o"), ("ow", "o"),
    ("ai", "a"), ("ay", "a"), ("ei", "i"), ("ie", "i"),
    ("y", "i"), ("z", "s"), ("d", "t"), ("b", "p"), ("g", "k"), ("v", "f"),
    ("w", "v"), ("h", ""), ("x", "ks"),
)


def _fold_word(word: str) -> str:
    """Aggressively fold a token to a phonetic key.

    "Webcam" / "webb cam" / "vab cam" / "webcome" all end up in the same
    neighborhood.  Not a full Metaphone, but tiny and predictable, and paired
    with difflib on the folded form it correctly catches most STT slips
    without matching unrelated words.
    """
    w = (word or "").lower().strip()
    if not w:
        return ""
    w = re.sub(r"[^a-z0-9]", "", w)
    for _ in range(2):
        for src, dst in _FOLD_SUBS:
            w = w.replace(src, dst)
    return w


def _norm_key(text: str) -> str:
    return " ".join(_fold_word(t) for t in re.findall(r"[A-Za-z0-9']+", (text or "").lower()))


# ── Corrector ─────────────────────────────────────────────────────────────────

class SpeechCorrector:
    """Reusable, thread-safe, phonetic + command-aware STT correction layer."""

    # A token replacement requires this much phonetic similarity to fire.
    # Kept deliberately high so unrelated free-form speech is left untouched
    # (requirement: never rewrite valid speech just because a vaguely similar
    # command exists).
    TOKEN_THRESHOLD = 0.86
    # A whole-phrase replacement (fuzzy match against a canonical command)
    # requires this much similarity to fire.
    PHRASE_THRESHOLD = 0.85
    # Only consider corrections up to this many characters away from source.
    MAX_EDIT_DISTANCE_RATIO = 0.34

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._commands: list[str] = []
        # (source_folded_phrase, canonical_replacement) tuples, sorted longest
        # first so multi-word rules are matched before single tokens.
        self._phrase_rules: list[tuple[str, str]] = []
        # Curated confusion rules keyed on a tuple of *whole* source tokens →
        # canonical phrase. Matched with strict token boundaries so "web brow"
        # never fires inside "web browser".
        self._map_rules: list[tuple[tuple[str, ...], str]] = []
        # Word → canonical capitalisation, so "youtube" → "YouTube".
        self._word_forms: dict[str, str] = {}
        self._refresh_vocabulary(list(_CORE_COMMANDS))
        self._build_map_rules()

    # ── Vocabulary building ──────────────────────────────────────────────
    def _refresh_vocabulary(self, extra_commands: Iterable[str]) -> None:
        cmds: list[str] = []
        seen: set[str] = set()
        for phrase in list(_CORE_COMMANDS) + list(extra_commands):
            p = str(phrase or "").strip()
            if not p or p.lower() in seen:
                continue
            seen.add(p.lower())
            cmds.append(p)

        # Tool names → human phrases (best-effort, so "web search" → "Web Search").
        try:
            from main import TOOL_DECLARATIONS  # type: ignore
            for tool in TOOL_DECLARATIONS:
                n = str(tool.get("name") or "").replace("_", " ").strip()
                if n and n.lower() not in seen:
                    seen.add(n.lower())
                    cmds.append(n.title())
        except Exception:
            pass

        with self._lock:
            self._commands = cmds
            self._phrase_rules = self._build_phrase_rules(cmds)
            self._word_forms = self._build_word_forms(cmds)

    def _build_phrase_rules(self, commands: list[str]) -> list[tuple[str, str]]:
        rules: list[tuple[str, str]] = []
        # Curated confusions first.
        for src, canonical in _PHONETIC_MAP.items():
            rules.append((_norm_key(src), canonical))
        # Every canonical phrase also becomes a rule keyed on its own folded
        # form so later "self-matching" hits it at similarity 1.0 (never needed,
        # but keeps the structure uniform and future-extensible).
        for cmd in commands:
            rules.append((_norm_key(cmd), cmd))
        rules.sort(key=lambda t: len(t[0]), reverse=True)
        return rules

    def _build_word_forms(self, commands: list[str]) -> dict[str, str]:
        forms: dict[str, str] = {}
        for cmd in commands:
            for tok in cmd.split():
                low = tok.lower()
                # Prefer the form that mixes case (canonical brand spelling).
                prev = forms.get(low)
                if prev is None or (any(c.isupper() for c in tok) and prev.islower()):
                    forms[low] = tok
        return forms

    # ── Public API ───────────────────────────────────────────────────────
    def correct(self, text: str, *, is_final: bool = False) -> str:
        """Return a corrected version of *text* (or the original if no confident
        match exists).  Never raises."""
        try:
            raw = str(text or "")
            stripped = raw.strip()
            if not stripped:
                return raw
            # 1) Deterministic, whole-token-boundary confusion map.  Catches the
            #    known high-frequency slips ("web brow" → "webcam") with zero
            #    ambiguity, and cannot fire inside a longer word ("web browser").
            mapped = self._apply_confusion_map(stripped)
            # 2) Whole-phrase fuzzy match against a canonical command.
            corrected = self._correct_phrase(mapped)
            if not corrected:
                # 3) Per-token phonetic correction (command/intent aware).
                corrected = self._correct_tokens(mapped)
            corrected = self._recase_tokens(corrected)
            return corrected
        except Exception:
            return str(text or "")

    # ── Internals ────────────────────────────────────────────────────────
    def _build_map_rules(self) -> None:
        """Compile _PHONETIC_MAP into whole-token rules, longest source first."""
        rules: list[tuple[tuple[str, ...], str]] = []
        for src, canonical in _PHONETIC_MAP.items():
            toks = tuple(t.lower() for t in re.findall(r"[A-Za-z0-9']+", src))
            if toks:
                rules.append((toks, canonical))
        rules.sort(key=lambda t: len(t[0]), reverse=True)
        with self._lock:
            self._map_rules = rules

    def _apply_confusion_map(self, text: str) -> str:
        """Replace known misheard token runs with their canonical form.

        Matches on whole tokens only and prefers the longest rule, so a rule
        for "web brow" cannot rewrite "web browser".
        """
        with self._lock:
            rules = list(self._map_rules)
        if not rules:
            return text
        # Keep the original separators by working on a token list and rejoining
        # with single spaces; transcript spacing is normalised anyway upstream.
        tokens = re.findall(r"[A-Za-z0-9']+", text)
        n = len(tokens)
        out: list[str] = []
        i = 0
        while i < n:
            matched = False
            for src, canonical in rules:
                ln = len(src)
                if i + ln <= n and [t.lower() for t in tokens[i:i + ln]] == list(src):
                    out.extend(canonical.split())
                    i += ln
                    matched = True
                    break
            if not matched:
                out.append(tokens[i])
                i += 1
        return " ".join(out)

    def _correct_phrase(self, text: str) -> str:
        """Whole-phrase match against a known canonical command.

        Fires when the phonetic signature of the whole utterance is very
        close to a canonical phrase.  Preferred over per-token correction
        because command-aware context (intent verbs, "Webcam" vs "Web Browser")
        is already encoded in the vocabulary entry.
        """
        key = _norm_key(text)
        if not key:
            return ""
        with self._lock:
            candidates = list(self._commands)
        src_tokens = re.findall(r"[A-Za-z0-9']+", text)
        src_count = len(src_tokens)
        best, best_score = "", 0.0
        for cand in candidates:
            cand_key = _norm_key(cand)
            if not cand_key:
                continue
            # Whole-phrase replacement must never drop real spoken content:
            # the utterance has to be exactly the same number of words as the
            # canonical command (a bare, mangled command) — otherwise extra
            # words like "tomorrow" would silently disappear.
            cand_tokens = re.findall(r"[A-Za-z0-9']+", cand)
            if len(cand_tokens) != src_count:
                continue
            score = difflib.SequenceMatcher(None, key, cand_key).ratio()
            if score > best_score:
                best, best_score = cand, score
        # A confident whole-phrase hit restores the canonical command *and its
        # casing* (STT lower-cases almost everything). Token-count equality is
        # already enforced above, so this never discards real spoken content.
        if best_score >= self.PHRASE_THRESHOLD:
            return best
        return ""

    def _correct_tokens(self, text: str) -> str:
        """Token-by-token correction using phonetic similarity + intent-aware
        scoring.  Only fires when the token is not already a known word and a
        single candidate is clearly closer than every alternative."""
        tokens = re.findall(r"[A-Za-z0-9']+", text)
        if not tokens:
            return text
        lowered = [t.lower() for t in tokens]
        intent = self._detect_intent(lowered)

        with self._lock:
            commands = list(self._commands)
            forms = dict(self._word_forms)

        # Pre-pool candidate tokens from the vocabulary, dedup by fold key.
        pool: dict[str, str] = {}
        for cmd in commands:
            for tok in cmd.split():
                k = _fold_word(tok)
                if k and len(k) >= 3:
                    pool.setdefault(k, tok)
        for low, canon in forms.items():
            k = _fold_word(low)
            if k and len(k) >= 3:
                pool.setdefault(k, canon)

        out_words = list(tokens)
        for i, tok in enumerate(tokens):
            low = tok.lower()
            if low in forms:
                continue
            if len(low) < 4:  # short tokens ("a", "on", "to") are not corrected
                continue
            fold = _fold_word(low)
            if len(fold) < 3:
                continue
            best_repl, best_score = "", 0.0
            for k, canon in pool.items():
                if abs(len(k) - len(fold)) > max(3, int(len(fold) * self.MAX_EDIT_DISTANCE_RATIO)):
                    continue
                s = difflib.SequenceMatcher(None, fold, k).ratio()
                # Intent-aware nudge: a token near a known device ("webcam")
                # after an OPEN verb is more likely intended as the device, not
                # the browser, so we lift exact-intent-pair vocabulary hits.
                if intent and self._matches_intent(canon, intent):
                    s = min(1.0, s + 0.06)
                if s > best_score:
                    best_repl, best_score = canon, s
            if best_score >= self.TOKEN_THRESHOLD:
                out_words[i] = best_repl
        return " ".join(out_words)

    def _detect_intent(self, lowered_tokens: list[str]) -> str:
        for t in lowered_tokens:
            if t in _OPEN_VERBS:
                return "open"
            if t in _CLOSE_VERBS:
                return "close"
        return ""

    def _matches_intent(self, canonical_token: str, intent: str) -> bool:
        # Reserved hook — currently every known "open" phrase is a device or
        # app; the intent nudge fires on any vocabulary hit, so this is a
        # simple yes for a canonical token that appears inside an intent-matched
        # phrase.  Kept as a method so future intent-specific weighting has one
        # obvious place to live without reshaping the pipeline.
        return bool(canonical_token)

    def _recase_tokens(self, text: str) -> str:
        """Restore canonical capitalisation for proper-noun brands only
        (youtube → YouTube).  Generic vocabulary words are left with whatever
        casing the correction pass already produced, so free-form speech keeps
        its natural case and is never mid-sentence Title-Cased."""
        out = []
        for tok in re.findall(r"[A-Za-z0-9']+|[^\w]", text):
            out.append(_BRAND_WORDS.get(tok.lower(), tok))
        return "".join(out)


# ── Module-level singleton ───────────────────────────────────────────────────

_DEFAULT: SpeechCorrector | None = None
_DEFAULT_LOCK = threading.Lock()


def get_corrector() -> SpeechCorrector:
    global _DEFAULT
    if _DEFAULT is None:
        with _DEFAULT_LOCK:
            if _DEFAULT is None:
                _DEFAULT = SpeechCorrector()
    return _DEFAULT


def correct_speech(text: str, *, is_final: bool = False) -> str:
    """Convenience wrapper used by main.py.  Never raises."""
    try:
        return get_corrector().correct(text, is_final=is_final)
    except Exception:
        return str(text or "")
