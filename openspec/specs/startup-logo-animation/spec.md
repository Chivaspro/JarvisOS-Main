## Purpose

Defines the startup reactor's settle sequence: the Arc-Reactor logo opens at 190 px and settles into its
compact home by smoothly interpolating position and size together in one animation timeline on the same
logo widget, driven by explicit STARTUP/ANIMATING/SETTLED states, so the transition never teleports,
never dead-ends, and cannot be interrupted by unrelated geometry writers.

## Requirements

### Requirement: The startup logo settles from 190 px to 180 px on the same asset

The startup sequence SHALL present the reactor logo at `STARTUP_LOGO_SIZE = 190` px and settle it to
180 px without replacing, recreating or substituting the logo widget or its artwork — only the widget's
geometry changes. The 190 px start and 180 px settled sizes SHALL be the same named constants across the
whole sequence.

#### Scenario: Startup size transition

- **WHEN** JARVIS launches and the startup sequence completes
- **THEN** the logo was presented at 190 px and finishes at 180 px, and the widget identity is unchanged
  from start to finish

#### Scenario: No asset substitution at settle

- **WHEN** the logo changes size during the settle
- **THEN** the same logo instance paints the same artwork at the new size

### Requirement: Position and size interpolate together in one timeline

The settle SHALL animate the window position and the logo size from a single animation timeline so both
interpolate simultaneously on one eased curve. The geometry SHALL progress monotonically between the
start and target rectangles (no step may place the window or logo outside the interpolated path), and no
code path SHALL assign the final geometry directly while the animation is running. An eased curve (ease-
in-out) SHALL be used.

#### Scenario: No teleport during settle

- **WHEN** the settle animation runs
- **THEN** each frame's window geometry lies on the interpolated path between the startup and compact
  rectangles, and the logo size moves continuously from 190 px toward 180 px

#### Scenario: Position and size move together

- **WHEN** the settle animation runs
- **THEN** the logo's size and the window's position/size advance on the same timeline — neither
  completes before the other

### Requirement: Explicit startup states own the sequence

The startup sequence SHALL have explicit states — STARTUP (large logo, waiting for the settle trigger),
ANIMATING (settle timeline running), SETTLED (final compact geometry) — and every trigger (speech
transition, fallback timer, user click, window close) SHALL act through those state guards. The states
SHALL be the single authority on whether the startup presentation, the settle animation or the settled
compact geometry is active.

#### Scenario: The fallback cannot run twice

- **WHEN** the settle has already started or completed and the fallback timer fires
- **THEN** the timer's action is a no-op

#### Scenario: State survives repeated triggers

- **WHEN** several settle triggers arrive in quick succession (speech transition and fallback together)
- **THEN** exactly one settle animation runs and the sequence ends in SETTLED exactly once

### Requirement: Clicking the logo during startup always settles

Clicking the reactor logo during the STARTUP state SHALL start (or fast-forward) the settle rather than
leaving the sequence half-finished, and then perform the click's normal action (opening the command
panel). The sequence SHALL never remain in STARTUP with no pending animation after a click, and a click
during ANIMATING SHALL NOT snap the geometry — it fast-forwards the running timeline to its end.

#### Scenario: Click before the first voice

- **WHEN** the user clicks the logo during the STARTUP state before any settle trigger has fired
- **THEN** the settle animation runs (or is fast-forwarded), the sequence reaches SETTLED, and the
  command panel opens

#### Scenario: Click during the settle animation

- **WHEN** the user clicks the logo while the settle animation is running
- **THEN** the animation fast-forwards smoothly to its final geometry (no instant jump) and the command
  panel opens

### Requirement: Unrelated geometry writers cannot interrupt the settle

While the sequence is not SETTLED, compact-layout and settings geometry writers (compact collapse/
expand, settings application, initial visibility application) SHALL NOT overwrite the window geometry or
the logo size; their layout changes are deferred until the sequence reaches SETTLED, or they delegate to
the sequence's own final geometry. Once SETTLED, these writers behave exactly as before.

#### Scenario: Settings applied during the settle

- **WHEN** a settings save that would resize the compact reactor arrives while the settle is animating
- **THEN** the animated geometry is not overwritten mid-flight and the settings take effect on the
  settled layout afterwards

#### Scenario: Collapse during the settle

- **WHEN** the compact layout is collapsed or re-applied while the sequence is in STARTUP or ANIMATING
- **THEN** the window does not jump to the compact geometry before the settle finishes

### Requirement: The settle is reliable across repeated launches

Every launch SHALL play the same settle sequence with the same start size, end size and end position; a
restart SHALL never leave the logo stuck large, at the wrong size, or at the wrong position, and no
animation or timer from a previous startup may survive into the next.

#### Scenario: Repeated launches settle identically

- **WHEN** JARVIS is restarted several times in a row
- **THEN** each launch presents the 190 px logo and settles to 180 px at the compact home with the same
  animation, and no leftover timer or animation object remains active after SETTLED

#### Scenario: Window resize or layout change during startup

- **WHEN** the screen or layout changes while the startup sequence is running
- **THEN** the logo does not suddenly jump; the settle target is re-clamped to the current screen and
  the animation continues smoothly
