"""Copy user-obtained official Live2D assets into the ignored local asset directory."""

import argparse
import json
import shutil
from pathlib import Path


def import_assets(core: Path, model: Path) -> None:
    root = Path(__file__).resolve().parents[1]
    destination = root / "apps/desktop/public/models/live2d"
    if not core.is_file() or core.name != "live2dcubismcore.min.js":
        raise ValueError("--core must point to the SDK's unmodified live2dcubismcore.min.js")
    if "Live2D Cubism Core" not in core.read_text(encoding="utf-8")[:2000]:
        raise ValueError("The Core file does not contain the expected original header")
    metadata = model / "Hiyori.model3.json"
    if not metadata.is_file():
        raise ValueError("--model must be the extracted directory containing Hiyori.model3.json")
    payload = json.loads(metadata.read_text(encoding="utf-8"))
    if not isinstance(payload, dict) or not isinstance(payload.get("FileReferences"), dict):
        raise TypeError("The model metadata is invalid")
    files = sorted(path for path in model.rglob("*") if path.is_file())
    allowed = {".json", ".png", ".moc3", ".txt", ".md"}
    for source in files:
        if source.is_symlink() or not source.resolve().is_relative_to(model.resolve()):
            raise ValueError("Model paths may not escape the selected directory")
        if source.suffix.lower() not in allowed:
            continue
        target = destination / "Hiyori" / source.relative_to(model)
        if not target.resolve().is_relative_to(destination.resolve()):
            raise ValueError("Model destination must stay within local Live2D assets")
    destination.mkdir(parents=True, exist_ok=True)
    shutil.copy2(core, destination / core.name)
    for source in files:
        if source.suffix.lower() in allowed:
            target = destination / "Hiyori" / source.relative_to(model)
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, target)
    print("Imported local assets. Restart the desktop; keep these assets out of public commits.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--core", type=Path, required=True)
    parser.add_argument("--model", type=Path, required=True)
    args = parser.parse_args()
    import_assets(args.core.resolve(), args.model.resolve())
