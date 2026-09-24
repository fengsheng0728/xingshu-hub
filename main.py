"""
星枢 Sync Hub — 多 Agent 协同中心
启动: python main.py
"""
import logging
logger = logging.getLogger("xingshu.main")


# ============ CD-082（2026-09-23 运维轮）：启动迁移检查 ============
# 背景：迁移形态此前只有手动 `python -m alembic upgrade head`，机房重启/换机部署后
# 忘了跑迁移 = 运行期撞缺列（CD-030 / CD-055 同族）。这里在 uvicorn.run 之前做一次检查：
#   - 库无 alembic_version 表（全新库 / 内联 DDL 建的库）→ 不猜、不迁移，只告警继续启动
#   - 已是最新 → 无动作
#   - 落后 + migrations.auto_upgrade=true（默认）→ 执行 `alembic upgrade head`
#   - 落后 + 开关关 → 告警后继续启动（存量部署行为不变）
#   - 迁移失败 → fail-closed 拒绝启动（与 TLS / 防火墙硬门同风格），绝不带着半个 schema 跑
# 测试态（models._in_test_context）在调用点直接跳过，避免测试库被迁移。


def _alembic_head(repo_root: str = "") -> str:
    """读迁移脚本目录的 head revision（只读脚本，不连库）。"""
    import os
    from alembic.config import Config as _AlembicConfig
    from alembic.script import ScriptDirectory
    repo_root = repo_root or os.path.dirname(os.path.abspath(__file__))
    acfg = _AlembicConfig(os.path.join(repo_root, "alembic.ini"))
    acfg.set_main_option("script_location", os.path.join(repo_root, "migrations", "alembic"))
    return ScriptDirectory.from_config(acfg).get_current_head()


def _db_alembic_version(db_path: str):
    """返回库内 alembic_version；无该表 → None（区分「没有迁移登记」与「登记为空」）。"""
    import os, sqlite3
    if not db_path or not os.path.exists(db_path):
        return None
    conn = sqlite3.connect(db_path)
    try:
        row = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='alembic_version'"
        ).fetchone()
        if not row:
            return None
        cur = conn.execute("SELECT version_num FROM alembic_version").fetchone()
        return cur[0] if cur else ""
    finally:
        conn.close()


def _run_alembic_upgrade(repo_root: str, db_path: str, config_dir: str = "") -> int:
    """在子进程里执行 `alembic upgrade head`（migrations/alembic/env.py 认 SYNC_HUB_DB）。返回 rc。"""
    import os, subprocess, sys
    env = dict(os.environ)
    env["SYNC_HUB_DB"] = os.path.abspath(db_path)
    if config_dir:
        env["SYNC_HUB_CONFIG_DIR"] = config_dir
    proc = subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head"],
        cwd=repo_root, env=env, capture_output=True, text=True,
    )
    if proc.returncode != 0:
        print(f"[迁移] alembic upgrade head 失败 rc={proc.returncode}"
              f"\n{(proc.stdout or '') + (proc.stderr or '')}", file=sys.stderr)
    else:
        print("[迁移] alembic upgrade head 完成")
    return proc.returncode


def run_startup_migration(db_path: str, repo_root: str = "",
                          auto_upgrade: bool = True, config_dir: str = "") -> dict:
    """启动迁移检查。返回 {"action", "from", "to"}；迁移失败抛 RuntimeError（调用方 fail-closed）。"""
    import os
    repo_root = repo_root or os.path.dirname(os.path.abspath(__file__))
    head = _alembic_head(repo_root)
    current = _db_alembic_version(db_path)

    if current is None:
        print(f"[迁移] 库内无 alembic_version 表（全新库 / 未登记迁移），跳过自动迁移: {db_path}")
        return {"action": "skipped_no_version_table", "from": "", "to": head}
    if current == head:
        return {"action": "up_to_date", "from": current, "to": head}
    if not auto_upgrade:
        print(f"[WARN] 库迁移落后（{current or '空'} -> {head}）且 migrations.auto_upgrade=false："
              "本次不迁移，请手动执行 `python -m alembic upgrade head`")
        return {"action": "skipped_disabled", "from": current, "to": head}

    print(f"[迁移] 库迁移落后：{current or '空'} -> {head}，执行 alembic upgrade head")
    rc = _run_alembic_upgrade(repo_root, db_path, config_dir)
    if rc != 0:
        raise RuntimeError(f"alembic upgrade head 失败（rc={rc}），拒绝带着未完成的迁移启动")
    after = _db_alembic_version(db_path)
    if after != head:
        raise RuntimeError(f"迁移执行后版本仍不符：期望 {head}，实际 {after!r}")
    print(f"[迁移] 完成：{current} -> {after}")
    return {"action": "upgraded", "from": current, "to": after}


# ============ CD-083/CD-099（2026-09-23）：启动横幅 + hub_token 启动硬门 ============

def startup_banner(host: str, port: int, tls_enabled: bool, cfg=None) -> str:
    """CD-083：启动配置横幅（单行）。敏感值打码——hub_token 只报 set/empty，绝不输出明文。"""
    from models import CONFIG as _cfg, HUB_VERSION
    cfg = cfg or _cfg
    token = str(getattr(cfg, "HUB_TOKEN", "") or "").strip()
    proxies = getattr(cfg, "TRUSTED_PROXIES", None) or []
    return (
        f"[banner] Sync Hub v{HUB_VERSION} "
        f"listen={host}:{port} tls={'on' if tls_enabled else 'off'} "
        f"auth_mode={getattr(cfg, 'AUTH_MODE', 'local')} "
        f"registration={getattr(cfg, 'AUTH_REGISTRATION', 'guarded')} "
        f"hub_token={'***' if token else '(empty)'} "
        f"trusted_proxies={len(proxies)}"
    )


def _token_placeholder(token: str) -> bool:
    """CD-099：空 token 或占位符（与 config.example.yaml 的 CHANGE_ME 占位口径一致）。"""
    t = str(token or "").strip()
    return (not t) or t.upper().startswith("CHANGE_ME")


def check_startup_token_policy(host: str, token: str) -> str:
    """CD-099（H-4 默认值收口）：hub_token 启动硬门判定。

    返回 "ok" | "warn" | "fatal"：
      - 空/占位 token + 非回环 host → "fatal"（调用方拒绝启动：0.0.0.0 裸奔 =
        任何局域网成员可匿名 register/读业务端点）
      - 空/占位 token + 回环 host   → "warn"（本机开发场景不阻断）
      - 已配真实 token              → "ok"
    NO_AUTH/测试态由调用方按 models._in_test_context() 跳过本判定。
    """
    if not _token_placeholder(token):
        return "ok"
    return "warn" if host in ("127.0.0.1", "localhost", "::1") else "fatal"


if __name__ == "__main__":
    from routes import app
    import os, sys, yaml, uvicorn

    config_dir = os.environ.get("SYNC_HUB_CONFIG_DIR", "./config")
    config_path = os.path.join(config_dir, "config.yaml")
    if not os.path.exists(config_path):
        os.makedirs(config_dir, exist_ok=True)
        default_config = {
            # CD-099：首跑生成的默认配置只绑回环（0.0.0.0 + 空 hub_token 会被启动硬门拒绝）
            "server": {"port": 3060, "host": "127.0.0.1"},
            "auth": {"enabled": True},
            "database": {"path": "./sync_hub.db", "backup_enabled": True, "backup_interval_days": 7},
            "retention": {"memory_days": 180, "events_days": 60, "tasks_days": 365},
            "logging": {"level": "info", "keep_days": 30},
            "ui": {"start_minimized": False, "close_to_tray": True},
        }
        with open(config_path, "w", encoding="utf-8") as f:
            yaml.dump(default_config, f, allow_unicode=True, default_flow_style=False)

    host, port = "0.0.0.0", 3060
    try:
        with open(config_path, "r", encoding="utf-8") as f:
            cfg = yaml.safe_load(f) or {}
        host = cfg.get("server", {}).get("host", "0.0.0.0")
        port = cfg.get("server", {}).get("port", 3060)
    except Exception as _exc:
        logger.debug("main silent-except(<module>): %s", _exc)

    # CD-068：可选传输层加密（默认关，零迁移）。开启后 REST→https、WS→wss。
    # 配了 TLS 但证书不可用 → fail-closed 拒绝启动，绝不静默跑明文。
    from tls_util import load_tls_config, validate_tls_files, scheme_for
    _tls = load_tls_config(config_path)
    _tls_err = validate_tls_files(_tls)
    if _tls_err:
        print(f"[FATAL] {_tls_err}", file=sys.stderr)
        print("[FATAL] 修复: 生成/放置 PEM 证书，或把 server.tls.enabled 置为 false。",
              file=sys.stderr)
        sys.exit(1)
    _tls_kwargs = {}
    _scheme = scheme_for(_tls["enabled"])
    if _tls["enabled"]:
        _tls_kwargs = {"ssl_certfile": _tls["certfile"], "ssl_keyfile": _tls["keyfile"]}
    elif host not in ("127.0.0.1", "localhost", "::1"):
        # 对外暴露 + 明文：凭据（Bearer）可被嗅探/中间人——只告警不阻断（存量部署不破坏）
        print(f"[WARN] 监听 {host} 且未启用 TLS：Bearer 凭据明文过网。"
              "生产对外请配置 server.tls（见 docs/external-api-access.md）",
              file=sys.stderr)

    is_electron = os.environ.get("SYNC_HUB_ELECTRON") == "1"
    
    # 生产保险：PyInstaller 打包构建中 NO_AUTH=1 拒绝启动
    if getattr(sys, 'frozen', False) and os.environ.get("SYNC_HUB_NO_AUTH", "").strip() in ("1", "true", "yes"):
        print("[FATAL] NO_AUTH=1 在生产打包中不允许启动。开发模式仅限源码运行。", file=sys.stderr)
        sys.exit(1)

    # P0 S4（2026-08-04）：生产打包 + 非回环绑定 + 无防火墙规则 → 拒绝启动并输出修复命令。
    # 源码模式保持警告（开发/测试环境无防火墙规则是常态，见 db.py check_windows_firewall）。
    if getattr(sys, 'frozen', False) and host not in ("127.0.0.1", "localhost", "::1"):
        try:
            from db import check_windows_firewall
            fw = check_windows_firewall(port)
            if isinstance(fw, dict) and not fw.get("rule_exists"):
                print(f"[FATAL] 检测到绑定 {host}:{port}（对外暴露）但未找到对应防火墙规则。", file=sys.stderr)
                print(f"[FATAL] 修复命令: netsh advfirewall firewall add rule name='Sync Hub {port}' "
                      f"dir=in action=allow protocol=TCP localport={port}", file=sys.stderr)
                print("[FATAL] 或显式设置 server.host: 127.0.0.1（仅本机访问）。", file=sys.stderr)
                sys.exit(1)
        except Exception as _exc:
            logger.debug("main silent-except(<module>): %s", _exc)
    
    # CD-099（2026-09-23，H-4 默认值收口）：hub_token 启动硬门（取代原 OGA guarded-only 检查，
    # 覆盖面从「guarded + 空 token」扩到「任何 registration 模式 + 空/占位 token + 非回环」——
    # 0.0.0.0 裸奔时任何局域网成员可匿名访问，与 registration 模式无关）。
    # NO_AUTH/测试态不拦（models._in_test_context 惯例：SYNC_HUB_NO_AUTH / PYTEST / DB_GUARD 标记）。
    from models import CONFIG, _in_test_context
    _tok_policy = check_startup_token_policy(host, CONFIG.HUB_TOKEN)
    if _tok_policy == "fatal" and not _in_test_context():
        print(f"[FATAL] auth.hub_token 为空或占位符（CHANGE_ME…）且监听 {host}（非回环）——拒绝启动。"
              '请在 config.yaml 填入强随机 hub_token(示例: python -c "import secrets; print(secrets.token_urlsafe(32))")'
              "，或把 server.host 改为 127.0.0.1（仅本机访问）。",
              file=sys.stderr)
        sys.exit(1)
    if _tok_policy == "warn" and not _in_test_context():
        print(f"[WARN] auth.hub_token 为空或占位符：当前监听 {host}（回环），允许启动；"
              "对外开放前必须配置强随机 hub_token（受管注册 guarded 的引导认证也依赖它）。",
              file=sys.stderr)

    # T17: 联邦防重放 nonce 持久化（独立 replay_nonce.db，与主库同目录）。
    # init_replay_store 内部已 try/except 兜底：建库失败降级纯内存打 warning，不阻断启动。
    import fed_crypto
    fed_crypto.init_replay_store(os.path.join(os.path.dirname(os.path.abspath(CONFIG.DB_PATH)), "replay_nonce.db"))

    # CD-082（2026-09-23 运维轮）：启动自动迁移检查（在 uvicorn.run 之前）。
    # 测试态跳过（models._in_test_context：单元测试与测试自 spawn 的 Hub 都不该动迁移）；
    # 生产/部署态默认执行，config 里 migrations.auto_upgrade: false 可关。
    from models import _in_test_context
    try:
        with open(config_path, "r", encoding="utf-8") as f:
            _mig_cfg = (yaml.safe_load(f) or {}).get("migrations", {}) or {}
    except Exception as _exc:
        logger.debug("main silent-except(<module>): %s", _exc)
        _mig_cfg = {}
    if _in_test_context():
        print("启动迁移检查：测试态跳过（SYNC_HUB_DB_GUARD / PYTEST / NO_AUTH 标记）")
    else:
        try:
            _mig = run_startup_migration(CONFIG.DB_PATH,
                                        auto_upgrade=bool(_mig_cfg.get("auto_upgrade", True)),
                                        config_dir=config_dir)
            print(f"启动迁移检查：{_mig['action']}（{_mig.get('from') or '空'} -> {_mig.get('to')}）")
        except Exception as _exc:
            print(f"[FATAL] 启动迁移失败: {_exc}", file=sys.stderr)
            print("[FATAL] 修复: 手动执行 `python -m alembic upgrade head` 后重启；"
                  "或（不推荐）临时置 config `migrations.auto_upgrade: false`", file=sys.stderr)
            sys.exit(1)

    log_level = "warning" if is_electron else "info"
    # CD-083：启动配置横幅（敏感值打码，token 明文绝不输出）
    print(startup_banner(host, port, _tls["enabled"], CONFIG))
    print(f"启动 Hub ({_scheme['http']}://{host}:{port}, electron={is_electron}, "
          f"tls={'on' if _tls['enabled'] else 'off'})")
    uvicorn.run(app, host=host, port=port, log_level=log_level, **_tls_kwargs)
