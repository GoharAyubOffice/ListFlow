"""Environment (.env) + config.toml loading; fails fast on missing keys (spec §2.1).

Implemented in Phase 3. Secrets are never printed or logged.
"""

import os
import tomllib
from decimal import Decimal
from pathlib import Path
from typing import Literal

from pydantic import BaseModel

REQUIRED_ENV_KEYS = ("EBAY_CLIENT_ID", "EBAY_CLIENT_SECRET")


def listflow_home() -> Path:
    """App state dir: ~/.listflow, overridable via LISTFLOW_HOME (tests use this)."""
    return Path(os.environ.get("LISTFLOW_HOME", str(Path.home() / ".listflow")))

# money keys in config.toml are converted to Decimal via str() at this boundary,
# so a bare toml number (margin = 0.25) never leaks a float into pricing paths
_TOML_MONEY_KEYS = ("margin", "fvf_rate", "fixed_fee")
_TOML_PLAIN_KEYS = (
    "max_qty",
    "boilerplate",
    "marketplace_id",
    "currency",
    "payment_policy_id",
    "return_policy_id",
    "fulfillment_policy_id",
    "ship_from_address_line1",
    "ship_from_city",
    "ship_from_postal_code",
    "ship_from_country",
)


class MissingConfigError(ValueError):
    def __init__(self, missing: list[str]):
        self.missing = missing
        super().__init__(
            "missing required configuration: "
            + ", ".join(missing)
            + " — copy .env.example to .env and fill in the values"
        )


class Settings(BaseModel):
    ebay_client_id: str
    ebay_client_secret: str
    ebay_ru_name: str | None = None  # RuName of the redirect URL (consent flow only)
    ebay_env: Literal["sandbox", "production"] = "sandbox"  # sandbox-first hard rule
    marketplace_id: str = "EBAY_GB"
    currency: str = "GBP"
    margin: Decimal = Decimal("0.20")
    fvf_rate: Decimal = Decimal("0.128")
    fixed_fee: Decimal = Decimal("0.30")
    max_qty: int = 3
    boilerplate: str = ""
    payment_policy_id: str | None = None
    return_policy_id: str | None = None
    fulfillment_policy_id: str | None = None
    # Ship-from address for the inventory location (eBay requires a real one).
    ship_from_address_line1: str = ""
    ship_from_city: str = ""
    ship_from_postal_code: str = ""
    ship_from_country: str = "GB"


def load_env_file(path: str | Path = ".env") -> None:
    """Set KEY=VALUE pairs from a .env file into os.environ (existing vars win)."""
    env_path = Path(path)
    if not env_path.exists():
        return
    for line in env_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key:
            os.environ.setdefault(key, value)


def load_settings(
    env_file: str | Path = ".env", toml_file: str | Path = "config.toml"
) -> Settings:
    """Load Settings from .env + optional config.toml; fail fast on missing keys."""
    load_env_file(env_file)

    missing = [key for key in REQUIRED_ENV_KEYS if not os.environ.get(key)]
    if missing:
        raise MissingConfigError(missing)

    data: dict = {
        "ebay_client_id": os.environ["EBAY_CLIENT_ID"],
        "ebay_client_secret": os.environ["EBAY_CLIENT_SECRET"],
        "ebay_ru_name": os.environ.get("EBAY_RU_NAME") or None,
        "ebay_env": os.environ.get("EBAY_ENV", "sandbox"),
    }

    toml_path = Path(toml_file)
    if toml_path.exists():
        overrides = tomllib.loads(toml_path.read_text(encoding="utf-8"))
        for key in _TOML_MONEY_KEYS:
            if key in overrides:
                data[key] = Decimal(str(overrides[key]))
        for key in _TOML_PLAIN_KEYS:
            if key in overrides:
                data[key] = overrides[key]

    return Settings(**data)
