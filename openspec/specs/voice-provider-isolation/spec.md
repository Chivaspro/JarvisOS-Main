## Purpose

Defines per-provider isolation for JARVIS voice configuration: each speech provider owns its own
independent configuration subtree, Save and Save as Default operate only on the selected provider, the
persisted default is always a valid provider/voice pair, and switching providers can never contaminate
another provider's settings or silently substitute a different voice (the historical Fish Audio → Gemini
"Charon" collision).

## Requirements

### Requirement: Voice configuration is provider-scoped

The system SHALL store voice configuration in a nested `voice` block with `activeProvider`,
`defaultProvider`, `defaultVoice`, and an independent subtree per provider under `voice.providers`
(`fishAudio`, `elevenLabs`, `gemini`, `piperOnnx`, `edgeTts`, `kokoro`, `sapi`). Each provider's subtree
SHALL contain that provider's own credentials, endpoint, model, voice and provider-specific options. A
write to one provider's subtree SHALL NOT change any other provider's subtree, and the flat legacy keys
SHALL remain readable (dual-read) until migrated.

#### Scenario: Each provider owns its fields

- **WHEN** the voice configuration is loaded
- **THEN** Fish Audio, ElevenLabs, Gemini, Piper/ONNX, Edge TTS, Kokoro and SAPI each read their settings
  from their own subtree and no provider's fields appear inside another provider's configuration

#### Scenario: Legacy flat keys still resolve

- **WHEN** a configuration contains only the legacy flat keys (`tts_engine`, `tts_voice`, `fish_audio_*`,
  `elevenlabs_*`, `onnx_*`, `sapi_voice`, `voice_name`)
- **THEN** the provider settings resolve from those flat keys exactly as before this change

### Requirement: Save is scoped to the selected provider

The **Save** action on the Voice page SHALL validate only the selected provider's fields and SHALL
persist only that provider's subtree, additionally recording it as `activeProvider`. It SHALL NOT modify
any other provider's subtree, the default pair, or any non-voice setting. After a successful Save the
cached TTS engines SHALL be rebuilt so the next speech uses the saved provider.

#### Scenario: Saving Fish Audio changes only Fish Audio

- **WHEN** the user selects Fish Audio, edits its API key, voice and model, and clicks Save
- **THEN** only `providers.fishAudio` and `activeProvider` change in the persisted configuration, the
  ElevenLabs, Gemini, Piper, Edge TTS, Kokoro and SAPI subtrees are byte-identical to before, and the
  next spoken reply uses the Fish Audio engine with the saved voice and model

#### Scenario: Invalid provider configuration is rejected without side effects

- **WHEN** the selected provider's fields fail validation (missing or malformed API key, invalid
  endpoint URL, invalid voice or model identifier)
- **THEN** the save is refused with a provider-specific message, no configuration file is modified, and
  the previously active provider remains active

### Requirement: Save as Default persists a validated provider/voice pair

The **Save as Default** action SHALL persist the selected provider's subtree (as Save does), then
additionally persist `defaultProvider` and `defaultVoice` as one atomic pair. The stored voice SHALL
belong to the stored provider; the pair SHALL be validated before writing and again when loaded, and an
invalid pair SHALL be rejected (leaving the previous working pair intact) rather than repaired silently.
The Gemini Live voice is the voice of the Gemini provider alone and SHALL never be stored as the default
voice of another provider.

#### Scenario: Saving a Fish Audio default

- **WHEN** Fish Audio is selected with a Fish Audio voice and the user clicks Save as Default
- **THEN** `defaultProvider` is `fishAudio` and `defaultVoice` is the selected Fish Audio voice, stored
  together, and no Gemini voice name is written anywhere in the configuration

#### Scenario: Mismatched default pair is rejected

- **WHEN** a configuration (persisted or being saved) contains `defaultProvider = fishAudio` with a
  `defaultVoice` that belongs to Gemini
- **THEN** the pair is reported as invalid, the configuration is not activated, and the previously valid
  pair stays in effect

#### Scenario: Default pair survives a restart

- **WHEN** JARVIS restarts after Save as Default
- **THEN** the same provider, voice and provider options are active without user interaction

### Requirement: Provider switching preserves every provider's configuration

Cycling the provider selector (Fish Audio → ElevenLabs → Gemini → Piper/ONNX → Edge TTS → back) SHALL
restore each provider's own saved settings into the form when it is re-selected, SHALL NOT write any
provider's subtree merely by switching, and SHALL NOT change `activeProvider` until the user saves. Each
provider's panel SHALL present only that provider's fields.

#### Scenario: Switching away and back restores settings

- **WHEN** the user selects Fish Audio, then ElevenLabs, then back to Fish Audio without saving
- **THEN** the Fish Audio panel shows the previously saved Fish Audio settings unchanged, and the
  configuration file is unchanged by the switching itself

#### Scenario: Only the selected panel is editable

- **WHEN** a provider is selected on the Voice page
- **THEN** only that provider's fields are visible and editable, and no other provider's fields are
  presented to the user

### Requirement: Voice and model refresh is provider-scoped

For providers that expose remote voice or model lists (Fish Audio, ElevenLabs), a **Refresh** action
SHALL fetch the list using that provider's stored credentials and populate it into that provider's panel
only. The user's selected option SHALL be preserved after refresh where it still exists, and a failed
refresh SHALL be reported in that panel without discarding the saved selection.

#### Scenario: Fish Audio refresh stays inside Fish Audio

- **WHEN** the user clicks Refresh in the Fish Audio panel
- **THEN** the Fish Audio voice list is fetched with the Fish Audio credentials and the populated list is
  offered only in the Fish Audio panel

#### Scenario: Refresh failure does not lose the saved voice

- **WHEN** a refresh fails (network error, invalid key)
- **THEN** the panel reports the failure and the previously selected voice remains selected

### Requirement: Runtime speech follows the active provider

The active provider, not the last touched settings field, SHALL determine who renders speech: an active
Gemini provider renders through Gemini (native audio, or the Piper voice when the Gemini renderer setting
is the Piper pipeline), and any other active provider renders through that provider's engine with that
provider's own voice and model. The system SHALL NOT silently fall back to a different provider on
failure; a provider failure SHALL be reported as that provider's error.

#### Scenario: Fish Audio active means Fish Audio speaks

- **WHEN** `activeProvider` is `fishAudio` and a reply is rendered locally
- **THEN** the Fish Audio engine is invoked with the configured Fish Audio voice, model and credentials,
  and no Gemini voice is substituted

#### Scenario: Gemini active with the Piper renderer

- **WHEN** `activeProvider` is `gemini` and the Gemini renderer is the Piper pipeline
- **THEN** the reply text is rendered by the Piper voice from the Piper subtree, as before this change

#### Scenario: Provider failure is reported, not masked

- **WHEN** the active provider fails to synthesize (network error, rejected key, bad voice)
- **THEN** the error names the active provider and no other provider's engine is started for that reply

### Requirement: One-time migration preserves all existing settings

On first run with the new schema, the system SHALL migrate the legacy flat keys into the provider
subtrees exactly once, recorded by a migration marker: Fish Audio keys → `providers.fishAudio`,
ElevenLabs keys → `providers.elevenLabs`, the ONNX keys → `providers.piperOnnx`, Edge TTS voice/speed →
`providers.edgeTts`, the Kokoro voice → `providers.kokoro`, the SAPI voice → `providers.sapi`, the
`voice_output_mode` + `tts_engine` combination → `activeProvider` (with the Gemini renderer derived from
the mode), and `voice_name` → `providers.gemini.voice`. The migration SHALL NOT invent a default pair
from the shared legacy voice field, SHALL preserve the existing Gemini Charon configuration, and SHALL
leave the flat keys in place (dual-read). After migration the active provider SHALL produce the same
audible behaviour as before the migration.

#### Scenario: All providers survive migration

- **WHEN** a pre-migration configuration with configured Fish Audio, ElevenLabs, Piper and Gemini settings
  is migrated
- **THEN** each provider's subtree contains that provider's previous values, including the Gemini
  Charon voice, and no provider's settings are lost or swapped

#### Scenario: The legacy shared voice never becomes a default

- **WHEN** a configuration that never had a default pair is migrated
- **THEN** `activeProvider` is set but no `defaultVoice` is invented from the legacy shared voice field

#### Scenario: Migration runs once

- **WHEN** JARVIS restarts after migration with edited provider settings
- **THEN** the migrated (edited) values are used and the flat keys are not re-applied over them

#### Scenario: Migration preserves the active pipeline

- **WHEN** a pre-migration configuration used the Gemini→Piper pipeline (or any legacy mode alias)
- **THEN** after migration the active provider is Gemini with the Piper renderer, and the audible
  behaviour is unchanged

### Requirement: Provider errors are specific and visible

The system SHALL raise or surface provider-specific validation and runtime errors — missing API key,
invalid API key or credentials, invalid base URL/endpoint, invalid model ID, invalid voice, provider
initialization failure, voice or model loading failure, and network/API failure — attributed to the
provider that caused them, in the Voice page status area and the application log.

#### Scenario: Missing key names its provider

- **WHEN** a provider's credentials are missing and speech is requested or its panel is saved
- **THEN** the error names that provider and the missing credential, and no unrelated provider is
  implicated
