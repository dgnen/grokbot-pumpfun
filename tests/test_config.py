"""Config: environment variables, secret masking, and pre-start checks.

These tests exist so a bad config fails at start, not an hour into
trading, and so the key does not leak into the log via a model dump.
"""

import json

import pytest

from src.models import Config, ConfigError, is_placeholder, mask

GOOD = {"grok": {"api_key": "xai-1234567890abcdef"}}


def config(**overrides) -> Config:
    raw = {"grok": {"api_key": "xai-1234567890abcdef"}}
    raw.update(overrides)
    return Config.from_raw(raw, env={})


# --- environment variables ------------------------------------------------


def test_env_overrides_file():
    cfg = Config.from_raw(GOOD, env={"GROKBOT_GROK_API_KEY": "xai-from-env"})
    assert cfg.grok.key == "xai-from-env"


def test_env_can_switch_mode():
    cfg = Config.from_raw(GOOD, env={"GROKBOT_MODE": "live"})
    assert cfg.is_live


def test_empty_env_value_does_not_override():
    """An empty docker-compose variable must not wipe the key from the file."""
    cfg = Config.from_raw(GOOD, env={"GROKBOT_GROK_API_KEY": ""})
    assert cfg.grok.key == "xai-1234567890abcdef"


def test_env_creates_missing_sections():
    cfg = Config.from_raw({}, env={"GROKBOT_HEALTH_PORT": "8080"})
    assert cfg.ops.health_port == 8080


def test_load_reads_yaml(tmp_path):
    path = tmp_path / "config.yaml"
    path.write_text("mode: dry-run\ngrok:\n  api_key: xai-from-file-1234\n")
    cfg = Config.load(path, env={})
    assert cfg.grok.key == "xai-from-file-1234"


# --- secrets --------------------------------------------------------------


def test_secrets_never_appear_in_dump():
    cfg = config(solana={"wallet_private_key": "5xPrivateKey"},
                 data={"api_key": "data-secret-key"})
    dumped = json.dumps(cfg.model_dump(mode="json"), ensure_ascii=False)
    assert "xai-1234567890abcdef" not in dumped
    assert "5xPrivateKey" not in dumped
    assert "data-secret-key" not in dumped


def test_secrets_not_in_repr():
    cfg = config()
    assert "xai-1234567890abcdef" not in repr(cfg)


def test_redacted_masks_but_keeps_shape():
    cfg = config(solana={"wallet_private_key": "5xPrivateKey"})
    redacted = cfg.redacted()
    assert redacted["grok"]["api_key"] == mask("xai-1234567890abcdef")
    assert "…" in redacted["grok"]["api_key"]
    assert redacted["risk"]["max_sol_per_trade"] == cfg.risk.max_sol_per_trade
    assert "xai-1234567890abcdef" not in json.dumps(redacted, ensure_ascii=False)


def test_summary_is_log_safe():
    cfg = config()
    assert "xai-1234567890abcdef" not in cfg.summary()
    assert "dry-run" in cfg.summary()


def test_mask_hides_short_secrets():
    assert mask("shortone") == "***"
    assert mask("") == "<empty>"


def test_placeholder_detection():
    assert is_placeholder("xai-YOUR-KEY-HERE")
    assert is_placeholder("")
    assert is_placeholder("   ")
    assert is_placeholder("<key here>")
    assert not is_placeholder("xai-1234567890abcdef")


# --- pre-start checks -----------------------------------------------------


def test_example_config_is_rejected_as_is():
    """config.example.yaml with placeholders must not start."""
    cfg = Config.load("config.example.yaml", env={})
    errors, _ = cfg.problems()
    assert any("grok.api_key" in e for e in errors)
    with pytest.raises(ConfigError):
        cfg.check_ready()


def test_good_config_passes():
    assert isinstance(config().check_ready(), list)


@pytest.mark.parametrize(
    "section,patch,marker",
    [
        ("risk", {"max_sol_per_trade": 0}, "max_sol_per_trade"),
        ("risk", {"daily_loss_limit_sol": -1}, "daily_loss_limit_sol"),
        ("risk", {"max_trades_per_day": 0}, "max_trades_per_day"),
        ("risk", {"max_open_positions": 0}, "max_open_positions"),
        ("risk", {"stop_loss_pct": 0}, "stop_loss_pct"),
        ("risk", {"stop_loss_pct": 100}, "stop_loss_pct"),
        ("filter", {"min_total_score": 1.5}, "min_total_score"),
        ("filter", {"max_curve_progress": 0}, "max_curve_progress"),
        ("filter", {"max_risk_score": 11}, "max_risk_score"),
        ("filter", {"min_age_seconds": -1}, "min_age_seconds"),
        ("grok", {"api_key": "xai-1234567890abcdef", "max_retries": 0}, "max_retries"),
        ("grok", {"api_key": "xai-1234567890abcdef", "timeout_seconds": 0}, "timeout"),
        ("ops", {"grok_max_concurrency": 0}, "grok_max_concurrency"),
    ],
)
def test_nonsense_values_are_errors(section, patch, marker):
    cfg = config(**{section: patch})
    errors, _ = cfg.problems()
    assert any(marker in e for e in errors), errors


def test_zero_weights_rejected():
    cfg = config(scoring={"weights": {"audit": 0, "narrative": 0, "timing": 0, "metrics": 0}})
    errors, _ = cfg.problems()
    assert any("weights" in e for e in errors)


def test_live_without_wallet_key_rejected():
    cfg = config(mode="live")
    errors, _ = cfg.problems()
    assert any("wallet_private_key" in e for e in errors)


def test_live_with_http_rpc_rejected():
    cfg = config(mode="live",
                 solana={"wallet_private_key": "5xRealKey", "rpc_url": "http://rpc.local"})
    errors, _ = cfg.problems()
    assert any("https" in e for e in errors)


def test_live_with_everything_set_passes():
    cfg = config(mode="live", solana={"wallet_private_key": "5xRealKey"})
    errors, _ = cfg.problems()
    assert errors == []


def test_warnings_do_not_block_start():
    cfg = config(filter={"min_total_score": 0.1})
    warnings = cfg.check_ready()
    assert any("min_total_score" in w for w in warnings)


def test_dry_run_ignores_wallet_key():
    """In dry-run the wallet is unused: no key is required at all."""
    errors, _ = config().problems()
    assert not any("wallet" in e for e in errors)
