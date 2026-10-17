# J.A.R.V.I.S. OS

A Windows-focused desktop AI assistant that combines voice conversation, a PyQt6 interface, computer and browser automation, coding assistance, system tools, and optional memory/integration backends.

> **Project status:** Experimental and under active development. Many capabilities depend on external API keys, optional engines, permissions, and compatible runtimes. Review each tool's permissions before using it on a machine containing sensitive data.

![image alt](https://www.google.com/imgres?q=Jarvis&imgurl=https%3A%2F%2Fjarvis.institute%2Fpage-art%2Fjarvis-command-center-real.webp&imgrefurl=https%3A%2F%2Fjarvis.institute%2F&docid=eCf6mSAqHae-MM&tbnid=1aEEr_jTuPmQJM&vet=12ahUKEwiG2ZamtcCXAxXGjuEIHQQrCKoQnPAOegQIQRAA..i&w=1200&h=672&hcb=2&ved=2ahUKEwiG2ZamtcCXAxXGjuEIHQQrCKoQnPAOegQIQRAA)

## Features

The repository currently contains source code for the following capabilities. Availability depends on configuration and the installed optional dependencies.

- **Voice assistant:** Gemini Live audio conversations, microphone input, audio output, wake-word support, voice settings, and configurable speech behavior.
- **Desktop interface:** PyQt6-based desktop/HUD UI and launch UI.
- **Web tools:** Web search and research, news lookup, weather search, and YouTube transcript support.
- **Computer and browser automation:** Playwright, Agent TARS, Windows application automation, screenshot/vision utilities, and mouse/keyboard control.
- **Coding assistance:** A developer agent that can plan project files, generate code, install dependencies, run a project, and attempt fixes. This feature should be used cautiously; see [Security notes](#security-notes).
- **System utilities:** File tools, reminders, system monitoring, screen processing, and proactive/background tasks.
- **Optional integrations:** MCP servers, external-app connectors, smart-home controls, and configurable memory/knowledge backends such as Mem0 and Qdrant.
- **Configuration and diagnostics:** A setup wizard, feature switches, provider-key checks, and startup diagnostics.

These are code-level capabilities, not a guarantee that every integration is enabled or will work on every machine.

## Platform and prerequisites

The setup and desktop-control paths are **primarily designed for Windows**. The project includes Windows-specific integrations such as `pywinauto`, `pywin32`, WMI, and Windows notification support. Some modules may run on other operating systems, but full non-Windows desktop-control support is not established by this repository review.

Before installing, prepare:

- **Python 3.11 or newer**. The setup script accepts Python 3.11+; it can recommend/install Python 3.13 on Windows when necessary.
- **Node.js 22.15.0 or newer** for the pinned Agent TARS runtime in `package.json`.
- **Git** to clone and update the repository.
- A **Gemini API key** for the primary Gemini-powered voice path. Other providers and integrations need their own credentials only when enabled.
- A working microphone and audio output for voice features.
- Internet access for model APIs, package installation, web search, and cloud integrations.

**Compatibility note:** `setup_jarvis.py` currently checks only for Node.js 18+, but `package.json` declares `node >=22.15.0`. Use Node.js 22.15.0+ until those checks are aligned. See the audit report/repository issue tracker for the recommended fix.

## Installation

The following steps are intended for Windows PowerShell or Command Prompt.

1. Clone the repository and enter the folder:

   ```powershell
   git clone https://github.com/Chivaspro/JarvisOS-Main.git
   cd JarvisOS-Main
   ```

2. Check the runtimes:

   ```powershell
   python --version
   node --version
   npm --version
   ```

   If `python` is not available, install Python 3.11+ and reopen the terminal. If Agent TARS is required, confirm that `node --version` reports `v22.15.0` or higher.

3. Start the interactive setup and control center:

   ```powershell
   python setup_jarvis.py
   ```

4. Follow the wizard to install the Python dependencies, configure API keys, and install optional components such as the Agent TARS Node runtime and Playwright's Chromium browser when needed. You do not need to configure every optional provider to start exploring the base app.

5. Launch J.A.R.V.I.S.:

   ```powershell
   python start_jarvis.py
   ```

   `start_jarvis.py` selects a Python interpreter that can import the required UI modules and can install requirements when necessary. After setup is complete, `python setup_jarvis.py --launch` is also available.

### Optional browser setup

The setup wizard can install the Playwright browser binary. If Playwright is installed but Chromium is missing, run this from the project environment:

```powershell
python -m playwright install chromium
```

The Node package is an automation runtime; `package.json` does **not** define a `dev` script. Use the Python setup/launch scripts above rather than `npm run dev`.

## Configuration and API keys

Use the setup wizard's API-key configuration flow where possible. The primary configuration file is:

```text
config/api_keys.json
```

The repository also provides `.env.example` as a placeholder/reference for supported environment values. Copy values you intend to use into your local configuration; never replace the placeholders with real credentials in a committed example.

- `config/api_keys.json` and `.env` are listed in `.gitignore`.
- The Gemini key is required for the primary Gemini voice experience. Other credentials—such as optional model providers, cloud TTS, memory/knowledge services, or external apps—are only needed when you enable their corresponding features.
- Feature flags are stored under the `features` configuration map and can also be managed in the app's Settings UI where supported.
- Do not commit, paste into an issue, or include in diagnostic logs any API key, password, token, session cookie, or private endpoint.

## Useful commands

Run these commands from the repository root with the intended Python interpreter.

| Command | Purpose |
|---|---|
| `python setup_jarvis.py` | Open the interactive setup and control center. |
| `python setup_jarvis.py --check` | Check the local environment. |
| `python setup_jarvis.py --diagnose` | Report the status of providers and optional engines. |
| `python setup_jarvis.py --test` | Test configured provider credentials where supported. |
| `python setup_jarvis.py --install-missing` | Install dependencies/components still missing according to the wizard. |
| `python setup_jarvis.py --launch` | Launch J.A.R.V.I.S. through the setup control center. |
| `python start_jarvis.py --check` | Check candidate Python interpreters without launching the application. |
| `python start_jarvis.py` | Resolve the runtime and launch the app. |

For development tests, install `pytest` in the development environment if it is not already present, then run the relevant suite:

```powershell
python -m pip install pytest
python -m pytest tests/unit
```

Tests that require credentials, devices, network access, or Windows-only integrations may need a dedicated environment or mocks.

## Project map

This is a guide to key entry points and subsystems, not an exhaustive listing of every file.

```text
JarvisOS-Main/
├── main.py                  # Desktop assistant/runtime entry point
├── start_jarvis.py          # Python interpreter resolver and launcher
├── setup_jarvis.py          # Setup, install, diagnostics, and control center
├── requirements.txt         # Python runtime dependencies
├── package.json             # Agent TARS Node runtime requirement
├── package-lock.json        # Pinned Node dependency tree
├── .env.example             # Safe configuration template
├── config/
│   └── settings.py          # Centralized configuration and feature defaults
├── core/                    # Prompt, model routing and core runtime modules
├── actions/                 # Tool/action implementations
├── security/                # Permission and approval gates
└── tests/unit/              # Unit tests
```

## Security notes

J.A.R.V.I.S. can interact with the local desktop, work with files, run generated code, and install packages. Treat these capabilities as powerful automation, not as harmless chat features.

- Keep the app and its dependencies updated; run it as a normal user rather than an administrator unless a specific task truly requires elevation.
- Keep desktop control, self-modifying features, and automatic coding approval disabled unless you intentionally need them and understand their scope.
- Review generated files, proposed dependencies, and commands before executing them. Do not point the developer agent at folders containing sensitive material.
- Use a separate test account or disposable VM for experimental code generation and automation until the coding agent runs in a properly isolated environment.
- Do not expose local services or automation endpoints directly to the internet without authentication, access controls, and a clear threat model.
- Use provider credentials with the minimum permissions required and rotate a key if it may have been exposed.

The coding agent and desktop-control tools are powerful. Review their generated output and granted permissions carefully, especially when the assistant is exposed to untrusted prompts or files.

## Known maintenance items

- Migrate the configured Gemini Live model before its currently documented shutdown date of **November 17, 2026**. Confirm the new model's event/API behavior with an integration test before releasing the migration.
- Align the Node.js version check in `setup_jarvis.py` with the `>=22.15.0` requirement declared by `package.json`.
- Harden coding-agent path validation, permission error handling, execution result checks, and project isolation.
- Add/verify CI that runs unit tests, static checks, dependency checks, and relevant security tests on each pull request.

These are maintenance recommendations based on the source reviewed for this README and should be re-checked as the code changes.

## Contributing

1. Open an issue to discuss a significant change or report a reproducible bug.
2. Create a focused branch for the change.
3. Add or update tests for behavior changes, especially permission boundaries and tool execution.
4. Run the applicable unit tests and setup diagnostics.
5. Open a pull request with a clear description, test results, and any configuration/migration notes.

Do not include real API keys, tokens, user data, or machine-specific logs in pull requests.

## License

The repository's `LICENSE` identifies the project as **Creative Commons Attribution-NonCommercial 4.0 International (CC BY-NC 4.0)** and states that commercial use is not permitted. Read the [LICENSE](LICENSE) and the [official license text](https://creativecommons.org/licenses/by-nc/4.0/legalcode) before redistributing or adapting the project. Obtain appropriate permission from the rights holder for uses not permitted by that license.

## Links

- [Source repository](https://github.com/Chivaspro/JarvisOS-Main)
- [Google Gemini API model documentation](https://ai.google.dev/gemini-api/docs/models)
- [Google Gemini API deprecations](https://ai.google.dev/gemini-api/docs/deprecations)
