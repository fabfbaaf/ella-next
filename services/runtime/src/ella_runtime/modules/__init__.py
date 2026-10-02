"""Module registry shown by the development API.

Status is explicit so a scaffold is never mistaken for a working integration.
"""

from pydantic import BaseModel


class ModuleInfo(BaseModel):
    id: str
    name: str
    status: str


MODULES = (
    ModuleInfo(id="M0", name="工程基础", status="scaffold"),
    ModuleInfo(id="M1", name="模型与人格", status="in_progress"),
    ModuleInfo(id="M2", name="长期记忆", status="in_progress"),
    ModuleInfo(id="M3", name="语音", status="in_progress"),
    ModuleInfo(id="M4", name="任务 Agent", status="in_progress"),
    ModuleInfo(id="M5", name="应用适配", status="in_progress"),
    ModuleInfo(id="M6", name="游戏平台", status="in_progress"),
    ModuleInfo(id="M7", name="游戏适配", status="planned"),
    ModuleInfo(id="M8", name="陪伴与界面", status="in_progress"),
)
