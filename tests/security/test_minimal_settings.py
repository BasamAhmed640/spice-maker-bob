import json

from boardmodeler.config import AppConfig, load_config, save_config


def test_settings_keep_only_overrides_and_round_trip(tmp_path):
    target = tmp_path / "settings.json"
    defaults = AppConfig()
    save_config(defaults, target)
    assert json.loads(target.read_text()) == {"config_version": defaults.config_version}
    configured = defaults.model_copy(deep=True)
    configured.agent_model = "user-selected-model"
    configured.ltspice.timeout_s = 73
    save_config(configured, target)
    assert load_config(target) == configured
    payload = json.loads(target.read_text())
    assert payload["ltspice"] == {"timeout_s": 73}
    assert set(payload) == {"config_version", "ltspice", "agent_model"}
