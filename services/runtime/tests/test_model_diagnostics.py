import asyncio

import httpx
import pytest

from ella_runtime.modules.models.diagnostics import probe_model
from ella_runtime.modules.models.settings import ProviderConfig


def test_probe_only_reads_metadata_and_never_redirects_credentials():
    calls=[]
    def respond(request):
        calls.append(request)
        assert request.method=='GET' and request.url.path=='/v1/models'
        return httpx.Response(200, json={'data':[{'id':'chat-model'}]})
    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
            result=await probe_model(ProviderConfig('test','chat-model','https://service.example/v1','private-key'), client=client)
            assert result['reachable'] and result['model_found']
    asyncio.run(run())
    assert len(calls)==1


@pytest.mark.parametrize('status', [302,401,404,500])
def test_probe_failure_is_sanitized(status):
    def respond(request):
        return httpx.Response(status, headers={'Location':'https://other.example/models'}, text='private-key bad-response')
    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
            with pytest.raises(ValueError) as failure:
                await probe_model(ProviderConfig('test','m','https://service.example/v1','private-key'), client=client)
            assert str(status) in str(failure.value)
            assert 'private-key' not in str(failure.value) and 'bad-response' not in str(failure.value)
    asyncio.run(run())
