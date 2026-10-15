## Context

The JARVIS dashboard is a single-file application (`dashboard/static/app.html`) containing inline CSS and JavaScript. The existing UI uses a generic system font stack (`-apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif`) and has no coordinated panel transitions. The microphone button currently shows a static 🎤 icon with a pulse animation but no real-time audio feedback. The 3D brain widget (`dashboard/brain3d.py`) and login screen already establish an Iron Man / JARVIS aesthetic with cyan accents, glassmorphism, and futuristic styling. This design extends that aesthetic to the chat panel, navigation, and microphone visualization.

See proposal.md for the motivation and scope.

## Goals / Non-Goals

**Goals:**
- Apply a consistent futuristic font to the chat panel and navigation tabs, layered over the existing system font as fallback.
- Implement a coordinated HUD transition for the Vision tab with smooth easing and reversed animation.
- Replace the static mic icon with a real-time soundwave driven by actual microphone volume.
- Reuse the existing single-file architecture; no rewrites, no removal of existing features.

**Non-Goals:**
- No changes to the backend, API, WebSocket protocol, encryption, or server logic.
- No new external JavaScript libraries or build tools.
- No changes to the login screen or 3D brain widget.
- No redesign of panel content or layout structure.

## Decisions

### Decision 1: Font loading via CSS @font-face with system fallback

**Rationale:** The dashboard is a static HTML file served directly by FastAPI. Loading a web font via `@font-face` from a local bundled font file (or a CDN with `font-display: swap`) is the simplest approach that works without a build step. The existing system font stack is preserved as the `font-family` fallback chain, guaranteeing readability when the font is unavailable.

**Alternatives considered:**
- *Google Fonts CDN link*: Simpler but adds an external dependency and a flash of unstyled text. Rejected in favor of `@font-face` with `font-display: swap` for more control.
- *Bundled font files only*: Most reliable offline, but requires shipping font files in the repo. We'll use a local `@font-face` declaration pointing to a font file in `dashboard/static/` with system font fallback.

### Decision 2: CSS-based panel transition with transform and opacity

**Rationale:** The Vision transition requires multiple panels to slide in opposite directions while the logo fades. CSS `transform: translateX()` and `opacity` are GPU-accelerated, run on the compositor thread, and can be coordinated via a single class toggle on a container element. This produces smooth 60fps animations with minimal JavaScript.

**Alternatives considered:**
- *JavaScript animation library (e.g., GSAP)*: Overkill for this single transition; adds an external dependency. Rejected.
- *JavaScript manual animation loop*: More control but harder to keep consistent timing/easing across elements. Rejected in favor of CSS transitions with a shared transition duration and easing function.

### Decision 3: Microphone soundwave driven by AudioContext analyser

**Rationale:** The existing mic handler already creates an `AudioContext` and an `AudioWorkletNode` (with a `ScriptProcessor` fallback) that extracts Float32 PCM from the microphone. We can insert an `AnalyserNode` between the source and the worklet node to read real-time volume data (`getByteTimeDomainData` or `getByteFrequencyData`) without interfering with the existing PCM streaming pipeline. The soundwave canvas or CSS elements are updated on a `requestAnimationFrame` loop, smoothed to prevent flickering.

**Alternatives considered:**
- *Web Speech API volume events*: Less granular and not available in all browsers. Rejected.
- *Separate audio context for visualization*: Would duplicate microphone access and could conflict with the existing context. Rejected in favor of tapping into the existing `AudioContext`.

### Decision 4: Reuse existing recording state styling

**Rationale:** The mic button already has `.recording` class and `mic-pulse` animation. The soundwave element will be shown/hidden alongside the existing recording state rather than replacing it, preserving all existing interactions.

## Risks / Trade-offs

[Risk: Font file not bundled or fails to load] → Mitigation: Use `font-family` fallback chain to the existing system font; the interface remains readable even if the font is unavailable.

[Risk: AnalyserNode adds minor CPU overhead] → Mitigation: Use a single `getByteTimeDomainData` call per animation frame (60fps max); the overhead is negligible compared to the existing PCM encoding and WebSocket streaming.

[Risk: Browser microphone permission prompts may block the analyser] → Mitigation: The analyser is created only after successful `getUserMedia` and `ctx.resume()`, exactly where the existing pipeline already runs.

[Risk: CSS transition performance on low-end devices] → Mitigation: Use only `transform` and `opacity` properties (GPU-accelerated); avoid animating `width`, `height`, or `top`/`left`.

## Migration Plan

This change is client-side only and requires no server migration. To deploy:
1. Update `dashboard/static/app.html` with the new CSS and JavaScript.
2. Add the font file to `dashboard/static/` and reference it in the `@font-face` rule.
3. No database, API, or configuration changes are needed.
4. To rollback, restore the previous version of `app.html` and remove the font file.

## Open Questions

- Should the font be bundled locally or loaded from a CDN? Decision: bundle locally for offline reliability; CDN as a future enhancement if needed.
- Which specific font family to use? This can be decided during implementation by selecting a font that matches the JARVIS aesthetic (e.g., a geometric sans-serif like Orbitron, Exo, or Montserrat) and testing readability.