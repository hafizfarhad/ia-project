"""Settings from environment variables, plus YAML config loading."""
from functools import lru_cache
from pathlib import Path

import yaml
from pydantic_settings import BaseSettings

ROOT = Path(__file__).resolve().parent.parent
CONFIG_DIR = ROOT / "config"


class Settings(BaseSettings):
    db_host: str = "postgres"
    db_port: int = 5432
    db_name: str = "walletwatch"
    db_user: str = "aml_app"
    db_password: str = ""

    api_url: str = "http://api:8000"
    jwt_secret: str = ""
    jwt_minutes: int = 30
    ingest_token: str = ""
    audit_hmac_key: str = ""
    demo_password: str = ""
    model_dir: str = str(ROOT / "models")


@lru_cache
def settings() -> Settings:
    return Settings()


@lru_cache
def load_yaml(name: str) -> dict:
    with open(CONFIG_DIR / f"{name}.yaml") as f:
        return yaml.safe_load(f)
