## Context

See `proposal.md` for motivation. The constraints that shape this design come from how the HUD is actually built today.

**Structure.** The desktop HUD is PyQt6, all in-process: `ui.py` (12,731 lines, `MainWindow` plus roughly 80 widget classes), `ui_settings.py` (2,608 lines, `SettingsWindow` — a `QDialog` with 17 tabs), `ui_theme.py` (284 lines of design tokens and QSS factory functions), `ui_layout.py` (252 lines, layout JSON plus the Layout Editor dialog), and `dashboard/brain3d.py` (857 lines, the Memory Core scene). `main.py` owns the backend and the voice session and holds no widgets.

**Four palettes, three radius sets, no shared authority.** `ui.py` declares `class C` (`PRI = #00d4ff`, an orange `ACC`, text `#8ffcff`). `ui_theme.py` declares a different language (accent `#56d6f5`, gold as the signature accent, `RADIUS_LG/MD/SM`, font roles). `dashboard/brain3d.py` hardcodes its own `#00d4ff` palette with a comment claiming it matches "ui_theme C". `ui_settings.py` hardcodes hex strings inline and never imports `ui_theme`; its own `_ControlCenterBackdrop` is the visual language the floating panels copy. `ui_layout.py` embeds a fifth inline stylesheet with radii 10/12/6. Styling is overwhelmingly inline: 320 `setStyleSheet` calls in `ui.py`, 39 in `ui_settings.py`.

**Fixed geometry instead of content-driven sizing.** `ui.py` has 106 `setFixedHeight`/`setFixedWidth`/`setFixedSize` calls but only 14 `setWordWrap` and 3 uses of elision. The shared panel header is `bar.setFixedHeight(54)` and must hold an icon, a 9.5pt Orbitron title, a 6.5pt subtitle, a live indicator, and a close button. Control heights are set per widget (26/28/30/34/36 px) and contradict `ui_theme.button_css(min_height=34)`.

**Two generations in one file.** `LegacyMainWindow` (line 3066) has no references anywhere; `_ArcLogo`, `VideoPreview`, and `_CommandWindow` are superseded by `RefinedArcLogo`, `_Model3DView`, and `RefinedCommandWindow`, which `MainWindow` actually uses. The legacy classes reference the legacy palette, keeping it alive.

**Drag is split and partly invisible.** `MainWindow.eventFilter` implements two drag paths: a plain drag for widgets tagged `_jarvis_panel_drag`, and an `Alt`+primary-button drag for any point over a widget tagged `_jarvis_floating_panel`. `_install_panel_drag_surface` adds a transparent 4px-tall `JarvisPanelDragStrip` at `(8, 34)` inside the 54px header band, and `_new_panel_header` tags the header bar, icon, and title. `MainWindow.resizeEvent` then calls `self.findChild(QWidget, 'JarvisPanelDragStrip')` — the first match anywhere in the tree — and repositions it with the **main window's** width, so resizing the main window silently resizes one arbitrary panel's drag strip and leaves the rest stale. `_HudPanel.mousePressEvent` claims the bottom-right 18×18 px for resize, so that corner cannot start a drag.

**The Memory Core anchor is not stable because it is the same variable as the camera and the clock.** In `dashboard/brain3d.py`, `_layout` and `_rotate_pt` call `_rotate(x, y, z, self._rot_x, self._rot_y, self._t * 0.2)`, where `_t` is wall-clock seconds, so every node — the `CORE` node included — is rotated by a continuously advancing angle. Left-drag mutates `_rot_x`/`_rot_y` (camera), so an attempt to move an orb rotates the whole scene. There is no per-orb drag, attraction, snap, or merge anywhere in the repository: the requested orb interaction is new behavior, not a repair. Also relevant: `_BRAIN_ORBS.append(self)` is never undone, `_particles` is uncapped and mutated per paint with a fixed `0.03` step on a 33 ms timer, `_pick`/`_pick_hover` recompute the full projection on every pointer move, and the `_offsets` cache is keyed by `(id, spread)` so it grows as node counts change.

**Test constraints.** There is no UI test infrastructure: `tests/` contains only the three voice test modules, and `pytest-qt` is not installed. The existing pattern is `pytest.importorskip("PyQt6.QtWidgets")` with `QT_QPA_PLATFORM=offscreen` and a module-scoped `QApplication` fixture (`tests/hybrid_voice/test_settings_default_voice.py`). The design must make layout, hitbox, and scene behavior assertable through that harness without new dependencies.

**Preservation constraints.** Existing features, panels, settings, shortcuts, and configuration keys must keep working. The stored layout JSON shape (`_layout_elements`) and the settings semantics are not changed. The browser dashboard (`dashboard/static/app.html`, `dashboard/server.py`) is out of scope.

## Goals / Non-Goals

**Goals:**

- One token authority, one panel/header component, and one alignment/spacing hierarchy used by every surface.
- Layout that is content-driven, so text fits by construction rather than by tuned coordinates.
- One explicit, discoverable drag model with hitboxes that cannot swallow control input and decoration that cannot swallow clicks.
- A Memory Core whose anchor position is independent of rotation, scale, camera, and animation, plus real orb drag/attraction/snap behavior.
- Bounded runtime cost and clean teardown, verified with instrumented counters rather than by eye.
- Removal of the unreferenced legacy UI generation.

**Non-Goals:**

- No new runtime dependency and no new UI framework; PyQt6 only, and `pytest-qt` is deliberately not added.
- No OpenGL or new 3D renderer; the existing painter-based projection stays.
- No new visual language invented from scratch — the existing arc-reactor cyan plus reserved gold accent is preserved as the identity.
- No changes to the browser dashboard, the voice pipeline, backend actions, or memory storage semantics.
- No change to the layout JSON schema or to which settings exist.
- Not aiming for a cinematic re-creation of any film interface, and no third-party art assets.

## Decisions

### Decision 1: Tokens stay in `ui_theme.py` and become the only authority

Extend `ui_theme.py` with the missing scales — a 4-step spacing scale, three border weights (already present as `BORDER`/`BORDER_SOFT`/`BORDER_FAINT`), the three radii, the status ramp, and the font roles — and make `ui_settings.py`, `ui_layout.py`, and `dashboard/brain3d.py` import them.

*Alternatives considered:* a new `ui_tokens.py` (rejected — adding a second token module recreates the exact problem, since the failures came from parallel authorities); expressing everything in QSS variables (rejected — the painter-based widgets read Python colour values, so QSS alone cannot reach them).

### Decision 2: One shared panel and header component

Promote the refined panel builders (`_panel_base`, `_new_panel_header`, `_HudPanel`) into the single supported construction path for every floating panel and Settings section, and give the header a real structure: icon, title, optional subtitle, status chip, close control — laid out by the shared spacing scale, with the title allowed to wrap to a second line before eliding.

*Alternatives considered:* restyling each panel individually to look similar (rejected — that is what produced four title treatments); keeping `ui_theme`'s QSS factories as the only component layer (rejected — the panel shell is painted, not styled).

### Decision 3: Drag is owned by the header widget, and the strip is deleted

Move drag handling out of `MainWindow.eventFilter` and onto a dedicated header widget that implements press/move/release, so a panel's drag behavior is local, testable, and cannot be re-entered through the shared event filter state. The whole header band minus the bounds of header controls is the drag zone, the grab affordance is a visible element (cursor plus a rendering accent, not an invisible strip), and the 4px overlay is removed. The `Alt`-drag fallback is removed from the primary path, and `resizeEvent` stops calling `findChild` on a shared object name; the resize grip keeps owning the bottom-right corner.

*Alternatives considered:* keep the `eventFilter` and only fix `resizeEvent` (rejected — the modifier requirement and the shared mutable drag state are the discoverability and correctness problem); use the platform-native move (`startSystemMove`) as the primary mechanism (deferred — it is a good follow-up for multi-monitor DPI correctness, but it bypasses the per-panel autosave and geometry clamping that the specs require, and it is harder to assert offscreen); make the entire panel draggable (rejected — it breaks sliders, lists, and the embedded browser).

### Decision 4: Text sizing policy — wrap for headings, elide for technical labels

Titles and section headings wrap to at most two lines and then elide; single-line technical labels (status chips, counters, telemetry, list metadata) elide with an ellipsis. Containers get minimum sizes instead of fixed sizes, and the header height becomes a minimum driven by its content.

*Alternatives considered:* shrink fonts to fit (rejected explicitly by the request); let text clip (rejected — that is the current bug); auto-scroll text (rejected — unusable for status indicators).

### Decision 5: Separate the Memory Core's concerns into explicit state

Split the scene into: `anchor` (constant world position), `view` (rotation, zoom, pan), `elements` (each orb's own world position and merged flag), and `clock` (elapsed time used only to advance cosmetic parameters). The time-driven rotation keeps driving the orbit shells and non-anchor orbs so the HUD still looks alive, but the anchor is excluded from it: the anchor's projected point is derived from the view and the panel geometry only, never from the animation clock. Node offsets stay deterministic and stable, and the frame's projection is computed once and reused.

*Alternatives considered:* remove all animated rotation (rejected — loses the live HUD feel the design language calls for); freeze the whole scene unless the user interacts (rejected — same reason, and it makes activity feedback invisible); keep rotating everything and merely re-centre the anchor afterwards (rejected — a corrective write per frame is exactly the coupling the spec forbids).

### Decision 6: Orb attraction as a frame-rate-independent damped approach, not a physics solver

Implement orb interaction as an explicit state machine (idle → hover → drag → attract → snap → merged) with time-based exponential smoothing (`alpha = 1 - exp(-k · dt)`) toward a target, an attraction radius, a smaller snap radius, a capped approach speed, and a one-shot merge guarded by a flag.

*Alternatives considered:* a spring/damper physics solver (rejected — springs are the usual source of the overshoot and jitter the specs prohibit, and tuning them is fragile); direct position assignment to the pointer (rejected — pointer jitter becomes orb jitter, and snap would teleport); reusing the existing projection for hit-testing only and moving orbs in screen space (rejected — screen-space orbs break under rotation and zoom).

### Decision 7: Budgets are explicit and instrumented

One timer per animated component; motion from elapsed time; a capped particle pool that drains to zero when idle; the per-frame projection shared by drawing, hit-testing, and hover; caches invalidated on data change and bounded by element count; the process-wide orb registry converted to weak references with unregistration on destroy or close.

*Alternatives considered:* keep the current 33 ms repaint and accept the cost (rejected — the specs require bounded idle work); drive everything from a single application-wide animation timer (rejected — it makes per-component start/stop and teardown harder to reason about, and couples unrelated panels).

### Decision 8: Configuration stays backward compatible and additive

Keep `ui_color`, `ui_font`, `features.draggable_panels`, and `features.panel_positions` readable and writable exactly as they are. Consolidating the palette must not reset a user's accent: a legacy accent value is mapped to its current token equivalent on load, and a custom user value is preserved verbatim. No key is removed or renamed.

*Alternatives considered:* introduce a new theme key and ignore the old one (rejected — silently discards a user preference); keep legacy accent values working as literal colours (rejected — they belong to the retired palette and would reintroduce it).

### Decision 9: Delete the unreferenced legacy UI generation

Remove `LegacyMainWindow`, `_ArcLogo`, `VideoPreview`, `_CommandWindow`, and the palette/style constants that only they use, in a task that first proves nothing references them. The user chose removal over documentation-only, to eliminate the second palette at its source.

*Alternatives considered:* keep the legacy classes for reference value (rejected by the user's decision); keep them but mark deprecated (rejected — deprecated-with-palette still ships the conflicting tokens).

*Consequence:* the two assets in `assets/` that only the legacy styles referenced (`Archivo Black`, `Futura Bold`) stop being referenced. Their files stay on disk and the audit reports them, per the design-system spec.

### Decision 10: Verification through offscreen Qt, without new dependencies

Reuse the existing `QT_QPA_PLATFORM=offscreen` + module-scoped `QApplication` harness. Tests assert behavior and geometry rather than pixels: label required size versus available space, rectangle intersections, drag-region containment, token vocabulary invariants, scene state isolation across simulated ticks, attraction monotonicity under different step intervals, merge idempotence, particle cap and drain, and registry baseline after create/destroy cycles. Purely visual quality stays a manual checklist.

*Alternatives considered:* add `pytest-qt` for `qtbot` conveniences (rejected — an unnecessary new dependency for the assertions we need, and the repo already has a working pattern); screenshot-diff testing (rejected — brittle with the shared checkout's fonts and DPI, and unverifiable on this machine).

## Risks / Trade-offs

- [The token consolidation touches most of `ui.py`, so a visual regression could hide in a large diff] → Order the work so each step is independently verifiable: tokens first with no visible change, then the shared components, then panel-by-panel migration, checking an offscreen render of every panel after each step.
- [Removing legacy classes could break an import or a `getattr` path that grep cannot see] → The removal task must import every affected module, construct the main window offscreen, and exercise the panel-opening entry points; `MainWindow` accesses many widgets by `getattr`, so those paths need an explicit smoke test rather than only a reference search.
- [Fixing the header drag zone changes hit behavior users may have adapted to, including the removed `Alt`-drag] → The header stays draggable without a modifier everywhere it was draggable before (plain drag inside the strip, header widgets), and the change adds coverage for the drag-versus-control cases that were previously broken.
- [Making the anchor rotation-free changes the Memory Core's look] → Intentional: the shells and non-anchor orbs keep rotating, so the scene still reads as live while the anchor becomes trustworthy. The change is documented in the spec's anchor-stability requirement.
- [Offscreen rendering cannot validate visual polish, spacing feel, or glow restraint] → Those stay manual verification items in the task list; the automated tests cover the structural rules (fit, collisions, vocabulary, budgets) that cause the reported defects.
- [A time-based easing model behaves differently on a heavily loaded machine] → The damping uses elapsed time, and tests assert monotonic approach and bounded speed under varying step intervals, which is exactly the failure mode the current fixed-step animation has.
- [Removing the global `eventFilter` drag path could drop behavior that other widgets rely on] → The popup-drag path is separate from the floating-panel path and is migrated deliberately; the reference search must enumerate every `_jarvis_panel_drag` and `_jarvis_popup_drag` consumer before removal.

## Migration Plan

1. Consolidate tokens and adjust `ui_theme.py`, `ui_settings.py`, `ui_layout.py`, and `dashboard/brain3d.py` to consume them, with the legacy accent mapping in place. No configuration keys change and no user preference is reset, so no data migration is required.
2. Introduce the shared panel/header component and migrate panels onto it, keeping the existing panel object names and the layout JSON shape so stored layouts and panel positions remain valid.
3. Replace the drag model and remove the shared overlay strip; verify that restored panel geometry still clamps to the visible screen.
4. Stabilize the Memory Core scene state, then add orb interaction, then bound the effect pools.
5. Remove the legacy UI generation and confirm the main window and every panel entry point still construct and open.
6. Rollback: revert the affected files. The only persisted artifacts are the existing configuration keys, whose values remain valid in both directions, so no rollback data step is needed.

## Open Questions

- The exact attraction radius, snap radius, damping constants, particle cap, and idle-work thresholds are tuning values. Their values do not change the specs or the task breakdown, and will be chosen during implementation against the structural assertions and then recorded in the design or in code comments.
- Adopting the platform-native window move for multi-monitor DPI changes is a deliberate deferred follow-up; it would improve native move behavior but interacts with per-panel geometry persistence, so it is out of scope here.
