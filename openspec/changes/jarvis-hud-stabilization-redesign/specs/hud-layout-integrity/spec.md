## Purpose

Defines the layout rules that keep HUD content readable and correctly positioned: content-driven sizing, no clipped or overlapping text, a consistent alignment and spacing hierarchy, and defined behavior when windows or screens change size.

## ADDED Requirements

### Requirement: Content-driven sizing

Containers SHALL derive their size from their content through size policies and minimum-size hints. A fixed width or height SHALL be used only where the content is provably bounded, and text-bearing widgets SHALL wrap or elide rather than clip.

#### Scenario: Text that exceeds its container is not clipped

- **WHEN** a label, title, or status string is longer than its container's available width
- **THEN** the text wraps to additional lines or is elided with a visible ellipsis, and no glyph is cut off or hidden behind a neighbouring element

#### Scenario: Fixed dimensions are justified

- **WHEN** the fixed-size declarations in the HUD are audited
- **THEN** each one either bounds non-text content (icons, indicators, separators) or is replaced by a content-driven constraint

### Requirement: Every label fits at default settings

At the default font, scale, and window size, every visible text element SHALL fit within its own bounds and within its container. No text SHALL render outside its container rectangle, and no text SHALL be truncated without an elision marker.

#### Scenario: Automated fit check across all panels

- **WHEN** every panel is constructed offscreen at its default size and each visible text element's laid-out size is compared with its container
- **THEN** no text element's required size exceeds the space available to it

#### Scenario: Long-content stress

- **WHEN** each panel is built with deliberately over-long titles, subtitles, and status strings
- **THEN** the header grows or wraps without clipping the title, and no other control is pushed outside the panel

### Requirement: No overlapping text or controls

Text-bearing elements SHALL NOT overlap each other or any interactive control. In particular, any pointer-capture region used for dragging SHALL be confined to the header band and SHALL NOT intersect title, subtitle, or status text bounds.

#### Scenario: Sibling element bounds do not collide

- **WHEN** the laid-out bounds of sibling widgets within a panel header and body are compared
- **THEN** no two text-bearing or interactive elements intersect

#### Scenario: Drag zone does not cover the title

- **WHEN** the header drag region's geometry is compared with the title and subtitle bounds
- **THEN** the drag region lies within the header band, spans the intended drag area, and does not intersect any text bounds

### Requirement: Consistent alignment and spacing hierarchy

Panels SHALL follow one visual hierarchy — title, title decoration, status or subtitle, content, controls — with margins and gaps taken from the shared spacing scale. Comparable elements SHALL share a common alignment grid and consistent edge insets.

#### Scenario: Content edges are aligned

- **WHEN** the left content edge of each panel's header, body, and control rows is measured
- **THEN** they align to a common inset within a small tolerance rather than using unrelated offsets

#### Scenario: Gaps come from the scale

- **WHEN** the vertical and horizontal gaps inside a panel are inspected
- **THEN** each gap is a value from the shared spacing scale, with no arbitrary per-widget spacing values

### Requirement: No cramped content or dead space

Panel minimum sizes SHALL be large enough for their content to be readable and operable, and content taller than the available area SHALL scroll rather than compress or overflow. Large unused gaps inside a panel SHALL be avoided.

#### Scenario: Minimum size keeps content usable

- **WHEN** a panel is resized to its minimum size
- **THEN** no control is clipped, unreachable, or pushed outside the visible bounds, and a scroll area is available if the content does not fit

#### Scenario: No unexplained empty regions

- **WHEN** a panel's layout is rendered at its default size
- **THEN** no single stretch region occupies a disproportionate share of the panel while content above it is compressed

### Requirement: Defined behavior on resize and screen change

On window resize, panel resize, and screen geometry change, panels SHALL reflow without losing content: text re-wraps, alignment is preserved, and no content becomes invisible. Panels SHALL remain within the visible screen area and SHALL NOT be left partly or fully offscreen.

#### Scenario: Panel remains reachable after resize

- **WHEN** a panel is resized smaller and then larger again
- **THEN** all of its content is visible or scrollable at both sizes and the header stays aligned

#### Scenario: Offscreen panels are brought back

- **WHEN** a panel's stored position places it partly or fully outside the available screen area, including after a display change
- **THEN** the panel is repositioned so its header and controls are reachable

#### Scenario: Main surface resize does not alter unrelated panels

- **WHEN** the main window is resized
- **THEN** the geometry of every floating panel is unchanged except for those the user resized

### Requirement: Text stays visible against scroll containers and overlays

Status labels, counters, and headings SHALL NOT be hidden behind scrollbars, overlays, or adjacent containers, and SHALL NOT disappear when a container scrolls or sizes to its content.

#### Scenario: Status indicators remain visible

- **WHEN** a panel containing a scrollable list is shown at its default size
- **THEN** its status indicator and counters are fully visible and not covered by the scroll area or its scrollbar
