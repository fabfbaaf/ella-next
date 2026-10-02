import asyncio

import pytest

from ella_runtime.modules.games.adapters.bannerlord import gabs_client
from ella_runtime.modules.games.adapters.bannerlord.gabs import BannerlordGabsBridge
from ella_runtime.modules.games.bridge import GameBridgeError


class FakeProcess:
    stopped = False

    def poll(self):
        return 0 if self.stopped else None

    def terminate(self):
        self.stopped = True

    def wait(self, _timeout):
        return 0


def test_failed_gabs_process_spawn_clears_dead_config_and_can_retry(monkeypatch):
    monkeypatch.delenv('ELLA_GABS_HTTP', raising=False)
    monkeypatch.delenv('ELLA_GABS_API_KEY', raising=False)
    monkeypatch.setattr(gabs_client, 'find_gabs_exe', lambda: 'fake-gabs.exe')
    monkeypatch.setattr(gabs_client, 'find_gabs_config_dir', lambda _: None)
    ports = iter([18701, 18702])
    monkeypatch.setattr(gabs_client, '_free_port', lambda: next(ports))
    calls = []
    process = FakeProcess()

    def spawn(*args, **kwargs):
        calls.append(args)
        if len(calls) == 1:
            raise PermissionError('private OS output must not be displayed')
        return process

    monkeypatch.setattr(gabs_client.subprocess, 'Popen', spawn)
    client = gabs_client.GabsMcpClient()

    async def ready(timeout=6):
        assert client._process is process

    monkeypatch.setattr(client, 'wait_ready', ready)

    async def run():
        with pytest.raises(gabs_client.GabsError, match='执行权限') as caught:
            await client.start()
        assert 'private OS' not in str(caught.value)
        assert client.config is None and client._process is None
        await client.start()
        assert client.config.endpoint == 'http://127.0.0.1:18702/mcp'
        assert len(calls) == 2
        await client.close()
        assert client.config is None and process.stopped

    asyncio.run(run())


def test_gabs_process_failure_is_a_bridge_error_instead_of_unhandled_os_error(monkeypatch):
    monkeypatch.delenv('ELLA_GABS_HTTP', raising=False)
    monkeypatch.setattr(gabs_client, 'find_gabs_exe', lambda: 'missing-gabs.exe')
    monkeypatch.setattr(gabs_client, 'find_gabs_config_dir', lambda _: None)
    monkeypatch.setattr(gabs_client, '_free_port', lambda: 18703)

    def spawn(*args, **kwargs):
        raise FileNotFoundError('not installed')

    monkeypatch.setattr(gabs_client.subprocess, 'Popen', spawn)
    bridge = BannerlordGabsBridge()
    with pytest.raises(GameBridgeError, match='未连接'):
        asyncio.run(bridge.observe())
    assert bridge.client.config is None
