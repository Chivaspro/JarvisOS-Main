## ADDED Requirements

### Requirement: Lifecycle-owned startup and shutdown for the voice chain
The voice chain (Gemini Live audio, hybrid local voice, wake detection, audio
device selection) SHALL be initialized and shut down by the application lifecycle
in a deterministic order (configuration → logging → Gemini → orchestration →
memory → computer → vision/audio → optional agents → UI → READY), instead of
ad-hoc startup code. Shutdown SHALL stop audio streams, camera, background tasks,
computer-control sessions and coding agents with no orphan processes, and SHALL
preserve the existing shutdown behavior of the voice chain itself.

#### Scenario: Startup order is deterministic
- **WHEN** the application boots
- **THEN** voice initializes after configuration and logging, and READY is reached without depending on optional subsystems

#### Scenario: Shutdown leaves no orphans
- **WHEN** the application exits
- **THEN** audio streams, camera, background tasks and agent processes are stopped and no orphan process remains

### Requirement: Voice works without optional subsystems
Voice interaction SHALL NOT require any optional subsystem introduced by this
change (orchestrator, Mem0, Cua backend, coding agents, Qdrant, Langfuse); each
optional subsystem's absence SHALL degrade to the local/default path without
affecting audio latency or the existing mode selection behavior.

#### Scenario: Fresh install without optional packages
- **WHEN** a fresh installation has none of the optional packages installed
- **THEN** voice conversation, wake word and audio device selection work exactly as before this change
