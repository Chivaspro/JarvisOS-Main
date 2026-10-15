## MODIFIED Requirements

### Requirement: The audible reply always uses the ONNX voice

In `gemini_piper` mode the audible reply SHALL be rendered by the local ONNX (Piper) voice configured in
the Piper provider's own configuration subtree, including when a different speech provider is active or
selected elsewhere in Settings. That other provider's selection SHALL remain in effect for its own mode
and SHALL NOT be altered by saving the Piper arrangement; conversely, saving or editing any other
provider SHALL NOT modify the Piper provider's subtree.

#### Scenario: Another engine is selected but Piper speaks

- **WHEN** the mode is `gemini_piper` and a different speech provider (for example EdgeTTS) is selected
  in Settings
- **THEN** the reply is still synthesized from the configured ONNX voice in the Piper provider's subtree
  and no other engine is invoked for it

#### Scenario: The local engine choice is preserved

- **WHEN** the user saves the Piper arrangement
- **THEN** the active/selected local provider's own configuration is unchanged, so switching to `local`
  mode still uses that provider's engine and voice

#### Scenario: The Piper arrangement is preserved from other providers' saves

- **WHEN** the user saves or edits any other speech provider's configuration
- **THEN** the Piper provider's subtree (model, sidecar configuration, speaker, execution provider) is
  unchanged, so switching to the Piper-rendered pipeline still uses that arrangement
