import asyncio

import pytest

from ella_runtime.modules.applications.browser import (
    BrowserClickTool,
    BrowserFillTool,
    BrowserOpenTool,
    BrowserReadTool,
    BrowserSession,
    BrowserUnavailable,
)


class FakePage:
    url = "about:blank"
    value = ""
    clicked = False

    def is_closed(self):
        return False

    async def goto(self, url, **kwargs):
        self.url = url
        return type("Response", (), {"status": 200})()

    async def title(self):
        return "Example"

    def locator(self, selector):
        assert selector in {"body", "#query", "#submit"}
        return self

    @property
    def first(self):
        return self

    async def count(self):
        return 1

    async def inner_text(self, **kwargs):
        return "页面已打开，包含测试内容"

    async def fill(self, value, **kwargs):
        self.value = value

    async def input_value(self, **kwargs):
        return self.value

    async def click(self, **kwargs):
        self.clicked = True

    def get_by_text(self, text, **kwargs):
        assert text == "已提交"
        return self

    async def is_visible(self):
        return self.clicked

    async def wait_for(self, **kwargs):
        if not self.clicked:
            raise TimeoutError()


def test_browser_tools_open_read_and_reconcile():
    session = BrowserSession()
    session.page = FakePage()
    opener = BrowserOpenTool(session)
    reader = BrowserReadTool(session)
    filler = BrowserFillTool(session)
    clicker = BrowserClickTool(session)

    async def run():
        opened = await opener.execute({"url": "https://example.com"}, action_id="one")
        assert opened.verified
        assert opened.details["title"] == "Example"
        read = await reader.execute(
            {"url": "https://example.com", "selector": "body", "expected_text": "测试内容"}, action_id="two"
        )
        assert read.verified
        assert (await opener.reconcile({"url": "https://example.com"}, action_id="one")).verified
        assert (
            await reader.reconcile(
                {"url": "https://example.com", "selector": "body", "expected_text": "不存在"},
                action_id="two",
            )
        ).verified is False
        filled = await filler.execute(
            {"url": "https://example.com", "selector": "#query", "value": "艾拉"},
            action_id="fill",
        )
        assert filled.verified
        clicked = await clicker.execute(
            {"url": "https://example.com", "selector": "#submit", "expected_after_text": "已提交"},
            action_id="click",
        )
        assert clicked.verified
        with pytest.raises(ValueError, match="必须指定"):
            await clicker.execute(
                {"url": "https://example.com", "selector": "#submit"}, action_id="bad"
            )
        with pytest.raises(ValueError, match="http/https"):
            await opener.execute({"url": "file:///secret"}, action_id="three")

    asyncio.run(run())


def test_browser_read_requires_the_exact_current_url():
    session = BrowserSession()
    session.page = FakePage()
    session.page.url = "https://example.com/private"
    reader = BrowserReadTool(session)

    async def run():
        with pytest.raises(BrowserUnavailable, match="当前网页与计划指定的网址不一致"):
            await reader.execute(
                {"url": "https://example.com/other", "selector": "body"}, action_id="read"
            )
        assert await reader.reconcile(
            {"url": "https://example.com/other", "selector": "body"}, action_id="read"
        ) is None
        with pytest.raises(TypeError, match="网址必须是字符串"):
            await reader.execute({"selector": "body"}, action_id="read")
        assert await reader.reconcile({"selector": "body"}, action_id="old-plan") is None

    asyncio.run(run())


def test_browser_click_rejects_postconditions_already_true_before_click():
    session = BrowserSession()
    session.page = FakePage()
    session.page.url = "https://example.com/account"
    clicker = BrowserClickTool(session)

    async def run():
        with pytest.raises(ValueError, match="网址与当前网址相同"):
            await clicker.execute({
                "url": session.page.url, "selector": "#submit",
                "expected_after_url": session.page.url,
            }, action_id="same-url")
        assert not session.page.clicked

        class AlreadyVisiblePage(FakePage):
            async def is_visible(self):
                return True

        session.page = AlreadyVisiblePage()
        session.page.url = "https://example.com/account"
        with pytest.raises(ValueError, match="点击前已出现"):
            await clicker.execute({
                "url": session.page.url, "selector": "#submit",
                "expected_after_text": "已提交",
            }, action_id="already-visible")
        assert not session.page.clicked
        assert await clicker.reconcile({
            "url": session.page.url, "selector": "#submit",
            "expected_after_text": "已提交",
        }, action_id="interrupted") is None

    asyncio.run(run())
