---
id: TASK-024
title: "服务器注册管理 Web 表单(REQ-002 注册入口)"
status: done
priority: P1
architecture: ARCH-002
dependencies: [TASK-011, TASK-004]
estimated_effort: S
executed_by: claude-opus-4-8[1m]
created: 2026-06-28
updated: 2026-06-28
---

## 目标

为面板提供一个**人类可手动填写监控目标连接/校验信息**的 Web 入口(SSH host/port/user/key、Azure VM 名、是否 GPU 等),补齐 REQ-002「注册机制」在 UI 层的缺口(此前只有 REST API,无表单)。非编程用户可直接在浏览器注册/删除服务器。

## 技术规格

- 复用既有 `GpuRepository.insert_server / get_all_servers / delete_server` 与 `ServerIn` 模型,不新增数据层。
- 新增 SSR 路由(`web/routes.py`):
  - `GET /servers` — 渲染注册表单 + 已注册列表
  - `POST /servers` — 表单提交(`Form`,python-multipart 已在依赖),写库后 303 重定向回 `/servers?flash=...`
  - `POST /servers/{id}/delete` — 删除后重定向
- 凭证保护:列表不展示 `ssh_key_path` 值;沿用 `ServerOut` 白名单约束(API 侧)。
- 多终端:纯表单 + 原生 CSS,无 JS 依赖(Kindle 可用);e-ink 高对比聚焦、无 box-shadow/动画。
- header 增加「总览 / 管理服务器」导航。

## 实现指引

- 表单字段映射到 `ServerIn`;空白串归一为 None;`has_gpu` 复选框 → bool。
- 重名 `aiosqlite.IntegrityError` → `flash=dup`;其它异常 → `flash=fail`。
- flash 码 → 中文消息映射,避免在 HTTP Location 头放非 ASCII。

## 测试要求

- [x] `GET /servers` 返回 200 且含表单与列表
- [x] `POST /servers` 注册后出现在列表,重名报错不崩
- [x] `POST /servers/{id}/delete` 删除生效
- [x] 全套 `pytest` 回归 308 passed(e-ink 约束:CSS 无 box-shadow)

## 完成标准

- [x] 浏览器可注册/删除监控目标,无需 curl
- [x] 凭证字段不在列表页明文展示
- [x] e-ink/响应式约束满足,导航入口可达
- [x] ruff 通过,测试全绿
