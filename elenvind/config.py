import tomllib
from pathlib import Path

config = {}

def load_config():
    config_path = Path(__file__).resolve().parent.parent / "config.toml"
    if not config_path.exists():
        raise FileNotFoundError(f"Configuration file not found: {config_path}")
    with open(config_path, "rb") as f:
        config.clear()
        config.update(tomllib.load(f))
    return config
