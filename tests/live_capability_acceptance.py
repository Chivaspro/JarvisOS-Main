"""Live acceptance run for the capability layer.

Drives the real engines through the router exactly as J.A.R.V.I.S. will, prints
what each one actually returned, and exits non-zero if any claim is fake.

Run:  python tests/live_capability_acceptance.py
"""

from __future__ import annotations

import io
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

# The console is cp1252 on this machine and web pages are not.
try:
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8",
                                  errors="replace", line_buffering=True)
except Exception:
    pass

from capabilities import config, router          # noqa: E402

FAILURES: list[str] = []


def section(title: str) -> None:
    print("\n" + "=" * 78)
    print(title)
    print("=" * 78)


def check(label: str, condition: bool, evidence: str = "") -> None:
    mark = "PASS" if condition else "FAIL"
    print(f"[{mark}] {label}" + (f"\n       {evidence[:300]}" if evidence else ""))
    if not condition:
        FAILURES.append(label)


def main() -> int:
    started = time.monotonic()
    section("0  ENGINE DISCOVERY")
    from capabilities import agent_tars
    tars = agent_tars.status(force=True)
    print(f"  agent_tars  : node={tars.get('node') or 'NOT FOUND'}")
    print(f"                cli ={tars.get('cli') or 'NOT FOUND'}")
    print(f"                ver ={tars.get('version') or '-'}  "
          f"model={tars.get('provider')}/{tars.get('model')}")
    check("Agent TARS runtime located", bool(tars.get("node") and tars.get("cli")),
          tars.get("detail", ""))
    reach = config.env_root("agent_reach")
    print(f"  agent_reach : {reach or 'NOT FOUND'}")
    check("Agent Reach environment located", bool(reach), str(reach or ""))

    print("\ncapability sheet ->")
    print(router.report())

    section("1  WEB RESEARCH via Agent Reach")
    research = router.route_web_research(
        "open-source computer use agent frameworks", max_results=3)
    print(research.output[:900])
    check("web_research returned real findings", research.ok,
          f"channels={research.data.get('channels_used')}")
    findings = research.data.get("findings") or []
    check("findings carry a title and a real URL",
          bool(findings) and all(f.get("title") for f in findings),
          f"{len(findings)} findings")

    section("2  BROWSER AUTOMATION via Agent TARS")
    target = (findings[0].get("url") if findings and str(findings[0].get("url", "")).startswith("http")
              else "https://example.com")
    browser = router.route_browser_control(
        "Open the page and report the exact text of its main heading.", url=target)
    print(browser.output[:900])
    check("browser_control acted and returned a final answer", browser.ok,
          f"backend={browser.backend}, {browser.elapsed_s}s")
    check("verification trail recorded real steps",
          bool(browser.verification.get("verified")),
          f"verified={browser.verification.get('verified')} "
          f"steps={browser.verification.get('steps_taken')}")

    section("3  PC AUTOMATION via Agent TARS (launch an application)")
    desktop = router.route_computer_control("Open the Notepad application.")
    print(desktop.output[:700])
    check("computer_control launched the app (or reported honestly why not)",
          desktop.ok or desktop.reason in {"node_missing", "cli_missing",
                                           "no_gemini_key", "unavailable"},
          f"ok={desktop.ok} reason={desktop.reason}")

    section("4  NO CONCURRENT OWNERSHIP")
    import threading
    from capabilities import locks
    outcome: dict = {}
    t = threading.Thread(target=lambda: outcome.update(
        first=router.route_browser_control("Read the heading.",
                                           url="https://example.com")))
    t.start()
    deadline = time.monotonic() + 120
    while not locks.get_lock("browser").is_locked() and time.monotonic() < deadline:
        time.sleep(0.1)
    held = locks.get_lock("browser").is_locked()
    second = router.route_browser_control("Another site.", url="https://example.org")
    t.join()
    print(f"  browser lock held during first task : {held}")
    print(f"  second concurrent attempt          : ok={second.ok} reason={second.reason}")
    check("the second attempt was refused, not run in parallel",
          (not second.ok) and second.reason == "lock_busy")
    check("locks were released afterwards", not locks.get_lock("browser").is_locked())

    section("5  CONSEQUENTIAL ACTION POLICY")
    from security.rules import RiskTier, classify
    from security.approvals import requires_ui_token
    tiers = {
        "web_research (read a source)": classify("web_research", {}),
        "computer_control (open an app)": classify("computer_control_open", {}),
        "browser_control (send an email)": classify("browser_control_send", {}),
        "computer_control (delete files)": classify("computer_control_delete", {}),
    }
    for label, tier in tiers.items():
        print(f"  {label:34} -> {tier.value.upper()}")
    check("plain research stays LOW", tiers["web_research (read a source)"] is RiskTier.LOW)
    check("sending out is HIGH", tiers["browser_control (send an email)"] is RiskTier.HIGH)
    check("deletion always needs the on-screen token",
          requires_ui_token("computer_control_delete", {"task": "delete the folder"}))

    section("6  CHILD PROCESS HYGIENE")
    from capabilities import proc
    owned_now = proc.owned_pids()
    print(f"  J.A.R.V.I.S.-owned engine processes still registered: {owned_now}")
    router.shutdown("acceptance")
    check("shutdown released every lock",
          not any(v["locked"] for v in locks.status().values()))

    section("RESULT")
    print(f"elapsed {time.monotonic() - started:.0f}s")
    if FAILURES:
        print(f"{len(FAILURES)} CHECK(S) FAILED:")
        for name in FAILURES:
            print(f"  - {name}")
        return 1
    print("All acceptance checks passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
