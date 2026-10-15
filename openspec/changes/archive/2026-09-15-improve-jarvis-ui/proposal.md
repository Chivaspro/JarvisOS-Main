## Why

The current JARVIS dashboard uses a generic system font and lacks coordinated HUD-style transitions between panels, and the microphone button shows no live audio feedback. This change aligns the interface with the Iron Man / JARVIS aesthetic already established in the 3D brain widget and login screen — crisp futuristic typography, coordinated panel transitions, and a microphone soundwave driven by actual mic input rather than a static icon.

## What Changes

- Introduce a consistent futuristic font stack for the chat panel and navigation tabs, layered over the existing system font as fallback to preserve readability.
- Add a coordinated HUD transition when the Vision tab is activated: System/Network panels slide left, Quick Info/Active Apps slide right, the main Jarvis logo fades out, with smooth easing and reversed animation on return.
- Replace the static microphone icon with a real-time soundwave visualization driven by the user's actual microphone volume levels, with smooth updates and idle state when silent.
- Reuse the existing UI architecture and components; no rewrites, no removal of existing features, buttons, panels, settings, or responsiveness.

## Capabilities

### New Capabilities

- `jarvis-ui-typography`: Futuristic, consistent typography for the chat panel and navigation tabs that matches the existing JARVIS/Iron Man-inspired design without reducing readability or breaking layout.
- `jarvis-ui-vision-transition`: Coordinated HUD-style panel transition animation when the Vision tab is activated, with smooth easing, consistent timing, no overlapping or jumping elements, and reversed animation when switching back.
- `jarvis-ui-mic-soundwave`: Real-time microphone soundwave visualization that responds to actual microphone volume, with smooth updates, idle state when silent, and preservation of existing voice input functionality.

### Modified Capabilities

- None. No existing spec-level requirements are being changed; this is purely an additive visual/interaction enhancement.

## Impact

- **Affected files**: `dashboard/static/app.html` (primary UI markup, styles, and scripts).
- **Affected systems**: Client-side browser only; no backend, API, or server changes required.
- **Dependencies**: No new external libraries. Fonts will be loaded via CSS `@font-face` from locally bundled font files or a web font CDN with system-font fallbacks. Audio processing continues to use the existing `AudioContext` / `AudioWorkletNode` pipeline already present in the mic button handler.
- **Risk**: Low. Changes are additive CSS and JavaScript within the existing single-file architecture. Existing voice input, encryption, WebSocket, and file-upload functionality are preserved.