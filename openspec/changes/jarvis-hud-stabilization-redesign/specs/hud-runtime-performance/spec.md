## Purpose

Keeps the HUD responsive and leak-free by bounding animation, timer, repaint, particle, and cache costs, and by requiring clean teardown of every visual component when it is hidden or destroyed.

## ADDED Requirements

### Requirement: One animation loop per visual component

Each animated component SHALL own at most one animation timer. Creating, showing, or re-opening a component SHALL NOT start an additional loop, and hiding it SHALL stop the loop it owns.

#### Scenario: Re-opening does not duplicate the loop

- **WHEN** a component is shown, hidden, and shown again repeatedly
- **THEN** the number of active animation timers for that component stays constant and its repaint rate does not increase

#### Scenario: Hidden components stop animating

- **WHEN** a component is hidden or closed
- **THEN** its animation and refresh timers are stopped and it performs no further repaint work

### Requirement: Motion is driven by elapsed time, not frame count

All animated motion SHALL be derived from elapsed wall-clock time, so animation speed is stable regardless of frame rate, dropped frames, or CPU load. Frame-count-based fixed-step motion SHALL NOT be used.

#### Scenario: Motion speed is stable under load

- **WHEN** the same animation runs over the same elapsed duration with a different frame interval
- **THEN** the total movement completes in the same elapsed time within a small tolerance

#### Scenario: CPU stall does not freeze or accelerate motion

- **WHEN** the process is briefly starved of CPU and then resumes
- **THEN** the animation continues from where it would be by elapsed time, without a visible jump or slowdown

### Requirement: Particle and effect pools are bounded

Particle and transient-effect counts SHALL be capped. Spawning beyond the cap SHALL discard the oldest entries, and the pool SHALL drain to empty when the component is idle.

#### Scenario: Burst of events stays within the cap

- **WHEN** a large burst of visual events is published in a short interval
- **THEN** the active particle count never exceeds the defined cap and no unbounded list grows

#### Scenario: Pool drains when idle

- **WHEN** the component is idle for a sustained period
- **THEN** the active particle count returns to zero and the component's memory use stops growing

### Requirement: Per-frame work is computed once and reused

Scene projection and layout geometry SHALL be computed once per rendered frame and reused by every consumer in that frame, including drawing, hit-testing, and hover feedback. Pointer movement SHALL NOT trigger a full recomputation of the scene per event.

#### Scenario: Pointer movement does not multiply work

- **WHEN** the pointer moves continuously over a scene containing many elements
- **THEN** the number of full scene projections per frame remains at most one, regardless of pointer event frequency

#### Scenario: Drawing reuses the frame's geometry

- **WHEN** a frame is drawn
- **THEN** the connections, element drawing, and hit-testing all consume the same computed projection for that frame

### Requirement: Internal caches stay bounded

Per-element caches SHALL NOT grow without bound as the underlying data changes. Cache entries for removed or changed elements SHALL be released, or the cache SHALL be bounded and evict deterministically.

#### Scenario: Removing content shrinks the cache

- **WHEN** the scene is refreshed with fewer elements than before
- **THEN** the cache size decreases with the element count rather than retaining stale entries

#### Scenario: Repeated refreshes do not accumulate

- **WHEN** the scene is refreshed many times with the same elements
- **THEN** the cache size remains stable and memory usage does not grow

### Requirement: Components unregister and tear down cleanly

A visual component SHALL unregister itself from any process-wide registry when destroyed, stop all of its timers, and leave no reference behind. No orphaned component SHALL continue to receive events, and no stale event filter or signal handler SHALL remain connected to a destroyed widget.

#### Scenario: Registry returns to baseline

- **WHEN** many instances of a component are created and destroyed
- **THEN** the registry size returns to its baseline and no instance remains referenced

#### Scenario: No work after destruction

- **WHEN** a component has been destroyed
- **THEN** publishing events to that component's former channel performs no work and raises no error

#### Scenario: No orphaned event handling

- **WHEN** a panel is closed and reopened repeatedly
- **THEN** interactions fire once per user action rather than accumulating duplicate handlers

### Requirement: Idle repaint and layout work is bounded

An idle interface SHALL NOT perform continuous expensive layout recalculation or unbounded repaint work. Applying theme tokens, showing or hiding panels, and resizing SHALL NOT trigger repeated cascading re-layouts.

#### Scenario: Idle cost is bounded

- **WHEN** the interface is left idle with panels open, including the animated ones
- **THEN** the number of layout and resize events per second stays below the defined threshold

#### Scenario: Theme application is not a re-layout storm

- **WHEN** the theme and tokens are applied to the interface
- **THEN** layout recalculation completes once per affected widget rather than repeatedly cascading

### Requirement: The interface stays responsive during animation

No animation or scene computation SHALL block interaction. Pointer-driven interactions SHALL continue to track the pointer while animated scenes are running.

#### Scenario: Dragging stays smooth while scenes animate

- **WHEN** the user drags a panel while an animated scene is running
- **THEN** the panel tracks the pointer without visible lag and input events are handled within the defined latency budget

#### Scenario: No blocking work on the interaction path

- **WHEN** any animated component updates
- **THEN** it performs no blocking file, network, or device operation on the interface thread
