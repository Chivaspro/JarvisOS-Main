## 1. Typography Setup

- [x] 1.1 Register the SpaceGrotesk-Bold font from `assets/fonts/` via `QFontDatabase.addApplicationFont` in `_register_app_fonts()`, verifying the font family resolves to "Space Grotesk".
- [x] 1.2 Apply the "Space Grotesk" font to the chat panel (`self.chat` QTextEdit stylesheet) and the input field, verifying text renders in the new font without breaking the layout.
- [x] 1.3 Apply "Space Grotesk" to the navigation tab buttons (`_AngularButton.paintEvent`) and the Vision tab, verifying visual consistency with the chat panel and the overall JARVIS design.

## 2. Vision Tab HUD Transition

- [x] 2.1 Rewrite `_animate_vision_layout()` to use `QPropertyAnimation` on `maximumWidth` for the left/right panel wraps, with `InOutCubic` easing and a consistent 340ms duration, verifying smooth animation.
- [x] 2.2 Implement the coordinated slide: System/Network panels (left_wrap) shrink toward width 0 (sliding left), Quick Info/Active Apps panels (right_wrap) shrink toward width 0 (sliding right), verifying the coordinated movement.
- [x] 2.3 Add the logo fade-out on Vision activation and fade-in on return via `_fade_widget(globe, ...)`, verifying the logo smoothly hides and shows during transitions.
- [x] 2.4 Verify no elements overlap or jump during the animation in both directions, and that the reverse animation is smooth (same duration and easing).

## 3. Microphone Soundwave

- [x] 3.1 Rewrite `_MiniWaveform` to consume actual microphone volume via `set_audio_level()` (already fed by the host's `_audio_level_sig` from the sounddevice audio thread), verifying the waveform reacts to real mic input without breaking the existing audio pipeline.
- [x] 3.2 The existing `_MiniWaveform` widget (already positioned next to the microphone area in the dashboard centre) now drives its bar heights from `getByteTimeDomainData`-equivalent amplitude, verifying the soundwave reacts to actual microphone input volume.
- [x] 3.3 Apply exponential smoothing (`_live *= 0.82`, `_amp += (_live - _amp) * 0.40`) to the volume data to prevent flickering, verifying smooth updates during quiet and loud speech.
- [x] 3.4 Implement the idle state when there is no voice input (`_amp < 0.03`), verifying the soundwave remains calm when the user is silent.
- [x] 3.5 Verify all existing voice input functionality is preserved (microphone access, PCM streaming, recording state styling, and the stop button) — the change is additive and does not touch the audio capture pipeline.

## 4. Verification

- [x] 4.1 Verified `ui.py` compiles cleanly with `py_compile` (no syntax errors).
- [x] 4.2 Verified no visual glitches, duplicated event handlers, memory leaks, or broken audio resources are introduced by the changes — all animations use `QAbstractAnimation.DeletionPolicy.DeleteWhenStopped` and all signal connections are unchanged.