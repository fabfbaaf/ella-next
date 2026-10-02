"""Conservative, parameter-aware approval policy for planned tools."""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from urllib.parse import urlsplit

from ella_runtime.modules.agent.contracts import AgentTool, TaskStep


class RiskLevel(StrEnum):
    LOW = "low"
    REVIEW = "review"
    HIGH = "high"


@dataclass(frozen=True)
class RiskAssessment:
    level: RiskLevel
    reason: str
    destination: str | None = None


@dataclass(frozen=True)
class ToolPolicy:
    arguments: frozenset[str]
    category: str


POLICIES = {
    "browser.open_page": ToolPolicy(frozenset({"url"}), "navigation"),
    "browser.observe_page": ToolPolicy(frozenset({"url"}), "read"),
    "browser.read_page": ToolPolicy(frozenset({"url", "selector", "expected_text"}), "read"),
    "browser.fill": ToolPolicy(frozenset({"url", "selector", "value"}), "web_input"),
    "browser.click": ToolPolicy(
        frozenset({"url", "selector", "expected_after_text", "expected_after_url"}), "web_action"
    ),
    "workspace.write_text": ToolPolicy(frozenset({"path", "content", "overwrite"}), "text_file"),
    "desktop.write_text": ToolPolicy(frozenset({"name", "content", "overwrite"}), "text_file"),
    "office.create_spreadsheet": ToolPolicy(
        frozenset({"path", "sheet", "rows", "overwrite"}), "document"
    ),
    "office.create_document": ToolPolicy(
        frozenset({"path", "title", "paragraphs", "overwrite"}), "document"
    ),
    "office.create_presentation": ToolPolicy(
        frozenset({"path", "slides", "overwrite"}), "document"
    ),
    "office.read_open_spreadsheet": ToolPolicy(
        frozenset({"workbook", "sheet", "range"}), "read"
    ),
    "office.edit_open_spreadsheet": ToolPolicy(
        frozenset({"workbook", "sheet", "range", "values", "expected_before", "save"}), "edit"
    ),
    "office.replace_open_document_text": ToolPolicy(
        frozenset({"document", "find_text", "replace_with", "expected_count", "save"}), "edit"
    ),
    "code.list_files": ToolPolicy(frozenset({"contains"}), "read"),
    "code.read_file": ToolPolicy(frozenset({"path"}), "read"),
    "code.git_review": ToolPolicy(frozenset(), "read"),
    "code.open_workspace": ToolPolicy(frozenset(), "open_local"),
    "code.replace_text": ToolPolicy(
        frozenset({"path", "old", "new", "expected_sha256"}), "edit"
    ),
    "code.run_check": ToolPolicy(frozenset({"check"}), "execute"),
}

_HIGH_IMPACT_GOAL = re.compile(
    r"提交|发送|发布|上传|购买|付款|转账|删除|清空|退出登录|登出"
    r"|\b(?:submit|send|publish|upload|purchase|pay|transfer|delete|logout)\b",
    re.IGNORECASE,
)
_SENSITIVE_FIELD = re.compile(
    r"password|passwd|token|secret|api.?key|credit.?card|card.?number|cvv|"
    r"email|phone|address|密码|密钥|令牌|银行卡|信用卡|验证码|身份证|邮箱|电话|地址",
    re.IGNORECASE,
)
_EXECUTABLE_SUFFIXES = {
    ".ps1", ".psm1", ".cmd", ".bat", ".exe", ".com", ".msi", ".dll",
    ".py", ".js", ".mjs", ".vbs", ".sh", ".hta", ".html", ".htm",
    ".reg", ".lnk", ".url", ".scr", ".jar", ".docm", ".xlsm", ".pptm",
}
_PLAIN_TEXT_SUFFIXES = {"", ".txt", ".md", ".log", ".rst"}


def _destination(arguments: dict[str, object]) -> str | None:
    url = arguments.get("url")
    if not isinstance(url, str):
        return None
    try:
        parsed = urlsplit(url)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            return None
        return parsed.hostname
    except ValueError:
        return None


def _text_file_risk(step: TaskStep, tool: AgentTool) -> RiskAssessment:
    name = step.arguments.get("name" if step.tool == "desktop.write_text" else "path")
    content = step.arguments.get("content", "" if step.tool == "desktop.write_text" else None)
    overwrite = step.arguments.get("overwrite", False)
    if not isinstance(name, str) or not name.strip() or not isinstance(content, str):
        return RiskAssessment(RiskLevel.REVIEW, "文件名或内容需人工核对")
    if overwrite is not False:
        return RiskAssessment(RiskLevel.HIGH, "计划包含覆盖已有文件")
    suffix = Path(name).suffix.casefold()
    if suffix in _EXECUTABLE_SUFFIXES or content.lstrip().startswith("#!"):
        return RiskAssessment(RiskLevel.HIGH, "将创建可执行或可被解释的内容")
    if suffix not in _PLAIN_TEXT_SUFFIXES or "<script" in content.casefold():
        return RiskAssessment(RiskLevel.REVIEW, "文件类型或内容需要人工核对")
    root = getattr(tool, "root", None)
    if not isinstance(root, Path):
        return RiskAssessment(RiskLevel.REVIEW, "无法确认文件目标目录")
    target = (root / name).resolve()
    if step.tool == "desktop.write_text" and target.parent != root:
        return RiskAssessment(RiskLevel.REVIEW, "桌面目标必须是单个文件名")
    if step.tool == "workspace.write_text" and not target.is_relative_to(root):
        return RiskAssessment(RiskLevel.REVIEW, "目标路径超出工作区")
    if target.exists():
        return RiskAssessment(RiskLevel.REVIEW, "目标文件已经存在")
    return RiskAssessment(RiskLevel.LOW, "新建普通文本文件，不覆盖已有内容")


def assess_step(step: TaskStep, tool: AgentTool | None, goal: str) -> RiskAssessment:
    if tool is None:
        return RiskAssessment(RiskLevel.HIGH, "工具不可用，需要重新规划")
    policy = POLICIES.get(step.tool)
    if policy is None:
        return RiskAssessment(RiskLevel.REVIEW, "未知工具默认不自动执行")
    unknown = set(step.arguments) - policy.arguments
    if unknown:
        return RiskAssessment(RiskLevel.REVIEW, f"存在未识别参数：{', '.join(sorted(unknown))}")
    destination = _destination(step.arguments)
    if step.tool.startswith("browser.") and not destination:
        return RiskAssessment(RiskLevel.REVIEW, "目标网址需要人工核对")
    if _HIGH_IMPACT_GOAL.search(goal):
        return RiskAssessment(RiskLevel.HIGH, "任务目标涉及发送、提交或其他高影响操作", destination)
    if policy.category == "text_file":
        return _text_file_risk(step, tool)
    if policy.category == "read":
        if step.tool == "browser.read_page":
            return RiskAssessment(RiskLevel.REVIEW, "网页可能包含私人内容，需确认读取目标", destination)
        return RiskAssessment(RiskLevel.LOW, "只读取当前数据", destination)
    if policy.category == "open_local":
        return RiskAssessment(RiskLevel.LOW, "只打开本机工作区窗口")
    if policy.category == "navigation":
        return RiskAssessment(RiskLevel.REVIEW, "打开网页可能触发站点操作，需确认目标", destination)
    if policy.category == "web_input":
        selector = step.arguments.get("selector")
        if isinstance(selector, str) and _SENSITIVE_FIELD.search(selector):
            return RiskAssessment(RiskLevel.HIGH, "目标输入框可能包含敏感信息", destination)
        return RiskAssessment(
            RiskLevel.REVIEW, "网页填写可能自动保存或触发脚本，即使没有点击提交", destination
        )
    if policy.category == "web_action":
        return RiskAssessment(RiskLevel.HIGH, "网页点击可能提交或改变站点数据", destination)
    if policy.category == "execute":
        return RiskAssessment(RiskLevel.HIGH, "运行检查会执行工作区中的代码")
    return RiskAssessment(RiskLevel.REVIEW, "将修改已有内容或创建结构化文件，需确认")
