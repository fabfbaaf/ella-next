"""Shared location for local runtime data that is kept outside Git."""

import os
from pathlib import Path


def default_data_dir() -> Path:
    configured = os.getenv("ELLA_DATA_DIR")
    if configured:
        return Path(configured).expanduser()
    local_app_data = os.getenv("LOCALAPPDATA")
    if local_app_data:
        return Path(local_app_data) / "EllaNext"
    return Path.home() / ".local" / "share" / "ella-next"
