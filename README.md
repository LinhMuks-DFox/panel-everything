# Panel Everything

一站式设备监控面板 — 实时查看所有计算设备的运行状态、计算任务、资源占用、AI 使用限额等。

## 特性

- 响应式 Web App，支持 Kindle / iPhone / iPad 等多终端
- 部署在树莓派上，轻量高效
- 多设备状态聚合展示

## 开发模式

本项目采用 **人类零代码** 模式，所有开发工作由 AI 完成：

```
人类(需求) → PM(精细化) → Architect(设计) → Coder(实现) → 交付
```

详见 `.claude/CLAUDE.md` 和 `roles/` 目录。

## 项目结构

```
├── roles/                     # AI 角色定义 (PM / Architect / Coder)
├── docs/
│   ├── requirements/          # 需求文档 (REQ-xxx)
│   ├── architecture/          # 架构设计 (ARCH-xxx)
│   ├── tasks/                 # 任务卡 (TASK-xxx)
│   ├── templates/             # 文档模板
│   └── progress/              # 进度跟踪
│       ├── STATUS.md          # 全局状态仪表板
│       ├── changelog/         # 按月归档的开发日志
│       ├── bugs/              # 每个 bug 一个文件
│       └── milestones/        # 里程碑记录
├── src/                       # 源代码
└── tests/                     # 测试
```

## 本地运行 / docker 运行

### 本地开发(需要 Python 3.12）

```bash
# 创建虚拟环境并安装(含开发依赖)
python3.12 -m venv .venv
source .venv/bin/activate          # zsh/bash;Windows PowerShell 用 .venv\Scripts\Activate.ps1
pip install -e ".[dev]"

# 启动(uvloop,单 worker,监听 :8080)
panel                               # 等价于 python -m panel.main
# 或显式:
uvicorn panel.main:app --host 0.0.0.0 --port 8080 --loop uvloop

# 健康检查
curl http://localhost:8080/healthz  # → {"status":"ok","db":"ok","time":"..."}

# 测试与 lint
ruff check src tests
pytest
```

### docker 运行

```bash
cp .env.example .env                # 按需修改;数据落 ./data(已挂卷)

docker compose up -d --build        # 起容器(restart: unless-stopped, mem_limit 512m)
docker compose ps                   # 等 STATUS 变 healthy
curl http://localhost:8080/healthz

docker compose logs -f panel
docker compose down
```

### 多 arch 镜像构建(树莓派 arm64 主目标)

```bash
# 需 buildx + qemu(首次:docker run --privileged --rm tonistiigi/binfmt --install arm64)
docker buildx create --name panelbuilder --driver docker-container --use
docker buildx build --platform linux/arm64,linux/amd64 -t panel-everything:latest .
```

> 容器以非 root 用户 `app` 运行;SQLite 数据库落在挂载卷 `/data/panel.db`。
> `mem_limit: 512m` 使用 compose v2 顶层字段(Docker Engine 直接生效);
> 若改用 Swarm/`docker stack` 部署需切换到 `deploy.resources.limits.memory`。

## 状态

> 项目阶段：基础设施搭建中(MS-001 / TASK-001 已完成工程骨架、容器化与 /healthz)
