"""Attach to already-open desktop Office windows and verify content edits."""

from __future__ import annotations

import asyncio
import re
from collections.abc import Callable
from typing import Any

from ella_runtime.modules.agent.contracts import ToolEvidence


class OfficeLiveUnavailable(RuntimeError):
    """Desktop Office or its active document is unavailable."""


_RANGE = re.compile(
    r"^[A-Z]{1,3}[1-9][0-9]{0,6}(?::[A-Z]{1,3}[1-9][0-9]{0,6})?$", re.IGNORECASE
)


def _active_application(progid: str, operation: Callable[[Any], ToolEvidence]) -> ToolEvidence:
    try:
        import pythoncom
        import win32com.client
    except ImportError as exc:
        raise OfficeLiveUnavailable("请安装 runtime[office-live] 依赖") from exc
    pythoncom.CoInitialize()
    try:
        try:
            app = win32com.client.GetActiveObject(progid)
        except Exception as exc:
            raise OfficeLiveUnavailable("请先在 Microsoft Office 中打开目标文件") from exc
        return operation(app)
    finally:
        pythoncom.CoUninitialize()


class _LiveOfficeTool:
    progid: str

    def __init__(self, connector: Callable[[], Any] | None = None) -> None:
        self.connector = connector

    async def _run(self, operation: Callable[[Any], ToolEvidence]) -> ToolEvidence:
        if self.connector is not None:
            return await asyncio.to_thread(lambda: operation(self.connector()))
        return await asyncio.to_thread(_active_application, self.progid, operation)


def _workbook(app: Any, arguments: dict[str, Any]) -> tuple[Any, Any, Any]:
    name = arguments.get("workbook")
    sheet_name = arguments.get("sheet")
    address = arguments.get("range")
    if not all(isinstance(item, str) and item.strip() for item in (name, sheet_name, address)):
        raise ValueError("请明确指定已打开的工作簿、工作表和单元格区域")
    if not _RANGE.fullmatch(address):
        raise ValueError("单元格区域必须使用 A1 或 A1:B2 格式")
    workbook = app.ActiveWorkbook
    if workbook is None or workbook.Name.casefold() != name.casefold():
        raise OfficeLiveUnavailable("当前 Excel 活动工作簿与任务指定名称不一致")
    try:
        sheet = workbook.Worksheets(sheet_name)
        target = sheet.Range(address)
    except Exception as exc:
        raise OfficeLiveUnavailable("指定的工作表或单元格区域不存在") from exc
    cells = int(target.Rows.Count) * int(target.Columns.Count)
    if cells > 4000:
        raise ValueError("单次最多读取或修改 4000 个单元格")
    return workbook, sheet, target


def _cell_data(target: Any) -> tuple[list[list[Any]], list[list[Any]]]:
    formulas: list[list[Any]] = []
    calculated: list[list[Any]] = []
    for row in range(1, int(target.Rows.Count) + 1):
        formula_row: list[Any] = []
        calculated_row: list[Any] = []
        for column in range(1, int(target.Columns.Count) + 1):
            cell = target.Cells(row, column)
            formula = cell.Formula
            value = cell.Value2
            formula_row.append(formula if isinstance(formula, str) and formula.startswith("=") else value)
            calculated_row.append(value)
        formulas.append(formula_row)
        calculated.append(calculated_row)
    return formulas, calculated


class ReadOpenSpreadsheetTool(_LiveOfficeTool):
    name = "office.read_open_spreadsheet"
    progid = "Excel.Application"
    description = (
        "读取已打开 Excel 的指定区域，包括公式和计算值。参数："
        '{"workbook":"文件名.xlsx","sheet":"工作表名","range":"A1:B5"}。'
    )

    async def execute(self, arguments: dict, *, action_id: str) -> ToolEvidence:
        def operation(app: Any) -> ToolEvidence:
            workbook, sheet, target = _workbook(app, arguments)
            formulas, calculated = _cell_data(target)
            return ToolEvidence(
                verified=True,
                details={
                    "workbook": workbook.Name,
                    "sheet": sheet.Name,
                    "range": arguments["range"],
                    "cells": formulas,
                    "calculated": calculated,
                },
            )

        return await self._run(operation)

    async def reconcile(self, arguments: dict, *, action_id: str) -> ToolEvidence | None:
        try:
            return await self.execute(arguments, action_id=action_id)
        except OfficeLiveUnavailable:
            return None


class EditOpenSpreadsheetTool(_LiveOfficeTool):
    name = "office.edit_open_spreadsheet"
    progid = "Excel.Application"
    description = (
        "直接修改当前已打开 Excel 的单元格内容或公式，读回核验。参数："
        '{"workbook":"文件名.xlsx","sheet":"工作表名","range":"A1:B2",'
        '"values":[["标题",10],["合计","=B1*2"]],'
        '"expected_before":[[null,null],[null,null]],"save":false}。'
        "expected_before 可选；保存仅用于已有路径的文件。"
    )

    @staticmethod
    def _expected(arguments: dict[str, Any], target: Any) -> list[list[Any]]:
        values = arguments.get("values")
        rows, columns = int(target.Rows.Count), int(target.Columns.Count)
        if (
            not isinstance(values, list)
            or len(values) != rows
            or any(not isinstance(row, list) or len(row) != columns for row in values)
            or any(type(value) not in {str, int, float, bool, type(None)} for row in values for value in row)
        ):
            raise ValueError("values 行列数必须与指定区域一致，且只包含单元格值或公式")
        return values

    async def execute(self, arguments: dict, *, action_id: str) -> ToolEvidence:
        def operation(app: Any) -> ToolEvidence:
            workbook, sheet, target = _workbook(app, arguments)
            values = self._expected(arguments, target)
            before, _ = _cell_data(target)
            save = arguments.get("save", False)
            if not isinstance(save, bool):
                raise TypeError("save 必须是布尔值")
            if save and not workbook.Path:
                raise ValueError("未保存过的工作簿没有路径，请先在 Excel 中保存")
            expected_before = arguments.get("expected_before")
            if before != values:
                if expected_before is not None and before != expected_before:
                    raise ValueError("实际单元格内容与 expected_before 不一致，已停止编辑")
                for row_index, row in enumerate(values, start=1):
                    for column_index, value in enumerate(row, start=1):
                        cell = target.Cells(row_index, column_index)
                        if isinstance(value, str) and value.startswith("="):
                            cell.Formula = value
                        else:
                            cell.Value2 = value
                app.Calculate()
            after, calculated = _cell_data(target)
            if save and after == values:
                workbook.Save()
            return ToolEvidence(
                verified=after == values,
                details={
                    "workbook": workbook.Name,
                    "sheet": sheet.Name,
                    "range": arguments["range"],
                    "before": before,
                    "after": after,
                    "calculated": calculated,
                    "saved": save and after == values,
                },
            )

        return await self._run(operation)

    async def reconcile(self, arguments: dict, *, action_id: str) -> ToolEvidence | None:
        def operation(app: Any) -> ToolEvidence:
            workbook, sheet, target = _workbook(app, arguments)
            values = self._expected(arguments, target)
            after, calculated = _cell_data(target)
            return ToolEvidence(
                verified=after == values,
                details={
                    "workbook": workbook.Name, "sheet": sheet.Name,
                    "range": arguments["range"], "after": after, "calculated": calculated,
                    "replayed": False,
                },
            )

        try:
            return await self._run(operation)
        except OfficeLiveUnavailable:
            return None


class ReplaceOpenDocumentTextTool(_LiveOfficeTool):
    name = "office.replace_open_document_text"
    progid = "Word.Application"
    description = (
        "直接替换当前已打开 Word 文档中的指定文本并核验。参数："
        '{"document":"文件名.docx","find_text":"旧文字",'
        '"replace_with":"新文字","expected_count":1,"save":false}。'
    )

    @staticmethod
    def _document(app: Any, arguments: dict[str, Any]) -> tuple[Any, str, str, int, bool]:
        name = arguments.get("document")
        old = arguments.get("find_text")
        new = arguments.get("replace_with")
        count = arguments.get("expected_count", 1)
        save = arguments.get("save", False)
        if (
            not isinstance(name, str) or not name.strip()
            or not isinstance(old, str) or not old or len(old) > 1000
            or not isinstance(new, str) or len(new) > 5000
            or type(count) is not int or not 1 <= count <= 1000
            or not isinstance(save, bool)
            or old in new
        ):
            raise ValueError("Word 替换参数无效")
        document = app.ActiveDocument
        if document is None or document.Name.casefold() != name.casefold():
            raise OfficeLiveUnavailable("当前 Word 活动文档与任务指定名称不一致")
        if save and not document.Path:
            raise ValueError("未保存过的文档没有路径，请先在 Word 中保存")
        return document, old, new, count, save

    async def execute(self, arguments: dict, *, action_id: str) -> ToolEvidence:
        def operation(app: Any) -> ToolEvidence:
            document, old, new, count, save = self._document(app, arguments)
            before = str(document.Content.Text)
            if before.count(old) != count:
                raise ValueError("文档中待替换文本的实际次数与 expected_count 不一致")
            prior_new_count = before.count(new) if new else 0
            finder = document.Content.Find
            finder.Execute(FindText=old, ReplaceWith=new, Replace=2)
            after = str(document.Content.Text)
            verified = old not in after and (not new or after.count(new) >= prior_new_count + count)
            if verified and save:
                document.Save()
            return ToolEvidence(
                verified=verified,
                details={
                    "document": document.Name, "find_text": old,
                    "expected_count": count, "remaining_count": after.count(old),
                    "replacement_count": after.count(new) if new else None,
                    "saved": save and verified,
                },
            )

        return await self._run(operation)

    async def reconcile(self, arguments: dict, *, action_id: str) -> ToolEvidence | None:
        def operation(app: Any) -> ToolEvidence:
            document, old, new, count, _ = self._document(app, arguments)
            text = str(document.Content.Text)
            verified = old not in text and (not new or text.count(new) >= count)
            return ToolEvidence(
                verified=verified,
                details={
                    "document": document.Name, "remaining_count": text.count(old),
                    "replacement_count": text.count(new) if new else None,
                    "replayed": False,
                },
            )

        try:
            return await self._run(operation)
        except OfficeLiveUnavailable:
            return None
