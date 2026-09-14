# -*- coding: utf-8 -*-
"""gitrepo.py — git 仓库操作层（阶段3-P0 主干-分干数据底座基建）

影子模式把 SQLite 数据镜像落 git 仓库群（真相源）时使用的 git 封装：
init / commit / log / diff / status / read_at（历史版本读取）/ head_hash / write_file
/ move_file / remove_file（阶段4-B1 反哺归档用，工作副本级移动/删除，git 历史留痕）。

铁律：
- **影子是增强不是依赖（D4）**：所有操作失败静默降级，返回 False/None/空列表，
  绝不抛异常阻塞主链路（调用方是影子写入钩子，主写入路径零依赖它）。
- Windows 兼容：subprocess list 参数（无 shell，中文路径/文件名安全）、
  encoding=utf-8、CREATE_NO_WINDOW 防黑窗。
- 同一仓库的写操作（ensure/commit）经 threading.Lock 串行化。
"""
import logging
import os
import subprocess
import threading

logger = logging.getLogger("xingshu.gitrepo")

# Windows 下防黑窗（git 是控制台程序）；非 Windows 无此 flag
_CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)

# 同一仓库的写操作（ensure/commit）串行化。
# 必须用 RLock：commit() 持锁后调 ensure()（仓库损坏/路径非法时），
# 普通 Lock 不可重入 → 自锁死锁（test_ensure_fails_on_invalid_root 实测挂起）。
_LOCK = threading.RLock()


class GitRepo:
    """单个 git 仓库的操作句柄。root 不存在时 ensure() 负责 init。"""

    def __init__(self, root: str, author: str = "xingshu-shadow",
                 email: str = "shadow@xingshu.local"):
        self.root = os.path.abspath(root)
        self.author = author
        self.email = email

    # ── 基础 ──
    def exists(self) -> bool:
        return os.path.isdir(os.path.join(self.root, ".git"))

    def ensure(self) -> bool:
        """仓库不存在则 init + 配置作者；已存在幂等。失败返回 False。"""
        with _LOCK:
            try:
                if not os.path.isdir(self.root):
                    os.makedirs(self.root, exist_ok=True)
                if not self.exists():
                    code, out = self._git(["init", "-b", "main"], check=False)
                    if code != 0:
                        logger.warning("git init 失败 %s: %s", self.root, out[:200])
                        return False
                self._git(["config", "user.name", self.author])
                self._git(["config", "user.email", self.email])
                return self.exists()
            except Exception:
                logger.exception("gitrepo ensure 异常 root=%s", self.root)
                return False

    # ── 文件写入 ──
    def write_file(self, relpath: str, content: str) -> bool:
        """写文件（自动建父目录）。返回是否成功落盘。"""
        try:
            full = os.path.join(self.root, relpath)
            parent = os.path.dirname(full)
            if parent and not os.path.isdir(parent):
                os.makedirs(parent, exist_ok=True)
            with open(full, "w", encoding="utf-8") as f:
                f.write(content)
            return True
        except Exception:
            logger.exception("gitrepo write_file 失败 %s", relpath)
            return False

    def commit(self, message: str, paths=None) -> bool:
        """add + commit。paths=None 提交全部改动。失败返回 False。"""
        with _LOCK:
            try:
                if not self.exists() and not self.ensure():
                    return False
                if paths:
                    for p in paths:
                        self._git(["add", "--", p], check=False)
                else:
                    self._git(["add", "-A"], check=False)
                code, out = self._git(["commit", "-m", message], check=False)
                if code != 0:
                    # "nothing to commit" 也算成功（无变更）
                    if "nothing to commit" in out:
                        return True
                    logger.warning("git commit 失败: %s", out[:200])
                    return False
                return True
            except Exception:
                logger.exception("gitrepo commit 异常")
                return False

    # ── 阶段4-B1: 移动/删除（反哺归档用，假删除语义） ──
    def move_file(self, src: str, dst: str, message: str = "") -> bool:
        """移动文件并提交（write+remove 组合，单次 commit）。

        假删除语义：git 历史保留旧路径内容，移动后 read_at(src, "HEAD~1") 仍可读。
        注：这是工作副本级操作；反哺流程的「归档」由调用方用语义化 dst
        （如 vault/_merged/<date>/<id>.md）调用本方法实现。
        失败返回 False 不抛（对齐 commit 返回值模式）。
        """
        with _LOCK:
            try:
                if not self.exists() and not self.ensure():
                    return False
                src_full = self._safe_path(src)
                dst_full = self._safe_path(dst)
                if src_full is None or dst_full is None:
                    logger.warning("gitrepo move_file 路径逃逸拒绝: %s -> %s", src, dst)
                    return False
                if not os.path.isfile(src_full) or os.path.exists(dst_full):
                    return False
                parent = os.path.dirname(dst_full)
                if parent and not os.path.isdir(parent):
                    os.makedirs(parent, exist_ok=True)
                os.replace(src_full, dst_full)
                return self.commit(message or f"move {src} -> {dst}")
            except Exception:
                logger.exception("gitrepo move_file 异常 %s -> %s", src, dst)
                return False

    def remove_file(self, relpath: str, message: str = "") -> bool:
        """删除工作副本文件并提交。

        假删除语义：git 历史保留内容，删除后 read_at(relpath, "HEAD~1") 仍可读；
        与设计文档「归档而非删除」一致（真归档走 move_file 到 vault/_merged/）。
        失败返回 False 不抛（对齐 commit 返回值模式）。
        """
        with _LOCK:
            try:
                if not self.exists() and not self.ensure():
                    return False
                full = self._safe_path(relpath)
                if full is None:
                    logger.warning("gitrepo remove_file 路径逃逸拒绝: %s", relpath)
                    return False
                if not os.path.isfile(full):
                    return False
                os.remove(full)
                return self.commit(message or f"remove {relpath}")
            except Exception:
                logger.exception("gitrepo remove_file 异常 %s", relpath)
                return False

    # ── 查询 ──
    def log(self, n: int = 20) -> list:
        """最近 n 条 commit：[{hash, subject, author, date}]。"""
        try:
            code, out = self._git(
                ["log", f"-{n}", "--pretty=format:%H|%s|%an|%ad", "--date=short"],
                check=False)
            if code != 0 or not out.strip():
                return []
            rows = []
            for line in out.splitlines():
                parts = line.split("|", 3)
                if len(parts) == 4:
                    rows.append({"hash": parts[0], "subject": parts[1],
                                 "author": parts[2], "date": parts[3]})
            return rows
        except Exception:
            return []

    def head_hash(self) -> str:
        """当前 HEAD commit hash（40 hex），无 commit 返回空串。"""
        try:
            code, out = self._git(["rev-parse", "HEAD"], check=False)
            return out.strip() if code == 0 else ""
        except Exception:
            return ""

    def status(self) -> str:
        """git status --porcelain（空串 = 干净）。失败返回空串。"""
        try:
            code, out = self._git(["status", "--porcelain"], check=False)
            return out.strip() if code == 0 else ""
        except Exception:
            return ""

    def diff(self, ref1: str = "HEAD", ref2: str = "") -> str:
        """两 ref 间 diff（ref2 缺省 = 工作区 vs ref1）。失败返回空串。"""
        try:
            args = ["diff", ref1] + ([ref2] if ref2 else [])
            code, out = self._git(args, check=False)
            return out if code == 0 else ""
        except Exception:
            return ""

    def read_at(self, relpath: str, ref: str = "HEAD") -> str | None:
        """读历史版本内容（git show ref:path）。不存在/失败返回 None。"""
        try:
            code, out = self._git(["show", f"{ref}:{relpath}"], check=False)
            return out if code == 0 else None
        except Exception:
            return None

    # ── 内部 ──
    def _safe_path(self, relpath: str):
        """相对路径解析 + 防逃逸（.. 越出 root 返回 None）。"""
        try:
            full = os.path.abspath(os.path.join(self.root, relpath))
            if os.path.commonpath([self.root, full]) != self.root:
                return None
            return full
        except Exception:
            return None

    def _git(self, args, check=True):
        """subprocess 调 git，list 参数无 shell。返回 (code, stdout)。"""
        cmd = ["git", "-C", self.root, *args]
        try:
            p = subprocess.run(cmd, capture_output=True, text=True,
                               encoding="utf-8", errors="replace",
                               timeout=30, creationflags=_CREATE_NO_WINDOW)
            return p.returncode, p.stdout
        except subprocess.TimeoutExpired:
            logger.warning("git 超时: %s", args[0] if args else "")
            return -1, ""
        except FileNotFoundError:
            logger.warning("git 不在 PATH（影子仓库不可用，降级）")
            return -1, ""
        except Exception:
            logger.exception("git 调用异常: %s", args[:1])
            return -1, ""
