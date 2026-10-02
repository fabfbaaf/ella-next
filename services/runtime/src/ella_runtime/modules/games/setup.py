"""Discover local games and deploy only the bundled, verified integration files.

Detection never writes a game directory. Deployment is deliberately conservative:
ambiguous instances, unknown versions and a running game require user intervention.
"""
from __future__ import annotations

import asyncio
import copy
import ctypes
import hashlib
import json
import os
import re
import shutil
import sys
import threading
import uuid
import zipfile
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path, PureWindowsPath
from typing import Any
from xml.etree import ElementTree

from ella_runtime.modules.games.launch import steam_libraries
from ella_runtime.storage_paths import default_data_dir

GAME_IDS = ("minecraft", "stardew_valley", "bannerlord")
_MANIFEST_IDS = {"minecraft": "minecraft", "stardew_valley": "stardew", "bannerlord": "bannerlord"}
_MAX_FILE = 128 * 1024 * 1024
_MAX_TOTAL = 512 * 1024 * 1024
_MAX_FILES = 2048


class GameSetupError(ValueError):
    """A deployment condition that can safely be shown to the user."""


def _game_id(value: str) -> str:
    value = "stardew_valley" if value == "stardew" else value
    if value not in GAME_IDS:
        raise GameSetupError("未知游戏")
    return value


def _version(value: str | None) -> tuple[int, ...] | None:
    if not isinstance(value, str):
        return None
    match = re.match(r"^v?(\d+(?:\.\d+){1,3})(?:\b|[+\-])", value.strip())
    return tuple(int(part) for part in match[1].split(".")) if match else None


def _matches(value: str | None, constraints: dict[str, Any], prefix: str) -> bool:
    actual = _version(value)
    if actual is None:
        return False
    exact = constraints.get(f"{prefix}_exact")
    allowed = constraints.get(f"{prefix}_versions")
    if exact is not None and actual != _version(str(exact)):
        return False
    if allowed is not None and actual not in [_version(str(item)) for item in allowed]:
        return False
    minimum = constraints.get(f"{prefix}_min")
    maximum = constraints.get(f"{prefix}_max_exclusive")
    if minimum is not None and actual < (_version(str(minimum)) or (999,)):
        return False
    return maximum is None or actual < (_version(str(maximum)) or (0,))


def _safe_relative(value: Any) -> Path:
    if not isinstance(value, str) or not value or len(value) > 400:
        raise GameSetupError("资源清单含无效路径")
    windows = PureWindowsPath(value)
    parts = value.replace("\\", "/").split("/")
    if windows.is_absolute() or windows.drive or any(
        part in ("", ".", "..") or ":" in part or part.endswith((".", " "))
        or re.match(r"^(CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])(?:\.|$)", part, re.IGNORECASE)
        for part in parts
    ):
        raise GameSetupError("资源清单含越界或特殊路径")
    return Path(*parts)


def _check_path(path: Path, root: Path | None = None) -> None:
    """Reject every existing symlink/junction component, including the root."""
    absolute = Path(os.path.abspath(path))
    if root is not None:
        boundary = Path(os.path.abspath(root))
        if absolute != boundary and boundary not in absolute.parents:
            raise GameSetupError("部署路径超出选定目录")
    for component in (absolute, *absolute.parents):
        if not component.exists() and not component.is_symlink():
            continue
        stat = component.lstat()
        if component.is_symlink() or getattr(stat, "st_file_attributes", 0) & 0x400:
            raise GameSetupError("游戏或资源路径含符号链接/目录联接，已停止部署")


def _hash(path: Path) -> str:
    _check_path(path)
    if not path.is_file() or path.stat().st_size > _MAX_FILE:
        raise GameSetupError("资源文件不存在或超过大小限制")
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _read_json(path: Path, *, limit: int = 2 * 1024 * 1024) -> dict[str, Any]:
    _check_path(path)
    if path.stat().st_size > limit:
        raise GameSetupError("配置文件超过大小限制")
    value = json.loads(path.read_text(encoding="utf-8-sig"))
    if not isinstance(value, dict):
        raise GameSetupError("配置文件格式无效")
    return value


def windows_file_version(path: Path) -> str | None:
    """Read PE metadata without launching the executable or importing game code."""
    if os.name != "nt" or not path.is_file():
        return None
    try:
        api = ctypes.WinDLL("version", use_last_error=True)
        api.GetFileVersionInfoSizeW.argtypes = [ctypes.c_wchar_p, ctypes.POINTER(ctypes.c_uint32)]
        api.GetFileVersionInfoSizeW.restype = ctypes.c_uint32
        api.GetFileVersionInfoW.argtypes = [ctypes.c_wchar_p, ctypes.c_uint32, ctypes.c_uint32, ctypes.c_void_p]
        api.VerQueryValueW.argtypes = [ctypes.c_void_p, ctypes.c_wchar_p,
                                     ctypes.POINTER(ctypes.c_void_p), ctypes.POINTER(ctypes.c_uint)]
        size = api.GetFileVersionInfoSizeW(str(path), None)
        if not size or size > 1024 * 1024:
            return None
        data = ctypes.create_string_buffer(size)
        if not api.GetFileVersionInfoW(str(path), 0, size, data):
            return None
        pointer = ctypes.c_void_p()
        length = ctypes.c_uint()
        if not api.VerQueryValueW(data, "\\", ctypes.byref(pointer), ctypes.byref(length)):
            return None
        values = ctypes.cast(pointer, ctypes.POINTER(ctypes.c_uint32))
        if length.value < 52 or values[0] != 0xFEEF04BD:
            return None
        # FileVersion (rather than .NET assembly version) is the displayed game version.
        high, low = values[2], values[3]
        parts = [high >> 16, high & 0xFFFF, low >> 16, low & 0xFFFF]
        while len(parts) > 2 and parts[-1] == 0:
            parts.pop()
        return ".".join(str(part) for part in parts)
    except (OSError, ValueError, AttributeError):
        return None


def game_is_running(root: Path, *, minecraft: bool = False) -> bool:
    """Inspect actual process image paths. Failure to inspect is never permission to write."""
    if os.name != "nt":
        raise GameSetupError("当前平台无法安全检查游戏进程")
    from ctypes import wintypes
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    psapi = ctypes.WinDLL("psapi", use_last_error=True)
    kernel.OpenProcess.restype = wintypes.HANDLE
    kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel.QueryFullProcessImageNameW.argtypes = [
        wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD),
    ]
    ids = (wintypes.DWORD * 32768)()
    needed = wintypes.DWORD()
    if not psapi.EnumProcesses(ids, ctypes.sizeof(ids), ctypes.byref(needed)):
        raise GameSetupError("无法检查游戏进程，请稍后重试")
    if needed.value >= ctypes.sizeof(ids):
        raise GameSetupError("进程列表过大，无法安全检查游戏是否运行")
    boundary = os.path.normcase(os.path.abspath(root))
    for pid in ids[:needed.value // ctypes.sizeof(wintypes.DWORD)]:
        handle = kernel.OpenProcess(0x1000, False, pid)
        if not handle:
            continue
        try:
            buffer = ctypes.create_unicode_buffer(32768)
            length = wintypes.DWORD(len(buffer))
            if not kernel.QueryFullProcessImageNameW(handle, 0, buffer, ctypes.byref(length)):
                continue
            image = Path(buffer.value)
            if minecraft and image.name.lower() in (
                "java.exe", "javaw.exe", "minecraftlauncher.exe", "minecraft.exe",
            ):
                return True
            image_path = os.path.normcase(os.path.abspath(image))
            if image_path.startswith(boundary + os.sep):
                return True
        finally:
            kernel.CloseHandle(handle)
    return False


def _default_bundle() -> Path | None:
    resources = os.getenv("ELLA_RESOURCES_DIR")
    if resources:
        for candidate in (Path(resources) / "game-setup", Path(resources) / "artifacts/game-setup"):
            if (candidate / "bundle.json").is_file():
                return candidate
    if getattr(sys, "frozen", False):
        return None
    for parent in Path(__file__).resolve().parents:
        candidate = parent / "artifacts/game-setup"
        if (candidate / "bundle.json").is_file():
            return candidate
    return None


class GameSetupManager:
    def __init__(
        self, bundle_root: Path | None = None, data_dir: Path | None = None, *,
        libraries: list[Path] | None = None, minecraft_roots: list[Path] | None = None,
        process_checker: Callable[[Path], bool] | None = None,
        version_reader: Callable[[Path], str | None] | None = None,
        java_major: int | None = None,
    ) -> None:
        self.bundle_root = Path(bundle_root) if bundle_root else _default_bundle()
        self.data_dir = Path(data_dir) if data_dir else default_data_dir()
        self.libraries = libraries
        self.minecraft_roots = minecraft_roots
        self.process_checker = process_checker
        self.version_reader = version_reader or windows_file_version
        self.java_major = java_major
        self._states: dict[str, dict[str, Any]] = {
            game: {"game_id": game, "state": "not_found", "detail": "尚未扫描游戏目录",
                   "candidates": [], "files_changed": [], "backup_dir": None,
                   "blockers": [], "ready": False, "detected": False}
            for game in GAME_IDS
        }
        self._selected: dict[str, dict[str, str]] = {}
        self._locks = {game: asyncio.Lock() for game in GAME_IDS}
        self._mutation_lock = threading.RLock()
        self._load_selections()

    def _load_selections(self) -> None:
        try:
            values = _read_json(self.data_dir / "game-setup/selections.json")
            for key, value in values.items():
                if key in GAME_IDS and isinstance(value, dict) and isinstance(value.get("path"), str):
                    self._selected[key] = {"path": value["path"], "profile": str(value.get("profile", ""))}
        except (OSError, ValueError, GameSetupError):
            pass

    def _save_selections(self) -> None:
        destination = self.data_dir / "game-setup/selections.json"
        _check_path(destination, self.data_dir)
        destination.parent.mkdir(parents=True, exist_ok=True)
        temporary = destination.with_name(f".selections-{uuid.uuid4().hex}.tmp")
        try:
            temporary.write_text(json.dumps(self._selected, ensure_ascii=False), encoding="utf-8")
            os.replace(temporary, destination)
        finally:
            temporary.unlink(missing_ok=True)

    def _manifest(self, game_id: str) -> dict[str, Any]:
        if self.bundle_root is None or not (self.bundle_root / "bundle.json").is_file():
            raise GameSetupError(
                "安装包缺少游戏接入资源，请更新艾拉安装包" if getattr(sys, "frozen", False)
                else "源码缺少游戏接入资源，请运行 scripts/prepare-game-setup.py 准备资源"
            )
        value = _read_json(self.bundle_root / "bundle.json")
        if value.get("schema_version") != 1 or not isinstance(value.get("games"), list):
            raise GameSetupError("游戏资源清单版本无效")
        matches = [item for item in value["games"] if isinstance(item, dict)
                   and item.get("id") == _MANIFEST_IDS[game_id]]
        if len(matches) != 1:
            raise GameSetupError("安装包未提供这款游戏的接入资源")
        return matches[0]

    def _roots(self, game_id: str) -> list[Path]:
        relative = {"stardew_valley": "Stardew Valley", "bannerlord": "Mount & Blade II Bannerlord"}
        roots = [library / "steamapps/common" / relative[game_id]
                 for library in (self.libraries if self.libraries is not None else steam_libraries())]
        explicit = os.getenv(f"ELLA_GAME_{game_id.upper()}_EXECUTABLE", "").strip()
        if explicit:
            path = Path(explicit).expanduser()
            if game_id == "stardew_valley":
                roots.append(path.parent)
            elif len(path.parents) >= 3:
                roots.append(path.parents[2])
        if game_id in self._selected:
            roots.append(Path(self._selected[game_id]["path"]))
        return list(dict.fromkeys(Path(os.path.abspath(path)) for path in roots))

    def _candidates(self, game_id: str) -> list[dict[str, Any]]:
        if game_id == "minecraft":
            return self._minecraft_candidates()
        candidates = []
        for root in self._roots(game_id):
            try:
                _check_path(root)
                if game_id == "stardew_valley":
                    if not (root / "Stardew Valley.exe").is_file():
                        continue
                    version = self.version_reader(root / "Stardew Valley.dll")
                    version = version or self.version_reader(root / "Stardew Valley.exe")
                    loader = root / "StardewModdingAPI.exe"
                    loader_version = (self.version_reader(root / "StardewModdingAPI.dll")
                                      or self.version_reader(loader)) if loader.is_file() else None
                else:
                    binaries = root / "bin/Win64_Shipping_Client"
                    if not any((binaries / name).is_file() for name in
                               ("Bannerlord.exe", "Bannerlord.Native.exe", "Bannerlord.BLSE.Standalone.exe")):
                        continue
                    version = self._bannerlord_version(root)
                    loader = binaries / "Bannerlord.BLSE.Standalone.exe"
                    loader_version = self.version_reader(loader) if loader.is_file() else None
                candidates.append({"path": str(root), "label": root.name, "version": version,
                                   "loader_version": loader_version, "profile": ""})
            except (OSError, ValueError):
                continue
        return candidates

    def _bannerlord_version(self, root: Path) -> str | None:
        path = root / "Modules/Native/SubModule.xml"
        try:
            _check_path(path, root)
            if path.stat().st_size <= 1024 * 1024:
                node = ElementTree.fromstring(path.read_text(encoding="utf-8-sig")).find("Version")
                if node is not None:
                    return node.attrib.get("value", "").removeprefix("v")
        except (OSError, ValueError, ElementTree.ParseError):
            pass
        return self.version_reader(root / "bin/Win64_Shipping_Client/Bannerlord.exe")

    def _minecraft_candidates(self) -> list[dict[str, Any]]:
        roots = self.minecraft_roots if self.minecraft_roots is not None else [
            Path(os.getenv("APPDATA", str(Path.home()))) / ".minecraft",
        ]
        selected = self._selected.get("minecraft")
        if selected:
            roots = [*roots, Path(selected["path"])]
        candidates: dict[tuple[str, str], dict[str, Any]] = {}
        for root in dict.fromkeys(Path(os.path.abspath(path)) for path in roots):
            try:
                profiles = _read_json(root / "launcher_profiles.json").get("profiles", {})
                if not isinstance(profiles, dict):
                    continue
                for profile_id, profile in list(profiles.items())[:200]:
                    if not isinstance(profile, dict) or not isinstance(profile_id, str):
                        continue
                    version_id = profile.get("lastVersionId")
                    if not isinstance(version_id, str):
                        continue
                    game_dir = Path(profile.get("gameDir", str(root))).expanduser()
                    if not game_dir.is_absolute():
                        game_dir = root / game_dir
                    game_dir = Path(os.path.abspath(game_dir))
                    _check_path(game_dir)
                    if not game_dir.is_dir():
                        continue
                    version, loader, java = self._minecraft_version(root, game_dir, version_id)
                    key = (str(game_dir), profile_id)
                    candidates[key] = {"path": str(game_dir), "label": str(profile.get("name", profile_id))[:120],
                                       "profile": profile_id, "version": version, "loader_version": loader,
                                       "profile_version": version_id,
                                       "java_required": java, "launcher_root": str(root),
                                       "java_path": profile.get("javaDir"),
                                       "base_installed": any((base / "versions/26.2/26.2.json").is_file()
                                                             and (base / "versions/26.2/26.2.jar").is_file()
                                                             for base in (root, game_dir))}
            except (OSError, ValueError, TypeError):
                continue
        return list(candidates.values())

    def _minecraft_version(self, root: Path, instance: Path, version_id: str) -> tuple[str | None, str | None, int | None]:
        visited = set()
        version, loader, java = None, None, None
        for _ in range(8):
            safe_id = _safe_relative(version_id)
            if len(safe_id.parts) != 1 or version_id in visited:
                raise GameSetupError("Minecraft 版本继承配置无效")
            visited.add(version_id)
            paths = [instance / "versions" / safe_id / f"{version_id}.json",
                     root / "versions" / safe_id / f"{version_id}.json"]
            path = next((path for path in paths if path.is_file()), None)
            if path is None:
                return version, loader, java
            metadata = _read_json(path)
            for library in metadata.get("libraries", [])[:1000]:
                if isinstance(library, dict) and isinstance(library.get("name"), str):
                    name = library["name"]
                    if name.startswith("net.fabricmc:fabric-loader:"):
                        loader = name.split(":")[2]
            requested_java = metadata.get("javaVersion", {}).get("majorVersion")
            if isinstance(requested_java, int):
                java = requested_java
            parent = metadata.get("inheritsFrom")
            if not parent:
                version = str(metadata.get("id", version_id))
                break
            if not isinstance(parent, str):
                raise GameSetupError("Minecraft 版本继承配置无效")
            version_id = parent
        return version, loader, java

    def _java_version(self, candidate: dict[str, Any]) -> int | None:
        if self.java_major is not None:
            return self.java_major
        releases: list[Path] = []
        selected_java = candidate.get("java_path")
        if selected_java:
            java = Path(selected_java)
            _check_path(java)
            if not java.is_file():
                return None
            release = java.parent.parent / "release"
            try:
                _check_path(release)
                if release.stat().st_size <= 100000:
                    match = re.search(r'JAVA_VERSION="(\d+)', release.read_text(encoding="utf-8"))
                    return int(match[1]) if match else None
            except (OSError, ValueError):
                return None
            return None
        java_home = os.getenv("JAVA_HOME")
        if java_home:
            releases.append(Path(java_home) / "release")
        executable = shutil.which("java")
        if executable:
            releases.append(Path(executable).parent.parent / "release")
        runtime = Path(candidate["launcher_root"]) / "runtime"
        if runtime.is_dir():
            for visited, (current, directories, files) in enumerate(
                os.walk(runtime, followlinks=False), start=1,
            ):
                if visited > 2000:
                    break
                if len(Path(current).relative_to(runtime).parts) >= 7 or len(releases) >= 100:
                    directories[:] = []
                if "release" in files:
                    releases.append(Path(current) / "release")
        for release in releases:
            try:
                _check_path(release)
                if release.stat().st_size > 100000:
                    continue
                match = re.search(r'JAVA_VERSION="(\d+)', release.read_text(encoding="utf-8"))
                if match and int(match[1]) >= 25:
                    return int(match[1])
            except (OSError, ValueError):
                continue
        return None

    def _allowed_destination(self, game_id: str, relative: Path, kind: str) -> bool:
        value = relative.as_posix()
        if game_id == "minecraft":
            return len(relative.parts) == 2 and relative.parts[0] == "mods" and bool(
                re.fullmatch(r"(?:ella-minecraft-bridge|fabric-api)[\w.+-]*\.jar", relative.name)
            )
        if game_id == "stardew_valley":
            if value.startswith("Mods/Ella.StardewBridge/"):
                return kind == "mod"
            if kind != "loader":
                return False
            if value.startswith(("smapi-internal/", "Mods/ConsoleCommands/", "Mods/SaveBackup/")):
                return True
            return relative.name in {
                "StardewModdingAPI.exe", "StardewModdingAPI.dll", "StardewModdingAPI.pdb",
                "StardewModdingAPI.deps.json", "StardewModdingAPI.runtimeconfig.json",
                "StardewModdingAPI.exe.config", "StardewModdingAPI.xml", "steam_appid.txt",
            } and len(relative.parts) == 1
        return (len(relative.parts) >= 3 and relative.parts[0] == "Modules"
                and relative.parts[1] in {"Bannerlord.GABS", "Bannerlord.Harmony", "Bannerlord.ButterLib",
                                          "Bannerlord.UIExtenderEx", "Bannerlord.MBOptionScreen"}) or (
            len(relative.parts) == 3 and relative.parts[:2] == ("bin", "Win64_Shipping_Client")
            and relative.name.startswith("Bannerlord.BLSE.") and relative.suffix.lower() in {".exe", ".dll", ".pdb", ".json", ".config"}
        )

    def _files(self, game_id: str, root: Path, entry: dict[str, Any], candidate: dict[str, Any]) -> list[tuple[Path, Path, str]]:
        rows = entry.get("files")
        if not isinstance(rows, list) or not rows or len(rows) > _MAX_FILES:
            raise GameSetupError("游戏资源清单文件数量无效")
        constraints = entry.get("version_constraints", {})
        existing_loader = candidate.get("loader_version")
        preserve_loader = game_id == "stardew_valley" and bool(existing_loader) and _matches(
            existing_loader, constraints, "loader",
        )
        result = []
        destinations = set()
        total = 0
        for row in rows:
            if not isinstance(row, dict):
                raise GameSetupError("游戏资源清单格式无效")
            source_relative = _safe_relative(row.get("source"))
            relative = _safe_relative(row.get("destination"))
            kind = row.get("kind")
            if kind not in ("mod", "loader", "library") or not self._allowed_destination(game_id, relative, kind):
                raise GameSetupError("资源清单尝试修改未授权游戏文件")
            key = str(relative).casefold()
            if key in destinations:
                raise GameSetupError("资源清单存在重复目标")
            destinations.add(key)
            source = self.bundle_root / source_relative  # type: ignore[operator]
            target = root / relative
            _check_path(source, self.bundle_root)
            _check_path(target, root)
            expected = row.get("sha256")
            if not isinstance(expected, str) or not re.fullmatch(r"[0-9a-fA-F]{64}", expected):
                raise GameSetupError("资源清单缺少有效 SHA256")
            expected = expected.lower()
            if _hash(source) != expected:
                raise GameSetupError("游戏接入资源校验失败，请重新安装艾拉")
            total += source.stat().st_size
            if total > _MAX_TOTAL:
                raise GameSetupError("游戏资源总大小超过限制")
            if kind != "loader" or not preserve_loader:
                result.append((source, target, expected))
        actions = entry.get("loader_actions", [])
        if not isinstance(actions, list) or len(actions) > 1:
            raise GameSetupError("资源清单包含不支持的安装动作")
        for action in actions:
            if game_id != "stardew_valley" or action != {
                "action": "copy_game_file", "source": "Stardew Valley.deps.json",
                "destination": "StardewModdingAPI.deps.json",
            }:
                raise GameSetupError("资源清单包含未授权安装动作")
            if not preserve_loader:
                source = root / "Stardew Valley.deps.json"
                target = root / "StardewModdingAPI.deps.json"
                if "stardewmoddingapi.deps.json" in destinations:
                    raise GameSetupError("SMAPI依赖文件必须从当前游戏生成，不能同时打包覆盖")
                _check_path(source, root)
                _check_path(target, root)
                result.append((source, target, _hash(source)))
        return result

    def _fabric_api(self, root: Path, files: list[tuple[Path, Path, str]]) -> bool:
        paths = [source for source, target, _ in files if target.name.startswith("fabric-api")]
        mods = root / "mods"
        _check_path(mods, root)
        if mods.is_dir():
            paths.extend(list(mods.glob("*.jar"))[:1000])
        for path in paths:
            try:
                _check_path(path)
                if path.stat().st_size > _MAX_FILE:
                    continue
                with zipfile.ZipFile(path) as archive:
                    info = archive.getinfo("fabric.mod.json")
                    if info.file_size > 100000:
                        continue
                    metadata = json.loads(archive.read(info))
                if metadata.get("id") == "fabric-api" and "26.2" in str(metadata.get("version", "")):
                    return True
            except (OSError, ValueError, KeyError, zipfile.BadZipFile):
                continue
        return False

    def _mod_conflicts(self, root: Path, files: list[tuple[Path, Path, str]]) -> bool:
        """Adding another jar with the same Fabric ID would prevent the game from booting."""
        targets = {target.resolve() for _, target, _ in files}
        mods = root / "mods"
        if not mods.is_dir():
            return False
        for path in list(mods.glob("*.jar"))[:1000]:
            _check_path(path, root)
            if path.resolve() in targets:
                continue
            try:
                if path.stat().st_size > _MAX_FILE:
                    continue
                with zipfile.ZipFile(path) as archive:
                    info = archive.getinfo("fabric.mod.json")
                    if info.file_size > 100000:
                        continue
                    metadata = json.loads(archive.read(info))
                if metadata.get("id") in {"fabric-api", "ella-minecraft-bridge"}:
                    return True
            except (OSError, ValueError, KeyError, zipfile.BadZipFile):
                continue
        return False

    def _running(self, root: Path, game_id: str) -> bool:
        return self.process_checker(root) if self.process_checker else game_is_running(
            root, minecraft=game_id == "minecraft",
        )

    def _inspect(self, game_id: str) -> tuple[dict[str, Any], list[tuple[Path, Path, str]]]:
        result: dict[str, Any] = {"game_id": game_id, "state": "not_found", "detail": "未找到游戏安装目录",
                                  "candidates": [], "files_changed": [], "backup_dir": None,
                                  "blockers": [], "pending_files": [], "ready": False, "detected": False}
        candidates = self._candidates(game_id)
        result["candidates"] = [dict(item) for item in candidates]
        result["detected"] = bool(candidates)
        selected = self._selected.get(game_id)
        if selected:
            matches = [item for item in candidates if os.path.normcase(item["path"]) == os.path.normcase(selected["path"])
                       and item.get("profile", "") == selected.get("profile", "")]
            if len(matches) != 1:
                result.update(state="needs_selection", detail="之前选择的游戏目录/实例已变化，请重新选择")
                return result, []
            candidate = matches[0]
        elif len(candidates) > 1:
            result.update(state="needs_selection", detail="检测到多个游戏目录/实例，请明确选择")
            return result, []
        elif not candidates:
            return result, []
        else:
            candidate = candidates[0]
        root = Path(candidate["path"])
        result.update(game_root=str(root), version=candidate.get("version"),
                      loader_version=candidate.get("loader_version"), profile=candidate.get("profile", ""))
        if game_id == "minecraft":
            result.update(launcher_root=candidate.get("launcher_root"),
                          profile_version=candidate.get("profile_version"),
                          loader_version=candidate.get("loader_version"),
                          java_required=candidate.get("java_required"),
                          base_installed=candidate.get("base_installed", False))
        try:
            if game_id == "minecraft":
                result["java_found"] = self._java_version(candidate)
            entry = self._manifest(game_id)
            if entry.get("support") != "supported":
                result.update(state="unsupported", detail=str(entry.get("notes") or "当前游戏版本暂不支持自动接入")[:500])
                return result, []
            constraints = entry.get("version_constraints", {})
            if isinstance(constraints, dict):
                result["bundled_loader_version"] = constraints.get("loader_bundled")
            if not isinstance(constraints, dict) or not _matches(candidate.get("version"), constraints, "game"):
                result.update(state="unsupported", detail="无法验证游戏版本，或版本不在插件支持范围")
                result["blockers"] = ["game_version"]
                return result, []
            blockers = []
            if game_id == "minecraft":
                if candidate.get("version") != "26.2":
                    result.update(state="unsupported", detail="当前 Minecraft 插件只支持 26.2")
                    return result, []
                if not _matches(candidate.get("loader_version"), {"loader_min": "0.19.5"}, "loader"):
                    blockers.append("fabric_0.19.5_required")
                if candidate.get("java_required") != 25 or (result.get("java_found") or 0) < 25:
                    blockers.append("java_25_required")
                if not candidate.get("base_installed"):
                    blockers.append("minecraft_26.2_not_installed")
            elif game_id == "stardew_valley":
                loader = root / "StardewModdingAPI.exe"
                if loader.is_file() and not candidate.get("loader_version"):
                    blockers.append("smapi_version_unknown")
            files = self._files(game_id, root, entry, candidate)
            if (
                game_id == "stardew_valley"
                and not _matches(candidate.get("loader_version"), constraints, "loader")
                and not any(target == root / "StardewModdingAPI.exe" for _, target, _ in files)
            ):
                blockers.append("smapi_4.5_required")
            if (
                game_id == "bannerlord"
                and not (root / "bin/Win64_Shipping_Client/Bannerlord.BLSE.Standalone.exe").is_file()
                and not any(target.name == "Bannerlord.BLSE.Standalone.exe" for _, target, _ in files)
            ):
                blockers.append("blse_required")
            if game_id == "minecraft" and not self._fabric_api(root, files):
                blockers.append("fabric_api_26.2_required")
            if game_id == "minecraft" and self._mod_conflicts(root, files):
                blockers.append("minecraft_mod_conflict")
            if blockers:
                result.update(state="blocked", detail="缺少兼容的前置组件，请查看阻塞项", blockers=blockers)
                return result, []
            if self._running(root, game_id):
                result.update(state="running", detail="游戏正在运行；退出游戏后会自动准备接入")
                return result, []
            pending = [(source, target, digest) for source, target, digest in files
                       if not target.is_file() or _hash(target) != digest]
            result.update(state="blocked" if pending else "ready", ready=not pending,
                          detail="游戏接入文件待准备" if pending else "游戏接入文件已就绪",
                          pending_files=[{"path": target.relative_to(root).as_posix(),
                                          "reason": "changed" if target.is_file() else "missing"}
                                         for _, target, _ in pending])
            bundled_loader = constraints.get("loader_bundled")
            if (
                game_id == "bannerlord" and candidate.get("loader_version") and bundled_loader
                and any(target.name.startswith("Bannerlord.BLSE.") for _, target, _ in pending)
                and _version(candidate["loader_version"]) != _version(bundled_loader)
            ):
                result["detail"] = (
                    f"BLSE 当前为 {candidate['loader_version']}，接入资源为 {bundled_loader}；"
                    "自动接入会先备份原文件，再替换为资源清单版本"
                )
            if game_id == "minecraft" and self.launch_path(game_id) is None:
                result["launch_detail"] = "未找到可信启动器；插件可用，请通过平时的启动器进入此实例"
            return result, pending
        except (OSError, ValueError, TypeError) as exc:
            result.update(state="error", detail=str(exc) if isinstance(exc, GameSetupError)
                          else "无法读取或校验游戏接入文件，请检查目录权限和资源清单")
            return result, []

    def scan(self) -> dict[str, dict[str, Any]]:
        with self._mutation_lock:
            for game_id in GAME_IDS:
                if self._states.get(game_id, {}).get("state") != "installing":
                    self._states[game_id] = self._inspect(game_id)[0]
            return copy.deepcopy(self._states)

    def status(self, game_id: str) -> dict[str, Any]:
        game_id = _game_id(game_id)
        return copy.deepcopy(self._states[game_id])

    def select_location(self, game_id: str, path: str | Path, *, profile: str | None = None) -> dict[str, Any]:
        game_id = _game_id(game_id)
        with self._mutation_lock:
            root = Path(path).expanduser()
            if root.is_file():
                root = root.parent if game_id == "stardew_valley" else root.parents[2] if game_id == "bannerlord" else root.parent
            root = Path(os.path.abspath(root))
            _check_path(root)
            previous = self._selected.get(game_id)
            self._selected[game_id] = {"path": str(root), "profile": profile or ""}
            candidates = [item for item in self._candidates(game_id)
                          if os.path.normcase(os.path.abspath(item["path"])) == os.path.normcase(str(root))]
            if profile is not None:
                candidates = [item for item in candidates if item.get("profile", "") == profile]
            if len(candidates) != 1:
                if previous is None:
                    self._selected.pop(game_id, None)
                else:
                    self._selected[game_id] = previous
                raise GameSetupError("目录不是可识别的游戏安装位置，或含多个 Minecraft 实例，请指定 profile")
            self._selected[game_id]["profile"] = candidates[0].get("profile", "")
            try:
                self._save_selections()
            except OSError as exc:
                if previous is None:
                    self._selected.pop(game_id, None)
                else:
                    self._selected[game_id] = previous
                raise GameSetupError("无法保存游戏目录选择，请检查艾拉数据目录权限") from exc
            self._states[game_id] = self._inspect(game_id)[0]
            return self.status(game_id)

    def launch_path(self, game_id: str) -> Path | None:
        game_id = _game_id(game_id)
        root_value = self._states.get(game_id, {}).get("game_root") or self._selected.get(game_id, {}).get("path")
        if game_id == "minecraft":
            explicit = os.getenv("ELLA_GAME_MINECRAFT_EXECUTABLE", "").strip()
            roots = [Path(explicit)] if explicit else []
            roots.extend([
                Path(os.getenv("PROGRAMFILES", r"C:\Program Files")) / "Minecraft Launcher/MinecraftLauncher.exe",
                Path(os.getenv("PROGRAMFILES(X86)", r"C:\Program Files (x86)")) / "Minecraft Launcher/MinecraftLauncher.exe",
                Path(os.getenv("LOCALAPPDATA", str(Path.home()))) / "Microsoft/WindowsApps/Minecraft.exe",
            ])
        elif root_value:
            root = Path(root_value)
            roots = [root / "StardewModdingAPI.exe"] if game_id == "stardew_valley" else [
                root / "bin/Win64_Shipping_Client/Bannerlord.BLSE.Standalone.exe",
            ]
        else:
            return None
        for path in roots:
            try:
                _check_path(path)
                if path.is_file() and path.suffix.lower() == ".exe":
                    return path
            except (OSError, ValueError):
                continue
        return None

    async def ensure(self, game_id: str) -> dict[str, Any]:
        game_id = _game_id(game_id)
        async with self._locks[game_id]:
            task = asyncio.create_task(asyncio.to_thread(self._ensure, game_id))
            try:
                return await asyncio.shield(task)
            except asyncio.CancelledError:
                # Cancellation must not release the lock while filesystem writes continue.
                await asyncio.shield(task)
                raise

    def is_running(self, game_id: str, root: Path | None = None) -> bool:
        game_id = _game_id(game_id)
        selected = root or self._states[game_id].get("game_root")
        if selected is None:
            raise GameSetupError("尚未选择游戏目录，不能安全检查进程")
        return self._running(Path(selected), game_id)

    def _ensure(self, game_id: str) -> dict[str, Any]:
        with self._mutation_lock:
            state, pending = self._inspect(game_id)
            self._states[game_id] = state
            if not pending:
                return copy.deepcopy(state)
            root = Path(state["game_root"])
            state.update(state="installing", detail="正在准备已验证的游戏接入文件")
            backup = self.data_dir / "game-setup/backups" / game_id / (
                datetime.now(UTC).strftime("%Y%m%dT%H%M%S") + "-" + uuid.uuid4().hex
            )
            changed: list[tuple[Path, Path | None, str]] = []
            temporary: Path | None = None
            try:
                _check_path(backup, self.data_dir)
                if self._running(root, game_id):
                    raise GameSetupError("游戏已开始运行，部署已停止")
                # Persist originals before replacing any file; all backups remain after rollback.
                originals: dict[Path, Path | None] = {}
                for source, target, digest in pending:
                    _check_path(target, root)
                    if _hash(source) != digest:
                        raise GameSetupError("资源文件在部署期间改变，已停止")
                    original = backup / target.relative_to(root) if target.exists() else None
                    if original is not None:
                        _check_path(original, backup)
                        original.parent.mkdir(parents=True, exist_ok=True)
                        shutil.copyfile(target, original)
                        if _hash(target) != _hash(original):
                            raise GameSetupError("原文件备份校验失败，已停止")
                    originals[target] = original
                backup.mkdir(parents=True, exist_ok=True)
                (backup / "receipt.json").write_text(json.dumps({"game_id": game_id, "game_root": str(root),
                    "files": [{"path": str(target.relative_to(root)), "new_sha256": digest,
                               "old_sha256": _hash(originals[target]) if originals[target] else None}
                              for _, target, digest in pending]}, ensure_ascii=False, indent=2), encoding="utf-8")
                for source, target, digest in pending:
                    if self._running(root, game_id):
                        raise GameSetupError("游戏已开始运行，部署已停止")
                    _check_path(target, root)
                    original = originals[target]
                    if (target.exists() and original is None) or (
                        original is not None and _hash(target) != _hash(original)
                    ):
                        raise GameSetupError("游戏文件在部署期间被其他程序修改，已停止")
                    target.parent.mkdir(parents=True, exist_ok=True)
                    _check_path(target, root)
                    temporary = target.with_name(f".ella-{uuid.uuid4().hex}.tmp")
                    shutil.copyfile(source, temporary)
                    if _hash(temporary) != digest:
                        raise GameSetupError("复制资源校验失败")
                    if self._running(root, game_id):
                        raise GameSetupError("游戏已开始运行，部署已停止")
                    os.replace(temporary, target)
                    temporary = None
                    changed.append((target, original, digest))
                    if _hash(target) != digest:
                        raise GameSetupError("部署后的文件校验失败")
                # Verify the whole resulting integration; no success on partial/invalid installation.
                final, remaining = self._inspect(game_id)
                if final["state"] not in ("ready", "running") or remaining:
                    raise GameSetupError("已复制文件但完整接入校验未通过，请检查前置组件")
                final.update(files_changed=[str(target.relative_to(root)) for target, _, _ in changed],
                             backup_dir=str(backup))
                self._states[game_id] = final
            except (OSError, ValueError, TypeError) as exc:
                rollback_failed = False
                for target, original, digest in reversed(changed):
                    try:
                        _check_path(target, root)
                        if _hash(target) != digest or self._running(root, game_id):
                            rollback_failed = True
                            continue
                        if original is None:
                            target.unlink()
                        else:
                            shutil.copyfile(original, target)
                            if _hash(target) != _hash(original):
                                rollback_failed = True
                    except (OSError, ValueError):
                        rollback_failed = True
                state.update(state="error", ready=False,
                             detail=(str(exc) if isinstance(exc, GameSetupError) else
                                     "游戏接入安装失败，请检查目录写入权限/磁盘空间"),
                             files_changed=[str(target.relative_to(root)) for target, _, _ in changed],
                             backup_dir=str(backup) if backup.exists() else None,
                             rollback_failed=rollback_failed)
                if rollback_failed:
                    state["detail"] += "；部分文件无法回退，请退出游戏后使用备份恢复"
                self._states[game_id] = state
            finally:
                if temporary is not None:
                    temporary.unlink(missing_ok=True)
            return copy.deepcopy(self._states[game_id])
