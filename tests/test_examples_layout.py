"""D-6（3-5b 资产出仓）回归断言：examples/ 布局 + /showcase 路由不回归

离线断言，不起真实端口：
1. GET /showcase 返回 200 且正文非空（TestClient 上下文管理器触发 lifespan）
2. examples/ 下 5 项资产存在；根目录 5 项不存在
3. sync_hub.spec datas 指向 examples/showcase
4. routes.py 读 ./examples/showcase/index.html
"""
from pathlib import Path

import pytest

# 用文件位置定位仓库根，不依赖 cwd
ROOT = Path(__file__).resolve().parents[1]

ASSETS = ["client.py", "demo_workflow.py", "seed_data.py", "showcase", "arch-site"]


@pytest.fixture(scope="module")
def client():
    """with 块触发 lifespan（铁律：否则 SharedWorkspace 跨 loop 死锁）"""
    from routes import app
    from fastapi.testclient import TestClient
    with TestClient(app) as c:
        yield c


def test_showcase_route_200_nonempty(client):
    """URL 白名单路由不回归：200 + 正文非空"""
    r = client.get("/showcase")
    assert r.status_code == 200
    assert r.text.strip()


@pytest.mark.parametrize("name", ASSETS)
def test_asset_moved_to_examples(name):
    assert (ROOT / "examples" / name).exists(), f"examples/{name} 缺失"
    assert not (ROOT / name).exists(), f"根目录仍残留 {name}"


def test_spec_datas_points_to_examples_showcase():
    text = (ROOT / "sync_hub.spec").read_text(encoding="utf-8")
    assert "'examples/showcase'" in text
    assert "('showcase', 'showcase')" not in text


def test_routes_reads_examples_showcase():
    text = (ROOT / "routes_pages.py").read_text(encoding="utf-8")
    assert "./examples/showcase/index.html" in text
