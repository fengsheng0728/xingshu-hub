"""静态页 _read_static_html 缓存测试（任务书 I）

覆盖：
① 首次读返回内容
② 二次读走缓存（同文件两次读 open 只调 1 次）
③ mtime 变化后重读
④ 文件不存在抛错且不缓存
"""
import builtins
import os

import pytest

import routes


@pytest.fixture(autouse=True)
def _clear_cache():
    routes._STATIC_HTML_CACHE.clear()
    yield
    routes._STATIC_HTML_CACHE.clear()


def _counting_open(monkeypatch):
    """统计 open 调用次数（只计目标文件，其他路径透传）。"""
    real_open = builtins.open
    counter = {"n": 0, "target": None}

    def _open(path, *args, **kwargs):
        if counter["target"] is not None and os.path.abspath(str(path)) == counter["target"]:
            counter["n"] += 1
        return real_open(path, *args, **kwargs)

    monkeypatch.setattr(builtins, "open", _open)
    return counter


def test_first_read_returns_content(tmp_path, monkeypatch):
    p = tmp_path / "page.html"
    p.write_text("<html>v1</html>", encoding="utf-8")
    counter = _counting_open(monkeypatch)
    counter["target"] = os.path.abspath(str(p))

    content = routes._read_static_html(str(p))
    assert content == "<html>v1</html>"
    assert counter["n"] == 1


def test_second_read_hits_cache(tmp_path, monkeypatch):
    p = tmp_path / "page.html"
    p.write_text("<html>v1</html>", encoding="utf-8")
    counter = _counting_open(monkeypatch)
    counter["target"] = os.path.abspath(str(p))

    first = routes._read_static_html(str(p))
    second = routes._read_static_html(str(p))
    assert first == second == "<html>v1</html>"
    # mtime 未变 → 第二次走缓存，open 只调 1 次
    assert counter["n"] == 1


def test_mtime_change_triggers_reread(tmp_path, monkeypatch):
    p = tmp_path / "page.html"
    p.write_text("<html>v1</html>", encoding="utf-8")
    counter = _counting_open(monkeypatch)
    counter["target"] = os.path.abspath(str(p))

    first = routes._read_static_html(str(p))

    # 写入新内容并显式改变 mtime（部分 FS 时间戳粒度粗，直接 os.utime 保证变化）
    p.write_text("<html>v2</html>", encoding="utf-8")
    st = p.stat()
    os.utime(p, (st.st_atime, st.st_mtime + 2))

    second = routes._read_static_html(str(p))
    assert first == "<html>v1</html>"
    assert second == "<html>v2</html>"
    assert counter["n"] == 2


def test_mtime_change_via_monkeypatched_getmtime(tmp_path, monkeypatch):
    p = tmp_path / "page.html"
    p.write_text("<html>v1</html>", encoding="utf-8")
    counter = _counting_open(monkeypatch)
    counter["target"] = os.path.abspath(str(p))

    routes._read_static_html(str(p))

    # 不改文件内容，仅伪造 mtime 变化 → 必须重读（此时第二次 open 发生）
    monkeypatch.setattr(routes.os.path, "getmtime", lambda _p: 999999.0)
    content = routes._read_static_html(str(p))
    assert content == "<html>v1</html>"
    assert counter["n"] == 2


def test_missing_file_raises_and_not_cached(tmp_path, monkeypatch):
    p = tmp_path / "missing.html"
    counter = _counting_open(monkeypatch)
    counter["target"] = os.path.abspath(str(p))

    # 不存在 → FileNotFoundError，异常语义与直接 open() 一致
    with pytest.raises(FileNotFoundError):
        routes._read_static_html(str(p))
    # 不缓存
    assert os.path.abspath(str(p)) not in routes._STATIC_HTML_CACHE

    # 文件出现后应能读到（证明此前的失败没有被缓存成"永久失败"）
    p.write_text("<html>now exists</html>", encoding="utf-8")
    assert routes._read_static_html(str(p)) == "<html>now exists</html>"
    assert counter["n"] == 1


def test_cache_keyed_by_absolute_path(tmp_path, monkeypatch):
    """同一文件的相对/绝对路径命中同一缓存条目。"""
    p = tmp_path / "page.html"
    p.write_text("<html>x</html>", encoding="utf-8")
    counter = _counting_open(monkeypatch)
    counter["target"] = os.path.abspath(str(p))

    monkeypatch.chdir(tmp_path)
    a = routes._read_static_html("page.html")          # 相对路径
    b = routes._read_static_html(os.path.abspath(str(p)))  # 绝对路径
    assert a == b
    assert counter["n"] == 1
