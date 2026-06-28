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

## 状态

> 项目阶段：框架搭建中
