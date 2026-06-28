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
| ARCH-004 | AI 额度监控（后期） | REQ-004 | draft | 2026-06-28 |

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
| TASK-016 | GPU 历史降采样 job(5m/1h) + 趋势查询 API *(MS-003，本期不实现)* | ARCH-002 | P2 | todo | — | 2026-06-28 |
| TASK-017 | 前端 GPU 趋势迷你图(默认折叠) *(MS-003，本期不实现)* | ARCH-002 | P2 | todo | — | 2026-06-28 |
| TASK-024 | 服务器注册管理 Web 表单(REQ-002 注册入口) | ARCH-002 | P1 | done | opus-4.8 | 2026-06-28 |
| TASK-020 | Tailscale 采集器(socket localapi) + 表 + 在线判定 | ARCH-003 | P1 | done | sonnet-4.6 | 2026-06-28 |
| TASK-021 | Tailscale REST API | ARCH-003 | P1 | done | sonnet-4.6 | 2026-06-28 |
| TASK-022 | 前端 NodeCard/NodeGrid/StaleWarning(e-ink 适配) | ARCH-003 | P1 | done | sonnet-4.6 | 2026-06-28 |
| TASK-023 | Azure-Tailscale 节点关联(node_azure_mapping + 徽标) *(MS-003，本期不实现)* | ARCH-003 | P2 | todo | — | 2026-06-28 |
| TASK-030 | 面板摄取端点 POST /api/ingest/ai-usage + AI 额度表 *(本期不实现)* | ARCH-004 | P2 | todo | — | 2026-06-28 |
| TASK-031 | 工作站 Reporter MVP:Codex 本地解析 *(本期不实现)* | ARCH-004 | P2 | todo | — | 2026-06-28 |
| TASK-032 | Reporter 扩展:Claude OAuth usage(带 JSONL 回退) *(本期不实现)* | ARCH-004 | P3 | todo | — | 2026-06-28 |
| TASK-033 | 前端 AI 额度卡片(泛化渲染, stale) *(本期不实现)* | ARCH-004 | P2 | todo | — | 2026-06-28 |

> **本期已交付**: TASK-001~005 (MS-001) + TASK-010~015、020~022 (MS-002)，共 **13 卡全部 done**。
> 其余 TASK（016/017/023/030~033）已出卡，本期不实现，对应 MS-003/MS-004。

## 质量指标

- **测试覆盖率**: 97%（308 passed，+1 integration 活体；1240 语句 / 42 未覆盖）
- **已知 Bug**: 0 open / 1 fixed（BUG-001 Tailscale localapi 协议，已修复）
- **待完成任务（本期）**: 0（13/13 done）
- **未实现任务（后期 MS-003/MS-004）**: 8（TASK-016/017/023/030~033）
- **已完成里程碑**: 2 / 4（MS-001、MS-002 代码完成并验证，待真机/人类验收）

## 里程碑概览

| ID | 标题 | 状态 | 关联任务 |
|----|------|------|----------|
| MS-001 | 基础设施跑通 | delivered | TASK-001~005 |
| MS-002 | 设备监控上线 | delivered | TASK-010~015、020~022 |
| MS-003 | 趋势与关联增强 | planned | TASK-016/017/023（本期不实现） |
| MS-004 | AI 额度监控 | planned | TASK-030~033（本期不实现） |
