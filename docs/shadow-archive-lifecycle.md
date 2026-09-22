# 影子档案生命周期（T31）

## 1. 机制概览

```
┌─────────────────┐     ┌──────────────────┐     ┌─────────────────┐
│  delete_memory  │────▶│  shadow_delete   │────▶│ archive_by_id   │
│  (memory.py)    │     │  (event_outbox)  │     │  (reconcile.py) │
└─────────────────┘     └──────────────────┘     └─────────────────┘
         │                                               │
         │  同事务 DELETE memory_versions                │  移动 → vault/_trash/{today}/...
         ▼                                               ▼
┌─────────────────┐     ┌──────────────────┐     ┌─────────────────┐
│ store_memory    │────▶│  shadow_archive  │────▶│ archive_by_path │
│ conflict_overw..│     │  (event_outbox)  │     │  (reconcile.py) │
└─────────────────┘     └──────────────────┘     └─────────────────┘
         │                                               │
         │  跨天覆盖时 old_date != new_date               │  移动 → vault/_trash/{today}/...
         ▼                                               ▼
┌─────────────────┐     ┌──────────────────┐
│ reconcile_shadow│     │  审计事件         │
│ _archives()     │────▶│  events.shadow_  │
│ (定时/手动触发)  │     │  archive         │
└─────────────────┘     └──────────────────┘
```

## 2. 事件驱动（即时路径）

### 2.1 删除记忆
- `delete_memory` 事务内：
  - `DELETE FROM memory_pool`
  - `DELETE FROM memory_versions`（CD-062 收口）
  - `INSERT INTO event_outbox (event_type='shadow_delete', ...)`
- outbox 消费者调用 `archive_by_memory_id()`：扫描所有分干的 `vault/memory/*/*.md`，匹配 memory_id → 移入 `_trash`

### 2.2 跨天覆盖
- `store_memory` 的 `conflict_overwrite` 路径：若 `old_date != new_date`，同事务追加 `shadow_archive` 事件（payload 含 `old_path`）
- outbox 消费者调用 `archive_by_path()`：直接移动指定旧路径到 `_trash`

## 3. 对账兜底（最终一致路径）

### 3.1 触发方式
- **启动对账**：`SYNC_HUB_SHADOW_RECONCILE_ON_START=1`（默认开启），延迟 10s 后自动运行；失败只告警，不阻塞启动
- **手动对账**：`POST /api/v1/maintenance/shadow-reconcile`（走 CD-061 运维门，需 manager/orchestrator/hub_token）

### 3.2 对账逻辑
```python
stats = reconcile_shadow_archives(data_trunk, db_path)
# 返回 {scanned, orphan_archived, duplicate_archived, skipped, errors}
```

1. 扫描所有分干 `vault/memory/*/*.md`
2. 按 memory_id 分组，查主库 `memory_pool`
3. **孤儿**：主库无该 memory_id → 全部归档（reason=`orphan`）
4. **重复旧档**：同一 memory_id 多日期 → 保留最新日期（字符串比较），其余归档（reason=`superseded`）
5. **幂等**：目标路径已存在时跳过，计入 `skipped`；连跑两次第二次 `orphan_archived + duplicate_archived == 0`

### 3.3 归档语义
- **禁止物理删除**：一律 `shutil.move` 到 `vault/_trash/{今日日期}/{原相对路径}`
- 每次归档写审计事件 `events.shadow_archive`（含 `memory_id` / `from` / `to` / `reason`）
- 失败路径 `logger.warning`（含 memory_id、路径、异常类型），禁止 `except: pass`

## 4. 运维操作

### 4.1 手动触发对账
```bash
curl -X POST http://localhost:3060/api/v1/maintenance/shadow-reconcile \
  -H "Authorization: Bearer $HUB_TOKEN"
```

### 4.2 从 _trash 恢复
```bash
# 找到被归档的档案
cd data-trunk/branches/default/vault/_trash/2026-09-20

# 移回原位（或新建日期目录）
mv vault/memory/2026-09-19/xxx.md ../../memory/2026-09-19/xxx.md
```

> 注：恢复后若该 memory_id 已在 `memory_pool` 中删除，下次对账会再次归档。如需永久保留，需同时恢复 DB 行。

### 4.3 关闭启动对账
```bash
export SYNC_HUB_SHADOW_RECONCILE_ON_START=0
python main.py
```

## 5. 改动清单

| 文件 | 改动 |
|------|------|
| `hub_mixins/memory.py` | delete_memory：入队 shadow_delete + 清 memory_versions；store_memory 覆盖路径：跨天入队 shadow_archive |
| `hub_mixins/shadow/reconcile.py` | 对账/归档实现（扫描、孤儿、重复、归档、审计） |
| `hub_mixins/shadow/__init__.py` | re-export `reconcile_shadow_archives` |
| `hub_core.py` | outbox 消费侧：`_shadow_delete_sync` / `_shadow_archive_sync`；启动对账开关 `schedule_shadow_reconcile()` |
| `routes_maintenance.py` | `POST /api/v1/maintenance/shadow-reconcile`（运维门 + ops_trigger 审计） |
| `tests/test_shadow_lifecycle.py` | R-1~R-4 + 事件消费 + 归档跳过 |
| `docs/shadow-archive-lifecycle.md` | 本文档 |
