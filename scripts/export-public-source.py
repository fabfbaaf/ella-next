"""Export a clean source snapshot without local data or unverified original assets."""

import argparse
import hashlib
import importlib.util
import json
import shutil
import subprocess
import zipfile
from pathlib import Path

BLOCKED_PREFIXES = (
    "artifacts/", "apps/desktop/public/models/live2d/", "apps/desktop/src-tauri/icons/",
    "apps/desktop/src-tauri/runtime/", "apps/desktop/src-tauri/game-setup/",
    "apps/desktop/src-tauri/artifacts/",
)
BLOCKED_FILES = {"apps/desktop/public/isla-pet.png", "apps/desktop/src-tauri/tauri.bundle.conf.json"}
BLOCKED_SUFFIXES = {".exe", ".dll", ".jar", ".zip", ".7z", ".sqlite", ".sqlite3", ".db", ".log", ".pem", ".key", ".moc3"}


def export(destination: Path) -> dict:
    root = Path(__file__).resolve().parents[1]
    destination = destination.resolve()
    if destination == root or destination.is_relative_to(root / ".git"):
        raise ValueError("The export must not overwrite the source repository or its Git directory")
    if destination.exists():
        raise ValueError("Choose a new export directory; existing files are never overwritten")
    listing = subprocess.run(
        ["git", "-c", f"safe.directory={root.as_posix()}", "ls-files", "--cached", "--others", "--exclude-standard", "-z"],
        cwd=root, check=True, capture_output=True,
    )
    paths = sorted(set(listing.stdout.decode("utf-8").strip("\0").split("\0")))
    selected = []
    for relative in paths:
        source = root / relative
        if (
            not relative or relative in BLOCKED_FILES or relative.startswith(BLOCKED_PREFIXES)
            or source.suffix.lower() in BLOCKED_SUFFIXES
            or source.name.startswith(".env") and source.name != ".env.example"
            or "session" == source.suffix.lstrip(".") or ".sqlite" in source.name
        ):
            continue
        if source.is_symlink() or not source.resolve().is_relative_to(root):
            raise ValueError(f"Source file escapes the project: {relative}")
        if source.is_file():
            selected.append((relative, source))
    if not selected or not any(name == "LICENSE" for name, _ in selected):
        raise ValueError("A source export requires a reviewed project and LICENSE")
    destination.mkdir(parents=True)
    for relative, source in selected:
        target = destination / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)
    spec = importlib.util.spec_from_file_location("ella_release_icons", root / "scripts/generate-release-icons.py")
    generator = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(generator)
    generator.generate(destination / "apps/desktop/src-tauri/icons")
    config_path = destination / "apps/desktop/src-tauri/tauri.conf.json"
    config = json.loads(config_path.read_text(encoding="utf-8"))
    config["bundle"]["icon"] = ["icons/32x32.png", "icons/128x128.png", "icons/icon.ico"]
    config_path.write_text(json.dumps(config, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    manifest = {
        path.relative_to(destination).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(destination.rglob("*")) if path.is_file()
    }
    manifest_path = destination.parent / (destination.name + "-manifest.json")
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    archive_path = destination.parent / (destination.name + ".zip")
    with zipfile.ZipFile(archive_path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for relative in manifest:
            archive.write(destination / relative, destination.name + "/" + relative)
    return {
        "directory": str(destination), "files": len(manifest), "archive": str(archive_path),
        "sha256": hashlib.sha256(archive_path.read_bytes()).hexdigest(),
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    print(json.dumps(export(parser.parse_args().output), ensure_ascii=False, indent=2))
