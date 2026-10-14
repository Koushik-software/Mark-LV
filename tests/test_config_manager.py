"""Baseline tests for memory/config_manager.py — api_keys.json access.

The important contracts: a missing or corrupt file never raises, unknown voices
and styles collapse to safe defaults (so a bad stored value cannot reach the
API), and setters are read-modify-write so they never clobber sibling keys.
"""
from __future__ import annotations

import json

from memory import config_manager as cm


def test_missing_config_reads_as_empty(isolated_config):
    assert cm.load_api_keys() == {}
    assert cm.get_gemini_key() is None
    assert cm.is_configured() is False


def test_corrupt_config_does_not_raise(isolated_config, capsys):
    isolated_config.parent.mkdir(parents=True, exist_ok=True)
    isolated_config.write_text("{broken", encoding="utf-8")
    assert cm.load_api_keys() == {}


def test_is_configured_requires_a_realistic_key(isolated_config):
    cm.save_api_keys("short")
    assert cm.is_configured() is False
    cm.save_api_keys("a" * 30)
    assert cm.is_configured() is True


def test_defaults_when_unset(isolated_config):
    assert cm.get_assistant_name() == "JARVIS"
    assert cm.get_user_name() == ""
    assert cm.get_voice() == cm.DEFAULT_VOICE
    assert cm.get_hud_style() == "face"


def test_voice_rejects_unknown_value(isolated_config):
    cm.save_voice("NotAVoice")
    assert cm.get_voice() == cm.DEFAULT_VOICE
    cm.save_voice("Kore")
    assert cm.get_voice() == "Kore"


def test_hud_style_rejects_unknown_value(isolated_config):
    cm.save_hud_style("nonsense")
    assert cm.get_hud_style() == "face"
    cm.save_hud_style("core")
    assert cm.get_hud_style() == "core"


def test_setters_do_not_clobber_siblings(isolated_config):
    cm.save_api_keys("k" * 30)
    cm.save_voice("Puck")
    cm.save_assistant_config("JARVIS", "Fatih")
    keys = cm.load_api_keys()
    assert keys["gemini_api_key"] == "k" * 30     # survived later writes
    assert keys["voice_name"] == "Puck"
    assert keys["user_name"] == "Fatih"


def test_turn_tuning_clamps_out_of_range(isolated_config):
    cm.save_turn_tuning({"silence_ms": 99999, "prefix_ms": -5})
    t = cm.get_turn_tuning()
    assert t["silence_ms"] == 3000
    assert t["prefix_ms"] == 0


def test_turn_tuning_defaults_off(isolated_config):
    assert cm.get_turn_tuning()["enabled"] is False


def test_proactive_audio_defaults_on(isolated_config):
    assert cm.get_proactive_audio_enabled() is True


def test_media_resolution_rejects_unknown(isolated_config):
    cm.save_media_resolution("ultra")
    assert cm.get_media_resolution() == "medium"


def test_plugin_enabled_defaults_true(isolated_config):
    assert cm.get_plugin_enabled("anything") is True
    cm.save_plugin_enabled("anything", False)
    assert cm.get_plugin_enabled("anything") is False


def test_plugin_config_merges_per_namespace(isolated_config):
    cm.save_plugin_config("printer", {"host": "1.2.3.4"})
    cm.save_plugin_config("printer", {"port": 9100})
    cm.save_plugin_config("bulb", {"token": "t"})
    assert cm.get_plugin_config("printer") == {"host": "1.2.3.4", "port": 9100}
    assert cm.get_plugin_setting("bulb", "token") == "t"
    assert cm.get_plugin_setting("bulb", "missing", "dflt") == "dflt"


def test_audio_devices_stored_as_names(isolated_config):
    cm.save_input_device("  Built-in Mic  ")
    cm.save_output_device("")
    assert cm.get_input_device() == "Built-in Mic"
    assert cm.get_output_device() == ""


def test_config_is_valid_json_after_writes(isolated_config):
    cm.save_api_keys("k" * 30)
    cm.save_voice("Aoede")
    cm.save_media_resolution("low")
    json.loads(isolated_config.read_text(encoding="utf-8"))   # must not raise
