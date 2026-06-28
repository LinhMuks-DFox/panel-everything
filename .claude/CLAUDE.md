# Panel Everything

一站式设备监控面板，实时查看所有计算设备的运行状态、计算任务、资源占用、AI 使用限额等。
Web app，响应式设计，支持 Kindle/iPhone/iPad 等多终端访问。部署目标：树莓派（ARM，资源受限）。

## AI 驱动开发模式

本项目采用 **人类零代码** 模式：人类只提需求和验收，所有产品/架构/代码工作由 AI 完成。

### 角色体系

三个 AI 角色协作，定义在 `roles/` 目录下：

| 角色 | 文件 | 职责 |
|------|------|------|
| **PM（产品经理）** | `roles/ROLE_PM.md` | 接收人类需求 → 精细化/消歧 → 输出需求文档 |
| **Architect（架构师）** | `roles/ROLE_Architect.md` | 接收需求文档 → 技术设计 → 分解任务卡 |
| **Coder（程序员）** | `roles/ROLE_Coder.md` | 接收任务卡 → 实现代码 + 测试 → 交付 |

### 混合模式运行

- PM 与人类在同一 Claude Code 会话中交互
- Architect 和 Coder 可以是同会话内的子 Agent，也可以拆到独立会话
- 角色文件既是 prompt 模板（供 Agent 调用时注入），也是参考文档

### 工作流

```
人类(自然语言) → PM(精细化) → [PM审核循环] → 需求文档(REQ)
                                                  ↓
                              Architect(设计) → [设计审核循环] → 架构文档(ARCH) + 任务卡(TASK)
                                                                                    ↓
                                                                 Coder(实现) → [代码审核] → 交付
```

每个环节支持多 Agent 并行和审核回退。

## 目录约定

```
panel_everything/
├── .claude/CLAUDE.md          # 本文件 — 项目级指令
├── roles/                     # AI 角色定义
│   ├── ROLE_PM.md
│   ├── ROLE_Architect.md
│   └── ROLE_Coder.md
├── docs/
│   ├── requirements/          # PM 产出：REQ-xxx.md
│   ├── architecture/          # Architect 产出：ARCH-xxx.md
│   ├── tasks/                 # Architect 产出：TASK-xxx.md
│   ├── templates/             # 文档模板
│   └── progress/              # 进度跟踪（STATUS / changelog / bugs / milestones）
├── src/                       # 源代码
└── tests/                     # 测试
```

## 工件格式

所有文档使用 **Markdown + YAML frontmatter** 格式。frontmatter 承载元数据（id、状态、优先级、关联关系），正文用 Markdown 描述内容。模板见 `docs/templates/`。

### 状态流转

- **REQ**: `draft` → `review` → `approved` / `rejected`
- **ARCH**: `draft` → `review` → `approved`
- **TASK**: `todo` → `in-progress` → `review` → `done` / `blocked`

### 编号规则

- 需求文档: `REQ-{三位数字}` (如 REQ-001)
- 架构文档: `ARCH-{三位数字}` (如 ARCH-001)
- 任务卡: `TASK-{三位数字}` (如 TASK-001)

## 部署约束

- **目标硬件**: 树莓派（ARM 架构，有限 RAM/CPU）
- 技术选型必须轻量，避免重型框架和高内存占用
- 前端必须响应式，适配从 Kindle e-ink 到 iPad 的各种屏幕

## 进度跟踪

`docs/progress/` 采用拆分结构，适合长期维护：

```
docs/progress/
├── STATUS.md              # 全局状态仪表板（单文件，始终更新为最新快照）
├── changelog/             # 开发日志，按月拆分：YYYY-MM.md
├── bugs/                  # Bug 跟踪，每个 bug 一个文件：BUG-xxx.md
└── milestones/            # 里程碑记录，每个里程碑一个文件：MS-xxx.md
```

| 内容 | 谁更新 | 何时更新 |
|------|--------|----------|
| `STATUS.md` | Architect | 任何 REQ/ARCH/TASK 状态变更时同步更新 |
| `changelog/YYYY-MM.md` | Coder | 任务开始和完成时追加记录（当月文件不存在则创建） |
| `bugs/BUG-xxx.md` | 发现者创建，修复者更新 | 发现 bug 时创建，修复后更新状态 |
| `milestones/MS-xxx.md` | Architect | 里程碑规划时创建，交付时更新 |

**规则**:
- STATUS.md 是全局唯一视图，所有状态变更必须同步反映
- changelog 每月文件内按时间倒序排列
- Bug 严重程度: `critical`（阻塞交付）> `major`（功能受损）> `minor`（体验问题）
- 模板见 `docs/templates/`

## 会话指引

当你进入一个新的 Claude Code 会话并需要扮演某个角色时：
1. 读取对应的 `roles/ROLE_xxx.md` 获取角色定义
2. 读取 `docs/` 下的相关文档了解当前项目状态
3. 严格按照角色定义的职责范围和输出规范工作
4. 产出物按模板格式写入对应目录
