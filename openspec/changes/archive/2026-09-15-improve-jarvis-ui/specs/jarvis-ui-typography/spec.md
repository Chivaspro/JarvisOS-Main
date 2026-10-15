## Purpose

Provides consistent, futuristic, clean typography for the JARVIS chat panel and navigation tabs that matches the existing Iron Man / JARVIS aesthetic without reducing readability or breaking the existing layout.

## ADDED Requirements

### Requirement: Futuristic font applied to chat panel text

The system SHALL apply a futuristic, clean font to all text inside the chat panel (message bubbles, labels, system messages, file cards, and the input field), while preserving the existing system font as a fallback to guarantee readability when the font is unavailable.

#### Scenario: Chat panel renders with new font

- **WHEN** the dashboard loads
- **THEN** all chat panel text uses the new futuristic font family with system font fallback

#### Scenario: Font fallback works when web font is unavailable

- **WHEN** the web font fails to load
- **THEN** the chat panel text falls back to the existing system font stack without breaking the layout

### Requirement: Complementary font applied to navigation tabs

The system SHALL apply the same or a complementary futuristic font to the text inside the navigation tabs, maintaining visual consistency with the chat panel and the overall JARVIS design.

#### Scenario: Navigation tabs render with complementary font

- **WHEN** the dashboard loads
- **THEN** all navigation tab text uses the complementary futuristic font with system font fallback

### Requirement: Typography does not reduce readability or break layout

The system SHALL maintain the existing font sizes, line heights, letter spacing semantics, and layout constraints; the new typography MUST NOT reduce readability or cause any layout breakage.

#### Scenario: Readability preserved at all viewport sizes

- **WHEN** the dashboard is viewed at any supported viewport width
- **THEN** all text remains legible and no element overflows or is clipped

#### Scenario: Existing layout constraints preserved

- **WHEN** the new font is applied
- **THEN** all existing paddings, margins, widths, and heights produce the same visual layout as before (no shift in element positions)