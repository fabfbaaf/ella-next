"""Deployment tests use fake games and synthetic payloads in temporary directories only."""

import asyncio
import hashlib
import io
import json
import os
import shutil
import threading
import zipfile
from functools import lru_cache
from pathlib import Path

import pytest
from fastapi import HTTPException

from ella_runtime import api
from ella_runtime.modules.games.adapters.bannerlord.gabs_client import (
    GabsError,
    find_gabs_config_dir,
)
from ella_runtime.modules.games.minecraft_setup import install_fabric_profile
from ella_runtime.modules.games.setup import GameSetupError, GameSetupManager
from ella_runtime.modules.games.setup_service import GameSetupService
from ella_runtime.runtime_session import RuntimeSession
from tests.api_client import authorized_client


def write(path, content):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    return path


def jar(mod_id, version):
    content = io.BytesIO()
    with zipfile.ZipFile(content, 'w') as archive:
        archive.writestr('fabric.mod.json', json.dumps({'id': mod_id, 'version': version}))
    return content.getvalue()


@pytest.fixture
def resource_bundle():
    """Opt in to separately prepared game payloads; ordinary clones need no binaries."""
    if os.getenv('ELLA_RESOURCE_INTEGRATION') != '1':
        pytest.skip('Set ELLA_RESOURCE_INTEGRATION=1 to test prepared game resources')
    bundle = Path(__file__).resolve().parents[3] / 'artifacts/game-setup'
    if not (bundle / 'bundle.json').is_file():
        pytest.skip('Optional game resources are missing: prepare artifacts/game-setup first')
    return bundle


@pytest.fixture
def setup(tmp_path, monkeypatch):
    for key in list(os.environ):
        if key.startswith(('ELLA_GAME_', 'ELLA_GABS_')):
            monkeypatch.delenv(key, raising=False)
    bundle = tmp_path / 'bundle'
    library = tmp_path / 'steam'
    data = tmp_path / 'data'
    monkeypatch.setenv('ELLA_DATA_DIR', str(data))
    rows = []
    for name, body, kind in (
        ('StardewModdingAPI.exe', b'loader-exe', 'loader'),
        ('StardewModdingAPI.dll', b'loader-dll', 'loader'),
        ('smapi-internal/loader.dll', b'loader-library', 'loader'),
        ('Mods/Ella.StardewBridge/Ella.StardewBridge.dll', b'ella-dll', 'mod'),
    ):
        source = write(bundle / 'stardew' / name, body)
        rows.append({'source': str(source.relative_to(bundle)).replace('\\', '/'),
                     'destination': name, 'kind': kind, 'sha256': hashlib.sha256(body).hexdigest()})
    manifest = {'schema_version': 1, 'games': [
        {'id': 'stardew', 'support': 'supported', 'version_constraints': {
            'game_min': '1.6.14', 'game_max_exclusive': '1.7.0', 'loader_min': '4.5.0',
        }, 'files': rows, 'loader_actions': [{'action': 'copy_game_file',
            'source': 'Stardew Valley.deps.json', 'destination': 'StardewModdingAPI.deps.json'}]},
        {'id': 'minecraft', 'support': 'supported', 'version_constraints': {'game_exact': '26.2'},
         'files': [], 'fabric_profile_id': 'fabric-loader-0.19.5-26.2'},
        {'id': 'bannerlord', 'support': 'supported', 'version_constraints': {'game_exact': '1.3.15'},
         'files': []},
    ]}
    write(bundle / 'bundle.json', json.dumps(manifest).encode())

    def version(path):
        if not path.is_file():
            return None
        return '4.5.2' if 'StardewModdingAPI' in path.name else '1.6.15'

    manager = GameSetupManager(bundle, data, libraries=[library], minecraft_roots=[],
                               process_checker=lambda _: False, version_reader=version, java_major=25)
    return {'root': tmp_path, 'bundle': bundle, 'library': library, 'data': data,
            'manifest': manifest, 'manager': manager}


def save_manifest(setup):
    (setup['bundle'] / 'bundle.json').write_text(json.dumps(setup['manifest']), encoding='utf-8')


def stardew(setup, library=None):
    root = (library or setup['library']) / 'steamapps/common/Stardew Valley'
    write(root / 'Stardew Valley.exe', b'game exe')
    write(root / 'Stardew Valley.dll', b'game dll')
    write(root / 'Stardew Valley.deps.json', b'{"game":"dependencies"}')
    return root


def minecraft(setup, *, fabric=False):
    root = setup['root'] / 'minecraft'
    profile_id = 'fabric' if fabric else 'vanilla'
    version_id = 'fabric-loader-0.19.5-26.2' if fabric else '26.2'
    profile = {'lastVersionId': version_id, 'name': profile_id}
    write(root / 'launcher_profiles.json', json.dumps({
        'profiles': {profile_id: profile}, 'selectedProfile': profile_id, 'unrelated': 'preserve',
    }).encode())
    write(root / 'versions/26.2/26.2.jar', b'fake game jar')
    write(root / 'versions/26.2/26.2.json', json.dumps({
        'id': '26.2', 'javaVersion': {'majorVersion': 25},
    }).encode())
    entry = setup['manifest']['games'][1]
    for mod_id, version, name in (
        ('ella-minecraft-bridge', '0.1.0', 'ella-minecraft-bridge-0.1.0.jar'),
        ('fabric-api', '0.156.0+26.2', 'fabric-api-0.156.0+26.2.jar'),
    ):
        body = jar(mod_id, version)
        write(setup['bundle'] / 'minecraft' / name, body)
        entry['files'].append({'source': f'minecraft/{name}', 'destination': f'mods/{name}',
                               'kind': 'mod', 'sha256': hashlib.sha256(body).hexdigest()})
    metadata = {'id': 'fabric-loader-0.19.5-26.2', 'inheritsFrom': '26.2',
                'mainClass': 'net.fabricmc.loader.impl.launch.knot.KnotClient',
                'libraries': [{'name': 'net.fabricmc:fabric-loader:0.19.5'}]}
    body = json.dumps(metadata).encode()
    destination = f'versions/{version_id}/{version_id}.json' if fabric else (
        'versions/fabric-loader-0.19.5-26.2/fabric-loader-0.19.5-26.2.json'
    )
    write(setup['bundle'] / 'minecraft/profile.json', body)
    entry['profile_resources'] = [{'source': 'minecraft/profile.json', 'destination': destination,
                                  'kind': 'loader', 'sha256': hashlib.sha256(body).hexdigest()}]
    if fabric:
        write(root / destination, body)
    save_manifest(setup)
    setup['manager'].minecraft_roots = [root]
    return root


def test_detect_fresh_game_without_loader_and_get_is_read_only(setup):
    root = stardew(setup)
    before = {str(path): path.read_bytes() for path in root.rglob('*') if path.is_file()}
    states = setup['manager'].scan()
    assert states['stardew_valley']['detected']
    assert states['stardew_valley']['game_root'] == str(root)
    assert not states['stardew_valley']['ready']
    assert before == {str(path): path.read_bytes() for path in root.rglob('*') if path.is_file()}
    assert not setup['data'].exists()


def test_deploy_fresh_loader_and_mod_is_idempotent_preserves_game_and_other_mods(setup):
    root = stardew(setup)
    unrelated = write(root / 'Mods/OtherMod/config.json', b'custom settings')
    state = asyncio.run(setup['manager'].ensure('stardew_valley'))
    assert state['ready'] and state['files_changed']
    assert (root / 'StardewModdingAPI.deps.json').read_bytes() == (root / 'Stardew Valley.deps.json').read_bytes()
    assert (root / 'Stardew Valley.dll').read_bytes() == b'game dll'
    assert unrelated.read_bytes() == b'custom settings'
    assert setup['manager'].launch_path('stardew_valley') == root / 'StardewModdingAPI.exe'
    backup_count = len(list((setup['data'] / 'game-setup/backups').rglob('receipt.json')))
    repeat = asyncio.run(setup['manager'].ensure('stardew_valley'))
    assert repeat['ready'] and not repeat['files_changed']
    assert len(list((setup['data'] / 'game-setup/backups').rglob('receipt.json'))) == backup_count


def test_existing_compatible_loader_preserved_and_plugin_backed_up(setup):
    root = stardew(setup)
    write(root / 'StardewModdingAPI.exe', b'custom compatible loader')
    write(root / 'StardewModdingAPI.dll', b'custom compatible library')
    plugin = write(root / 'Mods/Ella.StardewBridge/Ella.StardewBridge.dll', b'old ella')
    state = asyncio.run(setup['manager'].ensure('stardew_valley'))
    assert state['ready'] and plugin.read_bytes() == b'ella-dll'
    assert (root / 'StardewModdingAPI.exe').read_bytes() == b'custom compatible loader'
    assert (Path(state['backup_dir']) / 'Mods/Ella.StardewBridge/Ella.StardewBridge.dll').read_bytes() == b'old ella'


def test_ambiguous_games_require_selection_then_persist_without_deploying(setup):
    first = stardew(setup)
    second_library = setup['root'] / 'other-steam'
    second = stardew(setup, second_library)
    setup['manager'].libraries.append(second_library)
    assert setup['manager'].scan()['stardew_valley']['state'] == 'needs_selection'
    assert asyncio.run(setup['manager'].ensure('stardew_valley'))['state'] == 'needs_selection'
    setup['manager'].select_location('stardew_valley', second)
    assert not (second / 'StardewModdingAPI.exe').exists()
    assert asyncio.run(setup['manager'].ensure('stardew_valley'))['ready']
    assert not (first / 'StardewModdingAPI.exe').exists()
    new = GameSetupManager(setup['bundle'], setup['data'], libraries=setup['manager'].libraries,
                          minecraft_roots=[], process_checker=lambda _: False,
                          version_reader=setup['manager'].version_reader)
    assert new.scan()['stardew_valley']['game_root'] == str(second)


def test_running_game_waits_then_retry_installs(setup):
    root = stardew(setup)
    setup['manager'].process_checker = lambda _: True
    assert asyncio.run(setup['manager'].ensure('stardew_valley'))['state'] == 'running'
    assert not (root / 'StardewModdingAPI.exe').exists()
    setup['manager'].process_checker = lambda _: False
    assert asyncio.run(setup['manager'].ensure('stardew_valley'))['ready']


@pytest.mark.parametrize('version', [None, '1.5.6', '1.7.0'])
def test_unknown_or_incompatible_version_never_deploys(setup, version):
    root = stardew(setup)
    setup['manager'].version_reader = lambda _: version
    assert asyncio.run(setup['manager'].ensure('stardew_valley'))['state'] == 'unsupported'
    assert not (root / 'StardewModdingAPI.exe').exists()


@pytest.mark.parametrize('target', ['../outside.dll', 'C:/outside.dll', 'Mods/../../outside.dll',
                                    'Stardew Valley.dll', 'Mods/OtherMod/config.json', 'Mods/CON/file.dll'])
def test_manifest_cannot_change_game_or_unrelated_files(setup, target):
    root = stardew(setup)
    setup['manifest']['games'][0]['files'][0]['destination'] = target
    save_manifest(setup)
    assert asyncio.run(setup['manager'].ensure('stardew_valley'))['state'] == 'error'
    assert not (root / 'StardewModdingAPI.exe').exists()
    assert (root / 'Stardew Valley.dll').read_bytes() == b'game dll'


def test_corrupt_bundle_never_copies_partial_files(setup):
    root = stardew(setup)
    (setup['bundle'] / 'stardew/StardewModdingAPI.exe').write_bytes(b'corrupt')
    assert asyncio.run(setup['manager'].ensure('stardew_valley'))['state'] == 'error'
    assert not (root / 'StardewModdingAPI.exe').exists()


def test_copy_failure_rolls_back_and_keeps_original_backup(setup, monkeypatch):
    root = stardew(setup)
    plugin = write(root / 'Mods/Ella.StardewBridge/Ella.StardewBridge.dll', b'old ella')
    real_copy = shutil.copyfile

    def fail(source, destination, *args, **kwargs):
        if Path(source).name == 'StardewModdingAPI.dll':
            raise OSError('simulated full disk')
        return real_copy(source, destination, *args, **kwargs)

    monkeypatch.setattr(shutil, 'copyfile', fail)
    state = asyncio.run(setup['manager'].ensure('stardew_valley'))
    assert state['state'] == 'error' and not state['rollback_failed']
    assert not (root / 'StardewModdingAPI.exe').exists()
    assert plugin.read_bytes() == b'old ella'
    assert (Path(state['backup_dir']) / 'Mods/Ella.StardewBridge/Ella.StardewBridge.dll').read_bytes() == b'old ella'


def test_permission_error_visible_without_success(setup, monkeypatch):
    root = stardew(setup)

    def fail(*_args, **_kwargs):
        raise PermissionError('fixture directory denied')

    monkeypatch.setattr(shutil, 'copyfile', fail)
    state = asyncio.run(setup['manager'].ensure('stardew_valley'))
    assert state['state'] == 'error' and not state['ready']
    assert not (root / 'StardewModdingAPI.exe').exists()


def test_cancel_waits_for_transaction_and_does_not_leave_worker(setup, monkeypatch):
    root = stardew(setup)
    started = threading.Event()
    release = threading.Event()
    real_copy = shutil.copyfile

    def slow(source, destination, *args, **kwargs):
        if Path(source).name == 'StardewModdingAPI.exe':
            started.set()
            assert release.wait(5)
        return real_copy(source, destination, *args, **kwargs)

    monkeypatch.setattr(shutil, 'copyfile', slow)

    async def run():
        task = asyncio.create_task(setup['manager'].ensure('stardew_valley'))
        assert await asyncio.to_thread(started.wait, 5)
        task.cancel()
        await asyncio.sleep(0)
        assert not task.done()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert setup['manager'].status('stardew_valley')['ready']
        assert not list(root.rglob('.ella-*.tmp'))

    asyncio.run(run())


def test_minecraft_vanilla_gets_separate_profile_and_mods(setup):
    root = minecraft(setup)
    original = json.loads((root / 'launcher_profiles.json').read_text())

    async def run():
        service = GameSetupService(setup['manager'])
        service.start()
        await service.aclose()
        assert service.status('minecraft')['ready']
        assert '艾拉' in service.status('minecraft')['launch_detail']

    asyncio.run(run())
    profiles = json.loads((root / 'launcher_profiles.json').read_text())
    assert profiles['profiles']['vanilla'] == original['profiles']['vanilla']
    assert profiles['selectedProfile'] == original['selectedProfile']
    assert profiles['unrelated'] == 'preserve'
    added = [key for key in profiles['profiles'] if key.startswith('ella-fabric-')]
    assert len(added) == 1
    assert (root / 'mods/ella-minecraft-bridge-0.1.0.jar').is_file()
    before = (root / 'launcher_profiles.json').read_bytes()
    asyncio.run(setup['manager'].ensure('minecraft'))
    assert before == (root / 'launcher_profiles.json').read_bytes()


def test_minecraft_running_launcher_does_not_modify_profiles(setup):
    root = minecraft(setup)
    before = (root / 'launcher_profiles.json').read_bytes()
    setup['manager'].process_checker = lambda _: True

    async def run():
        service = GameSetupService(setup['manager'])
        service.start()
        await service.aclose()
        assert not service.status('minecraft')['ready']

    asyncio.run(run())
    assert (root / 'launcher_profiles.json').read_bytes() == before
    assert not (root / 'mods').exists()


def test_minecraft_ambiguous_profiles_require_explicit_profile(setup):
    root = minecraft(setup)
    profiles = json.loads((root / 'launcher_profiles.json').read_text())
    profiles['profiles']['vanilla-two'] = dict(profiles['profiles']['vanilla'])
    (root / 'launcher_profiles.json').write_text(json.dumps(profiles))
    assert setup['manager'].scan()['minecraft']['state'] == 'needs_selection'
    with pytest.raises(GameSetupError, match='profile'):
        setup['manager'].select_location('minecraft', root)
    setup['manager'].select_location('minecraft', root, profile='vanilla-two')
    assert setup['manager'].status('minecraft')['profile'] == 'vanilla-two'


def test_minecraft_missing_java_blocks_deployment(setup):
    root = minecraft(setup, fabric=True)
    setup['manager'].java_major = 17
    state = asyncio.run(setup['manager'].ensure('minecraft'))
    assert 'java_25_required' in state['blockers'] and not state['ready']
    assert not (root / 'mods').exists()


def test_minecraft_profile_hash_or_existing_library_conflict_stops(setup):
    root = minecraft(setup)
    state = setup['manager'].scan()['minecraft']
    path = root / setup['manifest']['games'][1]['profile_resources'][0]['destination']
    write(path, b'foreign Fabric profile')
    before = (root / 'launcher_profiles.json').read_bytes()
    with pytest.raises(ValueError, match='不同'):
        install_fabric_profile(setup['bundle'], setup['data'], state)
    assert path.read_bytes() == b'foreign Fabric profile'
    assert (root / 'launcher_profiles.json').read_bytes() == before


@pytest.mark.resource_integration
def test_bundle_real_resources_hashes_and_fixed_destinations(resource_bundle):
    bundle = resource_bundle
    manifest = json.loads((bundle / 'bundle.json').read_text())
    assert {game['id'] for game in manifest['games']} == {'stardew', 'minecraft', 'bannerlord'}
    for game in manifest['games']:
        assert game['support'] == 'supported'
        for item in [*game['files'], *game.get('profile_resources', []), *game['licenses']]:
            path = bundle / item['source']
            assert path.resolve().is_relative_to(bundle.resolve())
            assert hashlib.sha256(path.read_bytes()).hexdigest() == item['sha256']
    assert not list(bundle.rglob('config.user.json'))
    assert not list(bundle.rglob('*.sqlite3'))


def test_managed_gabs_config_changes_selected_game_retains_private_key(setup):
    first = setup['root'] / 'first/bin/Bannerlord.BLSE.Standalone.exe'
    second = setup['root'] / 'second/bin/Bannerlord.BLSE.Standalone.exe'
    folder = Path(find_gabs_config_dir(first))
    before = json.loads((folder / 'config.json').read_text())
    assert len(before['apiKey']) >= 32
    assert 'StoryMode' in before['games']['bannerlord']['args'][1]
    assert Path(find_gabs_config_dir(second)) == folder
    after = json.loads((folder / 'config.json').read_text())
    assert before['apiKey'] == after['apiKey']
    assert after['games']['bannerlord']['target'] == str(second)


def test_external_gabs_config_is_never_modified(setup, monkeypatch):
    folder = setup['root'] / 'external'
    write(folder / 'config.json', b'custom config')
    monkeypatch.setenv('ELLA_GABS_CONFIG_DIR', str(folder))
    assert find_gabs_config_dir(setup['root'] / 'game.exe') == str(folder)
    assert (folder / 'config.json').read_bytes() == b'custom config'


def test_tampered_managed_gabs_config_rejected(setup):
    folder = Path(find_gabs_config_dir(setup['root'] / 'game.exe'))
    (folder / 'config.json').write_text('{"ellaManaged":false}')
    with pytest.raises(GabsError, match='外部配置'):
        find_gabs_config_dir(setup['root'] / 'game.exe')


def test_api_get_is_passive_and_location_keeps_profile(setup, monkeypatch):
    root = minecraft(setup)
    service = GameSetupService(setup['manager'])
    monkeypatch.setattr(api, 'get_game_setup_service', lambda: service)
    client = authorized_client(api.app)
    try:
        assert client.get('/api/games/setup').status_code == 200
        assert not (root / 'mods').exists()
        assert client.put('/api/games/minecraft/setup/location', json={
            'path': str(root), 'profile': 'vanilla',
        }).status_code == 202
        assert setup['manager']._selected['minecraft']['profile'] == 'vanilla'
        assert client.put('/api/games/unknown/setup/location', json={'path': str(root)}).status_code == 404
        assert client.put('/api/games/stardew_valley/setup/location', json={'path': str(root)}).status_code == 422
    finally:
        client.close()


def test_startup_background_deploys_only_fixture_and_shutdown_waits(setup, monkeypatch):
    root = stardew(setup)
    service = GameSetupService(setup['manager'], interval=60)
    session = RuntimeSession(setup['data'] / 'runtime.session')
    monkeypatch.setattr(api, 'get_runtime_session', lambda: session)
    getter = lru_cache(lambda: service)
    monkeypatch.setattr(api, 'get_game_setup_service', getter)
    monkeypatch.setenv('ELLA_GAME_SETUP_AUTO', '1')

    async def run():
        async with api.runtime_lifespan(api.app):
            assert service._scan_task is not None
            await asyncio.wait_for(asyncio.shield(service._scan_task), timeout=15)
            assert service.status('stardew_valley')['ready'], service.snapshot()
        assert service._scan_task.done() and service._auto_task.done()

    asyncio.run(run())
    assert (root / 'StardewModdingAPI.exe').is_file()


def test_launch_blocked_setup_does_not_spawn_process(setup, monkeypatch):
    service = GameSetupService(setup['manager'])
    monkeypatch.setattr(api, 'get_game_setup_service', lambda: service)

    def forbidden(*_args, **_kwargs):
        raise AssertionError('A game must not launch from an unprepared installation')

    monkeypatch.setattr(api.subprocess, 'Popen', forbidden)
    with pytest.raises(HTTPException) as caught:
        asyncio.run(api.launch_game('stardew_valley'))
    assert caught.value.status_code == 409

def test_bannerlord_game_version_is_checked_before_any_copy(setup):
    root = setup['library'] / 'steamapps/common/Mount & Blade II Bannerlord'
    write(root / 'bin/Win64_Shipping_Client/Bannerlord.exe', b'fake base exe')
    version = write(root / 'Modules/Native/SubModule.xml', b'<Module><Version value="v1.2.12"/></Module>')
    loader = b'fake blse'
    write(setup['bundle'] / 'bannerlord/blse.exe', loader)
    entry = setup['manifest']['games'][2]
    entry['files'] = [{'source': 'bannerlord/blse.exe',
        'destination': 'bin/Win64_Shipping_Client/Bannerlord.BLSE.Standalone.exe',
        'kind': 'loader', 'sha256': hashlib.sha256(loader).hexdigest()}]
    save_manifest(setup)
    state = asyncio.run(setup['manager'].ensure('bannerlord'))
    assert state['state'] == 'unsupported'
    assert not (root / 'bin/Win64_Shipping_Client/Bannerlord.BLSE.Standalone.exe').exists()
    version.write_bytes(b'<Module><Version value="v1.3.15"/></Module>')
    state = asyncio.run(setup['manager'].ensure('bannerlord'))
    assert state['ready'] and state['version'] == '1.3.15'


def test_minecraft_duplicate_mod_is_blocked_and_preserved(setup):
    root = minecraft(setup, fabric=True)
    old = write(root / 'mods/older-api.jar', jar('fabric-api', '0.150.0+26.2'))
    state = asyncio.run(setup['manager'].ensure('minecraft'))
    assert 'minecraft_mod_conflict' in state['blockers'] and not state['ready']
    assert old.read_bytes() == jar('fabric-api', '0.150.0+26.2')
    assert not (root / 'mods/fabric-api-0.156.0+26.2.jar').exists()


@pytest.mark.resource_integration
def test_real_smapi_bundle_deploys_complete_loader_to_fixture(setup, resource_bundle):
    root = stardew(setup)
    manager = GameSetupManager(resource_bundle, setup['data'], libraries=[setup['library']],
        minecraft_roots=[], process_checker=lambda _: False,
        version_reader=setup['manager'].version_reader)
    state = asyncio.run(manager.ensure('stardew_valley'))
    assert state['ready'], state
    assert (root / 'smapi-internal/SMAPI.Toolkit.dll').is_file()
    assert (root / 'StardewModdingAPI.deps.json').read_bytes() == (root / 'Stardew Valley.deps.json').read_bytes()
    assert (root / 'Mods/Ella.StardewBridge/Ella.StardewBridge.dll').is_file()


@pytest.mark.resource_integration
def test_real_bannerlord_bundle_deploys_compatible_modules_to_fixture(setup, resource_bundle):
    root = setup['library'] / 'steamapps/common/Mount & Blade II Bannerlord'
    write(root / 'bin/Win64_Shipping_Client/Bannerlord.exe', b'fake base exe')
    write(root / 'Modules/Native/SubModule.xml', b'<Module><Version value="v1.3.15"/></Module>')
    manager = GameSetupManager(resource_bundle, setup['data'], libraries=[setup['library']],
        minecraft_roots=[], process_checker=lambda _: False)
    state = asyncio.run(manager.ensure('bannerlord'))
    assert state['ready'], state
    assert (root / 'bin/Win64_Shipping_Client/Bannerlord.BLSE.Standalone.exe').is_file()
    assert (root / 'Modules/Bannerlord.GABS/bin/Win64_Shipping_Client/Bannerlord.GABS.v1.3.15.dll').is_file()
    assert not (root / 'Modules/Bannerlord.GABS/bin/Gaming.Desktop.x64_Shipping_Client').exists()


def test_invalid_location_does_not_lose_previous_selection(setup):
    root = stardew(setup)
    setup['manager'].select_location('stardew_valley', root)
    with pytest.raises(GameSetupError):
        setup['manager'].select_location('stardew_valley', setup['root'] / 'unrelated')
    assert setup['manager'].status('stardew_valley')['game_root'] == str(root)


@pytest.mark.skipif(os.name != 'nt', reason='Windows paths ignore case')
def test_selected_directory_case_does_not_prevent_detection_or_persistence(setup):
    root = stardew(setup)
    manager = setup['manager']
    state = manager.select_location('stardew_valley', str(root).swapcase())
    assert state['detected'] and state['state'] != 'needs_selection'
    loaded = GameSetupManager(setup['bundle'], setup['data'], libraries=manager.libraries,
        minecraft_roots=[], process_checker=lambda _: False, version_reader=manager.version_reader)
    assert Path(loaded.scan()['stardew_valley']['game_root']) == root
    assert not (root / 'StardewModdingAPI.exe').exists()


def test_missing_desktop_resource_manifest_falls_back_to_source_bundle(tmp_path, monkeypatch):
    from ella_runtime.modules.games import setup as game_setup

    bundle = tmp_path / 'source-project/artifacts/game-setup'
    write(bundle / 'bundle.json', b'{"schema_version":1,"games":[]}')
    # Point source discovery at a temporary project, independent of local artifacts.
    monkeypatch.setattr(game_setup, '__file__', str(tmp_path / 'source-project/src/setup.py'))
    monkeypatch.setenv('ELLA_RESOURCES_DIR', str(tmp_path / 'absent-desktop-resources'))
    assert game_setup._default_bundle() == bundle


@pytest.mark.parametrize('relative', ['game-setup', 'artifacts/game-setup'])
def test_explicit_resource_manifest_is_used_before_source_bundle(tmp_path, monkeypatch, relative):
    from ella_runtime.modules.games.setup import _default_bundle
    bundle = tmp_path / relative
    write(bundle / 'bundle.json', b'{"schema_version":1}')
    monkeypatch.setenv('ELLA_RESOURCES_DIR', str(tmp_path))
    assert _default_bundle() == bundle


def test_frozen_runtime_missing_resources_cannot_fall_back_to_project(tmp_path, monkeypatch):
    import sys

    from ella_runtime.modules.games.setup import _default_bundle
    monkeypatch.setattr(sys, 'frozen', True, raising=False)
    monkeypatch.setenv('ELLA_RESOURCES_DIR', str(tmp_path))
    assert _default_bundle() is None


def test_pending_file_diagnostics_distinguish_missing_and_changed_without_writes(setup):
    root = stardew(setup)
    plugin = write(root / 'Mods/Ella.StardewBridge/Ella.StardewBridge.dll', b'old plugin')
    state = setup['manager'].scan()['stardew_valley']
    pending = {item['path']: item['reason'] for item in state['pending_files']}
    assert pending['StardewModdingAPI.exe'] == 'missing'
    assert pending['Mods/Ella.StardewBridge/Ella.StardewBridge.dll'] == 'changed'
    assert plugin.read_bytes() == b'old plugin'
    assert not (root / 'StardewModdingAPI.exe').exists()


def test_bannerlord_reports_loader_replacement_version_before_deploying(setup):
    root = setup['library'] / 'steamapps/common/Mount & Blade II Bannerlord'
    write(root / 'bin/Win64_Shipping_Client/Bannerlord.exe', b'fake game')
    write(root / 'Modules/Native/SubModule.xml', b'<Module><Version value="v1.3.15"/></Module>')
    installed = write(root / 'bin/Win64_Shipping_Client/Bannerlord.BLSE.Standalone.exe', b'new loader')
    body = b'bundled loader'
    write(setup['bundle'] / 'bannerlord/blse.exe', body)
    entry = setup['manifest']['games'][2]
    entry['version_constraints']['loader_bundled'] = '1.5.12'
    entry['files'] = [{'source': 'bannerlord/blse.exe',
        'destination': 'bin/Win64_Shipping_Client/Bannerlord.BLSE.Standalone.exe',
        'kind': 'loader', 'sha256': hashlib.sha256(body).hexdigest()}]
    save_manifest(setup)
    setup['manager'].version_reader = lambda _: '1.7.2'
    state = setup['manager'].scan()['bannerlord']
    assert state['loader_version'] == '1.7.2' and state['bundled_loader_version'] == '1.5.12'
    assert '1.7.2' in state['detail'] and '1.5.12' in state['detail'] and '备份' in state['detail']
    assert state['pending_files'] == [{'path': installed.relative_to(root).as_posix(), 'reason': 'changed'}]
    assert installed.read_bytes() == b'new loader'
