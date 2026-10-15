"""
Plugin discovery, validation, collision detection, and dispatch.

Discovery runs once (JarvisLive.__init__ calls discover_plugins()); the resulting
PluginRegistry is cached for the process lifetime. Enable/disable state is re-read
from config on every call to get_tool_declarations() / run() / list_for_ui(), so
toggling a plugin does not require restarting the app or re-importing anything.
"""
from __future__ import annotations

import importlib.util
import hashlib
import inspect
import re
import sys
import traceback
import types
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional

from memory.config_manager import get_plugin_enabled

_NAME_RE = re.compile(r"^[a-zA-Z_][a-zA-Z0-9_]{0,63}$")
_DEFAULT_PARAMS = {"type": "OBJECT", "properties": {}}


@dataclass
class PluginRecord:
    name: str
    description: str = ""
    parameters: dict = field(default_factory=lambda: dict(_DEFAULT_PARAMS))
    run: Optional[Callable] = None
    file: str = ""
    valid: bool = False
    error: str = ""


class PluginRegistry:
    def __init__(self, plugins: dict[str, PluginRecord], logger: Callable[[str], None]):
        self._plugins = plugins          # name -> PluginRecord, VALID entries only
        self._all_records: list[PluginRecord] = []   # valid + invalid, for UI listing
        self._logger = logger
        #: Core tool names, so a plugin that arrives *after* startup (a skill
        #: forged by evolution/vault.py) is held to exactly the same
        #: no-collision rule the startup scan applies.
        self._core_tool_names: set[str] = set()

    # -- called by evolution/vault.py when a new skill is forged at runtime --
    def register(self, rec: PluginRecord) -> tuple[bool, str]:
        """Install a validated record discovered after startup.

        Hot-registration is what makes a forged skill usable without a restart:
        the next ``get_tool_declarations()`` (i.e. the next Live connect) already
        contains it, and ``run()`` dispatches it immediately. Re-registering an
        existing name replaces that plugin's code — the forged skill is
        re-validated by the caller first.
        """
        if not rec.valid or rec.run is None:
            return False, rec.error or "the plugin record is not usable"
        if rec.name in self._core_tool_names:
            return False, f"'{rec.name}' collides with a core tool — rejected"
        other = self._plugins.get(rec.name)
        if other is not None and other.file != rec.file:
            return False, (f"'{rec.name}' is already provided by plugin "
                           f"'{other.file}' — rejected")
        self._plugins[rec.name] = rec
        self._all_records = [r for r in self._all_records if r.name != rec.name]
        self._all_records.append(rec)
        self._logger(f"Plugin hot-registered: {rec.name} ({rec.file})")
        return True, f"'{rec.name}' is live"

    # -- called by main.py at LiveConnectConfig build time --
    def get_tool_declarations(self) -> list[dict]:
        decls = []
        for name, rec in self._plugins.items():
            if get_plugin_enabled(name):
                decls.append({
                    "name": rec.name,
                    "description": rec.description,
                    "parameters": rec.parameters,
                })
        return decls

    def has(self, name: str) -> bool:
        return name in self._plugins

    # -- called by main.py from _execute_tool's else branch --
    def run(self, name: str, parameters: dict, player=None, session_memory=None) -> str:
        rec = self._plugins.get(name)
        if rec is None or not rec.valid:
            return f"Plugin '{name}' is not available."
        if not get_plugin_enabled(name):
            return f"The '{name}' plugin is currently disabled."
        try:
            return _call_run(rec.run, parameters, player, session_memory) or "Done."
        except Exception as e:
            self._logger(f"Plugin '{name}' crashed during run(): {e}")
            traceback.print_exc()
            return f"Sir, the '{name}' plugin failed: {e}"

    # -- called by ui.py's Plugin Manager overlay --
    def list_for_ui(self) -> list[dict]:
        out = []
        for rec in self._all_records:
            out.append({
                "name": rec.name,
                "description": rec.description,
                "file": rec.file,
                "valid": rec.valid,
                "error": rec.error,
                "enabled": get_plugin_enabled(rec.name) if rec.valid else False,
            })
        return out


def _call_run(run_fn, parameters, player, session_memory):
    """Invoke run() passing only the kwargs it actually declares (or all of them
    if it has **kwargs), so a minimal `def run(parameters):` plugin still works."""
    sig = inspect.signature(run_fn)
    has_var_kw = any(p.kind == inspect.Parameter.VAR_KEYWORD for p in sig.parameters.values())
    kwargs = {}
    if has_var_kw or "player" in sig.parameters:
        kwargs["player"] = player
    if has_var_kw or "session_memory" in sig.parameters:
        kwargs["session_memory"] = session_memory
    return run_fn(parameters, **kwargs)


def _validate(module, filename: str) -> PluginRecord:
    """Returns a PluginRecord; .valid=False + .error set on any problem. Never raises."""
    plugin_meta = getattr(module, "PLUGIN", None)
    if not isinstance(plugin_meta, dict):
        return PluginRecord(name=Path(filename).stem, file=filename,
                             error="Missing PLUGIN dict constant.")

    name = plugin_meta.get("name")
    if not isinstance(name, str) or not _NAME_RE.match(name):
        return PluginRecord(name=str(name or Path(filename).stem), file=filename,
                             error="PLUGIN['name'] missing or not a valid identifier "
                                   "(letters/digits/underscore, must start with letter/underscore).")

    description = plugin_meta.get("description")
    if not isinstance(description, str) or not description.strip():
        return PluginRecord(name=name, file=filename,
                             error="PLUGIN['description'] missing or empty.")

    parameters = plugin_meta.get("parameters", _DEFAULT_PARAMS)
    if not isinstance(parameters, dict) or parameters.get("type") != "OBJECT":
        return PluginRecord(name=name, file=filename,
                             error="PLUGIN['parameters'] must be a dict with \"type\": \"OBJECT\".")

    run_fn = getattr(module, "run", None)
    if not callable(run_fn):
        return PluginRecord(name=name, file=filename,
                             error="Missing callable run(parameters, ...) function.")

    return PluginRecord(name=name, description=description.strip(), parameters=parameters,
                         run=run_fn, file=filename, valid=True, error="")


def _safe_module_part(value: str) -> str:
    """Turn a plugin directory name into a valid, collision-resistant module part."""
    value = re.sub(r"[^a-zA-Z0-9_]+", "_", str(value)).strip("_") or "plugin"
    if value[0].isdigit():
        value = f"p_{value}"
    return value[:48]


def _ensure_package(name: str, path: Path) -> None:
    """Create a synthetic package for a plugin directory.

    Plugin folders are allowed to contain spaces and do not need an
    ``__init__.py``. Creating a small package object lets a plugin keep its real
    relative imports without requiring the user's
    folder to be importable as a normal Python package.
    """
    if name in sys.modules:
        return
    package = types.ModuleType(name)
    package.__path__ = [str(path)]  # type: ignore[attr-defined]
    package.__package__ = name
    sys.modules[name] = package


def _load_plugin_module(path: Path, plugins_dir: Path):
    """Load a direct or nested plugin with its sibling imports intact."""
    rel = path.relative_to(plugins_dir)
    parent = rel.parent
    root_name = "_jarvis_discovered_plugins"
    _ensure_package(root_name, plugins_dir)

    if parent == Path("."):
        package_name = root_name
    else:
        token = "_".join(_safe_module_part(part) for part in parent.parts)
        digest = hashlib.sha1(str(parent).encode("utf-8")).hexdigest()[:8]
        package_name = f"{root_name}.{token}_{digest}"
        _ensure_package(package_name, path.parent)

    module_digest = hashlib.sha1(str(rel).encode("utf-8")).hexdigest()[:10]
    module_name = f"{package_name}.{_safe_module_part(path.stem)}_{module_digest}"
    spec = importlib.util.spec_from_file_location(module_name, path)
    if spec is None or spec.loader is None:
        raise ImportError("could not build import spec")
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    # Plugins make their own sibling helpers importable by appending their
    # folder to sys.path at import time. FileFinder caches each sys.path entry's
    # directory listing, and __pycache__ files written by the concurrent
    # background import thread land in that directory *after* it was cached —
    # which surfaced as an intermittent "No module named <sibling>"
    # rejection naming a different sibling on every run. Refreshing the caches
    # (and retrying once, in case a helper was mid-write) makes discovery
    # deterministic without any plugin needing to know about it.
    importlib.invalidate_caches()
    try:
        spec.loader.exec_module(module)
    except ModuleNotFoundError:
        importlib.invalidate_caches()
        try:
            spec.loader.exec_module(module)
        except Exception:
            sys.modules.pop(module_name, None)
            raise
    except Exception:
        sys.modules.pop(module_name, None)
        raise
    return module


def discover_plugins(plugins_dir: Path, core_tool_names: set[str],
                      logger: Callable[[str], None] = print) -> PluginRegistry:
    """
    Scans plugins_dir and its plugin folders for *.py files. Files starting with
    '_' (e.g. __init__.py, _template.py, and shared helpers) are skipped.
    Import errors, validation errors, and name collisions are logged and the offending
    file is skipped — they NEVER raise out of this function and never abort the scan
    of remaining files.
    """
    plugins_dir.mkdir(parents=True, exist_ok=True)
    valid: dict[str, PluginRecord] = {}
    all_records: list[PluginRecord] = []

    files = sorted(
        (
            path for path in plugins_dir.rglob("*.py")
            if "__pycache__" not in path.parts
            and not any(part.startswith(".")
                        for part in path.relative_to(plugins_dir).parts)
        ),
        key=lambda p: str(p.relative_to(plugins_dir)).lower(),
    )
    for path in files:
        if path.name.startswith("_"):
            continue
        relative_name = str(path.relative_to(plugins_dir))
        try:
            module = _load_plugin_module(path, plugins_dir)
            rec = _validate(module, relative_name)

            if rec.valid and rec.name in core_tool_names:
                rec = PluginRecord(name=rec.name, file=relative_name,
                                    error=f"Name '{rec.name}' collides with a core tool — rejected.")
            elif rec.valid and rec.name in valid:
                other = valid[rec.name].file
                rec = PluginRecord(name=rec.name, file=relative_name,
                                    error=f"Name '{rec.name}' already used by plugin '{other}' — rejected.")

        except Exception as e:
            rec = PluginRecord(name=path.stem, file=relative_name,
                                error=f"Failed to load: {e}")
            traceback.print_exc()

        all_records.append(rec)
        if rec.valid:
            valid[rec.name] = rec
            logger(f"Plugin loaded: {rec.name} ({path.name})")
        else:
            logger(f"Plugin rejected: {path.name} — {rec.error}")

    registry = PluginRegistry(valid, logger)
    registry._all_records = all_records
    registry._core_tool_names = set(core_tool_names or ())
    logger(f"Plugin discovery complete: {len(valid)} active, "
           f"{len(all_records) - len(valid)} rejected, {len(all_records)} total.")
    return registry


# ── runtime registration (self-evolution) ─────────────────────────────────────
#
# Discovery runs once at startup, so a plugin file written later would sit on
# disk unseen until the next launch. The self-evolution subsystem therefore
# publishes its registry here: a skill forged mid-session is validated with the
# exact same rules and installed into the live registry, which is what makes
# "writes a brand-new tool and uses it immediately" true without a restart.

_RUNTIME_REGISTRY: "PluginRegistry | None" = None


def set_runtime_registry(registry: "PluginRegistry | None") -> None:
    """Publish the live registry for hot-registration (called from main.py)."""
    global _RUNTIME_REGISTRY
    _RUNTIME_REGISTRY = registry


def get_runtime_registry() -> "PluginRegistry | None":
    return _RUNTIME_REGISTRY


def register_runtime_plugin(path: Path, plugins_dir: Path | None = None) -> tuple[bool, str]:
    """Load, validate and hot-register one plugin ``.py`` written at runtime.

    Returns ``(ok, message)`` and never raises: a rejected file is reported with
    the same sentence the startup scan would have used.
    """
    registry = _RUNTIME_REGISTRY
    if registry is None:
        return False, "the plugin registry is not running yet"

    path = Path(path)
    root = Path(plugins_dir) if plugins_dir else path.parent.parent
    if not path.exists():
        return False, f"no such plugin file: {path}"
    try:
        relative_name = str(path.relative_to(root))
    except ValueError:
        relative_name = path.name

    try:
        module = _load_plugin_module(path, root)
        rec = _validate(module, relative_name)
    except Exception as exc:
        return False, f"failed to load: {exc}"

    if not rec.valid:
        return False, rec.error or "the plugin failed validation"
    return registry.register(rec)
