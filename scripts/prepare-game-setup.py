"""Prepare pinned, verified mod/loader resources without reading game or user data.

Run before desktop packaging. The bundle contains relative destinations and SHA256
checksums; the runtime still checks the installed game version before deployment.
No game binaries, saves, launcher profiles, credentials, or local config are copied.
"""

import argparse
import hashlib
import io
import json
import shutil
import subprocess
import zipfile
from pathlib import Path, PurePosixPath
from urllib.parse import quote, urlsplit

import httpx

SMAPI_VERSION = "4.5.2"
SMAPI_SHA256 = "dd01ddca7b566bfe0d3b3d2d03833496abc56c53da976241f2ab443f5484acc4"
FABRIC_API_VERSION = "0.156.0+26.2"
FABRIC_API_SHA256 = "8de18d9f6a8a2a5b2120ef9e8bffb79cc9b75989c0c022c39c9dfc1bc3a29a99"
FABRIC_LOADER_SHA256 = "93044e4dd46de5d8136701292f05e868da096d2c9fddb4793e4fdbcc63efc695"
PROFILE_ID = "fabric-loader-0.19.5-26.2"
PROFILE_URL = "https://meta.fabricmc.net/v2/versions/loader/26.2/0.19.5/profile/json"
MAVEN = "https://maven.fabricmc.net/"
BANNERLORD = (
    ("Bannerlord.GABS", "BUTR/Bannerlord.GABS", "v1.0.0", "Bannerlord.GABS.7z",
     "e0525fef3e08d26aedaccc5fd5901d1f068552ed54b5ef31c44f6b4d9b18d916", "LICENSE"),
    ("Bannerlord.BLSE", "BUTR/Bannerlord.BLSE", "v1.5.12", "Bannerlord.BLSE.7z",
     "17d1f399ce0b0a951c9f62a9d1c6097e7c9cd8e5504d3c9b10397761f94923fc", "LICENSE"),
    ("Bannerlord.Harmony", "BUTR/Bannerlord.Harmony", "v2.4.2.248", "Bannerlord.Harmony.7z",
     "097b69a8e9dd37252cef1fa3eff326bc69a9532cf0dc9e40784a1a572ea6e85b", "LICENSE"),
    ("Bannerlord.ButterLib", "BUTR/Bannerlord.ButterLib", "v2.12.0", "Bannerlord.ButterLib.7z",
     "78ce51c40918e891585eec04b3141b4cacd2cdd3c573d953773813e5e0d1ba4b", "LICENSE.txt"),
    ("Bannerlord.UIExtenderEx", "BUTR/Bannerlord.UIExtenderEx", "v2.13.3", "Bannerlord.UIExtenderEx.7z",
     "9748fff9d4f6944a62cf00144bab369e563e4cfe00e710364b069343e14cfe2d", "LICENSE"),
    ("Bannerlord.MBOptionScreen", "Aragas/Bannerlord.MBOptionScreen", "v5.12.3", "Bannerlord.MBOptionScreen.7z",
     "4ae69f7d56c99264a2d52cea4fe809cf77e41d332598bcb32e84577e06f29769", "LICENSE.txt"),
)
SMAPI_ROOT_FILES = {
    "StardewModdingAPI.exe", "StardewModdingAPI.dll", "StardewModdingAPI.exe.config",
    "StardewModdingAPI.runtimeconfig.json", "StardewModdingAPI.xml", "steam_appid.txt",
}
ALLOWED_DOWNLOAD_HOSTS = {
    "github.com", "raw.githubusercontent.com", "codeload.github.com", "meta.fabricmc.net",
    "maven.fabricmc.net", "www.gnu.org",
}
ELLA_LICENSE = """MIT License

Copyright (c) 2026 桔梗

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
"""


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def relative(name: str) -> str:
    if "\\" in name or ":" in name or "\0" in name:
        raise ValueError(f"Invalid resource path: {name!r}")
    path = PurePosixPath(name)
    if path.is_absolute() or not path.parts or any(part in {".", ".."} for part in path.parts):
        raise ValueError(f"Invalid resource path: {name!r}")
    return str(path)


class Preparer:
    def __init__(self, root: Path, *, offline: bool = False) -> None:
        self.root = root.resolve()
        self.output = self.root / "artifacts/game-setup"
        self.cache = self.root / "artifacts/game-setup-downloads"
        self.plugins = self.root / "artifacts/game-plugins"
        self.offline = offline
        self.client = httpx.Client(follow_redirects=True, timeout=90)
        self.cache.mkdir(parents=True, exist_ok=True)
        assert self.output.resolve().is_relative_to(self.root / "artifacts")
        if self.output.exists():
            # Only the explicitly generated game-setup directory is replaced.
            shutil.rmtree(self.output)
        self.output.mkdir(parents=True)

    def fetch(self, name: str, url: str, expected: str | None = None, candidates=()) -> bytes:
        cache_path = self.cache / relative(name)
        for candidate in (cache_path, *candidates):
            if candidate.is_file():
                data = candidate.read_bytes()
                if expected is None or digest(data) == expected:
                    if candidate != cache_path:
                        cache_path.parent.mkdir(parents=True, exist_ok=True)
                        cache_path.write_bytes(data)
                    return data
        if self.offline:
            raise RuntimeError(f"Verified resource is not cached: {name}")
        parsed = urlsplit(url)
        if parsed.scheme != "https" or parsed.hostname not in ALLOWED_DOWNLOAD_HOSTS:
            raise ValueError("Downloads must use a known official HTTPS source")
        response = self.client.get(url)
        response.raise_for_status()
        data = response.content
        if expected and digest(data) != expected:
            raise RuntimeError(f"Official resource checksum mismatch: {name}")
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        cache_path.write_bytes(data)
        return data

    def write(self, source: str, data: bytes, *, destination: str | None = None,
              kind: str = "mod", url: str | None = None) -> dict:
        source = relative(source)
        path = self.output / source
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        result = {"source": source, "sha256": digest(data), "size": len(data)}
        if destination is not None:
            result.update(destination=relative(destination), kind=kind)
        if url:
            result["url"] = url
        return result

    def license(self, title: str, url: str, name: str) -> dict:
        data = self.fetch(f"licenses/{name}", url)
        return {**self.write(f"licenses/{name}", data), "title": title, "url": url}

    def stardew(self, smapi_archive: Path | None = None) -> dict:
        url = f"https://github.com/Pathoschild/SMAPI/releases/download/{SMAPI_VERSION}/SMAPI-{SMAPI_VERSION}-installer.zip"
        data = self.fetch(f"SMAPI-{SMAPI_VERSION}-installer.zip", url, SMAPI_SHA256,
                          candidates=(smapi_archive,) if smapi_archive else ())
        outer = zipfile.ZipFile(io.BytesIO(data))
        install_name = f"SMAPI {SMAPI_VERSION} installer/internal/windows/install.dat"
        loader = zipfile.ZipFile(io.BytesIO(outer.read(install_name)))
        files = []
        for item in loader.infolist():
            if item.is_dir():
                continue
            target = relative(item.filename)
            if target not in SMAPI_ROOT_FILES and not target.startswith((
                "smapi-internal/", "Mods/ConsoleCommands/", "Mods/SaveBackup/",
            )):
                raise RuntimeError(f"Unexpected SMAPI Windows payload: {target}")
            if target.endswith(("config.user.json", "crash.marker", "update.marker")):
                raise RuntimeError("SMAPI payload contains a user-specific file")
            files.append(self.write(f"stardew/loader/{target}", loader.read(item),
                                    destination=target, kind="loader", url=url))
        assert SMAPI_ROOT_FILES.issubset({item["destination"] for item in files})
        for name in ("Ella.StardewBridge.dll", "manifest.json"):
            data = (self.plugins / "Ella.StardewBridge" / name).read_bytes()
            files.append(self.write(f"stardew/mod/{name}", data,
                                    destination=f"Mods/Ella.StardewBridge/{name}"))
        manifest = json.loads((self.plugins / "Ella.StardewBridge/manifest.json").read_text())
        assert manifest["MinimumApiVersion"] == "4.5.0"
        licenses = [self.license("SMAPI LGPL-3.0", "https://raw.githubusercontent.com/Pathoschild/SMAPI/4.5.2/LICENSE.txt", "SMAPI-LGPL-3.0.txt"),
                    self.license("GNU GPL v3", "https://www.gnu.org/licenses/gpl-3.0.txt", "GPL-3.0.txt")]
        source_url = "https://codeload.github.com/Pathoschild/SMAPI/zip/refs/tags/4.5.2"
        source = self.fetch("SMAPI-4.5.2-source.zip", source_url)
        licenses.append({**self.write("licenses/sources/SMAPI-4.5.2-source.zip", source),
                         "title": "Unmodified SMAPI 4.5.2 corresponding source", "url": source_url})
        readme = outer.read(f"SMAPI {SMAPI_VERSION} installer/README.txt")
        self.write("licenses/SMAPI-install-README.txt", readme)
        return {"id": "stardew", "support": "supported", "version_constraints": {
            "game_min": "1.6.14", "game_max_exclusive": "1.7.0", "loader_min": "4.5.0",
            "loader_bundled": SMAPI_VERSION,
        }, "files": files, "licenses": licenses, "loader_actions": [{
            "action": "copy_game_file", "source": "Stardew Valley.deps.json",
            "destination": "StardewModdingAPI.deps.json",
        }], "launch_executable": "StardewModdingAPI.exe", "notes": [
            "The loader payload is the official Windows install.dat; steam_appid.txt is its unmodified default.",
            "Copy the local game's deps.json as documented by SMAPI; do not distribute game files.",
            "Launch SMAPI directly; Steam global launch options and save files are not modified.",
        ]}

    def minecraft(self) -> dict:
        bridge = (self.plugins / "ella-minecraft-bridge-0.1.0.jar").read_bytes()
        with zipfile.ZipFile(io.BytesIO(bridge)) as archive:
            mod = json.loads(archive.read("fabric.mod.json"))
        assert mod["depends"]["minecraft"] == "~26.2"
        api_name = f"fabric-api-{FABRIC_API_VERSION}.jar"
        api_url = f"{MAVEN}net/fabricmc/fabric-api/fabric-api/{FABRIC_API_VERSION}/{api_name}"
        fabric_api = self.fetch(api_name, api_url, FABRIC_API_SHA256)
        files = [self.write("minecraft/mods/ella-minecraft-bridge-0.1.0.jar", bridge,
                            destination="mods/ella-minecraft-bridge-0.1.0.jar"),
                 self.write(f"minecraft/mods/{api_name}", fabric_api,
                            destination=f"mods/{api_name}", url=api_url)]
        profile = json.loads(self.fetch("fabric-profile-26.2-0.19.5.json", PROFILE_URL))
        assert profile["id"] == PROFILE_ID and profile["inheritsFrom"] == "26.2"
        assert profile["mainClass"] == "net.fabricmc.loader.impl.launch.knot.KnotClient"
        profile_json = json.dumps(profile, ensure_ascii=False, indent=2).encode()
        version_path = f"versions/{PROFILE_ID}/{PROFILE_ID}.json"
        profile_resources = [self.write(f"minecraft/fabric-profile/{version_path}", profile_json,
                                        destination=version_path, kind="loader", url=PROFILE_URL)]
        for library in profile["libraries"]:
            group, artifact, version = library["name"].split(":")
            artifact_path = f"{group.replace('.', '/')}/{artifact}/{version}/{artifact}-{version}.jar"
            expected = library.get("sha256")
            if library["name"] == "net.fabricmc:fabric-loader:0.19.5":
                expected = FABRIC_LOADER_SHA256
            if not expected or library["url"] != MAVEN:
                raise RuntimeError("Unverified Fabric profile library")
            url = MAVEN + quote(artifact_path, safe="/+.:-")
            data = self.fetch(f"fabric-libraries/{artifact_path}", url, expected)
            destination = f"libraries/{artifact_path}"
            profile_resources.append(self.write(f"minecraft/fabric-profile/{destination}", data,
                                                destination=destination, kind="library", url=url))
        licenses = [self.license("Fabric API Apache-2.0", "https://raw.githubusercontent.com/FabricMC/fabric-api/0.156.0%2B26.2/LICENSE", "Fabric-API-LICENSE.txt"),
                    self.license("Fabric Loader Apache-2.0", "https://raw.githubusercontent.com/FabricMC/fabric-loader/0.19.5/LICENSE", "Fabric-Loader-LICENSE.txt")]
        # Keep license and notice entries actually shipped inside each library jar.
        for entry in profile_resources:
            if entry.get("kind") != "library":
                continue
            data = (self.output / entry["source"]).read_bytes()
            with zipfile.ZipFile(io.BytesIO(data)) as jar:
                for name in jar.namelist():
                    if not name.endswith("/") and any(word in PurePosixPath(name).name.upper() for word in ("LICENSE", "NOTICE")):
                        safe_name = f"{PurePosixPath(entry['destination']).stem}-{PurePosixPath(name).name}"
                        record = self.write(f"licenses/fabric-libraries/{safe_name}", jar.read(name))
                        licenses.append({**record, "title": safe_name, "url": entry["url"]})
        return {"id": "minecraft", "support": "supported", "version_constraints": {
            "game_exact": "26.2", "loader_min": "0.19.5", "java_min": 25,
            "required_mods": ["fabric-api"],
        }, "files": files, "licenses": licenses, "profile_resources": profile_resources,
            "fabric_profile_id": PROFILE_ID,
            "profile": {"id": PROFILE_ID, "inherits_from": "26.2", "loader_version": "0.19.5",
                        "source_url": PROFILE_URL}, "notes": [
            "Game files, authentication and Java runtime remain supplied by the existing official launcher.",
            "profile_resources are for a separately validated launcher root, not the game instance root.",
            "An Ella Fabric profile must not replace the user's selected/default launcher profile.",
        ]}

    def bannerlord(self) -> dict:
        files, licenses = [], []
        old_names = {"Bannerlord.BLSE": "BLSE.7z", "Bannerlord.ButterLib": "ButterLib.7z",
                     "Bannerlord.Harmony": "Harmony.7z", "Bannerlord.UIExtenderEx": "UIExtenderEx.7z",
                     "Bannerlord.MBOptionScreen": "MCM.7z", "Bannerlord.GABS": "Bannerlord.GABS.7z"}
        versions = {}
        for component, repo, tag, asset, expected, license_name in BANNERLORD:
            url = f"https://github.com/{repo}/releases/download/{tag}/{asset}"
            candidate = self.plugins / "bannerlord-cache-v1.2.12" / old_names[component]
            self.fetch(f"{component}-{tag}.7z", url, expected, candidates=(candidate,))
            archive = self.cache / f"{component}-{tag}.7z"
            listing = subprocess.run(["tar", "-tf", str(archive)], capture_output=True, check=True)
            names = listing.stdout.decode("utf-8").splitlines()
            for original in names:
                target = relative(original.rstrip("/"))
                if original.endswith("/") or target.endswith(".pdb") or "/Gaming.Desktop.x64_Shipping_Client/" in target:
                    continue
                if component == "Bannerlord.BLSE":
                    if not target.startswith("bin/Win64_Shipping_Client/Bannerlord.BLSE."):
                        continue
                    kind = "loader"
                else:
                    if not target.startswith(f"Modules/{component}/"):
                        raise RuntimeError(f"Unexpected module payload: {target}")
                    kind = "mod"
                content = subprocess.run(["tar", "-xOf", str(archive), original],
                                         capture_output=True, check=True).stdout
                files.append(self.write(f"bannerlord/{target}", content,
                                        destination=target, kind=kind, url=url))
            license_url = f"https://raw.githubusercontent.com/{repo}/{tag}/{license_name}"
            licenses.append(self.license(component, license_url, f"{component}-LICENSE.txt"))
            versions[component] = tag.lstrip("v")
        destinations = {item["destination"] for item in files}
        assert "Modules/Bannerlord.GABS/bin/Win64_Shipping_Client/Bannerlord.GABS.v1.3.15.dll" in destinations
        assert "Modules/Bannerlord.ButterLib/bin/Win64_Shipping_Client/Bannerlord.ButterLib.Implementation.1.3.15.dll" in destinations
        assert "bin/Win64_Shipping_Client/Bannerlord.BLSE.Standalone.exe" in destinations
        return {"id": "bannerlord", "support": "supported", "version_constraints": {
            "game_exact": "1.3.15", "loader_bundled": "1.5.12", "modules": versions,
        }, "files": files, "licenses": licenses, "launch_executable": "bin/Win64_Shipping_Client/Bannerlord.BLSE.Standalone.exe", "notes": [
            "Only native game version 1.3.15 is supported by this curated bundle; all others are unsupported.",
            "Official GABS and ButterLib payloads include version-specific 1.3.15 implementations.",
            "The old cache folder name is not used as a compatibility guarantee; every archive is publisher-hash checked.",
            "TaleWorlds executables/configuration and GABS machine configuration are not included.",
        ]}

    def prepare(self, smapi_archive: Path | None, *, tauri_resources: bool) -> dict:
        try:
            games = [self.stardew(smapi_archive), self.minecraft(), self.bannerlord()]
            ella_license = self.write("licenses/Ella-Bridges-MIT.txt", ELLA_LICENSE.encode())
            for game in games:
                game["licenses"].append({**ella_license, "title": "Ella bridges MIT", "url": ""})
                targets = [item["destination"].casefold() for item in game["files"]]
                if len(targets) != len(set(targets)):
                    raise RuntimeError("Duplicate deployment destinations")
            manifest = {"schema_version": 1, "games": games}
            self.write("bundle.json", json.dumps(manifest, ensure_ascii=False, indent=2).encode())
            if tauri_resources:
                destination = self.root / "apps/desktop/src-tauri/game-setup"
                assert destination.resolve().is_relative_to(self.root / "apps/desktop/src-tauri")
                if destination.exists():
                    shutil.rmtree(destination)
                shutil.copytree(self.output, destination)
            return {"bundle": str(self.output), "games": {
                game["id"]: {"support": game["support"], "files": len(game["files"]),
                             "profile_resources": len(game.get("profile_resources", []))}
                for game in games
            }, "bytes": sum(path.stat().st_size for path in self.output.rglob("*") if path.is_file())}
        finally:
            self.client.close()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--offline", action="store_true", help="Require previously verified cached downloads")
    parser.add_argument("--smapi-archive", type=Path, help="Optional official SMAPI 4.5.2 installer ZIP to seed the cache")
    parser.add_argument("--tauri-resources", action="store_true", help="Also stage the generated bundle into Tauri resources")
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    assert (root / "services/runtime/src/ella_runtime/api.py").is_file()
    result = Preparer(root, offline=args.offline).prepare(args.smapi_archive, tauri_resources=args.tauri_resources)
    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()
