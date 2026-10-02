"""Minimal API boundary for the new runtime.

Feature routes are added by their owning modules as they are implemented.
"""

import asyncio
import base64
import binascii
import json
import logging
import os
import re
import subprocess
from contextlib import asynccontextmanager
from datetime import UTC, date, datetime
from functools import lru_cache
from pathlib import Path
from typing import Annotated, Literal
from weakref import WeakSet

from fastapi import Depends, FastAPI, Header, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel, Field

from ella_runtime import __version__
from ella_runtime.modules import MODULES
from ella_runtime.modules.agent.engine import AgentEngine, AgentError
from ella_runtime.modules.agent.store import TaskStore
from ella_runtime.modules.applications.browser import (
    BrowserClickTool,
    BrowserFillTool,
    BrowserObserveTool,
    BrowserOpenTool,
    BrowserReadTool,
    BrowserSession,
    BrowserUnavailable,
)
from ella_runtime.modules.applications.coding import (
    GitReviewTool,
    ListProjectFilesTool,
    OpenCodeWorkspaceTool,
    ReadProjectFileTool,
    ReplaceProjectTextTool,
    RunProjectCheckTool,
)
from ella_runtime.modules.applications.launcher import ApplicationLaunchError, launch_office
from ella_runtime.modules.applications.office_files import (
    DocumentTool,
    PresentationTool,
    SpreadsheetTool,
)
from ella_runtime.modules.applications.office_live import (
    EditOpenSpreadsheetTool,
    ReadOpenSpreadsheetTool,
    ReplaceOpenDocumentTextTool,
)
from ella_runtime.modules.applications.web_access import (
    BrowserSearchTool,
    WebResearch,
    inspect_page,
)
from ella_runtime.modules.applications.workspace_text import DesktopTextTool, WorkspaceTextTool
from ella_runtime.modules.backup import MAX_ARCHIVE_BYTES, BackupError, BackupService
from ella_runtime.modules.companion.dialogue import DialogueActions
from ella_runtime.modules.companion.store import CompanionStore
from ella_runtime.modules.games.adapters.bannerlord.gabs import BannerlordGabsBridge
from ella_runtime.modules.games.adapters.bannerlord.gabs_client import GabsError, find_gabs_exe
from ella_runtime.modules.games.bridge import (
    GAME_NAMES,
    GameBridgeError,
    LocalGameBridge,
    bridge_url,
)
from ella_runtime.modules.games.contracts import GameAction
from ella_runtime.modules.games.controller import GameControlError, GameController
from ella_runtime.modules.games.journal import GameJournal
from ella_runtime.modules.games.launch import (
    GameLaunchCoordinator,
    game_executable,
    installation_status,
)
from ella_runtime.modules.games.play import GamePlayError, GamePlayManager
from ella_runtime.modules.games.push_bridge import PUSH_GAMES, PushGameBridge
from ella_runtime.modules.games.screen_chat import (
    ForegroundWindowCapture,
    GameScreenChat,
    ScreenCaptureError,
)
from ella_runtime.modules.games.setup import GameSetupManager
from ella_runtime.modules.games.setup_service import GameSetupService
from ella_runtime.modules.memory.capture import MemoryCapture
from ella_runtime.modules.memory.contracts import (
    MemoryCorrection,
    MemoryCreate,
    MemoryKind,
    MemorySource,
)
from ella_runtime.modules.memory.retrieval import (
    EmbeddingProvider,
    EmbeddingSettings,
    HybridMemoryRetriever,
)
from ella_runtime.modules.memory.store import MemoryStore
from ella_runtime.modules.memory.summary import MemorySummaryExtractor
from ella_runtime.modules.models.config_store import (
    ModelConfigError,
    ModelConfigInput,
    ModelConfigStore,
    ModelSlot,
)
from ella_runtime.modules.models.contracts import ModelPurpose, TokenUsage
from ella_runtime.modules.models.conversations import ConversationService, ConversationStore
from ella_runtime.modules.models.gateway import ModelGateway
from ella_runtime.modules.models.provider import ModelProviderError
from ella_runtime.modules.models.usage_store import UsageStore
from ella_runtime.modules.voice.config_store import (
    VoiceConfigError,
    VoiceConfigInput,
    VoiceConfigStore,
    VoiceSlot,
)
from ella_runtime.modules.voice.context import VoiceContext
from ella_runtime.modules.voice.fish import (
    CombinedVoiceProvider,
    FishSpeechProvider,
    FishSpeechSettings,
)
from ella_runtime.modules.voice.history import VoiceHistory
from ella_runtime.modules.voice.provider import (
    OpenAICompatibleVoiceProvider,
    VoiceProviderError,
)
from ella_runtime.modules.voice.session import VoiceInterrupted, VoiceSession
from ella_runtime.modules.voice.streaming import StreamingVoiceTurn
from ella_runtime.runtime_session import RuntimeSession, loopback_host_allowed
from ella_runtime.storage_paths import default_data_dir

TRUSTED_ORIGINS = {
    "http://127.0.0.1:1421",
    "http://localhost:1421",
    "http://tauri.localhost",
    "tauri://localhost",
}
_game_controller_instances: WeakSet[GameController] = WeakSet()
MAX_REQUEST_BYTES = 12 * 1024 * 1024

@lru_cache
def get_runtime_session() -> RuntimeSession:
    return RuntimeSession()


@lru_cache
def get_backup_service() -> BackupService:
    return BackupService()


@asynccontextmanager
async def runtime_lifespan(_app: FastAPI):
    # Restore before opening stores or publishing a new desktop credential.
    backup = get_backup_service()
    session = None
    try:
        await asyncio.to_thread(backup.apply_pending_before_start)
        session = get_runtime_session()
        session.publish()
        setup = get_game_setup_service()
        if os.getenv("ELLA_GAME_SETUP_AUTO", "1").strip().lower() not in {"0", "false", "off"}:
            setup.start_auto()
        yield
    finally:
        # One failed shutdown must not leave another job using data after the
        # process lease is released. Close producers before their dependencies.
        for getter in (
            get_game_setup_service, get_dialogue_actions,
            get_game_launch_coordinator, get_game_play_manager,
            get_conversation_service,
        ):
            if getter.cache_info().currsize:
                try:
                    await getter().aclose()
                except Exception:  # noqa: BLE001 - continue closing all resources
                    logger.error("A runtime background service failed to close")
                finally:
                    getter.cache_clear()
        for controller in list(_game_controller_instances):
            if isinstance(controller.adapter, BannerlordGabsBridge):
                try:
                    await controller.adapter.client.close()
                except Exception:  # noqa: BLE001 - continue closing other clients
                    logger.error("A game client failed to close")
        for getter, method in (
            (get_browser_session, "close"), (get_memory_retriever, "aclose"),
        ):
            if getter.cache_info().currsize:
                try:
                    await getattr(getter(), method)()
                except Exception:  # noqa: BLE001 - release remaining resources
                    logger.error("A runtime resource failed to close")
                finally:
                    getter.cache_clear()
        try:
            if session is not None:
                session.close()
        finally:
            backup.close()
            get_backup_service.cache_clear()


app = FastAPI(title="Ella Next Runtime", version=__version__, lifespan=runtime_lifespan)
logger = logging.getLogger(__name__)
app.add_middleware(
    CORSMiddleware,
    allow_origins=sorted(TRUSTED_ORIGINS),
    allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE"],
    allow_headers=["*"],
)


@app.middleware("http")
async def authorize_runtime_request(request: Request, call_next):
    port = int(os.getenv("ELLA_RUNTIME_PORT", "8766"))
    if not loopback_host_allowed(request.headers.get("host"), port=port):
        return JSONResponse(status_code=400, content={"detail": "运行时仅接受本机地址"})
    origin = request.headers.get("origin")
    if origin is not None and origin not in TRUSTED_ORIGINS:
        return JSONResponse(status_code=403, content={"detail": "不受信任的请求来源"})
    if (
        request.url.path.startswith("/api/")
        and not request.url.path.startswith("/api/game-plugins/")
        and request.method != "OPTIONS"
        and not get_runtime_session().accepts_bearer(request.headers.get("authorization"))
    ):
        return JSONResponse(
            status_code=401,
            content={"detail": "桌面会话凭据缺失或已失效"},
            headers={"WWW-Authenticate": "Bearer"},
        )
    content_length = request.headers.get("content-length")
    if content_length is not None:
        if not content_length.isdecimal():
            return JSONResponse(status_code=400, content={"detail": "请求长度无效"})
        if int(content_length) > MAX_REQUEST_BYTES:
            return JSONResponse(status_code=413, content={"detail": "请求正文超过 12 MB"})
    if request.method not in {"GET", "HEAD", "OPTIONS"}:
        body = bytearray()
        async for chunk in request.stream():
            body.extend(chunk)
            if len(body) > MAX_REQUEST_BYTES:
                return JSONResponse(status_code=413, content={"detail": "请求正文超过 12 MB"})
        request._body = bytes(body)
    return await call_next(request)


@lru_cache
def get_usage_store() -> UsageStore:
    return UsageStore()


@lru_cache
def get_memory_store() -> MemoryStore:
    return MemoryStore()


@lru_cache
def get_memory_retriever() -> HybridMemoryRetriever:
    return HybridMemoryRetriever(get_memory_store(), EmbeddingProvider(EmbeddingSettings.from_env()))


@lru_cache
def get_model_config_store() -> ModelConfigStore:
    return ModelConfigStore()


@lru_cache
def get_voice_config_store() -> VoiceConfigStore:
    return VoiceConfigStore()


@lru_cache
def get_voice_session() -> VoiceSession:
    store = get_voice_config_store()
    asr, tts = store.asr_settings(), store.tts_settings()
    recognizer = OpenAICompatibleVoiceProvider(asr)
    speaker = FishSpeechProvider(tts) if isinstance(tts, FishSpeechSettings) else OpenAICompatibleVoiceProvider(tts)
    provider = CombinedVoiceProvider(recognizer, speaker)
    can_speak = tts.enabled if isinstance(tts, FishSpeechSettings) else tts.can_speak
    gateway = ModelGateway(
        get_model_config_store().settings, get_usage_store(),
        memory_retriever=get_memory_retriever(), web_context=get_web_research().context,
    )
    return VoiceSession(
        provider, gateway, get_usage_store(), tts=can_speak,
        dialogue_actions=get_dialogue_actions(),
        context=VoiceContext(ConversationStore()),
        history=VoiceHistory(ConversationStore(), MemorySummaryExtractor(gateway, get_memory_store())),
    )


@lru_cache
def get_agent_engine() -> AgentEngine:
    configured_root = os.getenv("ELLA_AGENT_WORKSPACE")
    workspace = Path(configured_root).expanduser() if configured_root else None
    configured_code_root = os.getenv("ELLA_CODE_WORKSPACE")
    code_workspace = Path(configured_code_root).expanduser() if configured_code_root else workspace
    browser = get_browser_session()
    return AgentEngine(
        ModelGateway(
            get_model_config_store().settings,
            get_usage_store(),
            memory_retriever=get_memory_retriever(),
        ),
        TaskStore(),
        [
            WorkspaceTextTool(workspace),
            DesktopTextTool(),
            ListProjectFilesTool(code_workspace),
            OpenCodeWorkspaceTool(code_workspace),
            ReadProjectFileTool(code_workspace),
            ReplaceProjectTextTool(code_workspace),
            RunProjectCheckTool(code_workspace),
            GitReviewTool(code_workspace),
            BrowserOpenTool(browser),
            BrowserReadTool(browser),
            BrowserObserveTool(browser),
            BrowserSearchTool(browser),
            BrowserFillTool(browser),
            BrowserClickTool(browser),
            SpreadsheetTool(workspace),
            DocumentTool(workspace),
            PresentationTool(workspace),
            ReadOpenSpreadsheetTool(),
            EditOpenSpreadsheetTool(),
            ReplaceOpenDocumentTextTool(),
        ],
    )


@lru_cache
def get_web_research() -> WebResearch:
    return WebResearch(get_browser_session())


@lru_cache
def get_browser_session() -> BrowserSession:
    return BrowserSession()


@lru_cache
def get_conversation_service() -> ConversationService:
    gateway = ModelGateway(
        get_model_config_store().settings,
        get_usage_store(),
        memory_retriever=get_memory_retriever(),
        web_context=get_web_research().context,
    )
    return ConversationService(
        gateway,
        ConversationStore(),
        summarizer=MemorySummaryExtractor(gateway, get_memory_store()),
        dialogue_actions=get_dialogue_actions(),
    )


@lru_cache
def get_companion_store() -> CompanionStore:
    return CompanionStore(timezone=os.getenv("ELLA_TIMEZONE", "Asia/Shanghai"))


def queue_progress(source_type: str, source_id: str, stage: str, text: str) -> None:
    get_companion_store().record_progress(source_type, source_id, stage, text)


@lru_cache
def get_dialogue_actions() -> DialogueActions:
    return DialogueActions(
        ModelGateway(get_model_config_store().settings, get_usage_store()),
        get_companion_store(), get_agent_engine(),
    )


@lru_cache
def get_screen_chat() -> GameScreenChat:
    return GameScreenChat(
        ForegroundWindowCapture(),
        ModelGateway(
            get_model_config_store().settings,
            get_usage_store(),
            memory_retriever=get_memory_retriever(),
        ),
    )


@lru_cache
def get_game_controller(game_id: str) -> GameController:
    if game_id == "bannerlord":
        adapter = BannerlordGabsBridge()
        adapter.client.launch_executable_provider = (
            lambda: get_game_setup_service().manager.launch_path("bannerlord")
        )
    elif (base_url := bridge_url(game_id)) is not None:
        token = os.getenv(f"ELLA_GAME_{game_id.upper()}_BRIDGE_TOKEN") or None
        adapter = LocalGameBridge(game_id, base_url, token=token)
    elif game_id in PUSH_GAMES:
        adapter = get_push_bridge(game_id)
    else:
        raise GameBridgeError("这款游戏尚未配置接口桥接")
    controller = GameController(adapter, GameJournal())
    _game_controller_instances.add(controller)
    return controller


@lru_cache
def get_push_bridge(game_id: str) -> PushGameBridge:
    return PushGameBridge(game_id)


@lru_cache
def get_game_play_manager() -> GamePlayManager:
    return GamePlayManager(
        ModelGateway(get_model_config_store().settings, get_usage_store()),
        progress=lambda game_id, stage, message: queue_progress(
            "game", game_id, stage, message
        ),
    )


_paused_games: set[str] = set()


class TaskCreate(BaseModel):
    goal: str = Field(min_length=1, max_length=2000)
    auto_run_safe: bool = False


class PlanRevision(BaseModel):
    plan_hash: str = Field(min_length=64, max_length=64)
    steps: list[dict] = Field(min_length=1, max_length=12)


class PlanApproval(BaseModel):
    plan_hash: str = Field(min_length=64, max_length=64)


class ChatRequest(BaseModel):
    message: str = Field(min_length=1, max_length=10000)
    conversation_id: str | None = None


class ChatArchive(BaseModel):
    archived: bool


class ReminderCreate(BaseModel):
    title: str = Field(min_length=1, max_length=300)
    due_at: datetime


class QuietHours(BaseModel):
    start: str
    end: str


class ScreenChatRequest(BaseModel):
    game_id: str = Field(min_length=1, max_length=100)
    window_title: str = Field(min_length=3, max_length=200)
    message: str = Field(min_length=1, max_length=2000)


class VoicePlayback(BaseModel):
    turn_id: str
    played_ratio: float = Field(ge=0, le=1)


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok", "version": __version__}


@app.get("/api/modules")
def modules() -> list[dict[str, str]]:
    return [module.model_dump() for module in MODULES]


class BrowserSettingsRequest(BaseModel):
    channel: str = Field(pattern="^(auto|msedge|chrome)$")
    auto_web: bool = True


class BrowserNavigation(BaseModel):
    url: str = Field(min_length=1, max_length=2000)


class BrowserSearchRequest(BaseModel):
    query: str = Field(min_length=1, max_length=300)


@app.get("/api/browser/status")
def browser_status() -> dict[str, object]:
    status = get_browser_session().status()
    research = get_web_research()
    return {**status, "last_query": research.last_query, "last_sources": research.last_sources,
            "last_error": research.last_error}


@app.put("/api/browser/settings")
async def browser_settings(payload: BrowserSettingsRequest) -> dict[str, object]:
    session = get_browser_session()
    return await session.configure(channel=payload.channel, auto_web=payload.auto_web)


@app.post("/api/browser/open")
async def browser_open(payload: BrowserNavigation) -> dict[str, object]:
    session = get_browser_session()
    try:
        return await BrowserOpenTool(session).open_and_inspect({"url": payload.url}, inspector=inspect_page)
    except (BrowserUnavailable, ValueError, TypeError) as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=409, detail="网页连接失败或超时") from exc


@app.get("/api/browser/page")
async def browser_page() -> dict[str, object]:
    session = get_browser_session()
    async with session.operation_lock:
        if session.page is None or session.page.is_closed():
            raise HTTPException(status_code=409, detail="请先打开受控浏览器")
        try:
            return await inspect_page(session.page)
        except Exception as exc:
            raise HTTPException(status_code=409, detail="当前网页暂时无法读取") from exc


@app.post("/api/browser/search")
async def browser_search(payload: BrowserSearchRequest) -> dict[str, object]:
    try:
        evidence = await BrowserSearchTool(get_browser_session()).execute({"query": payload.query}, action_id="admin-search")
        return evidence.details
    except (BrowserUnavailable, ValueError) as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=409, detail="搜索失败或超时，请检查网络或搜索页验证") from exc


@app.post("/api/browser/close", status_code=204)
async def browser_close() -> None:
    await get_browser_session().close()


@app.get("/api/applications/catalog")
async def application_catalog() -> dict[str, object]:
    from ella_runtime.modules.applications.launcher import _office_path
    result: dict[str, object] = {}
    for name, executable in (("excel", "EXCEL.EXE"), ("word", "WINWORD.EXE")):
        try:
            await asyncio.to_thread(_office_path, executable)
            result[name] = {"installed": True, "detail": "已找到本机应用，操作结果需逐项核验"}
        except ApplicationLaunchError:
            result[name] = {"installed": False, "detail": "未找到本机桌面 Office，仍可创建文件"}
    result["code"] = {"installed": True, "detail": "已接入当前工作区的开发工具"}
    return result


@app.post("/api/applications/{app_id}/launch")
async def launch_application(app_id: str) -> dict[str, str]:
    if app_id == "browser":
        try:
            session = get_browser_session()
            async with session.operation_lock:
                page = await session.ensure_page()
        except BrowserUnavailable as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return {"application": "browser", "state": "launched", "url": page.url}
    if app_id in {"excel", "word"}:
        try:
            return await launch_office(app_id)
        except ApplicationLaunchError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
    raise HTTPException(status_code=404, detail="应用未接入")


@app.get("/api/models/status")
def model_status(
    store: Annotated[ModelConfigStore, Depends(get_model_config_store)],
) -> dict[str, object]:
    try:
        return store.public_status()
    except ModelConfigError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


@app.post("/api/models/{slot}/probe")
async def model_probe(slot: ModelSlot, store: Annotated[ModelConfigStore, Depends(get_model_config_store)]) -> dict[str, object]:
    from ella_runtime.modules.models.diagnostics import probe_model
    try:
        settings = store.settings()
        effective_slot = settings.effective_slot(slot)
        config = getattr(settings, effective_slot)
        if config is None:
            raise ValueError("此路由未配置")
        result = await probe_model(config)
        return {**result, "effective_slot": effective_slot}
    except (ValueError, ModelConfigError) as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc


@app.post("/api/models/{slot}/test")
async def model_generation_test(
    slot: ModelSlot,
    store: Annotated[ModelConfigStore, Depends(get_model_config_store)],
    usage_store: Annotated[UsageStore, Depends(get_usage_store)],
    mode: Literal["text", "stream"] = "text",
) -> dict[str, object]:
    from ella_runtime.modules.models.diagnostics import run_model_test
    try:
        settings = store.settings()
        effective_slot = settings.effective_slot(slot)
        config = getattr(settings, effective_slot)
        if config is None:
            raise ValueError("此路由未配置")
        async with asyncio.timeout(30):
            result = await run_model_test(config, mode=mode)
        usage = TokenUsage.model_validate(result["usage"])
        if slot == "action":
            usage = usage.model_copy(update={"purpose": ModelPurpose.ACTION})
        usage_store.record(usage)
        return {**result, "usage": usage.model_dump(mode="json"), "mode": mode, "effective_slot": effective_slot}
    except TimeoutError as exc:
        raise HTTPException(status_code=502, detail="模型生成检查超过 30 秒，请核对服务响应速度") from exc
    except (ValueError, ModelConfigError, ModelProviderError) as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc


@app.put("/api/models/config/{slot}")
def save_model_config(
    slot: ModelSlot,
    value: ModelConfigInput,
    store: Annotated[ModelConfigStore, Depends(get_model_config_store)],
) -> dict[str, object]:
    try:
        return store.save(slot, value)
    except ModelConfigError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


@app.delete("/api/models/config/{slot}")
def reset_model_config(
    slot: ModelSlot,
    store: Annotated[ModelConfigStore, Depends(get_model_config_store)],
) -> dict[str, object]:
    try:
        return store.reset(slot)
    except ModelConfigError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


@app.get("/api/conversations/{identity}/task")
def conversation_task(identity: str) -> dict[str, object]:
    router = get_dialogue_actions()
    binding = router._binding(identity)
    if not binding:
        return {"task": None}
    try:
        task = router.agent.store.get(binding["task_id"])
    except KeyError:
        return {"task": None}
    return {"task": task.model_dump(mode="json"), "summary": router._status(task)}


@app.get("/api/conversations")
def list_conversations(
    service: Annotated[ConversationService, Depends(get_conversation_service)],
) -> list[dict[str, str | int | None]]:
    return service.store.list_chats()


@app.post("/api/conversations", status_code=201)
def create_conversation(
    service: Annotated[ConversationService, Depends(get_conversation_service)],
) -> dict[str, str | int | None]:
    return service.store.chat_metadata(service.store.create())


@app.patch("/api/conversations/{identity}/archive")
def archive_conversation(
    identity: str, request: ChatArchive,
    service: Annotated[ConversationService, Depends(get_conversation_service)],
) -> dict[str, str | int | None]:
    try:
        return service.store.set_archived(identity, request.archived)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="对话不存在") from exc


@app.post("/api/chat")
async def chat(
    request: ChatRequest,
    service: Annotated[ConversationService, Depends(get_conversation_service)],
    companion: Annotated[CompanionStore, Depends(get_companion_store)],
) -> dict[str, str | bool]:
    try:
        identity, reply = await service.reply(request.message, request.conversation_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="对话不存在") from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except ModelProviderError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    companion.touch()
    memory_saved = (
        MemoryCapture(get_memory_store()).capture(request.message, source_ref=f"chat:{identity}")
        if MemoryCapture.matchable(request.message)
        else False
    )
    return {"conversation_id": identity, "reply": reply, "memory_saved": memory_saved}


@app.get("/api/companion/status")
def companion_status(
    store: Annotated[CompanionStore, Depends(get_companion_store)],
) -> dict[str, object]:
    return store.status()


@app.post("/api/companion/feed")
def feed_companion(
    store: Annotated[CompanionStore, Depends(get_companion_store)],
) -> dict[str, object]:
    return store.feed()


@app.post("/api/companion/poll")
def poll_companion(
    store: Annotated[CompanionStore, Depends(get_companion_store)],
) -> list[dict[str, str]]:
    return store.poll_events()


@app.get("/api/companion/notifications")
def companion_notifications(
    store: Annotated[CompanionStore, Depends(get_companion_store)],
) -> list[dict[str, str | None]]:
    return store.list_notifications()


@app.post("/api/companion/notifications/{identity}/read", status_code=204)
def mark_companion_notification_read(
    identity: str, store: Annotated[CompanionStore, Depends(get_companion_store)],
) -> None:
    if not store.mark_notification_read(identity):
        raise HTTPException(status_code=404, detail="通知不存在")


@app.patch("/api/companion/quiet-hours")
def set_quiet_hours(
    settings: QuietHours, store: Annotated[CompanionStore, Depends(get_companion_store)]
) -> dict[str, object]:
    try:
        return store.set_quiet_hours(settings.start, settings.end)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@app.get("/api/reminders")
def list_reminders(
    store: Annotated[CompanionStore, Depends(get_companion_store)],
) -> list[dict[str, str | None]]:
    return store.list_reminders()


@app.post("/api/reminders", status_code=201)
def create_reminder(
    reminder: ReminderCreate,
    store: Annotated[CompanionStore, Depends(get_companion_store)],
) -> dict[str, str | None]:
    try:
        return store.add_reminder(reminder.title, reminder.due_at)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@app.delete("/api/reminders/{identity}", status_code=204)
def delete_reminder(
    identity: str, store: Annotated[CompanionStore, Depends(get_companion_store)]
) -> None:
    if not store.delete_reminder(identity):
        raise HTTPException(status_code=404, detail="提醒不存在")


@app.post("/api/games/screen-chat")
async def screen_chat(
    request: ScreenChatRequest,
    service: Annotated[GameScreenChat, Depends(get_screen_chat)],
) -> dict[str, str]:
    try:
        title, answer = await service.ask(
            game_id=request.game_id,
            window_title=request.window_title,
            message=request.message,
        )
    except ScreenCaptureError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except ModelProviderError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    return {"window_title": title, "reply": answer}


class PluginState(BaseModel):
    state: dict[str, object]


class PluginReceipt(BaseModel):
    action_id: str = Field(min_length=1, max_length=100)
    verified: bool
    state: dict[str, object]


class PluginEvent(BaseModel):
    kind: str = Field(alias="type", min_length=1, max_length=100)
    payload: dict[str, object] = Field(default_factory=dict)


class CompletionCondition(BaseModel):
    path: list[str] = Field(min_length=1, max_length=12)
    op: str = Field(pattern="^(eq|gte|lte|contains|near)$")
    value: object
    tolerance: float = Field(default=0, ge=0, allow_inf_nan=False)


class GamePlayConfirmation(BaseModel):
    confirmed: bool


class GamePlayRequest(BaseModel):
    goal: str = Field(min_length=1, max_length=500)
    max_steps: int = Field(default=60, ge=1, le=200)
    completion_conditions: list[CompletionCondition] = Field(default_factory=list, max_length=10)


def authenticated_plugin(game_id: str, authorization: str | None) -> PushGameBridge:
    if game_id not in PUSH_GAMES:
        raise HTTPException(status_code=404, detail="游戏插件不存在")
    bridge = get_push_bridge(game_id)
    try:
        bridge.authenticate(authorization)
    except GameBridgeError as exc:
        raise HTTPException(status_code=401, detail=str(exc)) from exc
    return bridge


@app.post("/api/game-plugins/{game_id}/state")
async def plugin_state(
    game_id: str, payload: PluginState, authorization: str | None = Header(default=None)
) -> dict[str, str]:
    bridge = authenticated_plugin(game_id, authorization)
    try:
        await bridge.publish(payload.state)
    except GameBridgeError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return {"status": "ok"}


@app.post("/api/game-plugins/{game_id}/events")
def plugin_event(
    game_id: str, payload: PluginEvent, authorization: str | None = Header(default=None)
) -> dict[str, str]:
    authenticated_plugin(game_id, authorization)
    messages = {
        "minecraft.player.damaged": "哎呀，掉血了！先留意生命值。",
        "minecraft.player.died": "啊，倒下了。我们先看看重生点和背包。",
        "minecraft.world.biome_changed": "换地形啦，看看附近有什么新东西。",
        "minecraft.world.dimension_changed": "跨维度了，先确认周围安全。",
        "stardew.player.damaged": "血量降了，矿洞里先别冒进。",
        "stardew.day.started": "新的一天！先看看天气和今天的农活。",
        "stardew.location.warped": "到新地方啦，我先看看周围。",
        "stardew.skill.level_up": "升级了！这次成长值得记一笔。",
        "stardew.story.started": "剧情开始了，我先认真听。",
        "stardew.festival.started": "节日开始了，今天可以轻松点。",
    }
    message = messages.get(payload.kind)
    if message:
        queue_progress("game", game_id, f"{payload.kind}:{datetime.now(UTC).timestamp()}", message)
    return {"status": "ok"}


@app.get("/api/game-plugins/{game_id}/commands/next")
async def plugin_next_command(
    game_id: str, authorization: str | None = Header(default=None),
    client_id: str | None = None, session_id: str | None = None,
) -> dict[str, object]:
    bridge = authenticated_plugin(game_id, authorization)
    try:
        action = await bridge.next_command(client_id, session_id)
    except GameBridgeError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return {"action": action.model_dump(mode="json") if action else None,
            "client_id": client_id, "session_id": session_id}


@app.post("/api/game-plugins/{game_id}/commands/receipt")
async def plugin_command_receipt(
    game_id: str, payload: PluginReceipt, authorization: str | None = Header(default=None)
) -> dict[str, str]:
    bridge = authenticated_plugin(game_id, authorization)
    try:
        await bridge.receipt(payload.action_id, payload.verified, payload.state)
    except GameBridgeError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return {"status": "ok"}


@lru_cache
def get_game_setup_service() -> GameSetupService:
    return GameSetupService(GameSetupManager())


class GameSetupLocation(BaseModel):
    path: str = Field(min_length=1, max_length=4096)
    profile: str | None = Field(default=None, max_length=200)


@app.get("/api/games/setup")
def game_setup_status() -> dict[str, object]:
    return get_game_setup_service().snapshot()


@app.post("/api/games/setup/scan", status_code=202)
async def scan_game_setup() -> dict[str, object]:
    service = get_game_setup_service()
    service.start()
    return service.snapshot()


@app.put("/api/games/{game_id}/setup/location", status_code=202)
async def select_game_setup_location(game_id: str, payload: GameSetupLocation) -> dict[str, object]:
    if game_id not in GAME_NAMES:
        raise HTTPException(status_code=404, detail="游戏未接入")
    service = get_game_setup_service()
    try:
        await service.select(game_id, payload.path, profile=payload.profile)
    except (ValueError, OSError) as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    if game_id == "bannerlord":
        for controller in list(_game_controller_instances):
            if isinstance(controller.adapter, BannerlordGabsBridge):
                await controller.adapter.client.close()
    return service.snapshot()


def game_installation_status(game_id: str) -> dict[str, object]:
    setup = get_game_setup_service().status(game_id)
    if setup.get("game_found") or setup.get("game_root"):
        path = get_game_setup_service().manager.launch_path(game_id)
        return {"installed": True, "executable": str(path) if path else None,
                "detail": setup.get("detail", "已检测到游戏")}
    return installation_status(game_id)


@app.get("/api/games/catalog")
async def game_catalog() -> list[dict[str, object]]:
    async def status(game_id: str, name: str) -> dict[str, object]:
        try:
            controller = get_game_controller(game_id)
            snapshot = await controller.observe()
            return {
                "id": game_id, "name": name, "bridge": "connected",
                "ready": snapshot.state.get("bridge_ready", True) is True,
                "mode": "api_control", "paused": game_id in _paused_games,
                "captured_at": snapshot.captured_at.isoformat(),
                "play": get_game_play_manager().status(game_id),
                "installation": game_installation_status(game_id),
                "launch": get_game_launch_coordinator().status(game_id),
            }
        except (GameBridgeError, GameControlError):
            try:
                configured = (
                    game_id in PUSH_GAMES
                    or (game_id == "bannerlord" and bool(
                        os.getenv("ELLA_GABS_HTTP") or find_gabs_exe()
                    ))
                    or (game_id != "bannerlord" and bridge_url(game_id) is not None)
                )
            except GameBridgeError:
                configured = True
            return {
                "id": game_id, "name": name,
                "bridge": "offline" if configured else "not_configured", "ready": False,
                "mode": "screen_chat", "paused": game_id in _paused_games,
                "play": get_game_play_manager().status(game_id),
                "installation": game_installation_status(game_id),
                "launch": get_game_launch_coordinator().status(game_id),
            }

    return list(await asyncio.gather(*(status(game_id, name) for game_id, name in GAME_NAMES.items())))


@lru_cache
def get_game_launch_coordinator() -> GameLaunchCoordinator:
    return GameLaunchCoordinator()


@app.get("/api/runtime/identity")
def runtime_identity() -> dict[str, object]:
    return {"application": "ella-next", "version": __version__, "pid": os.getpid(),
            "data_dir": str(default_data_dir().resolve())}


@app.post("/api/runtime/shutdown", status_code=202)
async def shutdown_runtime(request: Request) -> dict[str, str]:
    callback = getattr(request.app.state, "request_shutdown", None)
    if callback is None:
        raise HTTPException(status_code=409, detail="此运行方式不支持受控关闭")
    asyncio.get_running_loop().call_later(0.1, callback)
    return {"status": "shutting_down"}


@app.post("/api/games/{game_id}/launch-play")
async def launch_and_play(game_id: str, payload: GamePlayRequest) -> dict[str, object]:
    if game_id not in GAME_NAMES:
        raise HTTPException(status_code=404, detail="游戏未接入")
    if game_id in _paused_games:
        raise HTTPException(status_code=409, detail="请先恢复游戏操作，再启动一起玩")
    manager = get_game_play_manager()
    if manager.status(game_id)["status"] in {"running", "paused"}:
        raise HTTPException(status_code=409, detail="已有游玩任务，请先停止或恢复它")
    if not manager.gateway.settings.for_purpose("action").configured:
        raise HTTPException(status_code=409, detail="请先配置操作模型，再启动一起玩")
    try:
        return await get_game_launch_coordinator().begin(
            game_id, payload.goal, get_game_controller(game_id),
            lambda: launch_game(game_id), lambda: start_game_play(game_id, payload),
        )
    except (GameBridgeError, GamePlayError) as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@app.post("/api/games/{game_id}/launch-play/cancel")
async def cancel_launch_and_play(game_id: str) -> dict[str, object]:
    if game_id not in GAME_NAMES:
        raise HTTPException(status_code=404, detail="游戏未接入")
    await get_game_launch_coordinator().cancel(game_id)
    return get_game_launch_coordinator().status(game_id)


@app.post("/api/games/{game_id}/launch")
async def launch_game(game_id: str) -> dict[str, str]:
    if game_id not in GAME_NAMES:
        raise HTTPException(status_code=404, detail="游戏未接入")
    service = get_game_setup_service()
    setup = await service.prepare(game_id)
    if not setup.get("ready"):
        raise HTTPException(status_code=409, detail=str(setup.get("detail") or "游戏插件尚未准备好"))
    if game_id == "bannerlord":
        adapter = get_game_controller(game_id).adapter
        if isinstance(adapter, BannerlordGabsBridge):
            try:
                await adapter.client.start()
                await adapter.client.call_tool("games_start", {"gameId": "bannerlord"})
            except (GabsError, OSError) as exc:
                raise HTTPException(status_code=409, detail=f"骑砍启动失败：{exc}") from exc
            queue_progress(
                "game", game_id, "launched", f"{GAME_NAMES[game_id]}打开啦，我先看看现在是什么局面。"
            )
            return {"game_id": game_id, "state": "launched"}
    path = service.manager.launch_path(game_id) or game_executable(game_id)
    if path is None or not path.is_file() or path.suffix.lower() != ".exe":
        raise HTTPException(status_code=409, detail="请先配置该游戏的可执行文件路径")
    try:
        await asyncio.to_thread(subprocess.Popen, [str(path)], cwd=str(path.parent))
    except OSError as exc:
        raise HTTPException(status_code=409, detail="无法启动游戏") from exc
    queue_progress(
        "game", game_id, "launched", f"{GAME_NAMES[game_id]}打开啦，我先看看现在是什么局面。"
    )
    return {"game_id": game_id, "state": "launched"}


@app.get("/api/games/{game_id}/observe")
async def observe_game(game_id: str) -> dict[str, object]:
    try:
        return (await get_game_controller(game_id).observe()).model_dump(mode="json")
    except (GameBridgeError, GameControlError) as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


def bannerlord_controller() -> tuple[GameController, BannerlordGabsBridge]:
    controller = get_game_controller("bannerlord")
    if not isinstance(controller.adapter, BannerlordGabsBridge):
        raise GameBridgeError("骑砍高影响授权只支持 GABS 受控桥接")
    return controller, controller.adapter


@app.get("/api/games/bannerlord/high-impact")
async def bannerlord_high_impact_actions() -> dict[str, object]:
    try:
        _, adapter = bannerlord_controller()
        return await adapter.high_impact_actions()
    except GameBridgeError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@app.post("/api/games/bannerlord/high-impact/preview")
async def preview_bannerlord_high_impact(action: GameAction) -> dict[str, object]:
    try:
        _, adapter = bannerlord_controller()
        return await adapter.preview_high_impact(action)
    except GameBridgeError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@app.post("/api/games/bannerlord/high-impact/{preview_id}/execute")
async def execute_bannerlord_high_impact(preview_id: str) -> dict[str, object]:
    if "bannerlord" in _paused_games:
        raise HTTPException(status_code=409, detail="游戏操作已暂停")
    if get_game_play_manager().status("bannerlord")["status"] in {"running", "paused"}:
        raise HTTPException(status_code=409, detail="请先停止连续游玩，再确认高影响操作")
    try:
        controller, adapter = bannerlord_controller()
        result = await adapter.execute_high_impact(preview_id, controller)
        return result.model_dump(mode="json")
    except (GameBridgeError, GameControlError) as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@app.post("/api/games/{game_id}/actions")
async def act_in_game(game_id: str, action: GameAction) -> dict[str, object]:
    if game_id in _paused_games:
        raise HTTPException(status_code=409, detail="游戏操作已暂停")
    try:
        result = await get_game_controller(game_id).perform(action)
        if result.status.value == "verified":
            text = "这步走通了！我再看看游戏状态，别急着乱按。"
        else:
            text = "刚才这步结果不明，我先停下核对，免得重复操作。"
        queue_progress(
            "game", game_id, action.action_id, text
        )
        return result.model_dump(mode="json")
    except (GameBridgeError, GameControlError) as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@app.post("/api/games/{game_id}/actions/{action_id}/reconcile")
async def reconcile_game_action(game_id: str, action_id: str) -> dict[str, object]:
    try:
        result = await get_game_controller(game_id).reconcile(action_id)
        if result.status.value == "verified":
            queue_progress(
                "game", game_id, f"{action_id}:reconciled", "核对过了，刚才那步已经生效。"
            )
        return result.model_dump(mode="json")
    except (GameBridgeError, GameControlError, KeyError) as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@app.post("/api/games/{game_id}/play")
async def start_game_play(game_id: str, payload: GamePlayRequest) -> dict[str, object]:
    if game_id not in GAME_NAMES:
        raise HTTPException(status_code=404, detail="游戏未接入")
    if get_game_launch_coordinator().status(game_id)["status"] in {"launching", "waiting_for_save"}:
        raise HTTPException(status_code=409, detail="正在等待进入存档，请先取消等待")
    if game_id in _paused_games:
        raise HTTPException(status_code=409, detail="游戏操作已暂停")
    try:
        session = await get_game_play_manager().start(
            game_id, payload.goal, get_game_controller(game_id), max_steps=payload.max_steps,
            completion_conditions=[item.model_dump() for item in payload.completion_conditions]
        )
    except (GameBridgeError, GameControlError, GamePlayError) as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return session.public()


@app.get("/api/games/{game_id}/play")
def game_play_status(game_id: str) -> dict[str, object]:
    if game_id not in GAME_NAMES:
        raise HTTPException(status_code=404, detail="游戏未接入")
    return get_game_play_manager().status(game_id)


@app.post("/api/games/{game_id}/play/stop")
async def stop_game_play(game_id: str) -> dict[str, object]:
    if game_id not in GAME_NAMES:
        raise HTTPException(status_code=404, detail="游戏未接入")
    await get_game_launch_coordinator().cancel(game_id)
    await get_game_play_manager().stop(game_id)
    return get_game_play_manager().status(game_id)


@app.post("/api/games/{game_id}/play/confirm")
async def confirm_game_play(game_id: str, payload: GamePlayConfirmation) -> dict[str, object]:
    if game_id not in GAME_NAMES:
        raise HTTPException(status_code=404, detail="游戏未接入")
    if payload.confirmed is not True:
        raise HTTPException(status_code=422, detail="需要明确确认已完成目标")
    try:
        return (await get_game_play_manager().confirm(game_id, get_game_controller(game_id))).public()
    except (GamePlayError, GameBridgeError, GameControlError) as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@app.post("/api/games/{game_id}/{command}")
async def set_game_pause(game_id: str, command: str) -> dict[str, object]:
    if game_id not in GAME_NAMES or command not in {"pause", "resume"}:
        raise HTTPException(status_code=404, detail="游戏命令不存在")
    if command == "pause":
        await get_game_launch_coordinator().cancel(game_id)
        _paused_games.add(game_id)
        get_game_play_manager().pause(game_id)
    else:
        try:
            await get_game_play_manager().resume(game_id, get_game_controller(game_id))
        except (GamePlayError, GameBridgeError, GameControlError) as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        _paused_games.discard(game_id)
    return {"game_id": game_id, "paused": game_id in _paused_games}


@app.get("/api/chat/{identity}")
def chat_history(
    identity: str,
    service: Annotated[ConversationService, Depends(get_conversation_service)],
) -> dict[str, object]:
    try:
        return {
            "conversation_id": identity,
            "messages": [
                message.model_dump() for message in service.store.history(identity, limit=100)
            ],
        }
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="对话不存在") from exc


@app.delete("/api/chat/{identity}", status_code=204)
async def delete_chat(
    identity: str,
    service: Annotated[ConversationService, Depends(get_conversation_service)],
) -> None:
    if not await service.delete(identity):
        raise HTTPException(status_code=404, detail="对话不存在")


_active_voice_sockets = 0
_notification_audio: dict[str, dict[str, str]] = {}
_notification_lock = asyncio.Lock()


@app.exception_handler(RequestValidationError)
async def safe_validation_error(_request: Request, error: RequestValidationError):
    # Validation errors must never echo a submitted API key or audio body.
    return JSONResponse(status_code=422, content={"detail": [
        {"loc": list(item["loc"]), "type": item["type"], "msg": "输入格式无效"}
        for item in error.errors()
    ]})


@app.get("/api/voice/config")
def voice_config() -> dict[str, object]:
    try:
        return get_voice_config_store().public_status()
    except VoiceConfigError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


def require_voice_idle() -> None:
    if _active_voice_sockets or _notification_lock.locked():
        raise HTTPException(status_code=409, detail="请先关闭实时监听并等待播报结束，再保存语音配置")
    if get_voice_session.cache_info().currsize and get_voice_session().state.value in {"transcribing", "thinking", "speaking"}:
        raise HTTPException(status_code=409, detail="本轮语音仍在处理，请结束后保存配置")


@app.put("/api/voice/config/{slot}")
async def save_voice_config(slot: VoiceSlot, payload: VoiceConfigInput) -> dict[str, object]:
    require_voice_idle()
    try:
        result = get_voice_config_store().save(slot, payload)
    except VoiceConfigError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    get_voice_session.cache_clear()
    _notification_audio.clear()
    return result


@app.delete("/api/voice/config/{slot}")
async def reset_voice_config(slot: VoiceSlot) -> dict[str, object]:
    require_voice_idle()
    try:
        result = get_voice_config_store().reset(slot)
    except VoiceConfigError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    get_voice_session.cache_clear()
    _notification_audio.clear()
    return result


def require_backup_idle() -> None:
    require_voice_idle()
    if get_agent_engine.cache_info().currsize and get_agent_engine().store.has_running():
        raise HTTPException(status_code=409, detail="请等待当前任务结束后操作备份")
    if get_game_play_manager.cache_info().currsize:
        manager = get_game_play_manager()
        if any(manager.status(game_id)["status"] == "running" for game_id in GAME_NAMES):
            raise HTTPException(status_code=409, detail="请先暂停游戏操作后使用备份")


class BackupPreviewRequest(BaseModel):
    archive_base64: str = Field(min_length=1, max_length=4 * ((MAX_ARCHIVE_BYTES + 2) // 3))


class BackupConfirmation(BaseModel):
    id: str = Field(pattern="^[a-f0-9]{32}$")


@app.get("/api/backup/status")
def backup_status() -> dict:
    try:
        return get_backup_service().status()
    except BackupError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.get("/api/backup/export")
def backup_export() -> FileResponse:
    require_backup_idle()
    try:
        path = get_backup_service().export_archive()
        return FileResponse(path, media_type="application/zip", filename=path.name)
    except BackupError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/api/backup/preview")
def backup_preview(payload: BackupPreviewRequest) -> dict:
    require_backup_idle()
    try:
        raw_zip = base64.b64decode(payload.archive_base64, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise HTTPException(status_code=400, detail="备份文件编码无效") from exc
    if not raw_zip or len(raw_zip) > MAX_ARCHIVE_BYTES:
        raise HTTPException(status_code=413, detail="备份 ZIP 不能为空或超过 8 MiB")
    try:
        return get_backup_service().stage_restore(raw_zip)
    except BackupError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/api/backup/confirm")
def backup_confirm(payload: BackupConfirmation) -> dict:
    require_backup_idle()
    try:
        return get_backup_service().confirm_restore(payload.id)
    except BackupError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.delete("/api/backup/staged/{identity}", status_code=204)
def backup_cancel(identity: str) -> None:
    try:
        get_backup_service().cancel_restore(identity)
    except BackupError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


class VoiceContextRequest(BaseModel):
    conversation_id: str | None = Field(default=None, max_length=100)


@app.get("/api/voice/context")
def voice_context() -> dict[str, str | None]:
    return {"conversation_id": get_voice_session().context.get_context_source()}


@app.put("/api/voice/context")
def set_voice_context(payload: VoiceContextRequest) -> dict[str, str | None]:
    require_voice_idle()
    try:
        session = get_voice_session()
        previous = session.context.get_context_source()
        source = session.context.set_context_source(payload.conversation_id)
        try:
            get_dialogue_actions().handoff_reference(source, voice_id=session.history.conversation_id if session.history else "voice-main")
        except ValueError:
            session.context.set_context_source(previous)
            raise
        return {"conversation_id": source}
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@app.post("/api/voice/preview")
async def voice_preview() -> dict[str, str]:
    require_voice_idle()
    async with _notification_lock:
        session = get_voice_session()
        if not session.tts:
            raise HTTPException(status_code=409, detail="请先保存完整的语音合成配置")
        try:
            speech = await session.provider.synthesize("来了？我在。这个声音听起来怎么样？", task_id="voice-preview")
            if not speech.audio or len(speech.audio) > 12 * 1024 * 1024:
                raise VoiceProviderError("试听音频为空或过大")
            get_usage_store().record(speech.usage)
            return {"audio_base64": base64.b64encode(speech.audio).decode(), "media_type": speech.media_type}
        except VoiceProviderError as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc


@app.get("/api/voice/status")
def voice_status() -> dict[str, object]:
    try:
        status = get_voice_config_store().voice_status()
    except VoiceConfigError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    state = get_voice_session().state.value if get_voice_session.cache_info().currsize else "idle"
    return {**status, "state": state}


class SpokenReceipt(BaseModel):
    status: str = Field(pattern="^(played|failed)$")
    error: str | None = Field(default=None, max_length=300)


@app.post("/api/companion/notifications/{identity}/speech")
async def notification_speech(identity: str) -> dict[str, str]:
    store = get_companion_store()
    try:
        note = store.get_notification(identity)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="通知不存在") from exc
    if note["read_at"] or note["spoken_at"] or store.status()["quiet_now"]:
        raise HTTPException(status_code=409, detail="通知已处理或当前处于安静时段")
    session = get_voice_session()
    if not session.tts:
        raise HTTPException(status_code=409, detail="语音合成尚未配置，请在后台语音页设置")
    if session.state.value in {"transcribing", "thinking", "speaking"}:
        raise HTTPException(status_code=409, detail="正在对话，请稍后播报通知")
    async with _notification_lock:
        try:
            note = store.get_notification(identity)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="通知不存在") from exc
        if note["read_at"] or note["spoken_at"] or store.status()["quiet_now"]:
            raise HTTPException(status_code=409, detail="通知已处理或当前处于安静时段")
        session = get_voice_session()
        if not session.tts:
            raise HTTPException(status_code=409, detail="语音合成尚未配置，请在后台语音页设置")
        if session.state.value in {"transcribing", "thinking", "speaking"}:
            raise HTTPException(status_code=409, detail="正在对话，请稍后播报通知")
        if identity in _notification_audio:
            return _notification_audio[identity]
        try:
            speech = await session.provider.synthesize(str(note["text"]), task_id=f"notification:{identity}")
        except VoiceProviderError as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc
        if not speech.audio or len(speech.audio) > 12 * 1024 * 1024:
            raise HTTPException(status_code=502, detail="通知音频为空或超过限制")
        get_usage_store().record(speech.usage)
        result = {"audio_base64": base64.b64encode(speech.audio).decode(), "media_type": speech.media_type, "text": str(note["text"])}
        if len(_notification_audio) >= 32:
            _notification_audio.pop(next(iter(_notification_audio)))
        _notification_audio[identity] = result
        return result


@app.post("/api/companion/notifications/{identity}/spoken")
def notification_spoken(identity: str, receipt: SpokenReceipt) -> dict[str, bool]:
    changed = get_companion_store().speech_receipt(identity, receipt.status, receipt.error)
    if not changed:
        raise HTTPException(status_code=404, detail="通知不存在")
    if receipt.status == "played":
        _notification_audio.pop(identity, None)
    return {"saved": True}


@app.delete("/api/voice/history", status_code=204)
def clear_voice_history(session: Annotated[VoiceSession, Depends(get_voice_session)]) -> None:
    try:
        session.clear_history()
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@app.post("/api/voice/turn")
async def voice_turn(
    request: Request,
    session: Annotated[VoiceSession, Depends(get_voice_session)],
    companion: Annotated[CompanionStore, Depends(get_companion_store)],
) -> dict[str, str | None]:
    media_type = request.headers.get("content-type", "").split(";", 1)[0].lower()
    extension = {
        "audio/webm": "webm",
        "audio/ogg": "ogg",
        "audio/wav": "wav",
        "audio/mpeg": "mp3",
        "audio/mp4": "mp4",
    }.get(media_type)
    if extension is None:
        raise HTTPException(status_code=415, detail="不支持的录音格式")
    if (
        request.headers.get("content-length", "0").isdigit()
        and int(request.headers.get("content-length", "0")) > 10 * 1024 * 1024
    ):
        raise HTTPException(status_code=413, detail="录音超过 10 MB")
    chunks: list[bytes] = []
    size = 0
    async for chunk in request.stream():
        size += len(chunk)
        if size > 10 * 1024 * 1024:
            raise HTTPException(status_code=413, detail="录音超过 10 MB")
        chunks.append(chunk)
    audio = b"".join(chunks)
    if not audio:
        raise HTTPException(status_code=413, detail="录音为空或超过 10 MB")
    try:
        result = await session.turn(audio, filename=f"recording.{extension}", media_type=media_type)
        await session.flush_memory()
    except VoiceInterrupted as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except VoiceProviderError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    except ModelProviderError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    companion.touch()
    if MemoryCapture.matchable(result.transcript):
        MemoryCapture(get_memory_store()).capture(
            result.transcript,
            source_ref=(
                f"voice:{session.history.conversation_id}" if session.history is not None
                else f"voice:{datetime.now().astimezone().isoformat()}"
            ),
        )
    return {
        "transcript": result.transcript,
        "reply": result.reply,
        "turn_id": result.turn_id,
        "audio_base64": base64.b64encode(result.audio).decode() if result.audio else None,
        "audio_media_type": result.media_type,
        "speech_error": result.speech_error,
    }


@app.websocket("/api/voice/stream")
async def voice_stream(websocket: WebSocket) -> None:
    global _active_voice_sockets
    origin = websocket.headers.get("origin")
    port = int(os.getenv("ELLA_RUNTIME_PORT", "8766"))
    if (
        not loopback_host_allowed(websocket.headers.get("host"), port=port)
        or origin not in TRUSTED_ORIGINS
        or not get_runtime_session().accepts_websocket_protocols(
            websocket.scope.get("subprotocols", [])
        )
    ):
        await websocket.close(code=1008)
        return
    if _notification_lock.locked():
        await websocket.close(code=1013)
        return
    session = get_voice_session()
    if not get_voice_config_store().asr_settings().can_transcribe:
        await websocket.close(code=1011)
        return
    await websocket.accept(subprotocol="ella-auth")
    _active_voice_sockets += 1
    send_lock = asyncio.Lock()

    async def send(event: dict[str, object]) -> None:
        if event.get("type") == "transcript" and isinstance(event.get("text"), str):
            get_companion_store().touch()
            spoken = event["text"]
            if MemoryCapture.matchable(spoken):
                MemoryCapture(get_memory_store()).capture(
                    spoken,
                    source_ref=(
                        f"voice:{session.history.conversation_id}" if session.history is not None
                        else f"voice:{datetime.now().astimezone().isoformat()}"
                    ),
                )
        async with send_lock:
            await websocket.send_json(event)

    turn = StreamingVoiceTurn(session, send)
    try:
        while True:
            message = await websocket.receive()
            if message["type"] == "websocket.disconnect":
                break
            if message.get("bytes") is not None:
                try:
                    await turn.feed(message["bytes"])
                except ValueError as exc:
                    await send({"type": "error", "message": str(exc)})
                continue
            try:
                command = json.loads(message.get("text") or "")
                if not isinstance(command, dict):
                    raise TypeError("语音命令格式无效")
                kind = command.get("type")
                if kind == "finish":
                    await turn.finish_input(echo_reference=command.get("echo_reference", ""))
                elif kind == "ack":
                    index = command.get("index")
                    if type(index) is not int:
                        raise ValueError("播放片段编号无效")
                    turn.acknowledge(index)
                    session.schedule_memory_flush()
                    await send({"type": "acknowledged", "index": index})
                elif kind == "interrupt":
                    await turn.interrupt()
                    await send({"type": "interrupted"})
                    break
                else:
                    raise ValueError("未知语音命令")
            except (TypeError, ValueError, json.JSONDecodeError) as exc:
                await send({"type": "error", "message": str(exc)})
    except (WebSocketDisconnect, RuntimeError):
        pass
    finally:
        _active_voice_sockets -= 1
        if turn.ledger is None or not turn.ledger.saved:
            await turn.interrupt()
        session.schedule_memory_flush()
        try:
            await websocket.close()
        except RuntimeError:
            pass


@app.post("/api/voice/playback")
async def voice_playback(
    playback: VoicePlayback,
    session: Annotated[VoiceSession, Depends(get_voice_session)],
) -> dict[str, str]:
    try:
        session.acknowledge(playback.turn_id, played_ratio=playback.played_ratio)
        await session.flush_memory()
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return {"state": session.state.value}


@app.post("/api/voice/interrupt")
async def voice_interrupt(session: Annotated[VoiceSession, Depends(get_voice_session)]) -> dict[str, str]:
    session.interrupt()
    await session.flush_memory()
    return {"state": session.state.value}


@app.post("/api/voice/recover")
def voice_recover(session: Annotated[VoiceSession, Depends(get_voice_session)]) -> dict[str, str]:
    session.recover()
    return {"state": session.state.value}


@app.get("/api/workspaces")
def workspace_status(
    engine: Annotated[AgentEngine, Depends(get_agent_engine)],
) -> dict[str, str]:
    return {
        "code": str(engine.tools["code.list_files"].workspace.root),
        "files": str(engine.tools["workspace.write_text"].root),
    }


@app.post("/api/workspaces/code/open")
async def open_code_workspace(
    engine: Annotated[AgentEngine, Depends(get_agent_engine)],
) -> dict[str, object]:
    try:
        evidence = await engine.tools["code.open_workspace"].execute({}, action_id="workspace-open")
    except OSError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    return evidence.details


@app.get("/api/tasks")
def list_tasks(
    engine: Annotated[AgentEngine, Depends(get_agent_engine)],
) -> list[dict[str, object]]:
    return [task.model_dump(mode="json") for task in engine.store.list()]


@app.post("/api/tasks", status_code=201)
async def plan_task(
    request: TaskCreate, engine: Annotated[AgentEngine, Depends(get_agent_engine)]
) -> dict[str, object]:
    try:
        task = await engine.plan(request.goal)
    except AgentError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except ModelProviderError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    if request.auto_run_safe and engine.is_low_risk(task):
        engine.approve(task.id, task.plan_hash)
        task = await engine.run(task.id)
    if task.state.value == "complete":
        queue_progress(
            "task", task.id, "complete", "这项工作做完啦，核验结果我放在后台了。"
        )
    elif task.state.value == "waiting_approval":
        queue_progress(
            "task", task.id, "planned", "任务步骤我拆好了，等你看一眼计划。"
        )
    return task.model_dump(mode="json")


@app.get("/api/tasks/{identity}")
def get_task(
    identity: str, engine: Annotated[AgentEngine, Depends(get_agent_engine)]
) -> dict[str, object]:
    try:
        return engine.store.get(identity).model_dump(mode="json")
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="任务不存在") from exc


@app.put("/api/tasks/{identity}/plan")
def revise_task(
    identity: str, revision: PlanRevision,
    engine: Annotated[AgentEngine, Depends(get_agent_engine)],
) -> dict[str, object]:
    try:
        return engine.revise(identity, revision.plan_hash, revision.steps).model_dump(mode="json")
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="任务不存在") from exc
    except (AgentError, ValueError, TypeError) as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@app.post("/api/tasks/{identity}/approve")
def approve_task(
    identity: str, approval: PlanApproval,
    engine: Annotated[AgentEngine, Depends(get_agent_engine)],
) -> dict[str, object]:
    try:
        return engine.approve(identity, approval.plan_hash).model_dump(mode="json")
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="任务不存在") from exc
    except AgentError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@app.post("/api/tasks/{identity}/run")
async def run_task(
    identity: str, engine: Annotated[AgentEngine, Depends(get_agent_engine)]
) -> dict[str, object]:
    try:
        task = await engine.run(identity)
        if task.state.value == "complete":
            queue_progress(
                "task", identity, "complete", "搞定！这项工作的每一步都有核验记录。"
            )
        elif task.state.value == "needs_reconciliation":
            queue_progress(
                "task", identity, "uncertain", "有一步结果还不确定，我先停下核对现场。"
            )
        return task.model_dump(mode="json")
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="任务不存在") from exc
    except AgentError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@app.post("/api/tasks/{identity}/reconcile")
async def reconcile_task(
    identity: str, engine: Annotated[AgentEngine, Depends(get_agent_engine)]
) -> dict[str, object]:
    try:
        task = await engine.reconcile(identity)
        if task.state.value == "ready":
            queue_progress(
                "task", identity, "reconciled", "现场核对好了，可以接着完成剩下的步骤。"
            )
        return task.model_dump(mode="json")
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="任务不存在") from exc
    except AgentError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@app.get("/api/usage/summary")
def usage_summary(
    store: Annotated[UsageStore, Depends(get_usage_store)],
    from_date: date | None = None,
    to_date: date | None = None,
    task_id: str | None = None,
) -> dict[str, object]:
    return store.summary(from_date=from_date, to_date=to_date, task_id=task_id)


@app.get("/api/memory")
def list_memories(
    store: Annotated[MemoryStore, Depends(get_memory_store)],
    query: str = "",
    kind: MemoryKind | None = None,
    limit: int = 100,
) -> list[dict[str, object]]:
    records = (
        store.search(query, limit=limit) if query.strip() else store.list(kind=kind, limit=limit)
    )
    if kind is not None and query.strip():
        records = [record for record in records if record.kind == kind]
    return [record.model_dump(mode="json") for record in records]


@app.get("/api/memory/export")
def export_memories(
    store: Annotated[MemoryStore, Depends(get_memory_store)],
) -> JSONResponse:
    return JSONResponse(
        content=store.export(),
        headers={"Content-Disposition": 'attachment; filename="ella-memory.json"'},
    )


@app.post("/api/memory", status_code=201)
def create_memory(
    item: MemoryCreate,
    store: Annotated[MemoryStore, Depends(get_memory_store)],
) -> dict[str, object]:
    return store.create(item).model_dump(mode="json")


@app.get("/api/memory/{identity}/source")
def memory_source(
    identity: str,
    store: Annotated[MemoryStore, Depends(get_memory_store)],
    conversations: Annotated[ConversationService, Depends(get_conversation_service)],
) -> dict[str, object]:
    record = store.get(identity)
    if record is None:
        raise HTTPException(status_code=404, detail="记忆不存在")
    if record.source_type != MemorySource.CONVERSATION or not record.source_ref:
        raise HTTPException(status_code=404, detail="这条记忆没有对话来源")
    match = re.fullmatch(
        r"(?:chat|voice):([^:]+)(?::messages:(\d+)-(\d+))?", record.source_ref
    )
    if match is None:
        raise HTTPException(status_code=404, detail="原始对话来源不可读取")
    conversation_id, start, end = match.groups()
    try:
        messages = (
            conversations.store.history_window(conversation_id, int(start), int(end))
            if start is not None and end is not None
            else conversations.store.history(conversation_id, limit=20)
        )
    except (KeyError, ValueError) as exc:
        raise HTTPException(status_code=404, detail="原始对话已删除或范围无效") from exc
    return {
        "source_ref": record.source_ref,
        "messages": [message.model_dump() for message in messages],
    }


@app.patch("/api/memory/{identity}")
def correct_memory(
    identity: str,
    correction: MemoryCorrection,
    store: Annotated[MemoryStore, Depends(get_memory_store)],
) -> dict[str, object]:
    try:
        record = store.correct(identity, correction)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="记忆不存在") from exc
    return record.model_dump(mode="json")


@app.delete("/api/memory/{identity}", status_code=204)
def delete_memory(
    identity: str,
    store: Annotated[MemoryStore, Depends(get_memory_store)],
) -> None:
    if not store.delete(identity):
        raise HTTPException(status_code=404, detail="记忆不存在")
