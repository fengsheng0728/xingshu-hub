# -*- coding: utf-8 -*-
"""gitrepo.py 单测 — 阶段3-P0 主干-分干数据底座基建"""
import os
import pytest

from gitrepo import GitRepo


@pytest.fixture()
def repo(tmp_path):
    r = GitRepo(str(tmp_path / "r"))
    assert r.ensure()
    return r


def test_ensure_init_and_idempotent(repo):
    assert repo.exists()
    # 幂等：重复 ensure 不重建
    head1 = repo.head_hash()
    assert repo.ensure()
    assert repo.exists()
    assert repo.head_hash() == head1


def test_write_commit_log(repo):
    assert repo.write_file("a.txt", "hello")
    assert repo.commit("first")
    log = repo.log()
    assert len(log) == 1
    assert log[0]["subject"] == "first"
    assert len(log[0]["hash"]) == 40


def test_commit_chinese_filename_message(repo):
    assert repo.write_file("记忆/客户A.md", "内容")
    assert repo.commit("镜像: 客户A 记忆")
    log = repo.log()
    assert log[0]["subject"] == "镜像: 客户A 记忆"
    assert repo.read_at("记忆/客户A.md") == "内容"


def test_read_at_history(repo):
    assert repo.write_file("doc.md", "v1")
    assert repo.commit("v1")
    assert repo.write_file("doc.md", "v2")
    assert repo.commit("v2")
    assert repo.read_at("doc.md") == "v2"
    assert repo.read_at("doc.md", "HEAD~1") == "v1"
    # 不存在的文件
    assert repo.read_at("nope.md") is None


def test_diff_between_refs(repo):
    assert repo.write_file("d.md", "one")
    assert repo.commit("c1")
    assert repo.write_file("d.md", "two")
    assert repo.commit("c2")
    d = repo.diff("HEAD~1", "HEAD")
    assert "one" in d and "two" in d


def test_status_clean_and_dirty(repo):
    assert repo.write_file("s.md", "x")
    assert repo.commit("c")
    assert repo.status() == ""
    repo.write_file("s.md", "y")
    assert "s.md" in repo.status()


def test_head_hash_after_commit(repo):
    assert repo.head_hash() == ""
    repo.write_file("h.md", "x")
    repo.commit("c")
    assert len(repo.head_hash()) == 40


def test_ensure_fails_on_invalid_root(tmp_path):
    # root 是文件 → ensure 失败但不抛异常
    f = tmp_path / "blocker"
    f.write_text("i am a file", encoding="utf-8")
    r = GitRepo(str(f))
    assert r.ensure() is False
    # 操作全部静默降级
    assert r.commit("x") is False
    assert r.read_at("a.md") is None
    assert r.log() == []


def test_commit_nothing_to_commit(repo):
    repo.write_file("n.md", "x")
    assert repo.commit("c")
    # 无改动再次 commit → 视为成功（nothing to commit）
    assert repo.commit("again")
    assert len(repo.log()) == 1


def test_write_file_creates_parent_dirs(repo):
    assert repo.write_file("a/b/c/deep.md", "deep")
    assert os.path.isfile(os.path.join(repo.root, "a", "b", "c", "deep.md"))
