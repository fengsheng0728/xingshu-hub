"""
星枢 Sync Hub — 多 Agent 协同中心
启动: python main.py
"""
import logging
logger = logging.getLogger("xingshu.main")

if __name__ == "__main__":
    from routes import app
    import os, sys, yaml, uvicorn

    config_dir = os.environ.get("SYNC_HUB_CONFIG_DIR", "./config")
    config_path = os.path.join(config_dir, "config.yaml")
    if not os.path.exists(config_path):
        os.makedirs(config_dir, exist_ok=True)
        default_config = {
            "server": {"port": 3060, "host": "0.0.0.0"},
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
        logger.debug("main silent-except @30: %s", _exc)

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
            logger.debug("main silent-except @52: %s", _exc)
    
    # OGA: guarded 受管注册必须有部署级 hub_token —— 中间件(routes.py:152)靠
    # CONFIG.HUB_TOKEN 强制 register/bootstrap 引导端点认证, guarded 但 hub_token
    # 为空 = 匿名注册门失效(裸奔),拒绝启动。
    from models import CONFIG
    if CONFIG.AUTH_REGISTRATION == "guarded" and not str(CONFIG.HUB_TOKEN or "").strip():
        print('[FATAL] auth.registration=guarded 但 auth.hub_token 为空 — 受管注册必须有部署级凭据。'
              '请在 config.yaml 填入强随机 hub_token(示例: python -c "import secrets; print(secrets.token_urlsafe(32))")',
              file=sys.stderr)
        sys.exit(1)

    # T17: 联邦防重放 nonce 持久化（独立 replay_nonce.db，与主库同目录）。
    # init_replay_store 内部已 try/except 兜底：建库失败降级纯内存打 warning，不阻断启动。
    import fed_crypto
    fed_crypto.init_replay_store(os.path.join(os.path.dirname(os.path.abspath(CONFIG.DB_PATH)), "replay_nonce.db"))

    log_level = "warning" if is_electron else "info"
    print(f"启动 Hub (host={host}, port={port}, electron={is_electron})")
    uvicorn.run(app, host=host, port=port, log_level=log_level)
