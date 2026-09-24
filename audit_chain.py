"""
S2 审计 hash chain（2026-08-05）— 不可抵赖审计
================================================

D4 拍板（2026-08-05 用户处置）覆盖范围：
- audit_log + disclosure_log 双行级平行链
- memory_pool.jsonl / transport.jsonl 走文件级滚动链（窗口 hash 挂主链，
  不搬文件入库防存储翻倍）
- 非审计运行数据（buffer_log / wiki 等）明确排除

实现：
1. audit_log 表（主链，新建）：承接 events 类审计（_log_event 双写镜像）
   + jsonl 窗口锚定记录（entry_type='jsonl_anchor'）
2. disclosure_log 表：加 prev_hash / entry_hash 列（披露链）
3. 两条链均按  entry_hash = SHA256(canonical_json(payload) | "|" | prev_hash)
   追加；单事务读 MAX(log_id) + INSERT（SQLite 单写者天然串行）
4. jsonl 滚动链：每窗口（默认 1000 行）计算 sha256(窗口全部行原文拼接)，
   作为 anchor 记录写入 audit_log（ref_table 标明文件），verify 时重算对比
5. 校验：POST /api/audit/verify 遍历双链重放 + 重算 jsonl 窗口，输出
   first_bad_id / checked 数

设计约束：
- 写路径零阻塞：hash 计算是本地 CPU 极小开销（<0.1ms/条）
- 篡改/删除/插入任意一条 → 其后所有 hash 校验失败（链式依赖）
- verify 是只读操作，可任意调用
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import secrets
import socket
import sqlite3
import threading
import urllib.request
from datetime import datetime, timezone
from typing import Dict, List, Optional

logger = logging.getLogger("xingshu.audit_chain")

GENESIS = "GENESIS"
DEFAULT_WINDOW = 1000  # jsonl 滚动链窗口大小
DEFAULT_JSONL_ROTATE_BYTES = 20 * 1024 * 1024  # CD-022: 单文件超 20MB 轮转(归档保留, verify 多段校验)

# ---- 行级链（audit_log / disclosure_log） ----

# 审计记录字段顺序（canonical 化用）——决定 hash 稳定性的字段白名单。
# 新增列必须同步这里，否则历史 hash 全断（这是特性不是 bug：schema 变更
# 本身会触发 verify 失败，提醒需要显式迁移锚点）。
_AUDIT_FIELDS = (
    "log_id", "entry_type", "ref_table", "ref_id", "payload",
    "prev_hash", "entry_hash", "created_at",
)
_DISCLOSURE_FIELDS = (
    "log_id", "task_id", "from_agent_id", "to_agent_id", "memory_id",
    "disclosed_level", "disclosed_content", "disclosed_at", "reason", "trace_id",
    "prev_hash", "entry_hash",
)


def canonical_json(obj) -> str:
    """canonical JSON：sort_keys + 紧凑分隔符（hash 输入的稳定形式）。"""
    return json.dumps(obj, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def compute_hash(payload_json: str, prev_hash: str) -> str:
    """entry_hash = SHA256(canonical_payload | "|" | prev_hash)。"""
    return hashlib.sha256(f"{payload_json}|{prev_hash}".encode("utf-8")).hexdigest()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _conn(db_path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout = 5000")
    return conn


# ---- audit_log 主链 ----

class AuditChain:
    """audit_log 主链写入器（线程安全）。

    连接复用：每线程一个 SQLite 连接（threading.local），避免每次 append
    新建连接 + fsync 的开销——P99 写入延迟验收 < 5ms 依赖此优化。
    """

    def __init__(self, db_path: str):
        self._db_path = db_path
        self._lock = threading.Lock()
        self._local = threading.local()

    def _get_conn(self):
        conn = getattr(self._local, "conn", None)
        if conn is None:
            conn = sqlite3.connect(self._db_path, check_same_thread=False)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA busy_timeout = 5000")
            # CD-034 R2（2026-09-17 用户拍板）：审计连接改 FULL —— 断电/内核崩溃下
            # NORMAL 可能丢「上次 checkpoint 之后」的一段尾，且链前缀自洽导致本地校验
            # 查不出（只有外部锚能检出）。审计是低频写，fsync 代价可接受；
            # 注意此处不动 db.py 的全局热路径连接。回退 = 把本行改回 NORMAL。
            conn.execute("PRAGMA synchronous = FULL")
            self._local.conn = conn
        return conn

    def _tail_hash(self, conn) -> str:
        row = conn.execute(
            "SELECT entry_hash FROM audit_log ORDER BY log_id DESC LIMIT 1"
        ).fetchone()
        return row["entry_hash"] if row else GENESIS

    def append(self, entry_type: str, ref_table: str, ref_id: str,
               payload: dict) -> Dict[str, str]:
        """追加一条审计记录，返回 {log_id, prev_hash, entry_hash}。"""
        payload_json = canonical_json(payload)
        with self._lock:
            conn = self._get_conn()
            prev_hash = self._tail_hash(conn)
            entry_hash = compute_hash(payload_json, prev_hash)
            cur = conn.execute(
                "INSERT INTO audit_log (entry_type, ref_table, ref_id, payload,"
                " prev_hash, entry_hash, created_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?)",
                (entry_type, ref_table, ref_id, payload_json,
                 prev_hash, entry_hash, _now()),
            )
            conn.commit()
            return {"log_id": cur.lastrowid, "prev_hash": prev_hash,
                    "entry_hash": entry_hash}

    def verify(self, start_id: int = 0, end_id: int = 0) -> Dict:
        """重放校验主链。start_id/end_id 为 0 表示全链。

        无 audit_log 表（老库未迁移 S2）→ 返回 valid=True checked=0
        （无链即无可校验，不视为损坏）。
        """
        try:
            conn = self._get_conn()
        except Exception as e:
            logger.warning("audit_chain verify: 无法连接 %s", e)
            return {"valid": True, "checked": 0, "first_bad_id": None, "end_id": 0}
        try:
            conn.execute("SELECT 1 FROM audit_log LIMIT 1")
        except Exception:
            return {"valid": True, "checked": 0, "first_bad_id": None, "end_id": 0}
        # 链头语义（老库迁移兼容）：第一条有 hash 的记录为链起点
        if start_id <= 0:
            head = conn.execute(
                "SELECT COALESCE(MIN(log_id), 0) AS m FROM audit_log"
                " WHERE entry_hash != ''"
            ).fetchone()
            start_id = head["m"]
        if start_id <= 0:
            return {"valid": True, "checked": 0, "first_bad_id": None, "end_id": end_id}
        if end_id <= 0:
            row = conn.execute(
                "SELECT COALESCE(MAX(log_id), 0) AS m FROM audit_log").fetchone()
            end_id = row["m"]
        rows = conn.execute(
            "SELECT * FROM audit_log WHERE log_id BETWEEN ? AND ? ORDER BY log_id",
            (start_id, end_id),
        ).fetchall()
        expected_prev = GENESIS
        if start_id > 0:
            # 链外前驱：起点前一记录的 entry_hash
            prev = conn.execute(
                "SELECT entry_hash FROM audit_log WHERE log_id = ?",
                (start_id - 1,),
            ).fetchone()
            if prev and prev["entry_hash"]:
                expected_prev = prev["entry_hash"]
        checked = 0
        for r in rows:
            payload_json = canonical_json(json.loads(r["payload"])) if r["payload"] else ""
            recomputed = compute_hash(payload_json, expected_prev)
            if r["prev_hash"] != expected_prev or r["entry_hash"] != recomputed:
                return {"valid": False, "checked": checked,
                        "first_bad_id": r["log_id"], "reason": "hash_mismatch"}
            expected_prev = r["entry_hash"]
            checked += 1
        return {"valid": True, "checked": checked, "first_bad_id": None,
                "end_id": end_id}


# ---- disclosure_log 披露链 ----

class DisclosureChain:
    """disclosure_log 披露链（复用同构逻辑）。"""

    def __init__(self, db_path: str):
        self._db_path = db_path
        self._lock = threading.Lock()

    def _tail_hash(self, conn) -> str:
        # 排除未哈希记录（entry_hash='' 是 INSERT 后尚未回填的行）
        row = conn.execute(
            "SELECT entry_hash FROM disclosure_log WHERE entry_hash != ''"
            " ORDER BY log_id DESC LIMIT 1"
        ).fetchone()
        return row["entry_hash"] if row else GENESIS

    def append(self, log_id: int, row_data: Dict) -> Dict[str, str]:
        """在已有 disclosure_log 记录上补 prev_hash/entry_hash（同一事务）。

        注意：payload 从 DB 读实际行构造（与 verify 同源）——INSERT 时 NULL vs
        传入 "" 的字段（如 trace_id）若不归一，hash 会不一致。
        """
        with self._lock:
            conn = _conn(self._db_path)
            try:
                row = conn.execute(
                    "SELECT * FROM disclosure_log WHERE log_id = ?", (log_id,)
                ).fetchone()
                if row is None:
                    raise ValueError(f"disclosure_log {log_id} not found")
                rd = {k: row[k] for k in _DISCLOSURE_FIELDS if k in row.keys()}
                rd.pop("prev_hash", None)
                rd.pop("entry_hash", None)
                # 归一：None → ""（DB 未填字段与 JSON 空串一致）
                rd = {k: ("" if v is None else v) for k, v in rd.items()}
                payload_json = canonical_json(rd)
                prev_hash = self._tail_hash(conn)
                entry_hash = compute_hash(payload_json, prev_hash)
                conn.execute(
                    "UPDATE disclosure_log SET prev_hash=?, entry_hash=? WHERE log_id=?",
                    (prev_hash, entry_hash, log_id),
                )
                conn.commit()
                return {"log_id": log_id, "prev_hash": prev_hash,
                        "entry_hash": entry_hash}
            finally:
                conn.close()

    def append_row(self, row_data: Dict) -> Dict[str, str]:
        """XS-003（2026-09-08）：INSERT 与 hash 回填同一连接同一事务，失败整体
        回滚——不存在 entry_hash='' 的半截裸行。

        披露写入路径的唯一入口（disclosure._log_disclosure 使用）；
        append(log_id, ...) 保留给既有测试/历史补链。
        """
        with self._lock:
            conn = _conn(self._db_path)
            try:
                # 归一：None → ""（与 verify 端一致，防 hash 不一致）
                rd = {k: ("" if v is None else v) for k, v in row_data.items()}
                cols = _DISCLOSURE_FIELDS[1:10]  # 前 9 列（去掉 log_id/prev/entry）
                cur = conn.execute(
                    "INSERT INTO disclosure_log (task_id, from_agent_id,"
                    " to_agent_id, memory_id, disclosed_level, disclosed_content,"
                    " disclosed_at, reason, trace_id)"
                    " VALUES (?,?,?,?,?,?,?,?,?)",
                    tuple(rd.get(k, "") for k in cols),
                )
                log_id = cur.lastrowid
                # 同连接读回实际行构造 payload（与 verify 同源）
                row = conn.execute(
                    "SELECT * FROM disclosure_log WHERE log_id = ?", (log_id,)
                ).fetchone()
                rd = {k: row[k] for k in _DISCLOSURE_FIELDS if k in row.keys()}
                rd.pop("prev_hash", None)
                rd.pop("entry_hash", None)
                rd = {k: ("" if v is None else v) for k, v in rd.items()}
                payload_json = canonical_json(rd)
                prev_hash = self._tail_hash(conn)
                entry_hash = compute_hash(payload_json, prev_hash)
                conn.execute(
                    "UPDATE disclosure_log SET prev_hash=?, entry_hash=? WHERE log_id=?",
                    (prev_hash, entry_hash, log_id),
                )
                conn.commit()
                return {"log_id": log_id, "prev_hash": prev_hash,
                        "entry_hash": entry_hash}
            finally:
                conn.close()

    def verify(self, start_id: int = 0, end_id: int = 0) -> Dict:
        """重放校验披露链。

        XS-003（2026-09-08）：已链区间内遇 entry_hash='' 断链行 → 立即返回
        valid=False + reason="unlinked_row" + first_unlinked_id（不再静默跳过，
        消除旧两段式写入崩溃留下的永久审计盲区）；hash_mismatch 与 unlinked_row
        两种失败均带 unlinked_count/first_unlinked_id 附加键（hash_mismatch 时
        unlinked_count=0, first_unlinked_id=None）。
        既有语义保持：start_id<=0 时链头取 MIN(entry_hash!='')——S2 前无 hash
        历史行在链头之前，不扫、仍豁免；_tail_hash 排除空 hash 行行为不变。
        """
        conn = _conn(self._db_path)
        try:
            try:
                conn.execute("SELECT 1 FROM disclosure_log LIMIT 1")
            except Exception:
                # 无披露表/无披露链（老库）→ 无可校验，不视为损坏
                return {"valid": True, "checked": 0, "first_bad_id": None,
                        "end_id": 0}
            if end_id <= 0:
                row = conn.execute(
                    "SELECT COALESCE(MAX(log_id), 0) AS m FROM disclosure_log").fetchone()
                end_id = row["m"]
            # 链头语义（老库迁移兼容）：第一条有 hash 的记录为链起点——
            # S2 前历史记录无 hash（无法补链），从其后的链化记录开始校验。
            if start_id <= 0:
                head = conn.execute(
                    "SELECT COALESCE(MIN(log_id), 0) AS m FROM disclosure_log"
                    " WHERE entry_hash != ''"
                ).fetchone()
                start_id = head["m"]
            if start_id <= 0:
                return {"valid": True, "checked": 0, "first_bad_id": None,
                        "end_id": end_id}
            rows = conn.execute(
                "SELECT * FROM disclosure_log WHERE log_id BETWEEN ? AND ? ORDER BY log_id",
                (start_id, end_id),
            ).fetchall()
            expected_prev = GENESIS
            if start_id > 0:
                # 链外前驱：起点前一记录的 entry_hash
                prev = conn.execute(
                    "SELECT entry_hash FROM disclosure_log WHERE log_id = ?",
                    (start_id - 1,),
                ).fetchone()
                if prev and prev["entry_hash"]:
                    expected_prev = prev["entry_hash"]
            checked = 0
            for r in rows:
                # XS-003：已链区间内的断链行（entry_hash=''）显式报 invalid，
                # 不再静默跳过（防旧两段式写入崩溃留下永久审计盲区）
                if not r["entry_hash"]:
                    return {"valid": False, "checked": checked,
                            "first_bad_id": r["log_id"], "reason": "unlinked_row",
                            "unlinked_count": 1,
                            "first_unlinked_id": r["log_id"], "end_id": end_id}
                rd = {k: r[k] for k in _DISCLOSURE_FIELDS if k in r.keys()}
                rd.pop("prev_hash", None)
                rd.pop("entry_hash", None)
                # 归一：None → ""（与 append 同源，保证 hash 一致）
                rd = {k: ("" if v is None else v) for k, v in rd.items()}
                payload_json = canonical_json(rd)
                recomputed = compute_hash(payload_json, expected_prev)
                if r["prev_hash"] != expected_prev or r["entry_hash"] != recomputed:
                    return {"valid": False, "checked": checked,
                            "first_bad_id": r["log_id"], "reason": "hash_mismatch",
                            "unlinked_count": 0, "first_unlinked_id": None}
                expected_prev = r["entry_hash"]
                checked += 1
            return {"valid": True, "checked": checked, "first_bad_id": None,
                    "end_id": end_id}
        finally:
            conn.close()

    def backfill_unlinked(self) -> Dict:
        """XS-003 附带运维补链工具（先不接 CLI，登记即可）。

        为 entry_hash='' 的断链行补链：从首个断链行起重算其后的链段——
        prev 起点 = 首个断链行前一行的 entry_hash（无则 GENESIS；崩溃裸行
        位于链尾时等价于 _tail_hash(conn) 起点），维护 last_hash 随补链
        推进逐行更新，同连接顺序 UPDATE + 单次 commit。
        注意：断链行夹在其他已链行中间时，其后行的 prev_hash 已无法自洽，
        必须随链段整体重算（payload 字段不动，仅重算 prev/entry）才能让
        verify 恢复 valid。
        幂等：重复调用第二次 backfilled=0。锁保护。
        """
        with self._lock:
            conn = _conn(self._db_path)
            try:
                unlinked = conn.execute(
                    "SELECT log_id FROM disclosure_log WHERE entry_hash = ''"
                    " ORDER BY log_id"
                ).fetchall()
                if not unlinked:
                    return {"backfilled": 0}
                start = unlinked[0]["log_id"]
                prev = conn.execute(
                    "SELECT entry_hash FROM disclosure_log WHERE log_id < ?"
                    " ORDER BY log_id DESC LIMIT 1", (start,),
                ).fetchone()
                last_hash = prev["entry_hash"] if prev and prev["entry_hash"] \
                    else GENESIS
                rows = conn.execute(
                    "SELECT * FROM disclosure_log WHERE log_id >= ? ORDER BY log_id",
                    (start,),
                ).fetchall()
                for r in rows:
                    rd = {k: r[k] for k in _DISCLOSURE_FIELDS if k in r.keys()}
                    rd.pop("prev_hash", None)
                    rd.pop("entry_hash", None)
                    rd = {k: ("" if v is None else v) for k, v in rd.items()}
                    payload_json = canonical_json(rd)
                    entry_hash = compute_hash(payload_json, last_hash)
                    conn.execute(
                        "UPDATE disclosure_log SET prev_hash=?, entry_hash=?"
                        " WHERE log_id=?",
                        (last_hash, entry_hash, r["log_id"]),
                    )
                    last_hash = entry_hash
                conn.commit()
                return {"backfilled": len(unlinked)}
            finally:
                conn.close()


# ---- jsonl 文件级滚动链 ----

class JsonlRollingChain:
    """jsonl 审计文件滚动链：每窗口（默认 1000 行）算窗口 hash 挂主链。

    不搬文件入库（防存储翻倍，D4）；篡改 jsonl 任意行 → 窗口 hash 重算不匹配。
    """

    def __init__(self, db_path: str, file_path: str, ref_table: str,
                 window: int = DEFAULT_WINDOW, rotate_bytes: Optional[int] = None):
        self._db_path = db_path
        self._file_path = file_path
        self._ref_table = ref_table
        self._window = window
        self._rotate_bytes = rotate_bytes if rotate_bytes is not None \
            else DEFAULT_JSONL_ROTATE_BYTES
        self._chain = AuditChain(db_path)
        self._count = 0          # 本进程内累计行数（跨进程由 verify 全量重算兜底）
        self._lock = threading.Lock()

    def _read_lines(self) -> List[str]:
        if not os.path.exists(self._file_path):
            return []
        with open(self._file_path, "r", encoding="utf-8", errors="replace") as f:
            return f.readlines()

    def window_hash(self, lines: List[str]) -> str:
        return hashlib.sha256("".join(lines).encode("utf-8")).hexdigest()

    def append_line(self, line: str) -> Optional[Dict]:
        """追加一行 jsonl（写文件 + 计数）；累计到窗口阈值时写 anchor 到 audit_log。

        拥有文件写入职责（单一写入路径）：调用方不应再自行 open 写文件，
        否则计数与实际文件行数脱节。写失败抛异常由调用方处理（如 transport 的 fallback）。
        CD-022：写前检查大小，超阈值轮转（rename 归档，行号从 1 重计，_count 清零；
        归档文件由 verify_windows 按段重算，锚区间跨轮转不失真）。
        """
        os.makedirs(os.path.dirname(os.path.abspath(self._file_path)), exist_ok=True)
        self._rotate_if_needed()
        with open(self._file_path, "a", encoding="utf-8") as f:
            f.write(line)
        with self._lock:
            self._count += 1
            if self._count < self._window:
                return None
            self._count = 0
            lines = self._read_lines()
            if not lines:
                return None
            # 窗口 = 文件尾部 window 行
            win = lines[-self._window:]
            h = self.window_hash(win)
            start_line = max(1, len(lines) - self._window + 1)
            end_line = len(lines)
            return self._chain.append(
                "jsonl_anchor", self._ref_table,
                f"w-{start_line}-{end_line}",
                {"file": os.path.basename(self._file_path),
                 "start_line": start_line, "end_line": end_line,
                 "window_hash": h},
            )

    def _rotate_if_needed(self) -> None:
        """CD-022: 主文件超阈值 → rename 归档(同名 + UTC 时间戳后缀)。失败静默(D4)。
        归档名微秒 + 冲突自增——同秒多次轮转不覆盖旧段(审计完整性:段即证据)。"""
        if self._rotate_bytes <= 0:
            return
        try:
            if os.path.exists(self._file_path) and \
                    os.path.getsize(self._file_path) > self._rotate_bytes:
                ts = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S-%f")
                dst = f"{self._file_path}.{ts}"
                n = 1
                while os.path.exists(dst):
                    dst = f"{self._file_path}.{ts}-{n}"
                    n += 1
                os.replace(self._file_path, dst)
                self._count = 0  # 新文件行号从 1 重计
        except Exception as _exc:
            logger.warning("audit_chain silent-except(_rotate_if_needed): %s", _exc)

    def _file_segments(self) -> List[tuple]:
        """CD-022: [(path, lines)] 归档(ts 升序,旧→新)+ 主文件(末位)。
        行号全局连续 = 各段行数顺序拼接,锚区间可线性映射回所在段。"""
        base = os.path.basename(self._file_path)
        d = os.path.dirname(os.path.abspath(self._file_path))
        segs: List[tuple] = []
        try:
            arch = sorted(n for n in os.listdir(d) if n.startswith(base + "."))
        except Exception:
            arch = []
        for n in arch:
            p = os.path.join(d, n)
            try:
                with open(p, "r", encoding="utf-8", errors="replace") as f:
                    segs.append((p, f.readlines()))
            except Exception as _exc:
                logger.warning("audit_chain silent-except(_file_segments): %s", _exc)
        if os.path.exists(self._file_path):
            with open(self._file_path, "r", encoding="utf-8", errors="replace") as f:
                segs.append((self._file_path, f.readlines()))
        return segs

    def _declared_gaps(self) -> set:
        """已声明缺口的窗口 id 集合（CD-073）。

        主链里的 `anchor_gap` 事件 = 「这一段内容确实丢了，原因与范围已上链」。
        声明过的窗口不再算**未解释**断点，但会在 `declared_gaps` 里如实列出——
        目的是让「断链」有确定含义，而不是把缺口藏起来。**不改历史、不补内容。**
        """
        out = set()
        try:
            conn = _conn(self._db_path)
        except Exception:
            return out
        try:
            try:
                rows = conn.execute(
                    "SELECT ref_id FROM audit_log WHERE entry_type='anchor_gap'"
                    " AND ref_table=?", (self._ref_table,)).fetchall()
                out = {r["ref_id"] for r in rows if r["ref_id"]}
            except Exception:
                out = set()
        finally:
            conn.close()
        return out

    def verify_windows(self) -> Dict:
        """重算全部 jsonl 窗口 hash 与 audit_log anchor 对比。

        CD-022：行来源 = 归档序列 + 主文件（_file_segments），锚区间按其
        end_line 定位到所在段后取段内行重算——轮转后旧锚不因行号重置而失真。
        """
        segs = self._file_segments()
        if not segs or all(not lines for _, lines in segs):
            return {"valid": True, "windows": 0, "bad_windows": []}
        conn = _conn(self._db_path)
        try:
            try:
                conn.execute("SELECT 1 FROM audit_log LIMIT 1")
            except Exception:
                # 无主链表（老库）→ 无锚可校验
                return {"valid": True, "windows": 0, "bad_windows": []}
            rows = conn.execute(
                "SELECT * FROM audit_log WHERE entry_type='jsonl_anchor'"
                " AND ref_table=? ORDER BY log_id",
                (self._ref_table,),
            ).fetchall()
            bad = []
            k = 0          # 当前段指针(归档 ts 升序 + 主文件末位 = 时间序)
            cur_e = 0      # 上一锚 end_line(段内锚严格递增;新段首锚回落或相等 = 段切换)
            first = True
            for r in rows:
                try:
                    payload = json.loads(r["payload"])
                except Exception:
                    bad.append({"anchor_id": r["log_id"], "reason": "bad_payload"})
                    continue
                s, e = payload.get("start_line", 0), payload.get("end_line", 0)
                if not first and e <= cur_e:
                    k += 1  # 新段:段内行号重计
                first = False
                cur_e = e
                # 跳过无锚段(段行数不足 window 即轮转):锚区间超段尾 → 推进
                while k < len(segs) and e > len(segs[k][1]):
                    k += 1
                if k >= len(segs):
                    bad.append({"anchor_id": r["log_id"], "reason": "segment_missing",
                                "lines": f"{s}-{e}"})
                    continue
                seg_lines = segs[k][1]
                if s < 1 or e > len(seg_lines):
                    bad.append({"anchor_id": r["log_id"], "reason": "line_range",
                                "lines": f"{s}-{e}", "seg_lines": len(seg_lines)})
                    continue
                win = seg_lines[s - 1:e]
                h = self.window_hash(win)
                if h != payload.get("window_hash"):
                    bad.append({"anchor_id": r["log_id"], "reason": "hash_mismatch",
                                "lines": f"{s}-{e}"})
            declared = self._declared_gaps()
            declared_hits, undeclared = [], []
            for b in bad:
                _lines = b.get("lines") or ""
                if _lines and f"w-{_lines}" in declared:
                    declared_hits.append({**b, "declared": True})
                else:
                    undeclared.append(b)
            return {"valid": len(undeclared) == 0, "windows": len(rows),
                    "bad_windows": undeclared, "declared_gaps": declared_hits}
        finally:
            conn.close()


# ---- 综合校验（verify 端点用） ----

def current_chain_head(db_path: str) -> str:
    """主链当前链头 hash（阶段3-P2 交付4：git 历史 ↔ 哈希链互证用）。

    语义与 AuditChain._tail_hash 一致：空链/无 audit_log 表（老库）→ GENESIS；
    库文件不可连 → ""（D4 静默降级，调用方自行决定记不记录）。
    """
    try:
        conn = _conn(db_path)
    except Exception:
        return ""
    try:
        try:
            row = conn.execute(
                "SELECT entry_hash FROM audit_log ORDER BY log_id DESC LIMIT 1"
            ).fetchone()
        except Exception:
            return GENESIS  # 无 audit_log 表 = 空链
        return row["entry_hash"] if row else GENESIS
    finally:
        conn.close()


# ── 链头锚定外发（1a 补齐 2026-08-07；XS-004 落地 webhook 外发 2026-09-08） ──
# XS-004 已落地 HTTP webhook 外发：链头 hash POST 到配置的外部接收方（另一台机器/
# 审计服务器/对象存储 webhook）；本地 audit/anchor.txt 仅是快照，外部介质才是链尾
# 锚定真相（攻击者能改库就能顺手重写本地锚文件，本地文件不构成防链尾重写）。
try:
    from models import CONFIG as _CONFIG   # CD-070b：产物根可配（env > config.yaml > 仓库内默认）
except Exception:                          # 独立脚本场景：退回仓库内默认路径
    _CONFIG = None
# CD-070b（2026-09-20）：审计产物根统一到 _AUDIT_DIR（env SYNC_HUB_AUDIT_DIR > config audit.dir > 仓库内 audit/）。
_AUDIT_DIR = (_CONFIG.AUDIT_DIR if _CONFIG else "") or os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "audit")
_ANCHOR_FILE = os.path.join(_AUDIT_DIR, "anchor.txt")



def export_anchor(db_path: str, webhook_urls: Optional[List[str]] = None,
                  hub_name: str = "") -> Dict:
    """将 audit_log 链头 hash 写入本地锚定文件（快照）+ HTTP webhook 外发到外部介质。

    webhook_urls 为 None 时读 CONFIG.AUDIT_ANCHOR_URLS（默认空 = 不联网，行为同旧版）。
    网络异常逐 url 容错、绝不抛出（audit 路径不阻塞）。
    返回: {"anchor", "path", "written", "webhook_results", "remote_written", "error"?}
    """
    if webhook_urls is None:
        from models import CONFIG  # 函数内 import 防循环依赖（本模块顶部无 models import）
        webhook_urls = getattr(CONFIG, "AUDIT_ANCHOR_URLS", []) or []
    ac = AuditChain(db_path)
    conn = ac._get_conn()
    try:
        row = conn.execute(
            "SELECT entry_hash FROM audit_log ORDER BY log_id DESC LIMIT 1").fetchone()
    except Exception:
        return {"anchor": "", "path": _ANCHOR_FILE, "written": False,
                "webhook_results": [], "remote_written": False, "error": "无主链"}
    if not row:
        return {"anchor": "", "path": _ANCHOR_FILE, "written": False,
                "webhook_results": [], "remote_written": False, "error": "主链为空"}
    anchor = row["entry_hash"]
    # 本地快照（原逻辑保留：同机文件仅是快照，非锚定真相）
    result: Dict = {"anchor": anchor, "path": _ANCHOR_FILE, "written": False}
    try:
        os.makedirs(os.path.dirname(_ANCHOR_FILE), exist_ok=True)
        with open(_ANCHOR_FILE, "w", encoding="utf-8") as f:
            f.write(f"{_now()} {anchor}\n")
        result["written"] = True
    except Exception as e:
        result["error"] = str(e)
    # XS-004：HTTP POST 外发到外部介质；空列表 = 不联网直接返回
    webhook_results: List[Dict] = []
    for url in webhook_urls:
        body = json.dumps({
            "ts": _now(),
            "hub": hub_name or socket.gethostname(),
            "anchor": anchor,
            "file": os.path.basename(_ANCHOR_FILE),
        }).encode("utf-8")
        try:
            req = urllib.request.Request(
                url, data=body, headers={"Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=5) as resp:
                resp.read()
            webhook_results.append({"url": url, "ok": True})
        except Exception as e:
            webhook_results.append({"url": url, "ok": False, "error": str(e)[:200]})
    result["webhook_results"] = webhook_results
    result["remote_written"] = any(r["ok"] for r in webhook_results)
    return result


def verify_anchor(db_path: str) -> Dict:
    """校验链头与锚定文件一致（防链尾整体重写：锚定文件是外部只读介质）"""
    ac = AuditChain(db_path)
    conn = ac._get_conn()
    try:
        row = conn.execute(
            "SELECT entry_hash FROM audit_log ORDER BY log_id DESC LIMIT 1").fetchone()
    except Exception:
        return {"valid": True, "checked": 0}
    if not row:
        return {"valid": True, "checked": 0}
    if not os.path.exists(_ANCHOR_FILE):
        return {"valid": False, "checked": 1, "error": "锚定文件缺失（首次外发未执行）"}
    with open(_ANCHOR_FILE, "r", encoding="utf-8") as f:
        _content = f.read().strip()
    anchored = _content.split()[-1] if _content else ""
    match = anchored == row["entry_hash"]
    return {"valid": match, "checked": 1, "chain_tail": row["entry_hash"],
            "anchored": anchored, "first_bad_id": None if match else "tail"}


# ── CD-034 R3：链头外部时间戳（RFC3161 TSA）盖章 + 回拉比对 ──
# 为什么：本地 audit/anchor.txt 与链同机同目录，有本机写权限者可同时改链与锚（循环论证）。
# 权威时间证据必须来自信任域之外——公共 RFC3161 时间戳服务（免费）或自建 TSA。
# 语义要点：链头随每次写入变化，故判据不是「当前链头 == 盖章值」，而是
# 「被盖章的那个链头节点仍存在于链中」（见 tests/test_audit_tsa.py T4/T5）。
TSA_DIR = os.path.join(_AUDIT_DIR, "tsa")   # CD-070b：跟随 _AUDIT_DIR（SYNC_HUB_TSA_DIR 仍优先）

# 免费公共 RFC3161 时间戳服务（2026-09-20 实测可达，返回 ~6KB 真 token）。
# 换自建/RFC3161 商用 TSA 只需改 config audit.tsa.url。
DEFAULT_TSA_URL = "https://rfc3161.ai.moda/"
_TSA_INDEX_LOCK = threading.Lock()


def _der_len(n: int) -> bytes:
    """DER 长度编码（短/长形式）。"""
    if n < 0x80:
        return bytes([n])
    out = b""
    while n:
        out = bytes([n & 0xFF]) + out
        n >>= 8
    return bytes([0x80 | len(out)]) + out


def _der(tag: int, content: bytes) -> bytes:
    """DER TLV 编码。"""
    return bytes([tag]) + _der_len(len(content)) + content


def _der_int(value: int) -> bytes:
    """DER INTEGER（正数最小长度 + 高位补零防负数）。"""
    if value == 0:
        return _der(0x02, b"\x00")
    body = value.to_bytes((value.bit_length() + 7) // 8, "big")
    if body[0] & 0x80:
        body = b"\x00" + body
    return _der(0x02, body)


def build_tsa_query(imprint_hex: str) -> bytes:
    """构造 RFC3161 TimeStampReq（DER）：version=v1 + SHA-256 messageImprint
    + certReq=TRUE + 随机 nonce。手写 DER 是为了零依赖（不引入 asn1crypto/rsa 等）。
    """
    digest = bytes.fromhex(imprint_hex)
    if len(digest) != 32:
        raise ValueError("imprint 必须是 SHA-256(32 字节) hex")
    sha256_algid = bytes.fromhex("300d06096086480165030402010500")  # SEQUENCE{OID, NULL}
    message_imprint = _der(0x30, sha256_algid + _der(0x04, digest))
    body = _der_int(1)                    # version v1
    body += message_imprint
    # RFC3161 TimeStampReq 字段序：version, messageImprint, [reqPolicy], nonce,
    # [certReq], [extensions] —— certReq 必须排在 nonce 之后，写成对调的顺序会被
    # TSA 判为 Invalid TimeStampReq（2026-09-20 用 openssl ts -query 对照才发现，
    # 本机 openssl 生成的 tsq 即 nonce→certReq 顺序）。
    body += _der_int(secrets.randbits(63) or 1)  # nonce（防重放/防串答复）
    body += _der(0x01, b"\xff")           # certReq TRUE（要 TSA 附证书）
    return _der(0x30, body)


def _append_tsa_index(out_dir: str, rec: Dict) -> None:
    """盖章记录追加到 index.jsonl（含失败记录——失败也要留痕）。"""
    try:
        os.makedirs(out_dir, exist_ok=True)
        with _TSA_INDEX_LOCK:
            with open(os.path.join(out_dir, "index.jsonl"), "a",
                      encoding="utf-8") as f:
                f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except Exception as e:
        logger.warning("tsa index 写入失败（不阻塞）: %s", e)


def tsa_out_dir(out_dir: str = "") -> str:
    """盖章目录解析：显式入参 > SYNC_HUB_TSA_DIR 环境变量 > 仓库内 audit/tsa。

    env 覆盖是测试/多实例隔离用的（生产一个 Hub 一个库一个目录，不必设）。
    """
    return out_dir or os.environ.get("SYNC_HUB_TSA_DIR") or TSA_DIR


def tsa_stamp(db_path: str, tsa_url: str, out_dir: str = "", timeout: int = 10,
              hub_name: str = "") -> Dict:
    """对当前链头做 RFC3161 时间戳盖章。返回 {status, anchor, imprint, tsq, tsr, ...}。

    失败绝不抛（审计路径不阻塞主链路），逐次留痕到 index.jsonl。
    """
    anchor = current_chain_head(db_path) or ""
    if not anchor or anchor == GENESIS:
        return {"status": "error", "anchor": anchor, "error": "主链为空", "tsa_url": tsa_url}
    imprint = hashlib.sha256(anchor.encode("utf-8")).hexdigest()
    out_dir = tsa_out_dir(out_dir)
    _now_utc = datetime.now(timezone.utc)
    base = os.path.join(out_dir, f"{_now_utc.strftime('%Y%m%dT%H%M%SZ')}_{anchor[:8]}")
    result: Dict = {
        "anchor": anchor, "imprint": imprint, "tsa_url": tsa_url,
        "tsq": base + ".tsq", "tsr": base + ".tsr",
        "stamped_at": _now_utc.isoformat(), "hub": hub_name or socket.gethostname(),
    }
    try:
        query = build_tsa_query(imprint)
        req = urllib.request.Request(
            tsa_url, data=query,
            headers={"Content-Type": "application/timestamp-query",
                     "Accept": "application/timestamp-reply"})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            token = resp.read()
        if not token:
            raise RuntimeError("TSA 返回空 token")
        os.makedirs(out_dir, exist_ok=True)
        with open(result["tsq"], "wb") as f:
            f.write(query)
        with open(result["tsr"], "wb") as f:
            f.write(token)
        result["status"] = "ok"
        result["bytes"] = len(token)
        # 廉价自洽检查：token 里应含我们提交的 imprint（真签名验证需 TSA CA 证书）
        result["token_has_imprint"] = bytes.fromhex(imprint) in token
    except Exception as e:
        result["status"] = "error"
        result["error"] = f"{type(e).__name__}: {e}"[:300]
        _append_tsa_index(out_dir, result)
        return result
    _append_tsa_index(out_dir, result)
    return result


def verify_tsa(db_path: str, out_dir: str = "") -> Dict:
    """回拉比对：逐条盖章记录检查「被盖章的链头节点是否仍在链中」。

    - 节点在链中 → 该时间点之后链未被整段重写（链正常增长不算不一致）
    - 节点不在链中 → 判定整段重写/截断，返回 mismatches（调用方据此告警）
    - imprint 与 anchor 不自洽 → index 被手改，同样报不一致
    """
    out_dir = tsa_out_dir(out_dir)
    idx = os.path.join(out_dir, "index.jsonl")
    if not os.path.isfile(idx):
        return {"valid": True, "checked": 0, "mismatches": [], "out_dir": out_dir,
                "note": "无盖章记录（TSA 未启用或尚未盖章）"}
    mismatches: List[Dict] = []
    checked = 0
    try:
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
    except Exception as e:
        return {"valid": False, "checked": 0, "mismatches": [
            {"reason": "db_unavailable", "detail": str(e)[:200]}], "out_dir": out_dir}
    try:
        with open(idx, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except Exception:
                    continue  # 坏行跳过（不因一行脏数据判整链无效）
                if rec.get("status") != "ok":
                    continue
                anchor = rec.get("anchor") or ""
                if not anchor:
                    continue
                checked += 1
                if rec.get("imprint") != hashlib.sha256(anchor.encode("utf-8")).hexdigest():
                    mismatches.append({"anchor": anchor[:16],
                                       "stamped_at": rec.get("stamped_at"),
                                       "reason": "imprint_mismatch（盖章记录被篡改）",
                                       "tsr": rec.get("tsr")})
                    continue
                hit = conn.execute(
                    "SELECT log_id FROM audit_log WHERE entry_hash = ? LIMIT 1",
                    (anchor,)).fetchone()
                if not hit:
                    mismatches.append({
                        "anchor": anchor[:16],
                        "stamped_at": rec.get("stamped_at"),
                        "reason": "anchored_head_missing_from_chain（链被整段重写或截断）",
                        "tsr": rec.get("tsr"),
                    })
    finally:
        conn.close()
    return {"valid": not mismatches, "checked": checked,
            "mismatches": mismatches, "out_dir": out_dir}


# ── CD-034 R4（2026-09-21 拍板）：RFC3161 TSA 令牌离线验签 + 远端锚取回比对 ──
# 拍板①：钉 TSA 签名证书指纹 / 可信 CA 指纹——不钉则验签等于白验（谁都能自签一个
#         「合法」令牌）。指纹列表空 = 未配置 = fail-closed unverified（不假绿）。
# 拍板②：cryptography 只允许在验签路径内**延迟导入**，缺失即 unverified；
#         核心哈希链维持零依赖手写 DER 现状（上面的 _der* 编码器不动）。
#
# 信任配置来源（优先级：显式入参 > 环境变量 > JSON 配置文件）：
#   - 环境变量 SYNC_HUB_TSA_TRUSTED_FP：逗号/空白分隔的 SHA-256 指纹（hex，可带冒号）
#   - JSON 文件 audit/tsa_trust.json：{"trusted_fingerprints": ["..."]}
# 不碰 models.py（别人在改），故用 env + audit/ 下 JSON 承载配置。

_OID_SIGNED_DATA = "1.2.840.113549.1.7.2"       # PKCS#7 signedData
_OID_TSTINFO = "1.2.840.113549.1.9.16.1.4"      # id-ct-TSTInfo
_OID_MESSAGE_DIGEST = "1.2.840.113549.1.9.4"    # messageDigest 属性
_OID_CONTENT_TYPE = "1.2.840.113549.1.9.3"      # contentType 属性
_OID_SHA256 = "2.16.840.1.101.3.4.2.1"
_OID_RSA_SHA256 = "1.2.840.113549.1.1.11"       # sha256WithRSAEncryption
_OID_ECDSA_SHA256 = "1.2.840.10045.4.3.2"       # ecdsa-with-SHA256


def _der_read(buf: bytes, off: int = 0):
    """读一个 DER TLV → (tag, content, next_off)。验签路径的最小解码器
    （与上方手写 _der* 编码器同源风格：零依赖、只覆盖所需）。"""
    tag = buf[off]
    off += 1
    lb = buf[off]
    off += 1
    if lb & 0x80:
        n = lb & 0x7F
        length = int.from_bytes(buf[off:off + n], "big")
        off += n
    else:
        length = lb
    return tag, buf[off:off + length], off + length


def _der_children(content: bytes) -> List[tuple]:
    """把一段 DER 内容字节拆成 [(tag, content), ...]。"""
    out = []
    off = 0
    while off < len(content):
        tag, c, off = _der_read(content, off)
        out.append((tag, c))
    return out


def _der_oid_str(content: bytes) -> str:
    """OID 内容字节 → 点分字符串。"""
    first = content[0]
    parts = [str(first // 40), str(first % 40)]
    val = 0
    for b in content[1:]:
        val = (val << 7) | (b & 0x7F)
        if not b & 0x80:
            parts.append(str(val))
            val = 0
    return ".".join(parts)


def _load_crypto():
    """延迟导入 cryptography（拍板②：仅验签路径可用，缺失即 fail-closed）。

    返回 (x509, hashes, padding, ec, rsa) 元组；ImportError → None。
    注意：绝不在模块顶层 import——核心哈希链保持零依赖。
    """
    try:
        from cryptography import x509
        from cryptography.hazmat.primitives import hashes
        from cryptography.hazmat.primitives.asymmetric import ec, padding, rsa
        return x509, hashes, padding, ec, rsa
    except ImportError:
        return None


def _normalize_fingerprints(fps) -> List[str]:
    """指纹归一：去冒号/空白、小写。输入为 str（逗号/空白分隔）或列表。"""
    if not fps:
        return []
    if isinstance(fps, str):
        fps = fps.replace(",", " ").split()
    out = []
    for fp in fps:
        fp = "".join(str(fp).split()).replace(":", "").lower()
        if fp:
            out.append(fp)
    return out


def _load_trusted_fingerprints(trust_file: str = "") -> List[str]:
    """钉扎指纹来源：env SYNC_HUB_TSA_TRUSTED_FP > JSON 配置文件。

    空 = 未配置 = verify_tsa_token 一律 unverified（fail-closed，不假绿）。
    """
    env = os.environ.get("SYNC_HUB_TSA_TRUSTED_FP", "")
    if env.strip():
        return _normalize_fingerprints(env)
    path = trust_file or os.path.join(_AUDIT_DIR, "tsa_trust.json")
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return _normalize_fingerprints(data.get("trusted_fingerprints"))
    except FileNotFoundError:
        return []
    except Exception as e:
        logger.warning("tsa_trust 配置读取失败（按未配置 fail-closed）: %s", e)
        return []


def _parse_timestamp_resp(tsr: bytes) -> Dict:
    """解析 RFC3161 TimeStampResp → {status, tstinfo, signed_attrs?, signature,
    sig_alg, certs_der, signer_sid}。解析失败抛 ValueError。"""
    tag, content, _ = _der_read(tsr, 0)
    if tag != 0x30:
        raise ValueError("TimeStampResp 顶层不是 SEQUENCE")
    top = _der_children(content)
    if not top or top[0][0] != 0x30:
        raise ValueError("缺 PKIStatusInfo")
    status_children = _der_children(top[0][1])
    pki_status = int.from_bytes(status_children[0][1], "big") if status_children else 99
    if pki_status > 1:
        return {"status": pki_status}  # 2..5 = rejection（无 token 可验）
    if len(top) < 2 or top[1][0] != 0x30:
        raise ValueError("status=granted 但缺 timeStampToken")
    ci = _der_children(top[1][1])  # ContentInfo
    if _der_oid_str(ci[0][1]) != _OID_SIGNED_DATA:
        raise ValueError("ContentInfo 不是 signedData")
    if ci[1][0] != 0xA0:
        raise ValueError("signedData 缺 [0] EXPLICIT")
    tag, sd_content, _ = _der_read(ci[1][1], 0)  # [0] 内是完整 SignedData TLV
    if tag != 0x30:
        raise ValueError("SignedData 不是 SEQUENCE")
    sd = _der_children(sd_content)
    # SignedData ::= SEQUENCE { version, digestAlgorithms, encapContentInfo,
    #                           certificates [0] IMPLICIT OPT, crls [1] OPT, signerInfos }
    eci = _der_children(sd[2][1])  # EncapsulatedContentInfo
    if _der_oid_str(eci[0][1]) != _OID_TSTINFO:
        raise ValueError("eContentType 不是 id-ct-TSTInfo")
    if len(eci) < 2 or eci[1][0] != 0xA0:
        raise ValueError("缺 eContent（无 TSTInfo）")
    tag, tstinfo, _ = _der_read(eci[1][1], 0)  # [0] EXPLICIT OCTET STRING
    if tag != 0x04:
        raise ValueError("eContent 不是 OCTET STRING")
    certs_der: List[bytes] = []
    signer_infos = None
    for t, c in sd[3:]:
        if t == 0xA0:  # certificates [0] IMPLICIT：内容为证书 TLV 拼接
            off = 0
            while off < len(c):
                ct, cc, nxt = _der_read(c, off)
                if ct == 0x30:
                    certs_der.append(c[off:nxt])
                off = nxt
        elif t == 0x31:  # signerInfos SET
            signer_infos = c
    if not signer_infos:
        raise ValueError("缺 signerInfos")
    tag, si_content, _ = _der_read(signer_infos, 0)  # 取第一个 SignerInfo
    if tag != 0x30:
        raise ValueError("SignerInfo 不是 SEQUENCE")
    si = _der_children(si_content)
    # SignerInfo ::= SEQUENCE { version, sid, digestAlgorithm, signedAttrs [0] OPT,
    #                           signatureAlgorithm, signature }
    signed_attrs = None
    sig_alg = None
    signature = None
    for t, c in si[2:]:
        if t == 0xA0:
            signed_attrs = c
        elif t == 0x30:
            algid = _der_children(c)
            oid = _der_oid_str(algid[0][1])
            if sig_alg is None and oid in (_OID_SHA256,):
                continue  # digestAlgorithm
            sig_alg = oid if oid != _OID_SHA256 else sig_alg
        elif t == 0x04:
            signature = c
    if not sig_alg or signature is None:
        raise ValueError("缺 signatureAlgorithm 或 signature")
    return {"status": pki_status, "tstinfo": tstinfo, "signed_attrs": signed_attrs,
            "sig_alg": sig_alg, "signature": signature, "certs_der": certs_der}


def _parse_tstinfo(tstinfo: bytes) -> Dict:
    """解析 TSTInfo → {imprint, gen_time}。messageImprint 摘要是回拉比对的锚。"""
    tag, content, _ = _der_read(tstinfo, 0)
    if tag != 0x30:
        raise ValueError("TSTInfo 不是 SEQUENCE")
    ch = _der_children(content)
    # version, policy, messageImprint, serialNumber, genTime, ...
    mi = _der_children(ch[2][1])  # MessageImprint SEQUENCE
    imprint = mi[1][1]            # OCTET STRING 内容
    gen_raw = ch[4][1].decode("ascii")  # GeneralizedTime YYYYMMDDHHMMSSZ
    gen_time = datetime.strptime(gen_raw, "%Y%m%d%H%M%SZ").replace(tzinfo=timezone.utc)
    return {"imprint": imprint, "gen_time": gen_time}


def _verify_signature(crypto, cert, sig_alg: str, signature: bytes, data: bytes) -> bool:
    """用证书公钥验签（RSA PKCS#1 v1.5 / ECDSA，摘要固定 SHA-256）。"""
    _, hashes, padding, ec, _ = crypto
    try:
        pub = cert.public_key()
        if sig_alg == _OID_RSA_SHA256:
            pub.verify(signature, data, padding.PKCS1v15(), hashes.SHA256())
        elif sig_alg == _OID_ECDSA_SHA256:
            pub.verify(signature, data, ec.ECDSA(hashes.SHA256()))
        else:
            return False
        return True
    except Exception:
        return False


def _cert_signed_by(crypto, cert, issuer) -> bool:
    """cert 的签名是否由 issuer 公钥验证通过（RSA/ECDSA）。"""
    _, _, padding, ec, rsa = crypto
    try:
        pub = issuer.public_key()
        alg = cert.signature_hash_algorithm
        if isinstance(pub, rsa.RSAPublicKey):
            pub.verify(cert.signature, cert.tbs_certificate_bytes,
                       padding.PKCS1v15(), alg)
        else:
            pub.verify(cert.signature, cert.tbs_certificate_bytes,
                       ec.ECDSA(alg))
        return True
    except Exception:
        return False


def verify_tsa_token(tsr, chain_head: str, trusted_fingerprints=None,
                     trust_file: str = "") -> Dict:
    """RFC 3161 TimeStampResp 离线验签 + 指纹钉扎比对。

    tsr：token 字节或 .tsr 文件路径。chain_head：待比对的主链链头 hash（hex）。
    trusted_fingerprints：None = 走配置（env > audit/tsa_trust.json）；
    显式列表（含空列表）优先于配置——空列表 = 未配置 = fail-closed unverified。

    返回 {"status": "verified" | "failed" | "unverified", ...}：
    - verified：签名有效 + imprint == SHA256(chain_head) + 指纹命中钉扎
      （TSA 签名证书指纹直接命中，或证书链闭合到已钉扎的 CA）
    - failed：能验但不过（摘要被篡改 / 签名无效 / 指纹不匹配）——安全事件
    - unverified：无法验（缺 cryptography / 未配指纹）——不假绿、明确报出
    """
    crypto = _load_crypto()
    if crypto is None:
        return {"status": "unverified", "reason": "cryptography_unavailable",
                "detail": "验签依赖 cryptography 未安装；核心哈希链不受影响（零依赖）"}
    pins = set(_normalize_fingerprints(trusted_fingerprints)
               if trusted_fingerprints is not None
               else _load_trusted_fingerprints(trust_file))
    if not pins:
        return {"status": "unverified", "reason": "no_trusted_fingerprints",
                "detail": "未配置钉扎指纹（SYNC_HUB_TSA_TRUSTED_FP 或 "
                          "audit/tsa_trust.json）——不钉则验签等于白验，拒绝假绿"}
    if isinstance(tsr, str):
        with open(tsr, "rb") as f:
            tsr = f.read()
    try:
        parsed = _parse_timestamp_resp(tsr)
    except (ValueError, IndexError, UnicodeDecodeError) as e:
        return {"status": "failed", "reason": "malformed_token", "detail": str(e)[:200]}
    if "tstinfo" not in parsed:
        return {"status": "failed", "reason": "tsa_rejected",
                "detail": f"PKIStatus={parsed['status']}（TSA 拒绝了该请求）"}
    try:
        info = _parse_tstinfo(parsed["tstinfo"])
    except (ValueError, IndexError, UnicodeDecodeError) as e:
        return {"status": "failed", "reason": "malformed_tstinfo", "detail": str(e)[:200]}
    base: Dict = {"gen_time": info["gen_time"].isoformat(),
                  "imprint": info["imprint"].hex()}
    # ① 消息摘要比对：token 盖的必须是当前链头
    expected = hashlib.sha256(chain_head.encode("utf-8")).digest()
    if info["imprint"] != expected:
        return {**base, "status": "failed", "reason": "imprint_mismatch",
                "detail": "token 内的 messageImprint 与链头摘要不符（摘要被篡改/张冠李戴）"}
    x509, hashes = crypto[0], crypto[1]
    try:
        certs = [x509.load_der_x509_certificate(d) for d in parsed["certs_der"]]
    except Exception as e:
        return {**base, "status": "failed", "reason": "bad_certificates",
                "detail": str(e)[:200]}
    if not certs:
        return {**base, "status": "failed", "reason": "no_certificates",
                "detail": "token 未携带证书（无法验签）"}
    # ② 签名验证：signedAttrs 在场时签的是 SET OF(tag 0x31) 的 DER，
    #    且其 messageDigest 属性必须 == SHA256(TSTInfo)；否则签 TSTInfo 本体。
    if parsed["signed_attrs"] is not None:
        attrs = parsed["signed_attrs"]
        md_ok = False
        for t, c in _der_children(attrs):
            if t != 0x30:
                continue
            attr = _der_children(c)
            if _der_oid_str(attr[0][1]) == _OID_MESSAGE_DIGEST:
                vals = _der_children(attr[1][1])
                if vals and vals[0][1] == hashlib.sha256(parsed["tstinfo"]).digest():
                    md_ok = True
        if not md_ok:
            return {**base, "status": "failed", "reason": "message_digest_mismatch",
                    "detail": "signedAttrs 的 messageDigest 与 TSTInfo 摘要不符"}
        sign_input = _der(0x31, attrs)
    else:
        sign_input = parsed["tstinfo"]
    signer = next((c for c in certs
                   if _verify_signature(crypto, c, parsed["sig_alg"],
                                        parsed["signature"], sign_input)), None)
    if signer is None:
        return {**base, "status": "failed", "reason": "signature_invalid",
                "detail": "所有附带证书均无法验证签名"}
    base["tsa_cert_subject"] = signer.subject.rfc4514_string()
    # 签名证书在盖章时刻必须有效
    if not (signer.not_valid_before_utc <= info["gen_time"]
            <= signer.not_valid_after_utc):
        return {**base, "status": "failed", "reason": "cert_not_valid_at_gen_time",
                "detail": "签名证书在 genTime 时刻不在有效期内"}
    signer_fp = signer.fingerprint(hashes.SHA256()).hex()
    # ③ 指纹钉扎：TSA 签名证书直接命中 → 通过（私有 TSA 常为自签，不要求成链）
    if signer_fp in pins:
        return {**base, "status": "verified", "pinned": "tsa_cert",
                "fingerprint": signer_fp}
    # 否则要求证书链闭合到某个已钉扎 CA
    path, chain_ok = [signer], True
    cur = signer
    while cur.issuer != cur.subject:
        cur_fp = cur.fingerprint(hashes.SHA256())
        issuer = next(
            (c for c in certs if c.subject == cur.issuer
             and c.fingerprint(hashes.SHA256()) != cur_fp),
            None)
        if issuer is None or not _cert_signed_by(crypto, cur, issuer):
            chain_ok = False
            break
        path.append(issuer)
        cur = issuer
    if chain_ok and not _cert_signed_by(crypto, cur, cur):
        chain_ok = False  # 自签根的自签名验证失败
    if chain_ok:
        for ca in path[1:]:
            ca_fp = ca.fingerprint(hashes.SHA256()).hex()
            if ca_fp in pins:
                return {**base, "status": "verified", "pinned": "trusted_ca",
                        "fingerprint": ca_fp,
                        "chain_subjects": [c.subject.rfc4514_string() for c in path]}
    return {**base, "status": "failed", "reason": "fingerprint_not_pinned",
            "detail": "签名证书及链上 CA 指纹均未命中钉扎列表"
                      + ("" if chain_ok else "（且证书链未闭合）"),
            "fingerprint": signer_fp}


def _validate_anchor_url(url: str, allow_private: bool = True) -> Optional[str]:
    """SSRF 防护：仅 http/https、禁 userinfo。返回 None=合法，否则拒绝原因。

    内网保留段默认**放宽**（allow_private=True）：本产品是内网部署，
    锚接收方典型就是同网段的审计服务器（如 http://192.168.x.x/anchor），
    一刀切拒绝内网段会让默认部署不可用；URL 本身来自管理员配置
    （AUDIT_ANCHOR_URLS），非用户输入，SSRF 攻击面有限。
    若部署面变化（如 URL 来自低权角色），置 env SYNC_HUB_ANCHOR_ALLOW_PRIVATE=0
    收紧：拒绝私网/环回/链路本地/保留/组播/未指定地址（域名放行——离线无法
    判定解析结果，收紧到该粒度需要解析后校验，代价不值得）。
    """
    from urllib.parse import urlparse
    p = urlparse(url)
    if p.scheme not in ("http", "https"):
        return f"scheme 不允许: {p.scheme!r}（仅 http/https）"
    if not p.hostname:
        return "缺少主机名"
    if p.username or p.password:
        return "URL 不允许携带 userinfo（防凭据泄漏/混淆）"
    if not allow_private:
        import ipaddress
        try:
            ip = ipaddress.ip_address(p.hostname)
        except ValueError:
            return None  # 域名：离线无法判定，放行（见 docstring）
        if (ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved
                or ip.is_multicast or ip.is_unspecified):
            return f"内网/保留地址被拒绝（allow_private=False）: {p.hostname}"
    return None


def fetch_and_verify_anchors(db_path: str, urls: Optional[List[str]] = None,
                             timeout: int = 5,
                             allow_private: Optional[bool] = None) -> Dict:
    """从 AUDIT_ANCHOR_URLS 取回远端锚，与本地主链比对（XS-004 的取回半环）。

    语义（与 verify_tsa 同判据）：链头随写入变化，故判据不是「远端锚 == 当前
    链头」，而是「远端锚节点仍存在于链中」——在链中即该锚时刻之后未被整段
    重写。远端应答格式：JSON {"anchor": "<hex>"}，或纯文本「... <hex>」末词
    （兼容 anchor.txt 快照格式）。
    返回 {"valid", "checked", "unverified", "results"}：
    - valid=False 仅当某个成功取回的锚不在链中（重写嫌疑，安全事件）
    - 取不回（网络/格式）记 unverified 计数、不判 valid=False——取不回不是
      篡改证据，但也不构成确认，运维应按 unverified>0 告警
    """
    if urls is None:
        from models import CONFIG  # 函数内 import 防循环依赖
        urls = getattr(CONFIG, "AUDIT_ANCHOR_URLS", []) or []
    if allow_private is None:
        allow_private = os.environ.get("SYNC_HUB_ANCHOR_ALLOW_PRIVATE", "1") != "0"
    if not urls:
        return {"valid": True, "checked": 0, "unverified": 0, "results": [],
                "note": "未配置远端锚 URL（AUDIT_ANCHOR_URLS 默认空 = 休眠）"}
    head = current_chain_head(db_path)
    try:
        conn = _conn(db_path)
    except Exception as e:
        return {"valid": False, "checked": 0, "unverified": len(urls),
                "results": [], "error": f"db_unavailable: {str(e)[:200]}"}
    results: List[Dict] = []
    checked = unverified = 0
    valid = True
    try:
        for url in urls:
            rec: Dict = {"url": url}
            results.append(rec)
            reason = _validate_anchor_url(url, allow_private)
            if reason:
                rec.update(ok=False, status="rejected", reason=reason)
                unverified += 1
                continue
            try:
                req = urllib.request.Request(url, method="GET")
                with urllib.request.urlopen(req, timeout=timeout) as resp:
                    raw = resp.read(65536)  # 锚应答应是几百字节，截断防异常大响应
            except Exception as e:
                rec.update(ok=False, status="unverified",
                           reason=f"fetch_failed: {type(e).__name__}: {e}"[:200])
                unverified += 1
                continue
            anchor = ""
            try:
                anchor = str(json.loads(raw.decode("utf-8")).get("anchor") or "")
            except Exception:
                text = raw.decode("utf-8", errors="replace").strip()
                anchor = text.split()[-1] if text else ""
            rec["anchor"] = anchor[:16]
            if not anchor:
                rec.update(ok=False, status="unverified",
                           reason="应答中解析不到 anchor 字段")
                unverified += 1
                continue
            checked += 1
            if anchor == head:
                rec.update(ok=True, status="match_head")
            else:
                hit = conn.execute(
                    "SELECT log_id FROM audit_log WHERE entry_hash = ? LIMIT 1",
                    (anchor,)).fetchone()
                if hit:
                    rec.update(ok=True, status="match_chain", log_id=hit["log_id"])
                else:
                    rec.update(ok=False, status="mismatch",
                               reason="远端锚不在本地链中（链被整段重写/截断嫌疑）")
                    valid = False
    finally:
        conn.close()
    return {"valid": valid, "checked": checked, "unverified": unverified,
            "chain_head": head[:16] if head else head, "results": results}


def verify_all(db_path: str, jsonl_files: Dict[str, str],
               start_id: int = 0, end_id: int = 0) -> Dict:
    """综合校验：audit_log 主链 + disclosure_log 披露链 + jsonl 滚动链。"""
    ac = AuditChain(db_path)
    main = ac.verify(start_id, end_id)
    dc = DisclosureChain(db_path)
    disc = dc.verify()
    results = {"valid": True, "chains": {}}
    results["chains"]["audit_log"] = main
    results["chains"]["disclosure_log"] = disc
    for ref, path in jsonl_files.items():
        jc = JsonlRollingChain(db_path, path, ref)
        results["chains"][f"jsonl:{ref}"] = jc.verify_windows()
    for name, res in results["chains"].items():
        if not res.get("valid"):
            results["valid"] = False
    results["checked_total"] = sum(
        res.get("checked", 0) for res in results["chains"].values()
        if isinstance(res, dict))
    return results
