---
id: TASK-023
title: "Azure-Tailscale 节点关联（node_azure_mapping + 徽标）"
status: todo
priority: P3
architecture: ARCH-003
dependencies: [TASK-020, TASK-013]
estimated_effort: S
executed_by:
created: 2026-06-28
updated: 2026-06-28
---

## 目标

**本轮不实现（降级 P3）**：用户选择 A100 走 Azure 公网而非接入 Tailscale，A100 无 tailscale 节点；且 GPU 跳采已由 TASK-018 按 Azure 电源态实现。待出现同时在 Azure 与 Tailscale 的机器再启用。

本文档仅作设计占位，供 Coder 在需要时直接参照实现，无需重新设计。

建立 Azure VM（`servers` 表）与 Tailscale 节点（`tailscale_nodes` 表）的映射关系，
使面板可在 VM 卡片上展示对应节点的在线态徽标，同时在 GPU 采集时利用映射判断节点是否可达
（deallocated/tailscale offline 则跳过 SSH 采集）。

## 技术规格

### 文件路径

- `src/panel/db/migrations/004_node_azure_mapping.sql` — 映射表 DDL
- `src/panel/api/tailscale/routes.py` — 追加 `/api/tailscale/mapping` CRUD 端点
- `src/panel/domain/models.py` — 追加 `NodeAzureMappingResponse`
- `src/panel/web/templates/partials/_vm_card.html` — 追加 Tailscale 在线徽标
- `tests/api/test_node_mapping.py`

### 映射表 DDL（migrations/004_node_azure_mapping.sql）

```sql
-- Azure VM 与 Tailscale 节点的关联表
-- auto_matched: 1 表示系统自动匹配（hostname 相似度），0 表示人工设置
CREATE TABLE IF NOT EXISTS node_azure_mapping (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    server_id       INTEGER NOT NULL,                  -- references servers(id)
    tailscale_node_id INTEGER NOT NULL,                -- references tailscale_nodes(id)
    auto_matched    INTEGER NOT NULL DEFAULT 1,
    confidence      REAL,                              -- 0.0~1.0，自动匹配时记录置信度
    created_at      TEXT NOT NULL,
    updated_at      TEXT NOT NULL,
    UNIQUE(server_id, tailscale_node_id)
);

CREATE INDEX IF NOT EXISTS idx_mapping_server ON node_azure_mapping(server_id);
CREATE INDEX IF NOT EXISTS idx_mapping_node   ON node_azure_mapping(tailscale_node_id);
```

### 自动匹配策略

系统在每次 Tailscale 采集完成后，尝试将 `tailscale_nodes.hostname` 与 `servers.hostname`
进行模糊匹配（去前缀/后缀后比较，如 `muxdesktop-wsl-ubuntu` ↔ `muxdesktop`）：

1. 先做精确匹配（hostname 完全一致）。
2. 再做前缀匹配（tailscale hostname 以 server hostname 开头，或反之）。
3. 置信度 = 较短名称长度 / 较长名称长度（>=0.7 才自动插入）。
4. 若已存在 `auto_matched=0` 的人工记录，不覆盖。

人工可通过 API 修正或新建关联（`auto_matched=0`）。

### REST API 追加端点

```python
GET  /api/tailscale/mapping          # 列出所有映射
POST /api/tailscale/mapping          # 手动创建映射 {server_id, tailscale_node_id}
DELETE /api/tailscale/mapping/{id}   # 删除指定映射
```

### 前端：VM 卡片追加 Tailscale 徽标

在 `_vm_card.html` 的 VM 卡片头部追加：

```html
{% if vm.tailscale_node %}
<span class="status-dot
  {% if vm.tailscale_node.online_state == 'ONLINE' %}status-ok
  {% elif vm.tailscale_node.online_state == 'OFFLINE' %}status-warn
  {% else %}status-error{% endif %}"
  title="Tailscale: {{ vm.tailscale_node.hostname }} {{ vm.tailscale_node.online_state }}"
  aria-label="Tailscale {{ vm.tailscale_node.online_state }}">
  {% if vm.tailscale_node.online_state == 'ONLINE' %}●{% elif vm.tailscale_node.online_state == 'OFFLINE' %}◐{% else %}○{% endif %}
</span>
{% endif %}
```

### GPU 采集联动（TASK-013 调整）

在 `collectors/gpu/collector.py` 的 `collect()` 中，采集前查询映射：
- 若 server 在 `node_azure_mapping` 中存在对应 tailscale 节点，且该节点 `online_state != 'ONLINE'`，
  则跳过该 server 的 SSH 采集，记 `MetricSample(status="unreachable", value_text="tailscale_offline")`。
- 若 Azure VM `power_state` 为 `deallocated`/`stopped`，同样跳过（TASK-012/013 已有此逻辑，MS-003 确认整合）。

## 实现指引

1. 写 `migrations/004_node_azure_mapping.sql`，加入 `migrate.py` 加载顺序（按文件名升序，自动执行）。
2. 在 `collectors/tailscale/collector.py` 的 `collect()` 末尾（写完节点后）调用 `repo.auto_match_nodes()`，执行自动匹配逻辑（置信度>=0.7 则 upsert mapping，不覆盖 `auto_matched=0`）。
3. 实现映射 CRUD API，`POST` 时验证 `server_id` 和 `tailscale_node_id` 均存在，否则返回 422。
4. 在 Azure+GPU dashboard 聚合查询（TASK-014 的 `/api/v1/dashboard/azure`）中 LEFT JOIN `node_azure_mapping` + `tailscale_nodes`，将 tailscale 在线态嵌入 VM 响应对象。
5. 更新 `_vm_card.html` partial，读取 `vm.tailscale_node`（可为 None）条件性渲染徽标。
6. GPU collector 在 SSH 前查映射表，`online_state != 'ONLINE'` 时跳采，减少无效 SSH 超时。

## 测试要求

- [ ] 自动匹配测试：插入 `servers(hostname='muxdesktop')` 和 `tailscale_nodes(hostname='muxdesktop-wsl-ubuntu')`，调用 `auto_match_nodes()`，验证产生置信度>=0.7 的 mapping 记录。
- [ ] 精确匹配优先：hostname 完全一致时置信度=1.0。
- [ ] 人工记录保护：存在 `auto_matched=0` 记录时，自动匹配不修改。
- [ ] API CRUD 测试：POST 创建 → GET 列出 → DELETE 删除，验证正确。
- [ ] GPU 跳采测试：tailscale_node.online_state=OFFLINE 时，GPU collect() 不发起 SSH 连接（mock asyncssh.connect 验证未调用）。

## 完成标准

- [ ] `node_azure_mapping` 表正确创建，UNIQUE 约束生效。
- [ ] 自动匹配在 Tailscale 采集后触发，置信度<0.7 的不自动插入。
- [ ] VM 卡片在有 Tailscale 映射时显示对应节点在线态徽标。
- [ ] GPU 采集器在节点 Tailscale 离线时跳过 SSH，`collector_run` 记录对应 sample status。
- [ ] 映射 CRUD API 全部端点可用，无 5xx。
- [ ] `ruff check` 零 error，测试全绿。
