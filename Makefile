# Panel Everything — 傻瓜式运维目标。
#
# 常用:
#   make up        一键启动(= ./start.sh)
#   make up-seed   一键启动并预置注册 A100
#   make status    查看容器状态 + 健康检查
#   make logs      实时日志
#   make down      停止并移除容器
#
# 全部目标见 `make help`(默认目标)。

SHELL := /bin/bash

# 从 .env 读 PANEL_PORT(无 .env 或未设则默认 8080),用于 status 的 healthz 探测。
# 注意:make 会把行内裸 '#' 当注释,连 $(shell) 内也不例外,所以这里不写 '#';
# awk 只匹配「行首(可含空格)即 PANEL_PORT=」的行,注释行 '# PANEL_PORT' 因以 # 开头天然被排除。
PANEL_PORT := $(shell awk -F= '/^[[:space:]]*PANEL_PORT[[:space:]]*=/ {v=$$2} END {gsub(/[[:space:]"]/,"",v); if (v ~ /^[0-9]+$$/) print v; else print 8080}' .env 2>/dev/null || echo 8080)

.PHONY: help up up-seed down logs status rebuild seed

# 默认目标:打印帮助。
help:
	@echo "Panel Everything — make 目标:"
	@echo "  make up        一键启动 (= ./start.sh):自举 .env/secrets + 构建 + 等待就绪"
	@echo "  make up-seed   一键启动并预置注册 A100 (= ./start.sh --seed)"
	@echo "  make down      停止并移除容器 (docker compose down)"
	@echo "  make logs      跟随实时日志 (docker compose logs -f --tail=100)"
	@echo "  make status    容器状态 (docker compose ps) + healthz 探测"
	@echo "  make rebuild   强制重建:down 后重新 build 启动"
	@echo "  make seed      单独注册 A100 (scripts/seed_a100.sh)"
	@echo ""
	@echo "首次使用直接: make up   (不懂配置也能起,Azure/Tailscale 缺失自动降级)"

up:
	@./start.sh

up-seed:
	@./start.sh --seed

down:
	docker compose down

logs:
	docker compose logs -f --tail=100

status:
	docker compose ps
	@echo "---- healthz (http://localhost:$(PANEL_PORT)/healthz) ----"
	@curl -fsS "http://localhost:$(PANEL_PORT)/healthz" && echo "" || echo "healthz 未返回 200(容器可能未就绪 / 未启动)"

rebuild:
	docker compose down
	@./start.sh

seed:
	scripts/seed_a100.sh
