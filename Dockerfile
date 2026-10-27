# ============================================================
# Chrono Secretary —— 生产镜像
# 只复制 backend/ 与 frontend/，数据库通过 volume 挂载持久化。
# ============================================================
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    TZ=Asia/Shanghai

WORKDIR /app

# tzdata：APScheduler / dateparser 依赖准确时区；ca-certificates：httpx 访问 HTTPS
RUN apt-get update \
    && apt-get install -y --no-install-recommends tzdata ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# 先装依赖（利用镜像层缓存，改代码不必重装包）
COPY requirements.txt ./
RUN pip install --no-cache-dir -U pip \
    && pip install --no-cache-dir -r requirements.txt

# 再复制业务代码
COPY backend ./backend
COPY frontend ./frontend

# 数据目录（compose 会把 ./data 挂到这里）
RUN mkdir -p /app/data

EXPOSE 8000
VOLUME ["/app/data"]

HEALTHCHECK --interval=30s --timeout=6s --start-period=25s --retries=3 \
    CMD python -c "import sys,urllib.request; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/api/health', timeout=4).getcode()==200 else 1)"

CMD ["uvicorn", "backend.main:app", "--host", "0.0.0.0", "--port", "8000", "--proxy-headers"]
