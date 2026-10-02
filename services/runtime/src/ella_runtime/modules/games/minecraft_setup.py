"""Add an Ella Fabric profile to an existing official 26.2 installation."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import uuid
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any

from ella_runtime.modules.games.setup import GameSetupError


class MinecraftSetupError(GameSetupError):
    pass


def _safe_file(root: Path, relative: str) -> Path:
    if not isinstance(relative, str) or "\\" in relative:
        raise MinecraftSetupError("Fabric 资源路径无效")
    parts = PurePosixPath(relative)
    if parts.is_absolute() or PureWindowsPath(relative).drive or not parts.parts or any(
        part in {"", ".", ".."} or ":" in part or part.endswith((".", " "))
        or re.match(r"^(CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])(?:\.|$)", part, re.IGNORECASE)
        for part in relative.split("/")
    ):
        raise MinecraftSetupError("Fabric 资源路径无效")
    root = root.absolute()
    target = root.joinpath(*parts.parts)
    for parent in [target, *target.parents]:
        if parent.is_symlink() or (hasattr(parent, "is_junction") and parent.is_junction()):
            raise MinecraftSetupError("Minecraft 目录包含链接，请选择实际目录")
    if not target.resolve().is_relative_to(root.resolve()):
        raise MinecraftSetupError("Fabric 资源路径越界")
    return target


def _digest(path: Path) -> str:
    with path.open("rb") as handle:
        return hashlib.file_digest(handle, "sha256").hexdigest()


def install_fabric_profile(
    bundle_root: Path,
    data_dir: Path,
    status: dict[str, Any],
) -> str | None:
    """No game assets/downloads, login changes, existing profile edits, or selected-profile change."""
    if not (
        status.get("profile_version") == "26.2"
        and status.get("base_installed") is True
        and status.get("launcher_root")
        and status.get("game_root")
        and status.get("state") not in {"running", "needs_selection", "not_found", "error"}
    ):
        return None
    manifest = json.loads((bundle_root / "bundle.json").read_text(encoding="utf-8"))
    entry = next((game for game in manifest.get("games", []) if game.get("id") == "minecraft"), {})
    files = entry.get("profile_resources", [])
    version = entry.get("fabric_profile_id", "")
    if not files:
        return None
    if not re.fullmatch(r"fabric-loader-\d+\.\d+\.\d+-26\.2", version):
        raise MinecraftSetupError("Fabric 启动配置版本不受支持")
    launcher_root = Path(status["launcher_root"]).absolute()
    game_root = Path(status["game_root"]).absolute()
    profiles_path = _safe_file(launcher_root, "launcher_profiles.json")
    if not profiles_path.is_file():
        raise MinecraftSetupError("官方启动器配置不存在，请先打开一次 Minecraft 启动器")
    if profiles_path.stat().st_size > 2 * 1024 * 1024:
        raise MinecraftSetupError("Minecraft 启动器配置过大，请手动检查")
    original = profiles_path.read_bytes()
    document = json.loads(original)
    if not isinstance(document, dict) or not isinstance(document.get("profiles"), dict):
        raise MinecraftSetupError("Minecraft 启动器配置格式无效")
    # A stable per-instance ID avoids collisions across gameDir profiles.
    suffix = hashlib.sha256(str(game_root.resolve()).casefold().encode()).hexdigest()[:12]
    profile_id = f"ella-fabric-26.2-{suffix}"
    profile = {
        "name": "艾拉 · Fabric 26.2", "type": "custom", "lastVersionId": version,
        "gameDir": str(game_root), "ellaManaged": True,
    }
    source_profile = document["profiles"].get(status.get("profile"))
    if isinstance(source_profile, dict) and isinstance(source_profile.get("javaDir"), str):
        profile["javaDir"] = source_profile["javaDir"]
    prior = document["profiles"].get(profile_id)
    if prior is not None and not (
        isinstance(prior, dict) and prior.get("ellaManaged") is True
        and prior.get("lastVersionId") == version
        and Path(prior.get("gameDir", "")).resolve() == game_root.resolve()
    ):
        raise MinecraftSetupError("艾拉 Fabric 配置名称已被其它配置使用，请手动处理冲突")
    copies: list[tuple[Path, Path]] = []
    destinations: set[Path] = set()
    for item in files:
        relative = item.get("destination", "")
        allowed = relative == f"versions/{version}/{version}.json" or (
            relative.startswith("libraries/") and relative.endswith(".jar")
        )
        if not allowed:
            raise MinecraftSetupError("Fabric 资源目标不在允许目录内")
        source = _safe_file(bundle_root, item.get("source", ""))
        destination = _safe_file(launcher_root, relative)
        expected = item.get("sha256", "")
        if not re.fullmatch(r"[a-f0-9]{64}", expected) or not source.is_file() or _digest(source) != expected:
            raise MinecraftSetupError("Fabric 安装资源校验失败，请重新安装艾拉")
        if destination in destinations:
            raise MinecraftSetupError("Fabric 安装资源存在重复目标")
        destinations.add(destination)
        if destination.exists():
            if not destination.is_file() or _digest(destination) != expected:
                raise MinecraftSetupError("已有 Fabric 库与安装包不同，请手动确认后再接入")
        else:
            copies.append((source, destination))
    metadata_path = _safe_file(bundle_root, next(
        item["source"] for item in files if item["destination"] == f"versions/{version}/{version}.json"
    ))
    metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    if metadata.get("id") != version or metadata.get("inheritsFrom") != "26.2":
        raise MinecraftSetupError("Fabric 启动配置与已安装 Minecraft 不匹配")
    if not copies and prior is not None:
        return profile_id
    backup = data_dir / "game-setup/backups/minecraft-profile" / (
        datetime.now(UTC).strftime("%Y%m%dT%H%M%S") + "-" + uuid.uuid4().hex[:8]
    )
    _safe_file(data_dir, str(backup.relative_to(data_dir)).replace("\\", "/") + "/receipt.json")
    backup.mkdir(parents=True, exist_ok=False)
    (backup / "launcher_profiles.json").write_bytes(original)
    document["profiles"][profile_id] = prior or profile
    updated = json.dumps(document, ensure_ascii=False, indent=2).encode("utf-8")
    created: list[tuple[Path, str]] = []
    temporary: list[Path] = []
    replaced_profiles = False
    try:
        for source, destination in copies:
            destination.parent.mkdir(parents=True, exist_ok=True)
            staging = destination.with_name(destination.name + ".ella-" + uuid.uuid4().hex + ".tmp")
            temporary.append(staging)
            shutil.copyfile(source, staging)
            if _digest(staging) != _digest(source):
                raise MinecraftSetupError("Fabric 文件复制校验失败")
            if destination.exists():
                raise MinecraftSetupError("Minecraft 目录被其它程序修改，请关闭启动器后重试")
            # Hard-link publication is exclusive and cannot overwrite a concurrent writer.
            os.link(staging, destination)
            staging.unlink()
            created.append((destination, _digest(source)))
        if profiles_path.read_bytes() != original:
            raise MinecraftSetupError("Minecraft 启动器配置正在变化，请关闭启动器后重试")
        staging = profiles_path.with_name(profiles_path.name + ".ella-" + uuid.uuid4().hex + ".tmp")
        temporary.append(staging)
        staging.write_bytes(updated)
        staging.replace(profiles_path)
        replaced_profiles = True
        (backup / "receipt.json").write_text(json.dumps({
            "profile_id": profile_id, "launcher_root": str(launcher_root),
            "game_root": str(game_root), "created": [str(path) for path, _ in created],
            "profile_sha256": hashlib.sha256(updated).hexdigest(),
        }, ensure_ascii=False), encoding="utf-8")
    except Exception:
        if replaced_profiles and profiles_path.read_bytes() == updated:
            profiles_path.write_bytes(original)
        for path, digest in reversed(created):
            if path.is_file() and _digest(path) == digest:
                path.unlink(missing_ok=True)
        raise
    finally:
        for path in temporary:
            path.unlink(missing_ok=True)
    return profile_id
