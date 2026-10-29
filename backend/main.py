"""
backend/main.py
===============
FastAPI 入口（DeepSeek 负责 / 阶段 D3）。

启动顺序（lifespan）：
1. ``repo.init_db()``           建表 + 首次生成 ``ics_feed_token``
2. ``config_manager.initialize()``  从 SQLite 把用户上次的配置灌进内存总线
3. ``scheduler_manager.start()``    恢复所有未完成任务的提醒
4. ``channel_manager.start_all()``  按配置拉起微信 / 钉钉 / Telegram

退出时反向优雅关闭：先停渠道（不再收消息）、再停调度器（不再触发提醒）。

访问 ``http://localhost:8000/`` 直接就是管理后台，同源调用 ``/api/*``。
"""

import logging
import os
import sys
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import AsyncIterator

# Windows 控制台默认 GBK 编码下打印 Emoji / Banner 会抛 UnicodeEncodeError，统一重设为 UTF-8
for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        try:
            _stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from backend.api.routes import router
from backend.channels.manager import channel_manager
from backend.core.config_manager import config_manager
from backend.core.repository import repo
from backend.core.scheduler import scheduler_manager

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-7s | %(name)-28s | %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("chrono-secretary")

PROJECT_ROOT = Path(__file__).resolve().parents[1]
FRONTEND_DIR = PROJECT_ROOT / "frontend"
INDEX_FILE = FRONTEND_DIR / "index.html"

BANNER = r"""
   ____ _                       ____            _                  _
  / ___| |__  _ __ ___  _ __   / ___|  ___  ___| |_ ___ _ __ _   _| |
 | |   | '_ \| '__/ _ \| '_ \  \___ \ / _ \/ __| __/ _ \ '__| | | | |
 | |___| | | | | | (_) | | | |  ___) |  __/ (__| ||  __/ |  | |_| | |
  \____|_| |_|_|  \___/|_| |_| |____/ \___|\___|\__\___|_|   \__, |_|
                                                            |___/
  AI 个人日程与备忘录秘书 · 微信 / 钉钉 / Telegram · 0 重启热配置
"""


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """应用生命周期：启动装配 + 退出清理。"""
    print(BANNER)
    logger.info("正在启动 Chrono Secretary…")

    # 检查是否被误配为多进程模式（APScheduler + SQLite 必须单 worker 运行）
    web_concurrency = int(os.environ.get("WEB_CONCURRENCY", "1") or "1")
    if web_concurrency > 1:
        logger.warning("⚠️ 检测到 WEB_CONCURRENCY=%s，APScheduler 内存调度必须以单进程 (workers=1) 运行以避免重复推送！", web_concurrency)

    # 1) 数据库是地基，失败必须直接抛出让容器重启
    await repo.init_db()
    logger.info("✅ 数据库就绪")

    # 2) 配置总线（失败也继续，配置总线内部自带默认值兜底）
    try:
        await config_manager.initialize()
        tz_name = str(config_manager.get("timezone", "Asia/Shanghai") or "Asia/Shanghai")
        os.environ["TZ"] = tz_name
        if hasattr(time, "tzset"):
            time.tzset()
        logger.info("✅ 配置总线已加载 (时区: %s)", tz_name)
    except Exception as exc:  # noqa: BLE001
        logger.exception("配置总线初始化失败，将以默认配置继续运行: %s", exc)

    # 3) 调度器
    try:
        await scheduler_manager.start()
        logger.info("✅ 定时提醒调度器已启动")
    except Exception as exc:  # noqa: BLE001
        logger.exception("调度器启动失败，提醒功能暂不可用: %s", exc)

    # 4) 消息渠道（单个渠道失败不能拖垮整个服务）
    try:
        await channel_manager.start_all()
        status = channel_manager.get_status()
        logger.info("✅ 消息渠道已启动: %s", status)
    except Exception as exc:  # noqa: BLE001
        logger.exception("消息渠道启动异常: %s", exc)

    logger.info("🚀 管理后台： http://localhost:8000/")
    logger.info("📆 ICS 订阅： http://localhost:8000/api/calendar/feed/<ics_feed_token>.ics")

    try:
        yield
    finally:
        logger.info("正在关闭 Chrono Secretary…")
        for name, closer in (
            ("消息渠道", channel_manager.stop_all),
            ("调度器", scheduler_manager.stop),
        ):
            try:
                await closer()
                logger.info("✅ %s 已优雅关闭", name)
            except Exception as exc:  # noqa: BLE001
                logger.warning("%s 关闭时出现异常（已忽略）: %s", name, exc)
        logger.info("👋 再见")


app = FastAPI(
    title="Chrono Secretary",
    description="AI 个人日程与备忘录秘书 —— 微信 / 钉钉 / Telegram 全渠道 + Web 热配置看板",
    version="1.0.0",
    lifespan=lifespan,
    docs_url="/api/docs",
    redoc_url="/api/redoc",
    openapi_url="/api/openapi.json",
)

# CORS：本机前端同源，另外放开局域网/自定义前端调试
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
    expose_headers=["Content-Disposition"],
)

app.include_router(router)


# --------------------------------------------------------------------------- #
# 轻量探活接口（docker healthcheck / 反向代理用）
# 注意：必须注册在静态目录挂载之前，否则会被 "/" 的 mount 吞掉。
# --------------------------------------------------------------------------- #
@app.get("/api/status", include_in_schema=False)
async def api_status() -> JSONResponse:
    """轻量探活接口（给 docker healthcheck / 反向代理用）。"""
    try:
        channels = {str(getattr(k, "value", k)): bool(v) for k, v in channel_manager.get_status().items()}
    except Exception:  # noqa: BLE001
        channels = {}
    return JSONResponse({"status": "ok", "channels": channels})


# --------------------------------------------------------------------------- #
# 静态前端
# Starlette 按注册顺序匹配路由，mount("/") 会吞掉它之后注册的一切路径，
# 所以必须先 include_router(router) + 注册完所有显式路由，最后再 mount。
# --------------------------------------------------------------------------- #
if FRONTEND_DIR.is_dir():
    app.mount("/", StaticFiles(directory=str(FRONTEND_DIR), html=True), name="frontend")
    logger.info("前端静态目录已挂载: %s", FRONTEND_DIR)
else:  # pragma: no cover - 目录缺失时的友好提示
    logger.warning("未找到前端目录 %s，管理后台不可用", FRONTEND_DIR)

    @app.get("/", response_class=HTMLResponse)
    async def missing_frontend() -> str:
        return (
            "<h1>Chrono Secretary 后端已启动</h1>"
            "<p>但未找到 <code>frontend/index.html</code>，管理后台暂不可用。</p>"
            f"<p>API 文档：<a href='/api/docs'>/api/docs</a></p>"
        )


if __name__ == "__main__":  # pragma: no cover
    import uvicorn

    # 强制单 worker 运行（APScheduler + SQLite），并开启 proxy_headers 以支持 Nginx/Caddy 反代透传 X-Forwarded-*
    uvicorn.run(
        "backend.main:app",
        host=os.environ.get("CHRONO_HOST", "0.0.0.0"),
        port=int(os.environ.get("CHRONO_PORT", "8000")),
        workers=1,
        proxy_headers=True,
        forwarded_allow_ips="*",
        reload=False,
    )
