# 项目状态总览

> 最后更新: 2026-06-28

## 需求进度

| ID | 标题 | 优先级 | 状态 | 更新日期 |
|----|------|--------|------|----------|
| REQ-001 | 系统整体约束与部署要求 | P0 | draft | 2026-06-28 |
| REQ-002 | Azure 服务器监控 | P1 | draft | 2026-06-28 |
| REQ-003 | Tailscale 网络设备监控 | P1 | draft | 2026-06-28 |
| REQ-004 | AI 使用额度监控 | P1 | draft | 2026-06-28 |

> REQ 仍为 draft（状态属 PM 职责，未擅改）；架构与实现已基于其内容推进。

## 架构进度

| ID | 标题 | 关联需求 | 状态 | 更新日期 |
|----|------|----------|------|----------|
| ARCH-001 | 总体架构与基础设施 | REQ-001 | approved | 2026-06-28 |
| ARCH-002 | Azure VM + GPU 监控 | REQ-002 | approved | 2026-06-28 |
| ARCH-003 | Tailscale 网络监控 | REQ-003 | approved | 2026-06-28 |
| ARCH-004 | AI 额度监控 | REQ-004 | approved | 2026-06-28 |

## 任务进度

| ID | 标题 | 关联架构 | 优先级 | 状态 | 负责 | 更新日期 |
|----|------|----------|--------|------|------|----------|
| TASK-001 | 项目骨架 + Dockerfile(多阶段多 arch) + compose + /healthz | ARCH-001 | P0 | done | opus-4.8 | 2026-06-28 |
| TASK-002 | SQLite(WAL) 连接 + schema 基线 + repository 薄层 | ARCH-001 | P0 | done | opus-4.8 | 2026-06-28 |
| TASK-003 | Collector 框架:协议 + 注册表 + 调度器 + 框架级降级 | ARCH-001 | P0 | done | opus-4.8 | 2026-06-28 |
| TASK-004 | SSR 前端壳:base 布局 + 响应式/e-ink CSS + 轮询/meta-refresh 降级 | ARCH-001 | P0 | done | sonnet-4.6 | 2026-06-28 |
| TASK-005 | 配置与凭证管理 + response model 白名单 + 日志脱敏 | ARCH-001 | P0 | done | sonnet-4.6 | 2026-06-28 |
| TASK-010 | Azure/GPU 专用表 schema | ARCH-002 | P1 | done | sonnet-4.6 | 2026-06-28 |
| TASK-011 | 服务器注册 CRUD API(凭证不回传) | ARCH-002 | P1 | done | sonnet-4.6 | 2026-06-28 |
| TASK-012 | Azure VM 采集器(ClientSecretCredential, Reader) | ARCH-002 | P1 | done | opus-4.8 | 2026-06-28 |
| TASK-013 | SSH GPU 采集器(asyncssh + nvidia-smi 多卡解析) | ARCH-002 | P1 | done | opus-4.8 | 2026-06-28 |
| TASK-014 | Azure+GPU dashboard 聚合 API | ARCH-002 | P1 | done | sonnet-4.6 | 2026-06-28 |
| TASK-015 | 前端 VmCard + GpuCard + 状态徽标(e-ink 适配) | ARCH-002 | P1 | done | sonnet-4.6 | 2026-06-28 |
| TASK-016 | GPU 历史降采样 job(5m/1h) + 趋势查询 API | ARCH-002 | P2 | done | agents | 2026-06-28 |
| TASK-017 | 前端 GPU 趋势迷你图(默认折叠) | ARCH-002 | P2 | done | agents | 2026-06-28 |
| TASK-018 | Azure 动态公网 IP 解析 + 只读 SP 认证对齐 | ARCH-002 | P1 | done | agents | 2026-06-28 |
| TASK-019 | 只读 SP 创建指引 + A100 预置注册 + 部署文档 | ARCH-002 | P1 | done | agents | 2026-06-28 |
| TASK-024 | 服务器注册管理 Web 表单(REQ-002 注册入口) | ARCH-002 | P1 | done | opus-4.8 | 2026-06-28 |
| TASK-020 | Tailscale 采集器(socket localapi) + 表 + 在线判定 | ARCH-003 | P1 | done | sonnet-4.6 | 2026-06-28 |
| TASK-021 | Tailscale REST API | ARCH-003 | P1 | done | sonnet-4.6 | 2026-06-28 |
| TASK-022 | 前端 NodeCard/NodeGrid/StaleWarning(e-ink 适配) | ARCH-003 | P1 | done | sonnet-4.6 | 2026-06-28 |
| TASK-023 | Azure-Tailscale 节点关联(node_azure_mapping + 徽标) *(P3 deferred)* | ARCH-003 | P3 | todo | — | 2026-06-28 |
| TASK-030 | 面板摄取端点 POST /api/ingest/ai-usage + ai_provider 表 | ARCH-004 | P2 | done | agents | 2026-06-28 |
| TASK-031 | 工作站 Reporter MVP:Codex 本地解析 | ARCH-004 | P2 | done | agents | 2026-06-28 |
| TASK-032 | Reporter 扩展:Claude Code jsonl(带 OAuth 回退) | ARCH-004 | P3 | done | agents | 2026-06-28 |
| TASK-033 | 前端 AI 额度卡片(泛化渲染, stale) | ARCH-004 | P2 | done | agents | 2026-06-28 |
| TASK-040 | 通用 metric_history retention job | ARCH-001 | P2 | done | agents | 2026-06-28 |

> **已交付**: TASK-001~005 (MS-001) + TASK-010~015、020~022、024 (MS-002) + TASK-016/017/018/019/030/031/032/033/040 (MS-003/004/005)，全部 done。
> **本轮交付（多 Agent 并行波次）**: MS-003（GPU 趋势）、MS-004（AI 额度）、MS-005（REQ-002 真实对齐 + retention）三里程碑代码完成并提交；经 7 维对抗式评审（18 条确证发现，修复 15 / 延后 3）+ 全套测试。TASK-023 降级 P3，暂缓。
> **附加交付**: `start.sh` + `Makefile` 傻瓜一键启动；`docs/modules/` 全模块开发者文档（10 模块 + 索引）。

## 质量指标

- **测试**: 427 passed（`-m "not integration"`），ruff 全绿，`from panel.main import app` 正常
- **已知 Bug**: 0 open / 1 fixed（BUG-001 Tailscale localapi 协议，已修复）
- **已交付任务**: 23（MS-001 5 + MS-002 9 + MS-003/004/005 9，全部 done）
- **延后/后续**: TASK-023（P3 deferred）；评审延后 3 项（e-ink 灰度死代码、retention 回填桶、reporter 测试断言）；建议后续 asyncssh 延迟导入根治 Mac VZ SIGILL
- **运行验证**: 面板已在开发机（colima/QEMU）`Up (healthy)`，`/healthz` ok，各数据源未配置时优雅降级
- **里程碑**: MS-001/MS-002 delivered；MS-003/MS-004/MS-005 代码交付（待真机 A100 / Reporter 部署 / 人类验收）

## 里程碑概览

| ID | 标题 | 状态 | 关联任务 |
|----|------|------|----------|
| MS-001 | 基础设施跑通 | delivered | TASK-001~005 |
| MS-002 | 设备监控上线 | delivered | TASK-010~015、020~022、024 |
| MS-003 | 趋势与关联增强 | in-progress | TASK-016/017（023 降级 P3 暂缓） |
| MS-004 | AI 额度监控 | in-progress | TASK-030~033 |
| MS-005 | REQ-002 真实对齐 + 趋势/额度/retention 收尾 | in-progress | TASK-018/019/016/017/030~033/040 |
