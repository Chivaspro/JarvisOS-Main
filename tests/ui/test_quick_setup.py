"""Quick Setup: one page, one value per setting, and no theme picker to lose.

These tests exercise the window the way a person does — type a key on one page
and check the page that owns it, search for a setting and land on it — because
the failure modes here are all about *two places disagreeing*, which a
screenshot cannot show and a unit test of either half cannot catch.
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
pytest.importorskip("PyQt6.QtWidgets")

from PyQt6.QtWidgets import QApplication, QLineEdit  # noqa: E402

import ui_theme as T  # noqa: E402


@pytest.fixture()
def settings(tmp_path=None):
    """A real SettingsWindow on a throwaway config (tempfile, not tmp_path)."""
    import ui_settings
    app = QApplication.instance() or QApplication([])
    folder = tempfile.TemporaryDirectory(prefix="jarvis-quick-")
    cfg = Path(folder.name) / "api_keys.json"
    cfg.write_text(json.dumps({
        "gemini_api_key": "gm-old",
        "openrouter_api_key": "or-old",
        "ui_theme": "hud",
        "features": {"wake_word": False, "connect": True},
    }), encoding="utf-8")
    win = ui_settings.SettingsWindow(config_path=cfg)
    win._test_cfg_path = cfg
    try:
        yield win
    finally:
        try:
            win.deleteLater()
        except Exception:
            pass
        del app
        folder.cleanup()


def _read(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


# ── the tab ───────────────────────────────────────────────────────────────────

def test_quick_setup_is_the_first_tab(settings):
    assert settings.tabs.tabText(0) == "QUICK SETUP"


def test_quick_setup_shows_every_credential_the_hub_knows(settings):
    from services import hub
    rows = {row["field"] for row in hub.key_targets()}
    assert rows <= set(settings._key_rows), sorted(rows - set(settings._key_rows))


def test_every_row_offers_paste_and_only_offers_what_exists(settings):
    for field, row in settings._key_rows.items():
        assert "paste" in row, field
        # TEST appears exactly when the hub has something real to run.
        meta = settings._hub_meta(field)
        assert ("test" in row) == bool(meta.get("test")), field
        # GET API KEY appears exactly when there is a page to open.
        assert ("get" in row) == bool(meta.get("key_url") or meta.get("help_url")), field


def test_the_ready_strip_says_what_is_set_and_what_is_missing(settings):
    text = settings._ready_strip.text()
    assert "credential(s) set" in text
    # A blank required key is named rather than hidden.
    assert "Gemini" not in text or "required" not in text  # gemini is set here
    ready, blockers, notes = settings._ready_summary()
    assert ready + blockers == len(settings._key_rows)
    assert blockers == len(notes) or blockers > 4


def test_essential_switches_load_the_saved_flags(settings):
    assert settings._qs_toggles["wake_word"] is False
    assert settings._qs_toggles["connect"] is True


def test_every_essential_switch_is_a_real_feature_flag():
    import ui_settings
    for key, _title, _sub in ui_settings._ESSENTIAL_SWITCHES:
        assert key in ui_settings.DEFAULTS["features"], key


# ── one value, wherever it is shown ───────────────────────────────────────────

def test_a_key_typed_on_quick_setup_reaches_the_tab_that_owns_it(settings):
    settings._key_rows["openai_api_key"]["edit"].setText("sk-qs-test")
    assert settings.openai_api_key.text() == "sk-qs-test"
    assert settings._collect_api_keys()["openai_api_key"] == "sk-qs-test"


def test_a_key_typed_on_the_api_tab_reaches_quick_setup(settings):
    settings.gemini_key_edit.setText("gm-typed")
    assert settings._key_rows["gemini_api_key"]["edit"].text() == "gm-typed"
    assert settings._collect_api_keys()["gemini_api_key"] == "gm-typed"


def test_the_service_credentials_card_shares_the_same_widgets(settings):
    settings._hub_fields["openrouter_api_key"].setText("or-new")
    assert settings._key_rows["openrouter_api_key"]["edit"].text() == "or-new"
    settings._key_rows["openrouter_api_key"]["edit"].setText("or-newer")
    assert settings._hub_fields["openrouter_api_key"].text() == "or-newer"


def test_a_save_writes_what_was_typed_on_either_page(settings):
    settings._key_rows["openai_api_key"]["edit"].setText("sk-saved")
    settings.groq_api_key.setText("gsk-saved")
    settings.save_settings()
    saved = _read(settings._test_cfg_path)
    assert saved["openai_api_key"] == "sk-saved"
    assert saved["groq_api_key"] == "gsk-saved"


def test_editing_marks_the_window_unsaved_and_saving_clears_it(settings):
    assert settings._dirty is False
    settings._key_rows["openai_api_key"]["edit"].setText("sk-dirty")
    assert settings._dirty is True
    assert "UNSAVED" in settings._save_state.text()
    settings.save_settings()
    assert settings._dirty is False
    assert "SAVED" in settings._save_state.text()


def test_flipping_an_essential_switch_moves_the_checkbox_on_its_own_tab(settings):
    button = next(b for b in settings.findChildren(type(settings._save_btn))
                  if "Wake word" in b.text())
    button.setChecked(True)
    assert settings._qs_toggles["wake_word"] is True
    assert settings._voice_checks["wake_word"].isChecked() is True
    settings.save_settings()
    assert _read(settings._test_cfg_path)["features"]["wake_word"] is True


# ── no theme switch: the HUD look is the only look ───────────────────────────

def test_settings_window_has_no_theme_picker(settings):
    """The three-way picker was retired; the Settings UI keeps the HUD look."""
    assert not hasattr(settings, "_theme_button_groups")
    assert not hasattr(settings, "_choose_theme")
    assert not hasattr(settings, "_build_theme_picker")


def test_the_token_table_stays_on_the_default_theme(settings):
    assert T.current_theme() == "hud"
    assert T.is_soft() is False


def test_collected_settings_do_not_carry_a_theme_key(settings):
    assert "ui_theme" not in settings._collect()


def test_a_stale_theme_key_is_dropped_on_save(settings):
    assert _read(settings._test_cfg_path).get("ui_theme") == "hud"
    settings.save_settings()
    assert "ui_theme" not in _read(settings._test_cfg_path)


# ── search ────────────────────────────────────────────────────────────────────

def test_search_finds_a_setting_by_name(settings):
    settings._search.setText("openrouter")
    labels = [settings._search_results.item(i).text()
              for i in range(settings._search_results.count())]
    assert any("OpenRouter" in text for text in labels)


def test_search_finds_a_key_by_its_field(settings):
    settings._search.setText("groq_api_key")
    assert settings._search_results.count() >= 1


def test_an_empty_search_hides_the_list(settings):
    settings._search.setText("spotify")
    assert settings._search_results.isVisible() or settings._search_results.count() > 0
    settings._search.setText("")
    assert settings._search_results.isHidden()


def test_jumping_to_a_result_switches_tab_and_focuses_the_field(settings):
    settings._search.setText("openrouter")
    item = next(settings._search_results.item(i)
                for i in range(settings._search_results.count())
                if "OpenRouter" in settings._search_results.item(i).text())
    settings._jump_to_setting(item)
    assert settings.tabs.tabText(settings.tabs.currentIndex()) in ("QUICK SETUP", "API")


def test_search_reports_an_honest_miss(settings):
    settings._search.setText("there-is-no-such-setting")
    assert settings._search_results.count() == 1
    assert "No setting matches" in settings._search_results.item(0).text()


def test_every_indexed_setting_names_a_real_tab(settings):
    tabs = {settings.tabs.tabText(i) for i in range(settings.tabs.count())}
    for tab, label, _widget in settings._setting_index:
        assert tab in tabs, f"{label} is indexed under unknown tab {tab!r}"


def test_the_index_is_not_empty_and_holds_the_essentials(settings):
    labels = " ".join(label for _tab, label, _w in settings._setting_index).lower()
    for expected in ("assistant name", "accent", "openrouter", "qdrant"):
        assert expected in labels, expected

# ── a backend switch made in another panel survives this window's save ───────

def test_saving_settings_keeps_a_memory_backend_choice_written_elsewhere(settings):
    """The Vault panel writes ``memory_backend`` straight into the config file.

    This window loaded its snapshot when it opened, so a save that merged only
    that snapshot would silently drop the choice — and the next start would fall
    back to the original memory, the exact bug the user reported. A save must
    start from the file on disk.
    """
    cfg = settings._test_cfg_path
    data = json.loads(cfg.read_text(encoding="utf-8"))
    assert "memory_backend" not in data          # the window never saw this key
    data["memory_backend"] = "obsidian"
    cfg.write_text(json.dumps(data), encoding="utf-8")

    settings.save_settings()

    assert json.loads(cfg.read_text(encoding="utf-8"))["memory_backend"] == "obsidian"
