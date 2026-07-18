"""Config loading tests: .env parsing, fail-fast on missing keys, config.toml
overrides with Decimal-safe money values. Phase 3 — written before config.py logic.
"""

from decimal import Decimal

import pytest

from listflow.config import MissingConfigError, Settings, load_env_file, load_settings

REQUIRED_ENV = ("EBAY_CLIENT_ID", "EBAY_CLIENT_SECRET", "EBAY_RU_NAME", "EBAY_ENV")


@pytest.fixture(autouse=True)
def clean_environ(monkeypatch):
    """Tests must never see the developer's real eBay keys."""
    for key in REQUIRED_ENV:
        monkeypatch.delenv(key, raising=False)


def write_env(tmp_path, body: str):
    env_file = tmp_path / ".env"
    env_file.write_text(body, encoding="utf-8")
    return env_file


def test_load_env_file_parses_and_sets(tmp_path, monkeypatch):
    env_file = write_env(
        tmp_path,
        '# comment line\n\nEBAY_CLIENT_ID=abc123\nEBAY_CLIENT_SECRET="s3cret"\n'
        "EBAY_ENV=sandbox\nnot a kv line\n",
    )
    load_env_file(env_file)
    import os

    assert os.environ["EBAY_CLIENT_ID"] == "abc123"
    assert os.environ["EBAY_CLIENT_SECRET"] == "s3cret"  # quotes stripped


def test_load_env_file_does_not_override_existing(tmp_path, monkeypatch):
    monkeypatch.setenv("EBAY_CLIENT_ID", "from-real-env")
    env_file = write_env(tmp_path, "EBAY_CLIENT_ID=from-file\n")
    load_env_file(env_file)
    import os

    assert os.environ["EBAY_CLIENT_ID"] == "from-real-env"


def test_load_env_file_missing_file_is_noop(tmp_path):
    load_env_file(tmp_path / "does-not-exist.env")  # must not raise


def test_missing_keys_fail_fast_with_readable_message(tmp_path):
    env_file = write_env(tmp_path, "EBAY_CLIENT_ID=abc\n")
    with pytest.raises(MissingConfigError) as excinfo:
        load_settings(env_file=env_file, toml_file=tmp_path / "config.toml")
    message = str(excinfo.value)
    assert "EBAY_CLIENT_SECRET" in message
    assert ".env" in message  # actionable hint


def test_defaults_without_toml(tmp_path):
    env_file = write_env(tmp_path, "EBAY_CLIENT_ID=abc\nEBAY_CLIENT_SECRET=def\n")
    settings = load_settings(env_file=env_file, toml_file=tmp_path / "config.toml")
    assert settings.ebay_env == "sandbox"  # sandbox-first default
    assert settings.margin == Decimal("0.20")
    assert settings.fvf_rate == Decimal("0.128")
    assert settings.fixed_fee == Decimal("0.30")
    assert settings.max_qty == 3
    assert settings.marketplace_id == "EBAY_GB"
    assert settings.currency == "GBP"
    assert settings.ebay_ru_name is None


def test_toml_overrides_and_money_becomes_decimal(tmp_path):
    env_file = write_env(
        tmp_path, "EBAY_CLIENT_ID=abc\nEBAY_CLIENT_SECRET=def\nEBAY_RU_NAME=My_RuName\n"
    )
    toml_file = tmp_path / "config.toml"
    toml_file.write_text(
        'margin = 0.25\nfixed_fee = "0.30"\nmax_qty = 5\n'
        'boilerplate = "<p>Fast dispatch.</p>"\npayment_policy_id = "PAY-1"\n',
        encoding="utf-8",
    )
    settings = load_settings(env_file=env_file, toml_file=toml_file)
    assert settings.margin == Decimal("0.25")
    assert isinstance(settings.margin, Decimal)  # toml float converted via str, not float
    assert settings.fixed_fee == Decimal("0.30")
    assert settings.max_qty == 5
    assert settings.boilerplate == "<p>Fast dispatch.</p>"
    assert settings.payment_policy_id == "PAY-1"
    assert settings.ebay_ru_name == "My_RuName"


def test_ebay_env_production_respected(tmp_path):
    env_file = write_env(
        tmp_path, "EBAY_CLIENT_ID=abc\nEBAY_CLIENT_SECRET=def\nEBAY_ENV=production\n"
    )
    settings = load_settings(env_file=env_file, toml_file=tmp_path / "config.toml")
    assert settings.ebay_env == "production"


def test_invalid_ebay_env_rejected(tmp_path):
    env_file = write_env(
        tmp_path, "EBAY_CLIENT_ID=abc\nEBAY_CLIENT_SECRET=def\nEBAY_ENV=staging\n"
    )
    with pytest.raises(ValueError, match="sandbox|production"):
        load_settings(env_file=env_file, toml_file=tmp_path / "config.toml")


def test_settings_direct_construction():
    settings = Settings(ebay_client_id="a", ebay_client_secret="b")
    assert settings.ebay_env == "sandbox"
