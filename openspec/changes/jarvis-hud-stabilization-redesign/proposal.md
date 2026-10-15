## Why

The PyQt6 HUD has grown to 12,731 lines in `ui.py` plus 2,608 in `ui_settings.py`, and it now carries four competing palettes, two conflicting control-height systems, and two generations of widget classes in the same file. The result is the instability the audit turned up: text that clips inside fixed-height headers, a drag affordance that only works in a 4px invisible strip, a `resizeEvent` that moves the wrong panel's drag handle, and a Memory Core whose central orb never stops spinning because its position, the camera, and the animation clock are all the same variable.

This change stabilizes and modernizes the desktop HUD in one pass, because the visual and structural problems share root causes: there is no single design-token source, no shared panel/header component, no hitbox rules, and no separation between 3D position, rotation, camera, and animation state.

## What Changes

- Introduce one design-token source (`ui_theme.py`) as the only palette, typography, spacing, radius, border, and status-colour authority, and make `ui_settings.py`, `ui_layout.py`, and `dashboard/brain3d.py` consume it instead of hardcoding hex strings. Legacy `ui_color` values are migrated, not lost.
- Add a shared panel component vocabulary (panel base, title/header with decoration, status chip, section heading, control row) so every panel uses one title treatment instead of four.
- Fix text visibility structurally: replace fixed geometry with size policies, wrapping, and elision where the audit found fixed heights and widths colliding with their own content.
- Replace the split drag model (alt-drag anywhere, plain drag only inside an invisible 4px strip) with one explicit, discoverable header drag zone, and stop decorative layers from capturing mouse events.
- Split the Memory Core 3D state into strict position / rotation / scale / camera / animation concerns so the central orb is a stable anchor, then add explicit orb interaction (pick, drag, magnetic attraction, easing, snap/merge) which does not exist today.
- Bound runtime cost: one animation loop per widget, wall-clock-driven motion, capped particle pools, and leak-free teardown of the process-wide orb registry.
- **BREAKING** (internal only): delete the unreferenced legacy UI generation (`LegacyMainWindow`, `_ArcLogo`, `VideoPreview`, `_CommandWindow` and their orphaned stylesheet constants). No user-facing feature or setting is removed.

## Capabilities

### New Capabilities

- `hud-design-system`: one authoritative token set (surface/stroke/text/accent/status colours, three radii, three border weights, the font stack, and a spacing scale) plus the shared component vocabulary every panel and Settings tab renders through.
- `hud-layout-integrity`: measurable layout rules — content-driven sizing, no clipped or overlapping text, consistent alignment and spacing hierarchy, and defined behavior when a window or screen changes size.
- `hud-window-interaction`: one explicit drag model for every floating panel, drag zones that never swallow control input, decorative overlays that never capture clicks, deterministic z-order, and persisted panel geometry.
- `memory-core-3d`: the Memory Core's central orb as a permanently stable anchor, strict isolation of position/rotation/scale/camera/animation, secondary-orb drag with distance-based magnetic attraction and eased snap/merge, and lightweight cosmetic feedback that cannot move the anchor.
- `hud-runtime-performance`: enforced budgets for timers, repaint work, and particle counts, plus teardown rules that prevent orphaned widgets, stale event handlers, and unbounded caches.

### Modified Capabilities

- None. `openspec/specs/` currently holds only the three voice capabilities (`voice-output-routing`, `gemini-piper-pipeline`, `piper-onnx-execution`), whose requirements do not change. The archived `improve-jarvis-ui` change introduced `jarvis-ui-typography`, `jarvis-ui-vision-transition`, and `jarvis-ui-mic-soundwave`, but that change was archived without syncing main specs and its own artifacts describe the browser dashboard while its tasks edited `ui.py`; nothing is being modified in place. The typography intent from `jarvis-ui-typography` is folded into `hud-design-system` rather than duplicated.

## Impact

**Affected code**
- `ui.py` — `MainWindow` panel construction (`_install_panel_drag_surface`, `resizeEvent`, `_remember_panel_position`, `_restore_panel_position`), the shared header/panel builders, inline stylesheet calls, and removal of the legacy generation.
- `ui_settings.py` — 17 tabs and `_ControlCenterBackdrop` currently hardcode hex colours and never import `ui_theme`.
- `ui_theme.py` — becomes the single token + component source; gains the spacing scale and panel/header helpers.
- `ui_layout.py` — drops its duplicated inline stylesheet and uses the shared tokens; the Layout Editor keeps writing the same `_layout_elements` JSON shape.
- `dashboard/brain3d.py` — scene state split, orb interaction, particle/timer budgets, orb-registry teardown.
- `config/api_keys.json` (`ui_color`, `ui_font`, `features.panel_positions`, `features.draggable_panels`) — new values are written, existing keys stay readable.

**Affected systems**
- PyQt6 desktop HUD only. The browser dashboard (`dashboard/static/app.html`, `dashboard/server.py`), the voice pipeline, and all backend/action modules are unchanged.

**Dependencies**
- No new runtime dependencies. Tests reuse the existing `QT_QPA_PLATFORM=offscreen` + `QApplication` harness already used by `tests/hybrid_voice/test_settings_default_voice.py`; `pytest-qt` is deliberately not added.

**Risk**
- Visual regressions are the main risk, since the token consolidation touches most of `ui.py`. Mitigated by keeping the change behavioral-test-first, by rendering panels offscreen for programmatic geometry assertions, and by preserving the existing visual language (arc-reactor cyan, gold reserved for live/armed) rather than inventing a new one.
