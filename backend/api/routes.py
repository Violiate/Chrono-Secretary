"""
backend/api/routes.py
=====================
REST API 路由层（DeepSeek 负责 / 阶段 D3）。

核心是 ``PUT /api/config``：前端改完配置 -> 落库 -> ``config_manager`` 通知所有监听器
-> LLM / 渠道 / 日历全部原地热重载，**0 重启**。

以及 ``POST /api/chat/simulate``：网页内置 IM 模拟器。它把 ``channel_manager.send_message``
临时替换成一个「只在内存里接住消息」的假实现，这样你在网页里测试「引用回复 -> 推迟半小时」
时，绝不会真的把消息发到已经绑定的 Telegram 群或钉钉上。
"""

import asyncio
import logging
import uuid
from typing import Any, Dict, List, Optional, Tuple

from fastapi import APIRouter, HTTPException, Query, Request, Response
from pydantic import BaseModel, Field

from backend.calendars.ics_generator import ics_generator
from backend.channels.manager import channel_manager
from backend.calendars.manager import calendar_manager
from backend.core.config_manager import config_manager
from backend.core.database import task_to_dict, tasks_to_dicts
from backend.core.nlu_engine import nlu_engine
from backend.core.orchestrator import orchestrator
from backend.core.repository import repo
from backend.core.scheduler import scheduler_manager
from backend.core.schemas import ChannelType, UnifiedIncomingMessage

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api", tags=["Chrono Secretary"])

WEB_SIMULATOR_SENDER = "web-simulator"
ACTIVE_STATUSES = ("pending",)
DONE_STATUSES = ("completed", "cancelled")
RESERVED_CONFIG_KEYS = ("channel_status", "ics_feed_url")
# 这两个键必须落库为非负整数列表，前端偶尔会传 ["10", 0] / "10,0" / [10.0]，
# 在路由层统一规范化，避免 config_manager 的严格校验把整次保存打成 500。
INT_LIST_KEYS = ("default_schedule_remind_minutes", "default_ddl_advances")


# --------------------------------------------------------------------------- #
# 请求体模型
# --------------------------------------------------------------------------- #
class LLMTestPayload(BaseModel):
    base_url: str = ""
    api_key: str = ""
    model: str = ""


class CalendarTestPayload(BaseModel):
    provider: str = "caldav"
    config: Optional[Dict[str, Any]] = None


class ChatSimulatePayload(BaseModel):
    text: str = ""
    reply_to_msg_id: Optional[str] = None


class TaskPayload(BaseModel):
    """新建任务（也兼容前端直接发裸 dict，见 create_task 的兜底处理）。"""

    task_type: str = "schedule"
    title: str = ""
    description: Optional[str] = None
    start_time: Optional[str] = None
    end_time: Optional[str] = None
    deadline: Optional[str] = None
    rrule_str: Optional[str] = None
    remind_advance_minutes: List[int] = Field(default_factory=list)
    sender_id: str = "default"
    source_channel: str = "web"


# --------------------------------------------------------------------------- #
# 工具函数
# --------------------------------------------------------------------------- #
def _normalize_status_map(raw: Any) -> Dict[str, bool]:
    result: Dict[str, bool] = {"wechat_claw": False, "dingtalk": False, "telegram": False}
    if not isinstance(raw, dict):
        return result
    for key, value in raw.items():
        name = getattr(key, "value", None) or str(key)
        result[str(name)] = bool(value)
    return result


def _coerce_minute_list(value: Any) -> List[int]:
    """把 ``["10", 0]`` / ``"10,0"`` / ``[10.0]`` 统一成「降序去重」的非负整数列表。"""
    if isinstance(value, str):
        items: List[Any] = [part for part in value.replace("，", ",").split(",") if part.strip()]
    elif isinstance(value, (list, tuple, set)):
        items = list(value)
    elif value is None:
        items = []
    else:
        items = [value]

    result: List[int] = []
    for item in items:
        if isinstance(item, bool):
            continue
        try:
            minutes = int(float(str(item).strip()))
        except (TypeError, ValueError):
            continue
        if minutes >= 0 and minutes not in result:
            result.append(minutes)
    result.sort(reverse=True)
    return result


def _public_base_url(request: Optional[Request]) -> str:
    """拼接 ICS 订阅地址用的外部基地址（支持反向代理的 X-Forwarded-*）。"""
    if request is None:
        return "http://localhost:8000"
    forwarded_proto = request.headers.get("x-forwarded-proto")
    forwarded_host = request.headers.get("x-forwarded-host")
    if forwarded_host:
        proto = (forwarded_proto or request.url.scheme or "http").split(",")[0].strip()
        host = forwarded_host.split(",")[0].strip()
        return f"{proto}://{host}"
    return str(request.base_url).rstrip("/")


def _current_ics_token() -> str:
    token = ""
    try:
        token = str(config_manager.get("ics_feed_token", "") or "")
    except Exception:  # noqa: BLE001
        token = ""
    return token


async def _build_config_response(request: Optional[Request]) -> Dict[str, Any]:
    """``GET /api/config`` 与 ``PUT /api/config`` 共用的响应结构。"""
    try:
        cfg = config_manager.get_all()
    except Exception as exc:  # noqa: BLE001
        logger.warning("config_manager.get_all() 失败，回退数据库直读: %s", exc)
        cfg = await repo.get_all_configs()
    if not isinstance(cfg, dict):
        cfg = {}

    token = str(cfg.get("ics_feed_token") or _current_ics_token() or "")
    if not token:
        token = str(await repo.get_config("ics_feed_token", "") or "")
    cfg["ics_feed_token"] = token

    try:
        channel_status = _normalize_status_map(channel_manager.get_status())
    except Exception as exc:  # noqa: BLE001
        logger.warning("获取渠道状态失败: %s", exc)
        channel_status = _normalize_status_map(None)

    base = _public_base_url(request)
    ics_feed_url = f"{base}/api/calendar/feed/{token}.ics" if token else ""

    return {
        "ok": True,
        "config": cfg,
        "channel_status": channel_status,
        "ics_feed_url": ics_feed_url,
        "ics_feed_token": token,
    }


def _normalize_qrcode_payload(data: Any) -> Dict[str, Any]:
    """把微信适配器返回的扫码数据整理成前端能直接渲染的结构。"""
    payload: Dict[str, Any] = {}
    if isinstance(data, dict):
        payload = dict(data)
    elif isinstance(data, str):
        payload = {"qrcode_url": data}

    qrcode_id = str(payload.get("qrcode_id") or payload.get("qrcodeId") or payload.get("id") or "")
    qrcode_url = ""
    for key in ("qrcode_url", "qrcodeUrl", "url", "qrcode_link", "link", "qrcode"):
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            qrcode_url = value.strip()
            break

    qrcode_image = ""
    for key in ("qrcode_image", "qrcode_img", "qrcode_img_content", "image", "base64"):
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            qrcode_image = value.strip()
            break
    if qrcode_image and not qrcode_image.startswith(("http://", "https://", "data:")):
        # 纯 base64 内容补上 data URI 前缀，前端 <img> 可直接显示
        qrcode_image = f"data:image/png;base64,{qrcode_image}"
    if not qrcode_url and qrcode_image:
        qrcode_url = qrcode_image

    return {
        "ok": bool(qrcode_id or qrcode_url),
        "qrcode_id": qrcode_id,
        "qrcode_url": qrcode_url,
        "qrcode_image": qrcode_image,
        "message": str(payload.get("message") or payload.get("msg") or ""),
        "raw": payload,
    }


# --------------------------------------------------------------------------- #
# 健康检查
# --------------------------------------------------------------------------- #
@router.get("/health")
async def health() -> Dict[str, Any]:
    try:
        status = _normalize_status_map(channel_manager.get_status())
    except Exception:  # noqa: BLE001
        status = _normalize_status_map(None)
    return {
        "status": "ok",
        "service": "chrono-secretary",
        "version": "1.0.0",
        "channels": status,
        "active_channels": sum(1 for v in status.values() if v),
    }


# --------------------------------------------------------------------------- #
# 配置：读取 / 热写入
# --------------------------------------------------------------------------- #
@router.get("/config")
async def get_config(request: Request) -> Dict[str, Any]:
    return await _build_config_response(request)


@router.put("/config")
async def update_config(payload: Dict[str, Any], request: Request) -> Dict[str, Any]:
    """核心动态插入接口：数据库落盘 + 内存热重载（0 重启）。"""
    raw = payload if isinstance(payload, dict) else {}
    updates_raw = raw.get("config") if isinstance(raw.get("config"), dict) else raw

    updates: Dict[str, Any] = {}
    for key, value in updates_raw.items() if isinstance(updates_raw, dict) else []:
        if not isinstance(key, str) or not key.strip():
            continue
        if key in RESERVED_CONFIG_KEYS:
            continue
        if key == "ics_feed_token" and not value:
            continue  # 空值不允许清空订阅 token
        if key in INT_LIST_KEYS:
            value = _coerce_minute_list(value)
        updates[key] = value

    if not updates:
        return await _build_config_response(request)

    try:
        await config_manager.update_configs(updates)
    except (ValueError, TypeError, KeyError) as exc:
        # 用户填了非法值（dingtalk_enabled="yes"、timezone 不存在…）→ 400，不是 500。
        # 注意 zoneinfo.ZoneInfoNotFoundError 继承自 KeyError，必须一并接住。
        logger.warning("配置校验失败: %s", exc)
        raise HTTPException(status_code=400, detail=f"配置校验失败：{exc}") from exc
    except Exception as exc:  # noqa: BLE001
        logger.exception("配置热更新失败")
        raise HTTPException(status_code=500, detail=f"配置保存失败: {exc}") from exc

    logger.info("配置已热更新：%s", ", ".join(sorted(updates.keys())))
    result = await _build_config_response(request)
    result["updated_keys"] = sorted(updates.keys())
    return result


# --------------------------------------------------------------------------- #
# LLM 连通性测试
# --------------------------------------------------------------------------- #
@router.post("/llm/test")
async def test_llm(payload: LLMTestPayload) -> Dict[str, Any]:
    base_url = (payload.base_url or str(config_manager.get("llm_base_url", "") or "")).strip()
    api_key = (payload.api_key or str(config_manager.get("llm_api_key", "") or "")).strip()
    model = (payload.model or str(config_manager.get("llm_model", "") or "")).strip()

    if not base_url or not model:
        return {"ok": False, "message": "Base URL 与 Model Name 不能为空", "latency_ms": 0}

    try:
        ok, message, latency_ms = await nlu_engine.test_llm_connection(base_url, api_key, model)
    except Exception as exc:  # noqa: BLE001
        logger.warning("LLM 连通性测试异常: %s", exc)
        return {"ok": False, "message": f"测试调用异常：{exc}", "latency_ms": 0}

    if ok:
        try:
            await config_manager.update_configs({
                "llm_base_url": base_url,
                "llm_api_key": api_key,
                "llm_model": model,
            })
            message = f"{message}（已自动保存并热生效）"
        except Exception as exc:  # noqa: BLE001
            logger.warning("测试成功后自动保存 LLM 配置失败: %s", exc)

    return {
        "ok": bool(ok),
        "message": str(message or ("连接成功" if ok else "连接失败")),
        "latency_ms": int(latency_ms or 0),
    }


# --------------------------------------------------------------------------- #
# 微信 OpenClaw 扫码绑定
# --------------------------------------------------------------------------- #
def _wechat_adapter():
    adapter = getattr(channel_manager, "wechat_adapter", None)
    if adapter is None:
        raise HTTPException(status_code=503, detail="微信 OpenClaw 适配器不可用")
    return adapter


@router.get("/wechat/qrcode")
async def wechat_qrcode() -> Dict[str, Any]:
    try:
        data = await _wechat_adapter().get_login_qrcode()
    except HTTPException:
        raise
    except Exception as exc:  # noqa: BLE001
        logger.warning("生成微信绑定二维码失败: %s", exc)
        return {
            "ok": False,
            "qrcode_id": "",
            "qrcode_url": "",
            "qrcode_image": "",
            "message": f"生成二维码失败：{exc}",
        }
    return _normalize_qrcode_payload(data)


@router.get("/wechat/qrcode/status")
async def wechat_qrcode_status(qrcode_id: str = Query(default="")) -> Dict[str, Any]:
    if not qrcode_id:
        return {"ok": False, "status": "expired", "message": "缺少 qrcode_id"}

    try:
        data = await _wechat_adapter().poll_qrcode_status(qrcode_id)
    except HTTPException:
        raise
    except Exception as exc:  # noqa: BLE001
        logger.warning("轮询微信扫码状态失败: %s", exc)
        return {"ok": False, "status": "waiting", "message": f"查询失败：{exc}"}

    payload = data if isinstance(data, dict) else {}
    status = str(payload.get("status") or "waiting").lower()
    if status not in ("waiting", "scanned", "confirmed", "expired"):
        status = "waiting"

    online = False
    try:
        online = bool(_normalize_status_map(channel_manager.get_status()).get("wechat_claw"))
    except Exception:  # noqa: BLE001
        online = False

    return {
        "ok": True,
        "status": status,
        "online": online,
        "bot_token": payload.get("bot_token"),
        "message": str(payload.get("message") or ""),
    }


# --------------------------------------------------------------------------- #
# 日历连接测试
# --------------------------------------------------------------------------- #
@router.post("/calendar/test")
async def test_calendar(payload: CalendarTestPayload) -> Dict[str, Any]:
    provider = (payload.provider or "caldav").strip().lower()
    if provider not in ("caldav", "google"):
        return {"ok": False, "message": f"未知的日历类型：{provider}"}

    try:
        ok, message = await calendar_manager.test_connection(provider, payload.config)
    except Exception as exc:  # noqa: BLE001
        logger.warning("日历连接测试异常（%s）: %s", provider, exc)
        return {"ok": False, "message": f"测试异常：{exc}"}

    return {"ok": bool(ok), "message": str(message or ("连接成功" if ok else "连接失败"))}


# --------------------------------------------------------------------------- #
# 任务 CRUD
# --------------------------------------------------------------------------- #
@router.get("/tasks")
async def list_tasks(
    status: Optional[str] = Query(default=None),
    task_type: Optional[str] = Query(default=None),
) -> Dict[str, Any]:
    tasks = await repo.list_tasks(
        status=(status or None),
        task_type=(task_type or None),
    )
    return {"ok": True, "tasks": tasks_to_dicts(tasks), "total": len(tasks)}


async def _sync_calendar_create(task) -> None:
    """新建任务后同步远端日历，并回写 cal_event_uid（不改变 task 引用）。"""
    try:
        cal_uid = await calendar_manager.sync_create_event(task)
    except Exception as exc:  # noqa: BLE001 - 日历挂了也不能挡住建日程
        logger.warning("同步创建日历事件失败（任务 #%s）: %s", task.id, exc)
        return
    if cal_uid:
        task.cal_event_uid = str(cal_uid)
        await repo.update_task(task.id, {"cal_event_uid": str(cal_uid)})


@router.post("/tasks")
async def create_task(payload: Dict[str, Any]) -> Dict[str, Any]:
    data: Dict[str, Any] = dict(payload or {})

    title = str(data.get("title") or "").strip()
    if not title:
        raise HTTPException(status_code=400, detail="任务标题不能为空")

    task_type = str(data.get("task_type") or "schedule").strip().lower()
    if task_type not in ("schedule", "memo_ddl"):
        raise HTTPException(status_code=400, detail="task_type 只能是 schedule 或 memo_ddl")
    data["task_type"] = task_type

    if task_type == "memo_ddl" and not (data.get("deadline") or data.get("start_time")):
        raise HTTPException(status_code=400, detail="DDL 备忘录必须提供截止时间")

    data.setdefault("source_channel", "web")
    data.setdefault("sender_id", "default")

    task = await repo.create_task(data)
    await _sync_calendar_create(task)

    try:
        await scheduler_manager.schedule_task_jobs(task)
    except Exception as exc:  # noqa: BLE001
        logger.warning("注册定时提醒失败（任务 #%s）: %s", task.id, exc)

    return {"ok": True, "task": task_to_dict(task)}


@router.patch("/tasks/{task_id}")
async def update_task(task_id: int, payload: Dict[str, Any]) -> Dict[str, Any]:
    existing = await repo.get_task(task_id)
    if existing is None:
        raise HTTPException(status_code=404, detail=f"任务 #{task_id} 不存在")

    updates: Dict[str, Any] = dict(payload or {})
    updates.pop("id", None)

    task = await repo.update_task(task_id, updates)
    if task is None:
        raise HTTPException(status_code=404, detail=f"任务 #{task_id} 不存在")

    if task.status in DONE_STATUSES:
        try:
            await scheduler_manager.remove_task_jobs(task.id)
        except Exception as exc:  # noqa: BLE001
            logger.warning("移除定时提醒失败（任务 #%s）: %s", task.id, exc)
        if task.cal_event_uid:
            try:
                await calendar_manager.sync_delete_event(task.cal_event_uid)
            except Exception as exc:  # noqa: BLE001
                logger.warning("删除远端日历事件失败（任务 #%s）: %s", task.id, exc)
    else:
        try:
            await scheduler_manager.reschedule_task_jobs(task)
        except Exception as exc:  # noqa: BLE001
            logger.warning("重设定时提醒失败（任务 #%s）: %s", task.id, exc)
        if task.cal_event_uid:
            try:
                await calendar_manager.sync_update_event(task)
            except Exception as exc:  # noqa: BLE001
                logger.warning("更新远端日历事件失败（任务 #%s）: %s", task.id, exc)
        else:
            await _sync_calendar_create(task)

    return {"ok": True, "task": task_to_dict(task)}


@router.delete("/tasks/{task_id}")
async def delete_task(task_id: int) -> Dict[str, Any]:
    existing = await repo.get_task(task_id)
    if existing is None:
        raise HTTPException(status_code=404, detail=f"任务 #{task_id} 不存在")

    try:
        await scheduler_manager.remove_task_jobs(task_id)
    except Exception as exc:  # noqa: BLE001
        logger.warning("移除定时提醒失败（任务 #%s）: %s", task_id, exc)

    if existing.cal_event_uid:
        try:
            await calendar_manager.sync_delete_event(existing.cal_event_uid)
        except Exception as exc:  # noqa: BLE001
            logger.warning("删除远端日历事件失败（任务 #%s）: %s", task_id, exc)

    deleted = await repo.delete_task(task_id)
    if not deleted:
        raise HTTPException(status_code=500, detail=f"任务 #{task_id} 删除失败")

    return {"ok": True, "message": f"任务 #{task_id} 已删除"}


# --------------------------------------------------------------------------- #
# 网页对话模拟器（内置 IM，0 配置测试 NLU / 引用改期 / DDL 盘点）
# --------------------------------------------------------------------------- #
_simulate_lock = asyncio.Lock()


@router.post("/chat/simulate")
async def chat_simulate(payload: ChatSimulatePayload) -> Dict[str, Any]:
    text = (payload.text or "").strip()
    if not text:
        raise HTTPException(status_code=400, detail="消息内容不能为空")

    reply_to = (payload.reply_to_msg_id or "").strip() or None
    simulated_msg_id = f"web-sim-{uuid.uuid4().hex[:12]}"

    # 说明：模拟器使用 TELEGRAM 渠道标识走同一条编排链路（ChannelType 是冻结枚举）。
    # sender_id 固定为 web-simulator，且下面会临时屏蔽真实发送，绝不会骚扰真实用户。
    message = UnifiedIncomingMessage(
        channel=ChannelType.TELEGRAM,
        sender_id=WEB_SIMULATOR_SENDER,
        message_id=simulated_msg_id,
        text=text,
        reply_to_msg_id=reply_to,
        context_token=None,
        raw_payload={"source": "web_simulator"},
    )

    sent: List[Tuple[str, str]] = []

    async def _capture_send(channel, recipient_id, out_text, context_token=None):
        """假发送：只把消息收进内存，返回一个可被引用的本地消息 ID。"""
        msg_id = f"web-sim-{uuid.uuid4().hex[:12]}"
        sent.append((msg_id, str(out_text)))
        return msg_id

    reply_text = ""
    try:
        # 挂接与恢复都在锁内完成：并发请求不会把「假发送」互相覆盖成永久状态
        async with _simulate_lock:
            original_send = getattr(channel_manager, "send_message", None)
            patched = False
            if original_send is not None:
                try:
                    channel_manager.send_message = _capture_send  # type: ignore[method-assign]
                    patched = True
                except Exception as exc:  # noqa: BLE001
                    logger.debug("无法挂接模拟发送钩子（继续走真实发送）: %s", exc)

            try:
                reply = await orchestrator.handle_message(message)
                reply_text = str(reply).strip() if reply is not None else ""
            finally:
                if patched and original_send is not None:
                    try:
                        channel_manager.send_message = original_send  # type: ignore[method-assign]
                    except Exception as exc:  # noqa: BLE001
                        logger.debug("恢复 channel_manager.send_message 失败（已忽略）: %s", exc)
    except HTTPException:
        raise
    except Exception as exc:  # noqa: BLE001
        logger.exception("网页模拟对话处理失败")
        raise HTTPException(status_code=500, detail=f"模拟对话失败：{exc}") from exc

    if not reply_text:
        reply_text = "\n".join(t for _, t in sent if t).strip() or "（秘书没有返回任何内容）"

    last_msg_id = sent[-1][0] if sent else None
    bound_task = None
    if last_msg_id:
        bound_task = await repo.get_task_by_msg_id(ChannelType.TELEGRAM.value, last_msg_id)
    return {
        "ok": True,
        "reply": reply_text,
        "message_id": last_msg_id if bound_task is not None else None,
        "intercepted": len(sent),
        "reply_to_msg_id": reply_to,
    }


# --------------------------------------------------------------------------- #
# ICS 订阅源
# --------------------------------------------------------------------------- #
@router.get("/calendar/feed/{token}.ics")
async def calendar_feed(token: str) -> Response:
    expected = _current_ics_token()
    if not expected:
        expected = str(await repo.get_config("ics_feed_token", "") or "")
    if not token or not expected or token != expected:
        raise HTTPException(status_code=403, detail="订阅 token 无效")

    ics_text = await ics_generator.generate_ics_feed()
    return Response(
        content=ics_text,
        media_type="text/calendar; charset=utf-8",
        headers={
            "Cache-Control": "no-cache, max-age=0",
            "Content-Disposition": 'inline; filename="chrono-secretary.ics"',
            "X-Content-Type-Options": "nosniff",
        },
    )


__all__ = ["router"]
