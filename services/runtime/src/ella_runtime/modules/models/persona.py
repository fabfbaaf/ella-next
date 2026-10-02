"""Stable persona instructions shared by chat, voice, task, and game calls."""

import os
from importlib.resources import files
from pathlib import Path


def load_persona() -> str:
    configured = os.getenv("ELLA_PERSONA_FILE")
    if configured:
        path = Path(configured).expanduser()
        content = path.read_text(encoding="utf-8").strip()
    else:
        content = files("ella_runtime").joinpath("persona.md").read_text(encoding="utf-8").strip()
    if not content:
        raise ValueError("人格设定文件不能为空")
    return content


def system_prompt(persona: str, instructions: str = "") -> str:
    task = instructions.strip()
    if not task:
        return persona.strip()
    return f"{persona.strip()}\n\n当前任务要求：\n{task}"
