"""
backend/channels/telegram_bot.py
================================
Telegram 机器人适配器（getUpdates 长轮询）—— DeepSeek 负责 / 阶段 D2。

为什么手写 ``getUpdates`` 而不用 ``python-telegram-bot``：
- 零额外依赖、零事件循环冲突：前端热更新 Token 时 ``stop()`` -> ``start()`` 只是
  取消一个 asyncio Task，绝不会出现「another loop is already running」；
- 支持 ``telegram_proxy_url``（httpx 原生 proxy），国内网络友好；
- offset 游标自己维护，重启只补收未确认消息，不会重复消费。

引用回复（方法 A）：
``message.reply_to_message.message_id`` -> ``UnifiedIncomingMessage.reply_to_msg_id``。
"""

import asyncio
import logging
from typing import Any, Dict, Optional

import httpx

from backend.core.schemas import BaseChannelAdapter, ChannelType, UnifiedIncomingMessage

logger = logging.getLogger(__name__)

API_ROOT = "https://api.telegram.org"
LONG_POLL_TIMEOUT = 30            # getUpdates 服务端挂起秒数
MAX_TEXT_LENGTH = 4000            # Telegram 单条上限 4096，留余量


def _build_http_client(proxy_url: Optional[str]) -> httpx.AsyncClient:
    """按需带代理创建 httpx 客户端（兼容 httpx 0.23~0.28 的 proxy / proxies 参数差异）。"""
    timeout = httpx.Timeout(connect=15.0, read=LONG_POLL_TIMEOUT + 20.0, write=15.0, pool=15.0)
    proxy_url = (proxy_url or "").strip()
    if proxy_url:
        if "://" not in proxy_url:
            proxy_url = f"http://{proxy_url}"
        try:
            return httpx.AsyncClient(proxy=proxy_url, timeout=timeout)
        except TypeError:
            # httpx < 0.26 使用 proxies 参数
            return httpx.AsyncClient(proxies=proxy_url, timeout=timeout)
    return httpx.AsyncClient(timeout=timeout)


class TelegramChannelAdapter(BaseChannelAdapter):
    """Telegram 长轮询适配器。构造函数签名由「跨模块冻结接口契约」锁定。"""

    def __init__(self, config_manager, orchestrator):
        self.config_manager = config_manager
        self.orchestrator = orchestrator

        self.is_running: bool = False
        self._client: Optional[httpx.AsyncClient] = None
        self._client_proxy: Optional[str] = None
        self._runner: Optional[asyncio.Task] = None
        self._stop_event: asyncio.Event = asyncio.Event()
        self._offset: int = 0
        self._bot_username: str = ""

    # ------------------------------------------------------------------ #
    # 配置
    # ------------------------------------------------------------------ #
    def _token(self) -> str:
        return str(self.config_manager.get("telegram_bot_token", "") or "").strip()

    def _proxy(self) -> str:
        return str(self.config_manager.get("telegram_proxy_url", "") or "").strip()

    def _enabled(self) -> bool:
        return bool(self.config_manager.get("telegram_enabled", False))

    def _api_url(self, method: str) -> str:
        token = self._token()
        if not token:
            raise RuntimeError("telegram_bot_token 未配置")
        return f"{API_ROOT}/bot{token}/{method}"

    async def _ensure_client(self) -> httpx.AsyncClient:
        """客户端按代理配置复用；代理变更时自动重建。"""
        proxy = self._proxy()
        if self._client is None or self._client.is_closed or proxy != self._client_proxy:
            old = self._client
            self._client = _build_http_client(proxy)
            self._client_proxy = proxy
            if old is not None and not old.is_closed:
                try:
                    await old.aclose()
                except Exception as exc:  # noqa: BLE001
                    logger.debug("关闭旧的 Telegram HTTP 客户端失败（已忽略）: %s", exc)
        return self._client

    # ------------------------------------------------------------------ #
    # 生命周期
    # ------------------------------------------------------------------ #
    async def start(self) -> None:
        """非阻塞启动：拉起后台轮询协程后立刻返回。"""
        if self._runner is not None and not self._runner.done():
            logger.debug("Telegram 适配器已在运行，忽略重复 start()")
            return

        token = self._token()
        if not self._enabled():
            logger.info("Telegram 渠道未启用（telegram_enabled=false），跳过启动")
            self.is_running = False
            return
        if not token:
            logger.warning("telegram_bot_token 未配置，无法启动 Telegram 轮询")
            self.is_running = False
            return

        self._stop_event = asyncio.Event()
        self._runner = asyncio.create_task(self._poll_loop(), name="telegram-poll-loop")
        self.is_running = True
        logger.info("Telegram 轮询适配器已启动（bot=%s…）", token[:10])

        try:
            await asyncio.sleep(0)
        except asyncio.CancelledError:
            await self.stop()
            raise

    async def stop(self) -> None:
        """优雅停止：取消轮询协程并关闭 HTTP 客户端。可重复调用。"""
        self._stop_event.set()

        runner = self._runner
        self._runner = None
        if runner is not None and not runner.done():
            runner.cancel()
            try:
                await runner
            except asyncio.CancelledError:
                logger.debug("Telegram 轮询协程已取消")
            except Exception as exc:  # noqa: BLE001
                logger.debug("Telegram 轮询协程退出异常: %s", exc)

        self._client, client = None, self._client
        if client is not None and not client.is_closed:
            try:
                await client.aclose()
            except Exception as exc:  # noqa: BLE001
                logger.debug("关闭 Telegram HTTP 客户端失败（已忽略）: %s", exc)

        self.is_running = False
        logger.info("Telegram 适配器已停止")

    # ------------------------------------------------------------------ #
    # 长轮询
    # ------------------------------------------------------------------ #
    async def _poll_loop(self) -> None:
        backoff = 2.0
        first_round = True
        try:
            while not self._stop_event.is_set():
                delay = 0.0
                try:
                    if first_round:
                        first_round = False
                        await self._call_get_me()
                    handled = await self._poll_once()
                    if handled:
                        backoff = 2.0
                except asyncio.CancelledError:
                    raise
                except httpx.HTTPStatusError as exc:
                    status = exc.response.status_code if exc.response is not None else 0
                    body = ""
                    try:
                        body = exc.response.text[:200]
                    except Exception:  # noqa: BLE001
                        body = ""
                    if status == 401:
                        logger.error("Telegram Token 无效（401），暂停轮询 60s：%s", body)
                        delay = 60.0
                    elif status == 409:
                        logger.warning("Telegram 存在另一个 getUpdates 消费者（409），退避 10s")
                        delay = 10.0
                    else:
                        logger.warning("Telegram 接口 HTTP %s：%s", status, body)
                        delay = backoff
                        backoff = min(backoff * 2, 60.0)
                except asyncio.TimeoutError:
                    delay = 1.0
                except Exception as exc:  # noqa: BLE001 - 网络抖动自动恢复
                    logger.warning("Telegram 轮询异常: %s（%.0fs 后重试）", exc, backoff)
                    delay = backoff
                    backoff = min(backoff * 2, 60.0)

                if self._stop_event.is_set():
                    break
                if delay > 0:
                    try:
                        await asyncio.wait_for(self._stop_event.wait(), timeout=delay)
                    except asyncio.TimeoutError:
                        logger.debug("Telegram 轮询退避 %.0fs 结束，继续下一轮", delay)
        finally:
            self.is_running = False

    async def _call_get_me(self) -> None:
        """启动自检：拿 bot 用户名，顺便验证 Token 有效性。"""
        try:
            client = await self._ensure_client()
            resp = await client.post(self._api_url("getMe"))
            resp.raise_for_status()
            data = resp.json()
            result = data.get("result") or {}
            self._bot_username = str(result.get("username") or "")
            logger.info("✅ Telegram Bot 已连接：@%s", self._bot_username or "(unknown)")
        except Exception as exc:  # noqa: BLE001
            logger.warning("Telegram getMe 自检失败（不影响继续轮询）: %s", exc)

    async def _poll_once(self) -> bool:
        """拉一轮更新，处理完返回是否有消息。"""
        client = await self._ensure_client()
        payload: Dict[str, Any] = {
            "offset": self._offset,
            "timeout": LONG_POLL_TIMEOUT,
            "allowed_updates": ["message"],
        }
        resp = await client.post(self._api_url("getUpdates"), json=payload)
        resp.raise_for_status()
        data = resp.json()
        if not data.get("ok"):
            logger.warning("Telegram getUpdates 返回失败: %s", data.get("description"))
            return False

        updates = data.get("result") or []
        for update in updates:
            try:
                await self._handle_update(update)
            except Exception as exc:  # noqa: BLE001 - 单条消息失败不影响后续
                logger.exception("处理 Telegram 更新失败: %s", exc)
            finally:
                try:
                    self._offset = max(self._offset, int(update.get("update_id", 0)) + 1)
                except (TypeError, ValueError):
                    logger.debug("update_id 非法，跳过 offset 推进: %r", update.get("update_id"))
        return bool(updates)

    # ------------------------------------------------------------------ #
    # 消息解析
    # ------------------------------------------------------------------ #
    async def _handle_update(self, update: Dict[str, Any]) -> None:
        message = update.get("message") or update.get("edited_message")
        if not isinstance(message, dict):
            return

        text = str(message.get("text") or "").strip()
        if not text:
            logger.debug("Telegram 非文本消息（message_id=%s），忽略", message.get("message_id"))
            return

        chat = message.get("chat") or {}
        chat_id = chat.get("id")
        if chat_id is None:
            return
        sender_id = str(chat_id)

        platform_msg_id = str(message.get("message_id") or update.get("update_id") or "")

        # ---- 方法 A：引用回复检测 ----
        replied = message.get("reply_to_message")
        reply_to_msg_id: Optional[str] = None
        if isinstance(replied, dict) and replied.get("message_id") is not None:
            reply_to_msg_id = str(replied.get("message_id"))

        # Telegram 不需要 context_token，但保留字段以统一消息体
        unified = UnifiedIncomingMessage(
            channel=ChannelType.TELEGRAM,
            sender_id=sender_id,
            message_id=platform_msg_id,
            text=text,
            reply_to_msg_id=reply_to_msg_id,
            context_token=None,
            raw_payload=message,
        )

        logger.info(
            "📥 Telegram 消息 chat=%s msg_id=%s reply_to=%s text=%r",
            sender_id,
            platform_msg_id,
            reply_to_msg_id,
            text[:80],
        )

        reply = await self.orchestrator.handle_message(unified)
        if reply:
            logger.debug("Telegram 消息已处理，回复: %r", str(reply)[:120])

    # ------------------------------------------------------------------ #
    # 发送
    # ------------------------------------------------------------------ #
    async def send_text(
        self,
        recipient_id: str,
        text: str,
        context_token: Optional[str] = None,
    ) -> Optional[str]:
        """发送文本，返回 Telegram ``message_id``（字符串）用于引用追踪。"""
        text = (text or "").strip()
        if not text or not str(recipient_id or "").strip():
            return None
        if len(text) > MAX_TEXT_LENGTH:
            text = text[: MAX_TEXT_LENGTH - 3] + "..."

        payload = {
            "chat_id": str(recipient_id),
            "text": text,
            "disable_web_page_preview": True,
        }

        try:
            client = await self._ensure_client()
            resp = await client.post(self._api_url("sendMessage"), json=payload)
            resp.raise_for_status()
            data = resp.json()
            if not data.get("ok"):
                logger.warning("Telegram sendMessage 失败: %s", data.get("description"))
                return None
            result = data.get("result") or {}
            message_id = result.get("message_id")
            return str(message_id) if message_id is not None else None
        except Exception as exc:  # noqa: BLE001
            logger.warning("Telegram sendMessage 异常: %s", exc)
            return None

    async def send_chat_action(self, recipient_id: str, action: str = "typing") -> None:
        """可选：发送「正在输入…」，让长耗时 LLM 解析期间体验更好。"""
        try:
            client = await self._ensure_client()
            await client.post(
                self._api_url("sendChatAction"),
                json={"chat_id": str(recipient_id), "action": action},
            )
        except Exception as exc:  # noqa: BLE001
            logger.debug("Telegram sendChatAction 失败: %s", exc)


__all__ = ["TelegramChannelAdapter"]
