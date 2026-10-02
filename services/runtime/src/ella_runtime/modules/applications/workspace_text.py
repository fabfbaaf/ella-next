"""Verified text creation in the private workspace or the user's desktop."""

import ctypes
import hashlib
import os
from pathlib import Path
from uuid import UUID, uuid4

from ella_runtime.modules.agent.contracts import ToolEvidence
from ella_runtime.storage_paths import default_data_dir

_OFFICE_EXTENSIONS = {
    ".doc", ".docx", ".docm", ".xls", ".xlsx", ".xlsm",
    ".ppt", ".pptx", ".pptm", ".odt", ".ods", ".odp",
}


class WorkspaceTextTool:
    name = "workspace.write_text"
    description = (
        "在艾拉工作区中写入 UTF-8 文本文件并读回核验。参数："
        '{"path":"相对路径","content":"完整文件内容","overwrite":false}。'
        "默认不覆盖已有文件；不能访问工作区之外。"
    )

    def __init__(self, root: Path | None = None) -> None:
        self.root = (root or default_data_dir() / "workspace").resolve()
        self.root.mkdir(parents=True, exist_ok=True)

    def _target(self, arguments: dict) -> tuple[Path, bytes, bool]:
        name = arguments.get("path")
        content = arguments.get("content")
        overwrite = arguments.get("overwrite", False)
        if not isinstance(name, str) or not name.strip() or Path(name).is_absolute():
            raise ValueError("文件路径必须是工作区内的相对路径")
        if not isinstance(content, str) or not isinstance(overwrite, bool):
            raise TypeError("文件内容或覆盖设置无效")
        encoded = content.encode("utf-8")
        if len(encoded) > 1024 * 1024:
            raise ValueError("文本文件超过 1 MB")
        target = (self.root / name).resolve()
        if not target.is_relative_to(self.root) or target == self.root:
            raise ValueError("文件路径超出工作区")
        return target, encoded, overwrite

    async def execute(self, arguments: dict, *, action_id: str) -> ToolEvidence:
        target, content, overwrite = self._target(arguments)
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.exists() and not overwrite and target.read_bytes() != content:
            raise FileExistsError("目标文件已存在，计划未允许覆盖")
        temporary = target.parent / f".{target.name}.{uuid4().hex}.tmp"
        try:
            temporary.write_bytes(content)
            os.replace(temporary, target)
        finally:
            temporary.unlink(missing_ok=True)
        evidence = await self.reconcile(arguments, action_id=action_id)
        assert evidence is not None
        return evidence

    async def reconcile(self, arguments: dict, *, action_id: str) -> ToolEvidence | None:
        target, content, _ = self._target(arguments)
        if not target.is_file():
            return None
        actual = target.read_bytes()
        if actual != content:
            return None
        return ToolEvidence(
            verified=True,
            details={
                "path": str(target),
                "bytes": len(actual),
                "sha256": hashlib.sha256(actual).hexdigest(),
            },
        )


def _windows_desktop() -> Path:
    """Resolve the actual shell Desktop, including a redirected/OneDrive Desktop."""
    if os.name != "nt":
        raise RuntimeError("桌面文件工具目前仅支持 Windows")

    class _Guid(ctypes.Structure):
        _fields_ = [
            ("data1", ctypes.c_uint32),
            ("data2", ctypes.c_uint16),
            ("data3", ctypes.c_uint16),
            ("data4", ctypes.c_ubyte * 8),
        ]

    # FOLDERID_Desktop (the file-system Desktop, rather than the virtual namespace).
    folder_id = _Guid.from_buffer_copy(UUID("b4bfcc3a-db2c-424c-b029-7fe99a87c641").bytes_le)
    shell32 = ctypes.WinDLL("shell32", use_last_error=True)
    shell32.SHGetKnownFolderPath.argtypes = [
        ctypes.POINTER(_Guid), ctypes.c_uint32, ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p)
    ]
    shell32.SHGetKnownFolderPath.restype = ctypes.c_long
    result = ctypes.c_void_p()
    status = shell32.SHGetKnownFolderPath(ctypes.byref(folder_id), 0, None, ctypes.byref(result))
    if status != 0 or not result.value:
        raise OSError(f"无法定位系统桌面：0x{status & 0xffffffff:08x}")
    try:
        return Path(ctypes.wstring_at(result.value)).resolve(strict=True)
    finally:
        ole32 = ctypes.WinDLL("ole32", use_last_error=True)
        ole32.CoTaskMemFree.argtypes = [ctypes.c_void_p]
        ole32.CoTaskMemFree(result)


class DesktopTextTool:
    """Create one UTF-8 file on the real Desktop without changing existing files."""

    name = "desktop.write_text"
    description = (
        "只在 Windows 系统桌面根目录新建一个 UTF-8 文件，保留用户指定的精确文件名，"
        "不自动添加 .txt 等扩展名，不覆盖已有文件。参数："
        '{"name":"文件名（不可包含目录）","content":"完整文件内容，省略时为空"}。'
    )

    def __init__(self, root: Path | None = None) -> None:
        self.root = (root if root is not None else _windows_desktop()).resolve(strict=True)
        if not self.root.is_dir():
            raise NotADirectoryError(f"桌面目录不可用：{self.root}")

    def _target(self, arguments: dict) -> tuple[Path, bytes]:
        name = arguments.get("name")
        content = arguments.get("content", "")
        if not isinstance(name, str) or not name or name != name.strip() or len(name) > 255:
            raise ValueError("必须指定桌面文件的精确文件名")
        if name in {".", ".."} or name.endswith(".") or any(
            char in name for char in '<>:"/\\|?*\x00'
        ) or any(ord(char) < 32 for char in name):
            raise ValueError("桌面文件名不能包含路径或 Windows 保留字符")
        if name.split(".", 1)[0].upper() in {
            "CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)),
            *(f"LPT{i}" for i in range(1, 10)),
        }:
            raise ValueError("桌面文件名是 Windows 保留名称")
        if Path(name).suffix.casefold() in _OFFICE_EXTENSIONS:
            raise ValueError("桌面文本工具不能创建 Word、Excel 或 PPT 文件")
        if not isinstance(content, str) or arguments.get("overwrite", False) is not False:
            raise ValueError("文件内容无效或请求覆盖已有桌面文件")
        encoded = content.encode("utf-8")
        if len(encoded) > 1024 * 1024:
            raise ValueError("文本文件超过 1 MB")
        return self.root / name, encoded

    async def execute(self, arguments: dict, *, action_id: str) -> ToolEvidence:
        target, content = self._target(arguments)
        # Exclusive creation ensures even an existing empty file is never mistaken for our work.
        with target.open("xb") as stream:
            stream.write(content)
        actual = target.read_bytes()
        return ToolEvidence(
            verified=actual == content,
            details={
                "path": str(target),
                "bytes": len(actual),
                "sha256": hashlib.sha256(actual).hexdigest(),
            },
        )

    async def reconcile(self, arguments: dict, *, action_id: str) -> ToolEvidence | None:
        # A matching pre-existing file is insufficient proof that this action created it.
        return None
