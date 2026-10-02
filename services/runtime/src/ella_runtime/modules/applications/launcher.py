"""Launch visible installed desktop applications from the admin catalog."""

from __future__ import annotations

import asyncio
import subprocess
import sys
from pathlib import Path


class ApplicationLaunchError(RuntimeError):
    """The selected application is unavailable on this machine."""


_OFFICE_EXECUTABLES = {"excel": "EXCEL.EXE", "word": "WINWORD.EXE"}


def _office_path(executable: str) -> Path:
    if sys.platform != "win32":
        raise ApplicationLaunchError("桌面 Office 启动仅支持 Windows")
    import winreg

    key_name = rf"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\{executable}"
    for root in (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE):
        try:
            with winreg.OpenKey(root, key_name) as key:
                value = winreg.QueryValueEx(key, "")[0]
                path = Path(value)
                if path.is_file():
                    return path
        except OSError:
            continue
    raise ApplicationLaunchError(f"未找到已安装的 {executable}")


async def launch_office(app_id: str) -> dict[str, str]:
    executable = _OFFICE_EXECUTABLES.get(app_id)
    if executable is None:
        raise ApplicationLaunchError("未知的 Office 应用")
    path = await asyncio.to_thread(_office_path, executable)
    try:
        await asyncio.to_thread(subprocess.Popen, [str(path)], cwd=str(path.parent))
    except OSError as exc:
        raise ApplicationLaunchError(f"无法启动 {executable}") from exc
    return {"application": app_id, "state": "launched"}
