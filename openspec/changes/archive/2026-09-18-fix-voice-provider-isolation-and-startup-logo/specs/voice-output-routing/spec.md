## MODIFIED Requirements

### Requirement: SAVE AS DEFAULT VOICE persists the selected arrangement

The **SAVE AS DEFAULT VOICE** action SHALL persist the arrangement currently selected in Settings: the
chosen voice output mode, the selected Piper voice (model and sidecar configuration), the selected
execution provider, and the local engine settings. It SHALL additionally persist the selected provider
and its voice as one atomic, validated `defaultProvider`/`defaultVoice` pair whose voice belongs to its
provider. It SHALL NOT force the mode to `local`, SHALL NOT change the mode to any value the user did
not select, and SHALL NOT write any other provider's configuration. The saved arrangement SHALL be
restored on the next start.

#### Scenario: Saving a Piper arrangement

- **WHEN** the user has selected `gemini_piper`, the JARVIS High Piper voice and `auto` execution, then
  clicks SAVE AS DEFAULT VOICE
- **THEN** the stored mode remains `gemini_piper` with that Piper voice and execution provider, the
  default pair names the Piper provider and the Piper voice, and no local-standalone mode is activated

#### Scenario: Saving does not force local mode

- **WHEN** the user clicks SAVE AS DEFAULT VOICE with any mode selected
- **THEN** the stored and active mode equals the selected mode, and `local` appears only if the user
  selected it

#### Scenario: Saving one provider does not write another

- **WHEN** the user clicks SAVE AS DEFAULT VOICE with any provider selected
- **THEN** only the selected provider's subtree, the mode/renderer selection and the default pair change,
  and every other provider's configuration is byte-identical to before the save

#### Scenario: Saved arrangement survives a restart

- **WHEN** JARVIS restarts after SAVE AS DEFAULT VOICE
- **THEN** the same mode, Piper voice and execution provider are active without user interaction

#### Scenario: Save failure is reported and harmless

- **WHEN** persisting the default voice fails
- **THEN** the previous working configuration stays active, a message is shown, and JARVIS keeps running
