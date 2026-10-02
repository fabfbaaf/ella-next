import asyncio
import base64
from datetime import UTC, datetime

import pytest

from ella_runtime.modules.applications.browser import BrowserSession
from ella_runtime.modules.applications.web_access import (
    WebResearch,
    public_url,
    result_url,
    save_web_settings,
    web_settings,
)
from ella_runtime.modules.models.contracts import ModelMessage, ModelPurpose, ModelRequest
from ella_runtime.modules.models.gateway import ModelGateway


@pytest.mark.parametrize("url", ["file:///secret", "http://localhost/x", "http://127.0.0.1/x", "http://[::1]/x", "http://192.168.1.1/x", "https://user:password@example.com", "http://server"])
def test_research_rejects_local_or_credential_urls(url):
    with pytest.raises(ValueError):
        public_url(url)


def test_bing_link_decoding_retains_real_source():
    target = "https://example.com/docs?q=one"
    encoded = base64.urlsafe_b64encode(target.encode()).decode().rstrip("=")
    assert result_url(f"https://www.bing.com/ck/a?u=a1{encoded}") == target
    assert result_url("https://www.bing.com/search?q=one") is None


def test_settings_persist_without_starting_browser(monkeypatch, tmp_path):
    monkeypatch.setenv("ELLA_DATA_DIR", str(tmp_path))
    assert web_settings()["auto_web"] is True
    save_web_settings("chrome", False)
    session = BrowserSession()
    assert session.channel == "chrome"
    assert session.status()["connected"] is False
    assert session.status()["auto_web"] is False
    with pytest.raises(ValueError):
        save_web_settings("unknown", True)


def test_web_context_is_explicit_and_failed_search_does_not_fabricate(monkeypatch, tmp_path):
    monkeypatch.setenv("ELLA_DATA_DIR", str(tmp_path))
    research = WebResearch(BrowserSession())
    calls = []
    async def search(query):
        calls.append(query)
        return {"sources": [{"url": "https://example.com", "text": "data"}], "searched_at": datetime.now(UTC).isoformat()}
    research.research = search
    async def run():
        assert await research.context("你好") == ""
        context = await research.context("搜索今天的消息")
        assert "https://example.com" in context and "不可信数据" in context
        assert len(calls) == 1
        save_web_settings("auto", False)
        assert await research.context("搜索一个问题") == ""
        save_web_settings("auto", True)
        async def fail(query):
            raise RuntimeError("private output")
        research.research = fail
        context = await research.context("查一下消息")
        assert "联网检索失败" in context and "private output" not in context
    asyncio.run(run())


def test_only_chat_and_voice_receive_web_evidence():
    calls = []
    async def context(query):
        calls.append(query)
        return "<web_evidence>actual</web_evidence>"
    gateway = ModelGateway(None, None, web_context=context)
    async def run():
        for purpose in (ModelPurpose.CHAT, ModelPurpose.VOICE, ModelPurpose.ACTION):
            request = ModelRequest(purpose=purpose, messages=[ModelMessage(role="user", content="搜索资料")])
            prepared = await gateway._with_web(request)
            assert ("actual" in prepared.instructions) == (purpose != ModelPurpose.ACTION)
        assert len(calls) == 2
    asyncio.run(run())


def test_fallback_search_link_keeps_source_and_rejects_internal_redirect():
    assert result_url("https://duckduckgo.com/l/?uddg=https%3A%2F%2Fdocs.python.org%2F3%2F") == "https://docs.python.org/3/"
    assert result_url("https://duckduckgo.com/l/?uddg=http%3A%2F%2F127.0.0.1%2F") is None


def test_research_preserves_search_rendering_and_guards_source_navigation(monkeypatch):
    from ella_runtime.modules.applications import web_access
    events = []
    class Page:
        url = "https://docs.python.org/"
        async def bring_to_front(self): pass
        async def route(self, pattern, callback): events.append("guard")
        async def goto(self, url, **options):
            events.append("read")
            assert events[-2] == "guard"
            return type("Response", (), {"status": 200})()
        async def close(self): events.append("close")
    class Context:
        async def new_page(self): return Page()
    class Session:
        operation_lock = asyncio.Lock()
        _context = Context()
        async def ensure_page(self): pass
    async def search(page, query):
        assert not events
        events.append("search")
        return {"results": [{"url": "https://docs.python.org/", "title": "Python"}], "captured_at": "now"}
    async def inspect(page, **options):
        return {"url": page.url, "text": "Official documentation"}
    monkeypatch.setattr(web_access, "search_page", search)
    monkeypatch.setattr(web_access, "inspect_page", inspect)
    result = asyncio.run(WebResearch(Session()).research("Python documentation"))
    assert result["sources"][0]["read"] is True
    assert events == ["search", "guard", "read", "close"]
