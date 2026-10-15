## Purpose

Makes the Memory Core's 3D scene stable and predictable: the central orb is a permanent anchor, position / rotation / scale / camera / animation are independent concerns, and secondary memory orbs can be dragged, attracted, and merged without disturbing anything else.

## ADDED Requirements

### Requirement: The central orb is a stable anchor

The Memory Core's central orb SHALL occupy a fixed, predictable position within its panel. It SHALL NOT continuously rotate, drift, float, or change position as a result of animation, camera movement, elapsed time, or the movement of any other orb.

#### Scenario: Anchor holds still while idle

- **WHEN** the Memory Core panel is idle with no user input for an extended period
- **THEN** the central orb's rendered centre stays within a small fixed tolerance of its resting position, and does not rotate

#### Scenario: Anchor holds still while the scene animates

- **WHEN** activity, memory events, or cosmetic effects are animating around the orb
- **THEN** the orb's rendered centre remains within the same tolerance

#### Scenario: Anchor holds still while another orb moves

- **WHEN** a secondary orb is dragged anywhere within the scene
- **THEN** the central orb's rendered centre is unchanged

### Requirement: Position, rotation, scale, camera, and animation are isolated

Scene state SHALL be partitioned so that position, rotation, scale, camera, and animation are stored and modified independently. No animation or interaction SHALL have a code path that can alter the orb's anchor position, and changing one concern SHALL NOT change the others as a side effect.

#### Scenario: Rotating the scene does not move the anchor

- **WHEN** the view rotation changes through user rotation or camera movement
- **THEN** the anchor's world position is unchanged

#### Scenario: Zoom and pan change only the view

- **WHEN** the user zooms or pans the scene
- **THEN** only the projected view changes; the anchor's world position and the relative arrangement of the scene are unchanged

#### Scenario: Animation cannot write to the anchor

- **WHEN** the animation clock advances with any activity state
- **THEN** the anchor position value reported by the scene is unchanged

### Requirement: Secondary orbs are draggable

The user SHALL be able to select a secondary orb and drag it. While dragging, the selected orb follows the pointer smoothly and every other orb remains stable.

#### Scenario: Dragging a selected orb

- **WHEN** the user presses on a secondary orb and moves the pointer
- **THEN** that orb follows the pointer, remains selected for the duration of the drag, and no other orb moves

#### Scenario: Dragging the background does not grab an orb

- **WHEN** the user presses on empty scene space
- **THEN** no orb is captured and the existing scene-level interaction is unchanged

### Requirement: Magnetic attraction is smooth and monotonic

When a dragged orb enters the attraction radius of the core, a magnetic attraction SHALL begin. Attraction strength SHALL increase smoothly as distance decreases, motion SHALL be interpolated with easing rather than applied as an instantaneous jump, and the orb SHALL NOT oscillate, jitter, or accelerate violently.

#### Scenario: Attraction starts at the boundary

- **WHEN** a dragged orb crosses the attraction radius while still under the user's control
- **THEN** the orb begins to be pulled toward the core with a force that starts near zero and grows as the distance shrinks

#### Scenario: Approach is monotonic and bounded

- **WHEN** a dragged orb is released inside the attraction radius but outside the snap radius
- **THEN** its distance to the core decreases monotonically toward the pulling target, its speed stays below the defined maximum, and it does not overshoot and bounce back

#### Scenario: No jitter near the boundary

- **WHEN** an orb hovers near the attraction boundary
- **THEN** its position settles without visible oscillation under the defined position tolerance

### Requirement: Snap and merge are smooth and idempotent

When a dragged orb enters the snap radius, it SHALL move smoothly to the core and merge or lock into it. Merging SHALL be applied once, SHALL NOT teleport the orb, and SHALL leave the scene in a consistent state.

#### Scenario: Releasing inside the snap radius

- **WHEN** the user releases an orb inside the snap radius
- **THEN** the orb eases into the core and becomes merged or locked, with no position jump at any frame

#### Scenario: Releasing outside the snap radius

- **WHEN** the user releases an orb outside the snap radius
- **THEN** the orb comes to rest at a stable position with eased motion and without oscillating

#### Scenario: Merge is applied once

- **WHEN** an orb has merged into the core
- **THEN** repeated frames or repeated pointer events do not merge it again or duplicate it in the scene

#### Scenario: Interrupted drag leaves a consistent scene

- **WHEN** a drag is interrupted by the panel closing, resizing, or losing pointer capture
- **THEN** no orb remains stuck to the pointer, the scene's stored state matches what is rendered, and reopening the panel renders normally

### Requirement: Cosmetic effects never move the anchor

Optional visual feedback — including connecting lines, energy trails, pulses, glow, magnetic rings, particle flow, and subtle scale transitions — SHALL be lightweight and SHALL NOT modify the anchor position or any orb's world position. Disabling the effects SHALL NOT change interaction behavior.

#### Scenario: Effects cannot shift the scene

- **WHEN** the effects are enabled and active
- **THEN** the anchor position and every orb's world position are identical to their values with the effects disabled

#### Scenario: Effects degrade gracefully

- **WHEN** the effects are disabled
- **THEN** dragging, attraction, and snapping behave exactly as before, and the panel still renders without error

### Requirement: Scene rendering adapts to panel size without losing content

Changing the Memory Core panel's size or the window size SHALL re-frame the scene (scale and camera framing) without moving the anchor out of view or pushing the visible scene outside the panel bounds.

#### Scenario: Shrinking the panel keeps the anchor visible

- **WHEN** the panel is resized to its minimum size
- **THEN** the anchor is still visible within the panel and secondary orbs are not clipped out in a way that makes interaction impossible

#### Scenario: Growing the panel re-frames, not re-lays-out

- **WHEN** the panel is enlarged
- **THEN** the scene scales up around the anchor and the relative arrangement of orbs is preserved

### Requirement: Memory activity remains observable

The scene SHALL continue to reflect real memory activity (saves, recalls, learns, and related brain state) without the visualization interfering with the underlying data or with interaction.

#### Scenario: Activity events are reflected visually

- **WHEN** a memory event is published while the panel is open
- **THEN** the scene shows a bounded visual response and the anchor and scene state remain consistent

#### Scenario: Visualization failure does not break memory storage

- **WHEN** the scene cannot render for any reason
- **THEN** memory events are still stored and retrievable, and no exception escapes to the caller publishing them
