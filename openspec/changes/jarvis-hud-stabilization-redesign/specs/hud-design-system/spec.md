## Purpose

Gives the entire desktop HUD one authoritative design language: a single token set for colour, type, geometry, and spacing, plus a small shared vocabulary of panel, title, status, and control components that every surface renders through.

## ADDED Requirements

### Requirement: Single source of design tokens

Every colour, radius, border weight, font family, and spacing value used by the desktop HUD SHALL come from one shared design-token set. No view module SHALL declare its own palette, radius scale, or font stack.

#### Scenario: Token change propagates everywhere

- **WHEN** a token value (for example the primary accent) is changed in the shared token set
- **THEN** every surface that uses that token renders the new value without editing any other module

#### Scenario: No module declares a private palette

- **WHEN** the HUD modules are audited for colour constants
- **THEN** the main window, Settings, the layout editor, and the Memory Core each resolve colour from the shared token set rather than from locally declared hex values

### Requirement: Closed token vocabulary

The design language SHALL expose exactly three corner radii, three border weights, one primary accent, one signature accent reserved for the live/armed state, and a defined status colour set. Values outside this vocabulary SHALL NOT be used for borders, radii, or accents.

#### Scenario: Radii and border weights stay within the vocabulary

- **WHEN** every border and rounded-rectangle drawn by the HUD is inspected
- **THEN** each uses one of the three defined radii and one of the three defined border weights

#### Scenario: Signature accent stays reserved

- **WHEN** the interface is idle
- **THEN** the signature accent appears only on elements representing a live, armed, or active state, and never as a general decoration or default control colour

### Requirement: Typography stack with guaranteed fallback

The HUD SHALL use a defined display, interface, and monospace font stack drawn from the bundled fonts, registered once at startup, with a system fallback chain. A missing or unavailable font SHALL NOT change layout, truncate text, or prevent the interface from rendering.

#### Scenario: Font unavailable at startup

- **WHEN** a bundled font cannot be loaded
- **THEN** the affected text renders in the fallback family and no element changes size or position beyond normal font-metric differences

#### Scenario: No dangling font references

- **WHEN** the font families referenced anywhere in the HUD are compared with the families bundled and registered at startup
- **THEN** every referenced family is bundled and registered, and no surface requests a family that is missing or unregistered

#### Scenario: Unused bundled fonts are reported

- **WHEN** the bundled font files are compared with the families the HUD actually uses
- **THEN** each unused file is reported in the audit so it can be adopted or retired deliberately, rather than being referenced by accident

### Requirement: One title treatment for every panel

Every panel SHALL present its heading through the same component and hierarchy: title, title decoration, then status or subtitle, with content and controls below. No panel SHALL define its own title styling.

#### Scenario: Titles are consistent across panels

- **WHEN** any two floating panels are compared
- **THEN** both use the same title font, letter spacing, colour role, and decoration style, and their titles share the same baseline offset from the panel edge

#### Scenario: Title decoration never collides with content

- **WHEN** a panel's title decoration, title text, and status indicator are laid out
- **THEN** their bounds do not intersect each other at any supported panel width

### Requirement: Shared component vocabulary

Buttons, fields, chips, section headings, and cards SHALL come from a small shared set of components whose dimensions derive from the spacing scale. Control heights SHALL NOT be set per widget with arbitrary fixed values.

#### Scenario: Equivalent controls match across surfaces

- **WHEN** a primary action button is rendered in a floating panel and in Settings
- **THEN** both resolve to the same height, radius, border weight, and font role

#### Scenario: Spacing scale change is global

- **WHEN** a spacing-scale value is changed
- **THEN** the affected margins, gaps, and control padding update across every surface that uses the scale

### Requirement: Existing user accent preference is preserved

An accent colour already stored in the user's configuration SHALL continue to apply after the token consolidation, migrated to the nearest token when it is a legacy value. The user's preference SHALL NOT be silently reset or ignored.

#### Scenario: Legacy accent value is migrated

- **WHEN** the stored accent colour is a value from the previous palette
- **THEN** the interface renders the corresponding current accent and re-theming still applies to already-created widgets

#### Scenario: Custom accent value still honoured

- **WHEN** the stored accent colour is a custom value the user chose
- **THEN** that colour is applied at startup and survives a theme refresh

### Requirement: Restrained and original decorative language

Decorative elements SHALL be limited to thin technical lines, segmented lines, corner brackets, status dots, micro labels, separators, and subtle grids or telemetry marks. Glow SHALL be punctuation only, with a bounded number of glow layers and opacity. Decorative elements SHALL NOT cover, overlap, or obscure any text, control, or status indicator, and SHALL NOT be copied from third-party artwork.

#### Scenario: Glow stays bounded

- **WHEN** any panel is rendered
- **THEN** the number of glow layers drawn around an edge is at most three and no glow layer exceeds the defined opacity ceiling

#### Scenario: Decoration never obscures information

- **WHEN** decorative elements and content bounds are compared
- **THEN** no decorative element intersects a text, control, or status region

#### Scenario: No continuous decorative motion competes for attention

- **WHEN** the interface is idle
- **THEN** decorative animation is limited to the defined subtle effects and does not move or resize text or controls
