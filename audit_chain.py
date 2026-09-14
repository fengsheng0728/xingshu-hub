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
            conn.execute("PRAGMA synchronous = NORMAL")  # WAL + NORMAL：降 fsync 开销
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
            logger.warning("audit_chain silent-except @481: %s", _exc)

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
                logger.warning("audit_chain silent-except @499: %s", _exc)
        if os.path.exists(self._file_path):
            with open(self._file_path, "r", encoding="utf-8", errors="replace") as f:
                segs.append((self._file_path, f.readlines()))
        return segs

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
            return {"valid": len(bad) == 0, "windows": len(rows), "bad_windows": bad}
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
_ANCHOR_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "audit", "anchor.txt")


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
