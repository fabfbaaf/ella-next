import asyncio
from typing import ClassVar

import pytest

from ella_runtime.modules.applications.browser import (
    BrowserFillTool,
    BrowserObserveTool,
    BrowserOpenTool,
    BrowserReadTool,
    BrowserSession,
    BrowserUnavailable,
)


class FakePage:
    url = "about:blank"
    value = ""

    def is_closed(self):
        return False

    async def goto(self, url, **kwargs):
        self.url = url
        return type("Response", (), {"status": 200})()

    async def title(self):
        return "Example"

    def locator(self, selector):
        return self

    @property
    def first(self):
        return self

    async def count(self):
        return 1

    async def fill(self, value, **kwargs):
        self.value = value

    async def input_value(self, **kwargs):
        return self.value



def test_task_fill_is_atomic_against_admin_navigation_and_close():
    async def run():
        started, release = asyncio.Event(), asyncio.Event()
        events = []

        class Page(FakePage):
            url = "https://example.com/form"

            async def fill(self, value, **kwargs):
                events.append("fill")
                started.set()
                await release.wait()
                self.value = value

            async def input_value(self, **kwargs):
                events.append("verify")
                return self.value

            async def goto(self, url, **kwargs):
                events.append("navigate")
                return await super().goto(url, **kwargs)

        class Context:
            async def close(self):
                events.append("close")

        session = BrowserSession()
        session.page = Page()
        fill = asyncio.create_task(BrowserFillTool(session).execute({
            "url": "https://example.com/form", "selector": "#query", "value": "hello",
        }, action_id="task-fill"))
        await started.wait()
        navigate = asyncio.create_task(BrowserOpenTool(session).open_and_inspect(
            {"url": "https://example.com/new"}, inspector=lambda page: page.title(),
        ))
        await asyncio.sleep(0)
        session._context = Context()
        close = asyncio.create_task(session.close())
        await asyncio.sleep(0)
        assert events == ["fill"]
        release.set()
        result, title, _ = await asyncio.wait_for(asyncio.gather(fill, navigate, close), 1)
        assert result.verified and result.details["url"] == "https://example.com/form"
        assert title == "Example"
        assert events == ["fill", "verify", "navigate", "close"]
        assert session.page is None

    asyncio.run(run())


@pytest.mark.parametrize("method", ["execute", "reconcile"])
def test_read_url_check_and_snapshot_share_navigation_lock(method):
    async def run():
        started, release = asyncio.Event(), asyncio.Event()

        class Page(FakePage):
            url = "https://example.com/current"

            async def inner_text(self, **kwargs):
                started.set()
                await release.wait()
                assert self.url == "https://example.com/current"
                return "original"

        session = BrowserSession()
        session.page = Page()
        read = asyncio.create_task(getattr(BrowserReadTool(session), method)(
            {"url": session.page.url}, action_id="read",
        ))
        await started.wait()
        opening = asyncio.create_task(BrowserOpenTool(session).execute(
            {"url": "https://example.com/new"}, action_id="open",
        ))
        await asyncio.sleep(0)
        assert session.page.url == "https://example.com/current"
        release.set()
        result, _ = await asyncio.wait_for(asyncio.gather(read, opening), 1)
        assert result.details["text_excerpt"] == "original"
        assert result.details["url"] == "https://example.com/current"
        assert session.page.url == "https://example.com/new"

    asyncio.run(run())


def test_close_cleans_runtime_handles_even_when_context_close_fails():
    async def run():
        stopped = []

        class Context:
            async def close(self):
                raise RuntimeError("gone")

        class Driver:
            async def stop(self):
                stopped.append(True)

        session = BrowserSession()
        session.page, session._context, session._playwright = FakePage(), Context(), Driver()
        with pytest.raises(RuntimeError, match="gone"):
            await session.close()
        assert stopped == [True]
        assert session.page is None and session._context is None and session._playwright is None

    asyncio.run(run())


def test_settings_change_waits_for_active_page_transaction(monkeypatch, tmp_path):
    monkeypatch.setenv("ELLA_DATA_DIR", str(tmp_path))

    async def run():
        session = BrowserSession(channel="msedge")
        closed = []

        class Context:
            async def close(self):
                closed.append(True)

        session._context, session.page = Context(), FakePage()
        async with session.operation_lock:
            configuring = asyncio.create_task(session.configure(channel="chrome", auto_web=False))
            await asyncio.sleep(0)
            assert not closed and session.channel == "msedge"
        result = await asyncio.wait_for(configuring, 1)
        assert closed == [True]
        assert result["channel"] == "chrome" and result["auto_web"] is False

    asyncio.run(run())


def test_observe_page_uses_real_locators_and_never_reads_input_values():
    class Control:
        def __init__(self, attrs, text="", enabled=True):
            self.attrs, self.text, self.enabled = attrs, text, enabled

        async def get_attribute(self, name, **kwargs):
            assert name != "value"
            return self.attrs.get(name)

        async def inner_text(self, **kwargs):
            assert self.attrs.get("type") != "password"
            return self.text

        async def is_enabled(self):
            return self.enabled

        async def input_value(self, **kwargs):
            raise AssertionError("must not read any input value")

    class Locator:
        def __init__(self, controls):
            self.controls = controls

        async def count(self):
            return len(self.controls)

        def nth(self, index):
            return self.controls[index]

        async def inner_text(self, **kwargs):
            return "Sign in and search"

    class Page(FakePage):
        url = "https://example.com/account"
        controls: ClassVar[dict] = {
            "link": [Control({"id": "docs", "href": "/docs"}, "Help")],
            "button": [Control({"aria-label": "Search"}, "Search")],
            "input": [Control({"id": "password", "type": "password", "aria-label": "Password"}),
                      Control({"id": 'query"one', "type": "text", "placeholder": "Query"})],
            "select": [],
        }

        def locator(self, selector):
            if selector == "body":
                return Locator([])
            for kind, group in BrowserObserveTool._groups:
                if selector == group:
                    return Locator(self.controls[kind])
            assert selector.startswith("[")
            return Locator([object()])

    async def run():
        session = BrowserSession()
        session.page = Page()
        tool = BrowserObserveTool(session)
        evidence = await tool.execute({"url": session.page.url}, action_id="observe")
        assert evidence.verified
        assert evidence.details["inputs_read"] is False
        controls = evidence.details["controls"]
        assert len(controls) == 4
        assert controls[0]["selector"] == '[id="docs"]' and controls[0]["href"] == "/docs"
        assert controls[2]["sensitive"] is True
        assert controls[3]["selector"] == '[id="query\\"one"]'
        assert "value" not in str(controls)
        with pytest.raises(BrowserUnavailable):
            await tool.execute({"url": "https://example.com/other"}, action_id="wrong-page")
        assert await tool.reconcile({}, action_id="old-plan") is None

    asyncio.run(run())
