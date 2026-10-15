"""Geospatial placement for the HoloGlobe.

The globe draws places; this module is the part that knows where a place *is*.
Three sources, in order, and the answer always says which one was used:

1. **The built-in gazetteer** — about eighty of the world's cities, capitals and
   landmarks with exact coordinates. Instant, offline, and the common case.
2. **The geocoding service** (open-meteo, no key) — for anything else, with a
   bounded timeout so a slow network delays one answer rather than the session.
3. **A coordinate pair** the model already sent, used verbatim.

Nothing guesses: a name that cannot be resolved comes back as a sentence naming
what was searched and what came back, never as a marker dropped at 0,0 in the
Atlantic (which is what a silent failure looks like on a globe).
"""

from __future__ import annotations

import re
from typing import Any

__all__ = ["GAZETTEER", "geocode", "resolve", "run", "status"]

#: name → (latitude, longitude, country/region)
GAZETTEER: dict[str, tuple[float, float, str]] = {
    "london": (51.5074, -0.1278, "United Kingdom"),
    "paris": (48.8566, 2.3522, "France"),
    "berlin": (52.5200, 13.4050, "Germany"),
    "madrid": (40.4168, -3.7038, "Spain"),
    "rome": (41.9028, 12.4964, "Italy"),
    "amsterdam": (52.3676, 4.9041, "Netherlands"),
    "brussels": (50.8503, 4.3517, "Belgium"),
    "vienna": (48.2082, 16.3738, "Austria"),
    "zurich": (47.3769, 8.5417, "Switzerland"),
    "geneva": (46.2044, 6.1432, "Switzerland"),
    "stockholm": (59.3293, 18.0686, "Sweden"),
    "oslo": (59.9139, 10.7522, "Norway"),
    "copenhagen": (55.6761, 12.5683, "Denmark"),
    "helsinki": (60.1699, 24.9384, "Finland"),
    "dublin": (53.3498, -6.2603, "Ireland"),
    "lisbon": (38.7223, -9.1393, "Portugal"),
    "barcelona": (41.3874, 2.1686, "Spain"),
    "prague": (50.0755, 14.4378, "Czechia"),
    "warsaw": (52.2297, 21.0122, "Poland"),
    "budapest": (47.4979, 19.0402, "Hungary"),
    "athens": (37.9838, 23.7275, "Greece"),
    "istanbul": (41.0082, 28.9784, "Türkiye"),
    "moscow": (55.7558, 37.6173, "Russia"),
    "kyiv": (50.4501, 30.5234, "Ukraine"),
    "reykjavik": (64.1466, -21.9426, "Iceland"),
    "new york": (40.7128, -74.0060, "United States"),
    "washington": (38.9072, -77.0369, "United States"),
    "boston": (42.3601, -71.0589, "United States"),
    "chicago": (41.8781, -87.6298, "United States"),
    "houston": (29.7604, -95.3698, "United States"),
    "denver": (39.7392, -104.9903, "United States"),
    "seattle": (47.6062, -122.3321, "United States"),
    "san francisco": (37.7749, -122.4194, "United States"),
    "los angeles": (34.0522, -118.2437, "United States"),
    "las vegas": (36.1699, -115.1398, "United States"),
    "miami": (25.7617, -80.1918, "United States"),
    "atlanta": (33.7490, -84.3880, "United States"),
    "toronto": (43.6532, -79.3832, "Canada"),
    "vancouver": (49.2827, -123.1207, "Canada"),
    "montreal": (45.5019, -73.5674, "Canada"),
    "mexico city": (19.4326, -99.1332, "Mexico"),
    "havana": (23.1136, -82.3666, "Cuba"),
    "bogota": (4.7110, -74.0721, "Colombia"),
    "lima": (-12.0464, -77.0428, "Peru"),
    "santiago": (-33.4489, -70.6693, "Chile"),
    "buenos aires": (-34.6037, -58.3816, "Argentina"),
    "sao paulo": (-23.5505, -46.6333, "Brazil"),
    "rio de janeiro": (-22.9068, -43.1729, "Brazil"),
    "cairo": (30.0444, 31.2357, "Egypt"),
    "casablanca": (33.5731, -7.5898, "Morocco"),
    "lagos": (6.5244, 3.3792, "Nigeria"),
    "nairobi": (-1.2921, 36.8219, "Kenya"),
    "johannesburg": (-26.2041, 28.0473, "South Africa"),
    "cape town": (-33.9249, 18.4241, "South Africa"),
    "dubai": (25.2048, 55.2708, "United Arab Emirates"),
    "abu dhabi": (24.4539, 54.3773, "United Arab Emirates"),
    "doha": (25.2854, 51.5310, "Qatar"),
    "riyadh": (24.7136, 46.6753, "Saudi Arabia"),
    "tehran": (35.6892, 51.3890, "Iran"),
    "karachi": (24.8607, 67.0011, "Pakistan"),
    "delhi": (28.6139, 77.2090, "India"),
    "new delhi": (28.6139, 77.2090, "India"),
    "mumbai": (19.0760, 72.8777, "India"),
    "bengaluru": (12.9716, 77.5946, "India"),
    "bangalore": (12.9716, 77.5946, "India"),
    "chennai": (13.0827, 80.2707, "India"),
    "kolkata": (22.5726, 88.3639, "India"),
    "colombo": (6.9271, 79.8612, "Sri Lanka"),
    "dhaka": (23.8103, 90.4125, "Bangladesh"),
    "kathmandu": (27.7172, 85.3240, "Nepal"),
    "bangkok": (13.7563, 100.5018, "Thailand"),
    "singapore": (1.3521, 103.8198, "Singapore"),
    "kuala lumpur": (3.1390, 101.6869, "Malaysia"),
    "jakarta": (-6.2088, 106.8456, "Indonesia"),
    "manila": (14.5995, 120.9842, "Philippines"),
    "hanoi": (21.0278, 105.8342, "Vietnam"),
    "ho chi minh city": (10.8231, 106.6297, "Vietnam"),
    "hong kong": (22.3193, 114.1694, "Hong Kong"),
    "shanghai": (31.2304, 121.4737, "China"),
    "beijing": (39.9042, 116.4074, "China"),
    "shenzhen": (22.5431, 114.0579, "China"),
    "taipei": (25.0330, 121.5654, "Taiwan"),
    "seoul": (37.5665, 126.9780, "South Korea"),
    "tokyo": (35.6762, 139.6503, "Japan"),
    "osaka": (34.6937, 135.5023, "Japan"),
    "sydney": (-33.8688, 151.2093, "Australia"),
    "melbourne": (-37.8136, 144.9631, "Australia"),
    "perth": (-31.9505, 115.8605, "Australia"),
    "brisbane": (-27.4698, 153.0251, "Australia"),
    "auckland": (-36.8485, 174.7633, "New Zealand"),
    "wellington": (-41.2866, 174.7756, "New Zealand"),
    "honolulu": (21.3069, -157.8583, "United States"),
    "anchorage": (61.2181, -149.9003, "United States"),
    "edinburgh": (55.9533, -3.1883, "United Kingdom"),
    "manchester": (53.4808, -2.2426, "United Kingdom"),
    "birmingham": (52.4862, -1.8904, "United Kingdom"),
    "glasgow": (55.8642, -4.2518, "United Kingdom"),
    "munich": (48.1351, 11.5820, "Germany"),
    "frankfurt": (50.1109, 8.6821, "Germany"),
    "hamburg": (53.5511, 9.9937, "Germany"),
    "milan": (45.4642, 9.1900, "Italy"),
    "naples": (40.8518, 14.2681, "Italy"),
    "marseille": (43.2965, 5.3698, "France"),
    "lyon": (45.7640, 4.8357, "France"),
    "nice": (43.7102, 7.2620, "France"),
    "porto": (41.1579, -8.6291, "Portugal"),
    "seville": (37.3891, -5.9845, "Spain"),
    "valencia": (39.4699, -0.3763, "Spain"),
}

_ALIASES = {
    "nyc": "new york", "new york city": "new york", "la": "los angeles",
    "sf": "san francisco", "dc": "washington", "bombay": "mumbai",
    "peking": "beijing", "saigon": "ho chi minh city", "kiyv": "kyiv",
    "kiev": "kyiv", "st petersburg": "saint petersburg",
}

_COORDS = re.compile(r"^\s*(-?\d{1,3}(?:\.\d+)?)\s*[, ]\s*(-?\d{1,3}(?:\.\d+)?)\s*$")


def _clean(name: str) -> str:
    text = str(name or "").strip().lower()
    text = re.sub(r"[.,;]+$", "", text)
    text = re.sub(r"\s+", " ", text)
    return _ALIASES.get(text, text)


def _gazetteer(name: str):
    key = _clean(name)
    if key in GAZETTEER:
        return GAZETTEER[key], key
    # A prefix match catches "London, England" and "Paris France" without ever
    # inventing a position: an ambiguous prefix returns nothing at all.
    matches = [(key_name, value) for key_name, value in GAZETTEER.items()
               if key_name.startswith(key) or key.startswith(key_name)]
    if len(matches) == 1:
        return matches[0][1], matches[0][0]
    return None, key


def _coordinates(name: str):
    match = _COORDS.match(str(name or ""))
    if not match:
        return None
    try:
        lat, lon = float(match.group(1)), float(match.group(2))
    except ValueError:
        return None
    if not (-90 <= lat <= 90 and -180 <= lon <= 180):
        return None
    return lat, lon


def geocode(name: str, timeout: float = 8.0) -> dict:
    """Resolve a place name. Returns ``{ok, place|reason, source}``."""
    text = str(name or "").strip()
    if not text:
        return {"ok": False, "reason": "no place name was given", "source": ""}

    direct = _coordinates(text)
    if direct is not None:
        return {"ok": True, "source": "coordinates",
                "place": {"name": text, "latitude": direct[0],
                          "longitude": direct[1], "country": ""}}

    found, key = _gazetteer(text)
    if found is not None:
        latitude, longitude, country = found
        return {"ok": True, "source": "gazetteer",
                "place": {"name": key.title(), "latitude": latitude,
                          "longitude": longitude, "country": country}}

    try:
        import requests
        response = requests.get(
            "https://geocoding-api.open-meteo.com/v1/search",
            params={"name": text, "count": 1, "language": "en", "format": "json"},
            timeout=timeout)
        payload = response.json() if response.status_code == 200 else {}
        results = (payload or {}).get("results") or []
        if results:
            hit = results[0]
            return {"ok": True, "source": "geocoding service",
                    "place": {"name": str(hit.get("name") or text),
                              "latitude": float(hit.get("latitude")),
                              "longitude": float(hit.get("longitude")),
                              "country": str(hit.get("country") or ""),
                              "region": str(hit.get("admin1") or "")}}
        return {"ok": False, "source": "geocoding service",
                "reason": f"nothing matched '{text}'"}
    except Exception as exc:
        return {"ok": False, "source": "geocoding service",
                "reason": (f"'{text}' is not in the built-in gazetteer and the "
                           f"geocoding service could not be reached ({exc})")}


def resolve(name: str) -> tuple[dict | None, str]:
    """``(place, "")`` or ``(None, sentence)`` for the model to read."""
    result = geocode(name)
    if result.get("ok"):
        place = dict(result["place"])
        place["source"] = result.get("source", "")
        return place, ""
    return None, (f"I could not place '{name}': {result.get('reason')}. Give me a "
                  f"city name, or its latitude and longitude.")


def status() -> str:
    """What the globe can do right now (no network is touched)."""
    return (f"{len(GAZETTEER)} places are known offline; anything else is looked "
            f"up through the open geocoding service when the network allows.")


def _places_from(args: dict) -> tuple[list[dict], list[str]]:
    """Every place named in the request, plus the ones that could not be placed."""
    names: list[str] = []
    for key in ("place", "places", "query", "target", "name"):
        value = args.get(key)
        if isinstance(value, (list, tuple)):
            names.extend(str(item) for item in value)
        elif isinstance(value, str) and value.strip():
            names.extend(part.strip() for part in value.split(",") if part.strip())
    places: list[dict] = []
    failed: list[str] = []
    for name in names[:24]:
        place, error = resolve(name)
        if place is None:
            failed.append(error)
        else:
            places.append(place)
    return places, failed


def _ok(detail: str, **evidence: Any) -> dict:
    return {"ok": True, "controller": "globe", "detail": detail, "reason": "",
            "evidence": evidence}


def _fail(detail: str, reason: str = "not_found") -> dict:
    return {"ok": False, "controller": "globe", "detail": detail, "reason": reason,
            "evidence": {}}


def _h_places(args: dict) -> dict:
    """Resolve every named place and hand the coordinates back as evidence.

    The coordinates travel with the result rather than being looked up again by
    whoever draws them: a pane that re-resolved a name could show a marker the
    model was never told about, and the two answers would disagree silently.
    """
    places, failed = _places_from(args)
    lines: list[str] = []
    for place in places:
        country = f" ({place['country']})" if place.get("country") else ""
        lines.append(f"{place['name']}{country} "
                     f"{place['latitude']:.2f}, {place['longitude']:.2f}"
                     + (f" [{place['source']}]" if place.get("source") else ""))
    detail = (f"Plotted {len(places)} place(s): " + "; ".join(lines)) if places \
        else "No place could be plotted, so the globe was left as it was."
    if failed:
        detail += " " + " ".join(failed)

    focus = str(args.get("focus") or "").strip()
    if focus:
        wanted = _clean(focus)
        match = next((p["name"] for p in places
                      if _clean(p["name"]) == wanted), "")
        if match:
            detail += f" {match} is already in view."
        else:
            failed.append(f"'{focus}' is not among the places just plotted; "
                          f"plot it, or focus it on its own.")
        result_focus = match
    else:
        result_focus = places[0]["name"] if len(places) == 1 else ""

    return {"ok": bool(places), "controller": "globe", "detail": detail,
            "reason": "" if places else "not_found",
            "places": places, "failed": failed, "focus": result_focus,
            "evidence": {"count": len(places), "failed": len(failed)}}


def _h_list(_args: dict) -> dict:
    names = sorted(GAZETTEER)
    lines = ["Places known offline:"]
    for index in range(0, len(names), 6):
        lines.append("  " + ", ".join(names[index:index + 6]))
    lines.append(status())
    return _ok("\n".join(lines), count=len(names))


def _h_status(_args: dict) -> dict:
    return _ok(status())


def _h_clear(_args: dict) -> dict:
    """Nothing to resolve — the pane clears its markers on this evidence."""
    return {"ok": True, "controller": "globe", "detail": "The globe was cleared.",
            "reason": "", "places": [], "failed": [], "cleared": True,
            "evidence": {"count": 0}}


def _h_focus(args: dict) -> dict:
    """One named place, so the pane can rotate it back to the camera."""
    name = str(args.get("focus") or args.get("place") or "").strip()
    place, error = resolve(name)
    if place is None:
        return _fail(error, "not_found")
    return {"ok": True, "controller": "globe", "reason": "", "failed": [],
            "detail": (f"{place['name']} is at {place['latitude']:.2f}, "
                       f"{place['longitude']:.2f}."),
            "places": [place], "focus": place["name"],
            "evidence": {"count": 1}}


_HANDLERS = {
    "resolve": _h_places,
    "plot": _h_places,
    "show": _h_places,
    "focus": _h_focus,
    "clear": _h_clear,
    "list": _h_list,
    "status": _h_status,
}


def run(action_name: str, args: dict) -> dict:
    """Resolve the places a globe action names. Never raises."""
    handler = _HANDLERS.get(str(action_name or "").strip().lower())
    if handler is None:
        return _fail(f"'{action_name}' is not a globe action. Use one of: "
                     f"{', '.join(sorted(_HANDLERS))}.", "unknown_action")
    try:
        return handler(dict(args or {}))
    except Exception as exc:
        return _fail(f"{type(exc).__name__}: {exc}", "action_failed")
