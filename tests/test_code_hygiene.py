"""CD-037 机器断言（T26）：routes.py 死 import / shadow 包行尾 / 过期行号引用。

H-1 routes.py 无 F401（刻意 re-export 已逐名 noqa 豁免，见 routes.py 文件头注释）
H-2 hub_mixins/shadow/*.py 文件内不混行尾（全 CRLF 或全 LF 均可；跨平台口径）
H-3 全仓 *.py 不存在 `xxx.py:123` 形式的行号引用（拆分后行号必失真，
 
H-4 å¨ä» `silent-except(<name>)` ä½ç½®æ è®°å¿é¡»ç­äºå¶æå¨å½æ°å
    ï¼æ¨¡åçº§ç¨ <module>ï¼ââå `@<è¡å·>` å½¢å¼ä¼éä»£ç æ¬å¨å¨é¨å¤±ç
   一律改用符号名引用；白名单例外逐条列明如下）
"""
import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent

# H-3 白名单例外（非本任务白名单文件，禁止修改，故登记为例外）：
#   docs/ 归档脚本与既有测试文件内的历史引用，内容为历史快照，不随拆分失真。
H3_EXCEPTIONS = {
    # CD-060（T27）新增：lazy 建表的**说明性引用**（指向创建者位置，非过期日志串）
    "db.py": 4,
    "tests/test_schema_hard_equality.py": 2,
    "docs/e2e_backfeed_b2.py": 2,
    "tests/test_403_policy_matrix.py": 1,
    "tests/test_shared_read_audit.py": 1,
    "tests/test_xs003_disclosure_chain.py": 1,
}

_LINE_REF_RE = re.compile(r"[A-Za-z_]+\.py:[0-9]+")


def _run_ruff_f401():
    ruff = shutil.which("ruff")
    cmd = [ruff, "check", "routes.py", "--select", "F401"] if ruff else None
    if cmd is None:
        # 退路：python -m ruff
        probe = subprocess.run(
            ["python", "-m", "ruff", "--version"],
            capture_output=True, text=True, cwd=ROOT,
        )
        if probe.returncode != 0:
            return None
        cmd = ["python", "-m", "ruff", "check", "routes.py", "--select", "F401"]
    return subprocess.run(cmd, capture_output=True, text=True, cwd=ROOT)


def test_h1_routes_no_dead_imports():
    proc = _run_ruff_f401()
    if proc is None:
        pytest.skip("ruff 不可用")
    findings = [ln for ln in proc.stdout.splitlines() if ln.startswith("F401")]
    assert not findings, "routes.py 仍存在 F401 死 import：\n" + "\n".join(findings)


def test_h2_shadow_no_mixed_eol():
    """文件内**不得混行尾**（全 CRLF 或全 LF 都可——检出/入库行尾由 git 决定，
    跨平台 CI 下不能要求「必须 CRLF」；混行尾才是真问题）。

    口径修正（CD-037/H-2，2026-09-20）：原断言「无裸 LF」隐含要求 CRLF，在 Linux/LF 检出环境
    会假红；改为只判「同一文件内 CRLF 与裸 LF 并存」。"""
    offenders = []
    for f in sorted((ROOT / "hub_mixins" / "shadow").glob("*.py")):
        raw = f.read_bytes()
        crlf = raw.count(b"\r\n")
        bare = raw.count(b"\n") - crlf
        if crlf and bare:
            offenders.append("%s 混行尾: CRLF=%d bare_LF=%d" % (f.name, crlf, bare))
    assert not offenders, "shadow 包存在混行尾：\n" + "\n".join(offenders)


def test_h3_no_stale_line_refs():
    found = {}
    for f in sorted(ROOT.rglob("*.py")):
        rel = f.relative_to(ROOT).as_posix()
        if rel == "tests/test_code_hygiene.py":
            continue  # 本文件含正则模式自身，豁免
        if any(part in {".git", ".hermes", "node_modules"} for part in f.parts):
            continue
        try:
            text = f.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        n = len(_LINE_REF_RE.findall(text))
        if n:
            found[rel] = n
    assert found == H3_EXCEPTIONS, (
        "行号引用命中与登记例外不一致（新增须改符号名引用；"
        "例外变更须逐条登记）：\n  实测=%r\n  例外=%r" % (found, H3_EXCEPTIONS)
    )



def test_h4_silent_except_markers_match_enclosing_function():
    """H-4（CD-037 残余① 配套）：日志串里的位置标记必须与所在函数一致。

    原形式是 `silent-except @<行号>`，代码一搬动就全部失真（2026-09-21 实测
    64 处里 62 处已失真）。本轮改为符号名标记 `silent-except(<func>)`，并用本
    断言保真——标记写错即红，不会再腐烂。模块级 except 用 `<module>`。
    """
    import ast

    pattern = re.compile(r"silent-except\(([^)]*)\)")
    offenders = []
    for f in sorted(ROOT.rglob("*.py")):
        rel = f.relative_to(ROOT).as_posix()
        if rel == "tests/test_code_hygiene.py":
            continue  # 本文件含模式自身，豁免（同 H-3）
        if any(part in {".git", ".hermes", "node_modules", "__pycache__"} for part in f.parts):
            continue
        try:
            text = f.read_text(encoding="utf-8")
            tree = ast.parse(text)
        except (UnicodeDecodeError, OSError, SyntaxError):
            continue
        spans = [(n.lineno, getattr(n, "end_lineno", n.lineno), n.name)
                 for n in ast.walk(tree)
                 if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]

        def _enclosing(ln):
            best = None
            for s, e, name in spans:
                if s <= ln <= e and (best is None or s > best[0]):
                    best = (s, name)
            return best[1] if best else "<module>"

        for i, line in enumerate(text.splitlines(), start=1):
            for m in pattern.finditer(line):
                expect = _enclosing(i)
                if m.group(1) != expect:
                    offenders.append(
                        "%s:%d 标记(%s) != 所在函数(%s)" % (rel, i, m.group(1), expect))
    assert not offenders, (
        "silent-except 位置标记与所在函数不一致（搬动代码后须同步标记）：\n"
        + "\n".join(offenders))
