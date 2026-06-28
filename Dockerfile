# syntax=docker/dockerfile:1
#
# Panel Everything — 多阶段、多 arch(linux/arm64 主目标 + linux/amd64)、非 root。
#
# 多 arch 构建:
#   docker buildx build --platform linux/arm64,linux/amd64 -t panel-everything:latest .
# 单 arch 本地:
#   docker build -t panel-everything:latest .

# ---------- builder ----------
FROM python:3.12-slim-bookworm AS builder

ENV PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /build

# 仅拷贝构建依赖所需文件,最大化 layer 缓存。
COPY pyproject.toml README.md ./
COPY src ./src

# 装到独立前缀,runtime 阶段整体拷走(避免带 builder 工具链)。
RUN python -m pip install --upgrade pip \
    && python -m pip install --prefix=/install .

# ---------- runtime ----------
FROM python:3.12-slim-bookworm AS runtime

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PATH="/usr/local/bin:${PATH}"

# 非特权用户;/data 为 SQLite 卷挂载点,赋权给 app。
RUN useradd --create-home --uid 10001 app \
    && mkdir -p /data \
    && chown -R app:app /data

# 拷贝 builder 装好的依赖(含可执行入口)。
COPY --from=builder /install /usr/local

WORKDIR /app
COPY --chown=app:app src ./src

ENV PYTHONPATH=/app/src \
    PANEL_DB_PATH=/data/panel.db \
    PANEL_PORT=8080

USER app
EXPOSE 8080

# 容器健康检查:命中 /healthz。
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://localhost:8080/healthz').status==200 else 1)"

# uvloop 由 main 选用;此处直接起 uvicorn(单 worker)。
CMD ["uvicorn", "panel.main:app", "--host", "0.0.0.0", "--port", "8080", "--loop", "uvloop", "--workers", "1"]
