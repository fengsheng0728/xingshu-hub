"""
Memory Pool 审计 — append-only JSONL
复用 Agent 端 AuditLogger 格式：每行一条 JSON 记录。
写入 audit/memory_pool.jsonl
"""
import json, os, time
from datetime import datetime

try:
    from models import CONFIG as _CONFIG   # CD-070b：产物根可配（env > config.yaml > 仓库内默认）
except Exception:                          # 独立脚本场景：退回仓库内默认路径
    _CONFIG = None
# CD-070b（2026-09-20）：审计根可配（测试态 → tmp）。既有测试 monkeypatch 本模块 AUDIT_DIR 仍生效。
AUDIT_DIR = (_CONFIG.AUDIT_DIR if _CONFIG else "") or os.path.join(
    os.path.dirname(os.path.dirname(__file__)), "audit")
AUDIT_FILE = os.path.join(AUDIT_DIR, "memory_pool.jsonl")

# S2：jsonl 滚动链（窗口 hash 挂 audit_log 主链；DB 不可用静默跳过）
_rolling_chain = None

def _get_rolling_chain():
    global _rolling_chain
    if _rolling_chain is None:
        try:
            from audit_chain import JsonlRollingChain
            db_path = ""
            try:
                from models import CONFIG
                db_path = CONFIG.DB_PATH
            except Exception:
                db_path = os.environ.get("SYNC_HUB_DB", "")
            if db_path:
                _rolling_chain = JsonlRollingChain(db_path, AUDIT_FILE, "memory_pool.jsonl")
        except Exception:
            _rolling_chain = None
    return _rolling_chain


def _ensure_dir():
    os.makedirs(AUDIT_DIR, exist_ok=True)


def audit_memory(action: str, agent_id: str, memory_key: str,
                  memory_id: str = "", raise_on_error: bool = False, **kwargs):
    """
    追加一条审计记录。

    action: write | delete | inject | merge | conflict_overwrite
    actor: user | agent | system
    额外字段（session_id, similarity, old_content, new_content, confidence 等）
    作为 kwargs 传入。
    raise_on_error: CD-045 outbox 消费者专用——True 时写失败向上抛（fail-closed
    感知，事件行留 pending 重试）；默认 False 保持旧语义（静默不阻塞主流程）。
    """
    _ensure_dir()

    entry = {
        "ts": datetime.utcnow().isoformat() + "Z",
        "action": action,
        "agent_id": agent_id,
        "memory_key": memory_key,
        "memory_id": memory_id,
    }
    entry.update({k: v for k, v in kwargs.items() if v})

    try:
        line = json.dumps(entry, ensure_ascii=False) + "\n"
        # S2：滚动链统一写入（写文件 + 窗口锚定）；链不可用时降级原样写
        ch = _get_rolling_chain()
        if ch:
            ch.append_line(line)
        else:
            with open(AUDIT_FILE, "a", encoding="utf-8") as f:
                f.write(line)
    except Exception:
        if raise_on_error:
            raise
        pass  # 审计写入失败静默，不阻塞主流程
