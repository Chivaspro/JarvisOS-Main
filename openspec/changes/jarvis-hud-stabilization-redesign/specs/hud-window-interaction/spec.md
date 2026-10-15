## Purpose

Defines one predictable interaction model for every floating HUD panel: how a panel is dragged, what must never be captured by a drag zone or a decorative layer, how stacking order behaves, and how panel geometry is remembered and restored.

## ADDED Requirements

### Requirement: Every floating panel has one discoverable drag zone

Each floating panel SHALL be movable by dragging its header band with a plain primary-button drag, and that header SHALL present a visible grab affordance. A keyboard modifier SHALL NOT be required to move a panel.

#### Scenario: Plain drag on the header moves the panel

- **WHEN** the user presses the primary button inside the panel header and moves the pointer
- **THEN** the panel follows the pointer smoothly and its header stays under the grab point

#### Scenario: Pressing in the content area does not move the panel

- **WHEN** the user presses the primary button on non-interactive content area of a panel
- **THEN** the panel does not move

#### Scenario: Drag is discoverable

- **WHEN** the pointer moves over the panel header
- **THEN** the cursor indicates that the header is draggable, and the same affordance is used on every floating panel

### Requirement: Dragging never displaces control input

Interactive controls SHALL receive pointer events in preference to any drag handling. Buttons, toggles, sliders, text inputs, tabs, list items, scroll areas, numeric fields, and embedded browser surfaces SHALL behave normally while the panel remains stationary.

#### Scenario: Header controls remain clickable

- **WHEN** the user clicks a button, toggle, or close control inside a header
- **THEN** that control activates and the panel does not move

#### Scenario: Dragging a slider does not drag the panel

- **WHEN** the user drags a slider handle or scrollbar inside a panel
- **THEN** the value or scroll position changes and the panel geometry is unchanged

#### Scenario: Embedded browser interaction is unaffected

- **WHEN** the user interacts with page content inside the embedded browser pane
- **THEN** the page receives the interaction normally and the panel does not move or resize

### Requirement: No invisible or oversized hit regions

Any region that captures pointer input for dragging SHALL be either visually apparent or confined to the header band. Hit regions SHALL NOT be larger than the affordance they represent, and SHALL NOT lie over text or over any control.

#### Scenario: Drag region cannot be triggered from an invisible strip

- **WHEN** the pointer presses outside the header band, anywhere else in the panel
- **THEN** no drag begins

#### Scenario: Hit region geometry is bounded

- **WHEN** every drag-capturing region's geometry is inspected
- **THEN** each lies within its panel's header band and intersects no adjacent panel or text bounds

### Requirement: Decorative layers never capture mouse events

Backgrounds, grids, glows, brackets, separators, scan overlays, and animated decoration SHALL pass pointer events through to the panel or control beneath them. A decorative element SHALL capture pointer input only when it is itself an explicit interactive control.

#### Scenario: Click on decoration reaches what is underneath

- **WHEN** the user clicks on a decorative backdrop, grid, or glow region of a panel
- **THEN** the click is delivered to the widget underneath, or to the panel itself, and is not swallowed

#### Scenario: Closing a panel still works over decoration

- **WHEN** the user clicks a panel's close control while decorative layers are rendered
- **THEN** the panel closes

### Requirement: Deterministic stacking order

Stacking order SHALL be deterministic: interacting with a panel brings that panel to the front consistently, decorative overlays SHALL always render behind the content they decorate, and the main surface's stay-on-top behavior with respect to other applications SHALL be preserved.

#### Scenario: Raising a panel is consistent

- **WHEN** the user interacts with one panel and then another
- **THEN** the most recently interacted panel is drawn above the other, and its header remains clickable

#### Scenario: Overlays stay behind content

- **WHEN** a panel with a decorative backdrop is shown
- **THEN** the backdrop renders behind the panel's content and cannot appear over controls or text

### Requirement: Panel geometry is remembered and restored safely

Each panel's position and size SHALL persist across restarts and SHALL be restored within the currently available screen area. Saving or restoring one panel's geometry SHALL NOT modify any other panel's geometry.

#### Scenario: Positions survive a restart

- **WHEN** the user moves and resizes panels, then restarts the application
- **THEN** each panel reappears at its remembered position and size, clamped to the visible screen

#### Scenario: One panel's resize leaves others untouched

- **WHEN** one panel is resized
- **THEN** every other panel's position and size are exactly unchanged

#### Scenario: Main-window resize leaves panel geometry untouched

- **WHEN** the main window is resized or maximized
- **THEN** no floating panel is moved or resized as a side effect

### Requirement: Dragging is always safely recoverable

A drag that is interrupted — pointer released outside the panel, pointer capture lost, the panel hidden or resized mid-drag, or the screen changed mid-drag — SHALL leave the panel in a consistent, reachable state with no stuck or lost interaction.

#### Scenario: Release outside the panel

- **WHEN** the user drags a panel and releases the pointer while it is outside the panel or the window
- **THEN** the drag ends, the panel remains wholly reachable on the screen, and its position is remembered

#### Scenario: Panel hidden mid-drag

- **WHEN** a panel is hidden or closed while a drag is in progress
- **THEN** no subsequent pointer movement moves any panel, and reopening the panel behaves normally

### Requirement: Existing keyboard and shortcut behavior is preserved

Existing HUD keyboard interaction SHALL continue to work after the drag model changes, including mute, fullscreen, and interrupt shortcuts, and shortcut handling SHALL NOT be blocked by a drag zone or an overlay.

#### Scenario: Shortcuts still fire

- **WHEN** the user presses the documented global shortcuts while panels are open
- **THEN** the corresponding action fires exactly once

#### Scenario: Drag zone does not intercept key handling

- **WHEN** a panel header has focus or the pointer is over a drag zone
- **THEN** keyboard shortcuts behave exactly as they do elsewhere in the interface
