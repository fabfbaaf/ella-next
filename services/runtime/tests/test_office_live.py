import asyncio

import pytest

from ella_runtime.modules.applications.office_live import (
    EditOpenSpreadsheetTool,
    OfficeLiveUnavailable,
    ReplaceOpenDocumentTextTool,
)


class Cell:
    def __init__(self, value=None):
        self.Value2 = value
        self.Formula = value


class Range:
    def __init__(self):
        self.data = [[Cell("旧标题"), Cell(2)], [Cell(None), Cell(None)]]
        self.Rows = type("Rows", (), {"Count": 2})()
        self.Columns = type("Columns", (), {"Count": 2})()

    def Cells(self, row, column):
        return self.data[row - 1][column - 1]


class Sheet:
    Name = "Sheet1"

    def __init__(self):
        self.target = Range()

    def Range(self, address):
        assert address == "A1:B2"
        return self.target


class Workbook:
    Name = "计划.xlsx"
    Path = "C:\\work"

    def __init__(self):
        self.sheet = Sheet()
        self.saved = False

    def Worksheets(self, name):
        assert name == "Sheet1"
        return self.sheet

    def Save(self):
        self.saved = True


class Excel:
    def __init__(self):
        self.ActiveWorkbook = Workbook()
        self.calculated = False

    def Calculate(self):
        self.calculated = True


def test_live_excel_edits_actual_cells_and_verifies():
    app = Excel()
    tool = EditOpenSpreadsheetTool(lambda: app)
    arguments = {
        "workbook": "计划.xlsx", "sheet": "Sheet1", "range": "A1:B2",
        "expected_before": [["旧标题", 2], [None, None]],
        "values": [["标题", 2], ["合计", "=B1*2"]], "save": True,
    }
    evidence = asyncio.run(tool.execute(arguments, action_id="edit-1"))
    assert evidence.verified
    assert evidence.details["before"] == [["旧标题", 2], [None, None]]
    assert evidence.details["after"] == arguments["values"]
    assert app.calculated and app.ActiveWorkbook.saved
    assert asyncio.run(tool.reconcile(arguments, action_id="edit-1")).verified


def test_live_excel_refuses_wrong_workbook_or_stale_before():
    app = Excel()
    tool = EditOpenSpreadsheetTool(lambda: app)
    arguments = {
        "workbook": "别的文件.xlsx", "sheet": "Sheet1", "range": "A1:B2",
        "values": [["新", 2], [None, None]],
    }
    with pytest.raises(OfficeLiveUnavailable):
        asyncio.run(tool.execute(arguments, action_id="wrong"))
    arguments["workbook"] = "计划.xlsx"
    arguments["expected_before"] = [["过期", 2], [None, None]]
    with pytest.raises(ValueError, match="expected_before"):
        asyncio.run(tool.execute(arguments, action_id="stale"))
    assert app.ActiveWorkbook.sheet.target.Cells(1, 1).Value2 == "旧标题"


class WordContent:
    def __init__(self):
        self.Text = "旧文字。这里还有旧文字。"
        self.Find = self

    def Execute(self, *, FindText, ReplaceWith, Replace):
        assert Replace == 2
        self.Text = self.Text.replace(FindText, ReplaceWith)


class WordDocument:
    Name = "说明.docx"
    Path = "C:\\work"

    def __init__(self):
        self.Content = WordContent()
        self.saved = False

    def Save(self):
        self.saved = True


def test_live_word_replaces_and_verifies_open_document():
    document = WordDocument()
    app = type("Word", (), {"ActiveDocument": document})()
    tool = ReplaceOpenDocumentTextTool(lambda: app)
    arguments = {
        "document": "说明.docx", "find_text": "旧文字", "replace_with": "新文字",
        "expected_count": 2, "save": True,
    }
    evidence = asyncio.run(tool.execute(arguments, action_id="word-1"))
    assert evidence.verified and evidence.details["remaining_count"] == 0
    assert document.saved
    assert asyncio.run(tool.reconcile(arguments, action_id="word-1")).verified
