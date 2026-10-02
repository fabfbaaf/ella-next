import asyncio

import pytest

from ella_runtime.modules.applications.office_files import (
    DocumentTool,
    PresentationTool,
    SpreadsheetTool,
)


def test_office_tools_create_and_reopen_verified_files(tmp_path):
    root = tmp_path / "office"
    cases = [
        (
            SpreadsheetTool(root),
            {
                "path": "reports/budget.xlsx",
                "sheet": "预算",
                "rows": [["项目", "金额"], ["设备", 5060]],
            },
        ),
        (
            DocumentTool(root),
            {"path": "reports/notes.docx", "title": "会议纪要", "paragraphs": ["今天讨论了计划。"]},
        ),
        (
            PresentationTool(root),
            {
                "path": "reports/plan.pptx",
                "slides": [{"title": "项目计划", "bullets": ["人格", "游戏"]}],
            },
        ),
    ]

    async def run():
        for index, (tool, arguments) in enumerate(cases):
            result = await tool.execute(arguments, action_id=f"office-{index}")
            assert result.verified
            assert result.details["content_verified"] is True
            assert (root / arguments["path"]).is_file()
            assert (await tool.reconcile(arguments, action_id=f"office-{index}")).verified
            with pytest.raises(FileExistsError):
                await tool.execute(
                    {**arguments, "title": "different"}
                    if isinstance(tool, DocumentTool)
                    else {**arguments, "rows": [["不同"]]}
                    if isinstance(tool, SpreadsheetTool)
                    else {**arguments, "slides": [{"title": "不同"}]},
                    action_id="overwrite",
                )

    asyncio.run(run())


def test_office_paths_stay_in_workspace(tmp_path):
    tool = SpreadsheetTool(tmp_path / "office")
    with pytest.raises(ValueError, match="超出工作区"):
        asyncio.run(tool.execute({"path": "../outside.xlsx", "rows": [["x"]]}, action_id="bad"))
