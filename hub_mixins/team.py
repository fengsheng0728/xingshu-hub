"""星枢 SyncHub — team Mixin"""
import logging
logger = logging.getLogger("xingshu.team")

import asyncio
import json
import hashlib
import time
import os
import sqlite3
import shutil
import secrets
from datetime import datetime, timezone, timedelta
from typing import Dict, List, Optional, Any
import numpy as np

from envelope import envelope_ping, serialize
from transport_audit import log_ping_pong


def _fetch_url_sync(request, timeout: float) -> bytes:
    """同步 HTTP 请求并读取响应体——仅经 asyncio.to_thread 调用，避免阻塞事件循环"""
    import urllib.request
    with urllib.request.urlopen(request, timeout=timeout) as resp:
        return resp.read()


class TeamMixin:
    """Auto-generated mixin — do not edit manually unless you know why."""

    def _validate_remote_url(self, url: str) -> bool:
        """URL 安全校验 — 仅允许 RFC1918 私网 + 3060 端口"""
        import ipaddress
        from urllib.parse import urlparse
        parsed = urlparse(url)
        if parsed.scheme != "http":
            return False
        port = parsed.port or 80
        if port != 3060:
            return False
        try:
            ip = ipaddress.ip_address(parsed.hostname)
        except ValueError:
            return False
        if ip.is_loopback or ip.is_link_local or ip.is_multicast or ip.is_unspecified:
            return False
        if not ip.is_private:
            return False
        return True


    async def request_pairing(self, agent_id: str) -> dict:
        """发起配对 → 生成 6 位码，5 分钟 TTL（T11：secrets 随机源，审计不落明文码）"""
        code = f"{secrets.randbelow(1000000):06d}"
        from datetime import datetime, timezone, timedelta
        expires = (datetime.now(timezone.utc) + timedelta(minutes=5)).isoformat()
        with self._db() as conn:
            c = conn.cursor()
            c.execute(
                "INSERT INTO pairing_codes (code, hub_id_a, agent_id_a, expires_at) VALUES (?, ?, ?, ?)",
                (code, self.hub_id, agent_id, expires),
            )
            conn.commit()
        await self._log_event("pairing_requested", agent_id, {})
        return {"pairing_code": code, "expires_at": expires}


    async def accept_pairing(self, agent_id: str, req: dict) -> dict:
        """接受配对 — 校验码 → 交换 key → 写入 team_members"""
        code = req.get("code", "").strip()
        remote_url = req.get("remote_hub_url", "").strip()
        remote_hub_id = req.get("remote_hub_id", "").strip()
        remote_agent_id = req.get("remote_agent_id", "").strip()

        if not code or not remote_url or not remote_hub_id:
            return {"error": "code, remote_hub_url, remote_hub_id 必填"}

        # 1. 校验 URL
        if not self._validate_remote_url(remote_url):
            return {"error": "URL 校验失败：仅允许内网 HTTP + 3060 端口", "url": remote_url}

        # 2. 配对码校验由对方 Hub 的 /pair/exchange 完成（码存在对方 pairing_codes 表，
        #    本地查表必失败——R3-FIX 注册 exchange 路由后本地校验段移除，防误拒绝）

        # 3. 握手：X25519 DH + 配对码 → session_key → AES-GCM 加密交换 api_key
        import secrets
        from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey, X25519PublicKey
        from cryptography.hazmat.primitives.kdf.hkdf import HKDF
        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM

        # 生成临时 X25519 密钥对
        private_key = X25519PrivateKey.generate()
        public_bytes = private_key.public_key().public_bytes(
            encoding=serialization.Encoding.Raw, format=serialization.PublicFormat.Raw
        )

        # 发给对方 Hub（携带 DH 公钥 + 配对码认证）
        import urllib.request
        try:
            req_body = json.dumps({
                "agent_id": agent_id,
                "hostname": self.hostname,
                "user_name": req.get("user_name", agent_id),
                "role": "worker",
                "department": req.get("department", ""),
                "remote_hub_url": req.get("own_hub_url", ""),
                "dh_public": public_bytes.hex(),  # X2
                "code": code,
            }).encode()
            hr = urllib.request.Request(
                f"{remote_url}/api/v1/team/pair/exchange",
                data=req_body,
                headers={"Content-Type": "application/json", "Authorization": f"Bearer {code}"},
                method="POST",
            )
            remote_info = json.loads(await asyncio.to_thread(_fetch_url_sync, hr, 8))
        except Exception as e:
            return {"error": f"握手失败: {str(e)[:80]}"}

        # 解析对方响应：DH 公钥 + 加密的 api_key + nonce
        remote_public_hex = remote_info.get("dh_public", "")
        encrypted_key_hex = remote_info.get("encrypted_key", "")
        nonce_hex = remote_info.get("nonce", "")
        if not remote_public_hex or not encrypted_key_hex or not nonce_hex:
            return {"error": "对方握手响应不完整"}

        # 派生共享密钥
        remote_public = bytes.fromhex(remote_public_hex)
        shared = private_key.exchange(X25519PublicKey.from_public_bytes(remote_public))
        session_key = HKDF(
            algorithm=hashes.SHA256(), length=32,
            salt=code.encode(), info=b"xingshu-team-pairing-v1",
        ).derive(shared)

        # 解密对方的 api_key
        aesgcm = AESGCM(session_key)
        try:
            remote_api_key = aesgcm.decrypt(
                bytes.fromhex(nonce_hex), bytes.fromhex(encrypted_key_hex), None
            ).decode()
        except Exception:
            return {"error": "解密对方 api_key 失败——配对码可能不匹配"}

        if not remote_api_key:
            return {"error": "对方未返回 api_key"}

        # 4. used 标记与 api_key 交换均在对方 Hub 的 exchange 完成（本地无此配对码）

        # 5. 写入 team_members（remote_agent_id 优先取请求参数，缺省用对方 Hub 返回的发起方 agent_id）
        from datetime import datetime, timezone, timedelta
        now = datetime.now(timezone.utc).isoformat()
        expires = (datetime.now(timezone.utc) + timedelta(days=30)).isoformat()
        if not remote_agent_id:
            remote_agent_id = remote_info.get("agent_id_a", "")
        with self._db() as conn:
            c = conn.cursor()
            c.execute(
                """INSERT INTO team_members
                   (local_agent_id, remote_hub_id, remote_hub_url, remote_agent_id, remote_api_key,
                    hostname, user_name, role, department, paired_at, key_expires_at, shared_secret)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
                (agent_id, remote_hub_id, remote_url, remote_agent_id, remote_api_key,
                 req.get("hostname", ""), req.get("user_name", ""),
                 req.get("role", "worker"), req.get("department", ""),
                 now, expires, session_key.hex()),  # P2: 落库共享密钥供联邦加密
            )
            conn.commit()

        await self._log_event("team_paired", agent_id, {"remote_hub": remote_hub_id, "remote_agent": remote_agent_id})
        return {"status": "paired", "remote_hub_id": remote_hub_id}


    async def list_team_members(self, agent_id: str) -> dict:
        """列出已配对的团队成员"""
        with self._db() as conn:
            c = conn.cursor()
            c.execute(
                "SELECT id, remote_hub_id, remote_hub_url, remote_agent_id, hostname, user_name, role, department, paired_at, key_expires_at, last_heartbeat, revoked_at FROM team_members WHERE local_agent_id = ?",
                (agent_id,),
            )
            rows = c.fetchall()
        return {"members": [{
            "id": r[0], "remote_hub_id": r[1], "remote_hub_url": r[2],
            "remote_agent_id": r[3], "hostname": r[4], "user_name": r[5],
            "role": r[6], "department": r[7], "paired_at": r[8],
            "key_expires_at": r[9], "last_heartbeat": r[10],
            "online": r[10] is not None and (datetime.now(timezone.utc) - datetime.fromisoformat(r[10])).total_seconds() < 60 if r[10] else False,
            "revoked": bool(r[11]),
        } for r in rows]}


    async def remove_team_member(self, member_id: int, agent_id: str) -> dict:
        """移出团队：本地标记 revoked + 通知对方"""
        with self._db() as conn:
            c = conn.cursor()
            c.execute("SELECT remote_hub_url, remote_agent_id, remote_hub_id FROM team_members WHERE id = ? AND local_agent_id = ?", (member_id, agent_id))
            row = c.fetchone()
            if not row:
                return {"error": "成员不存在"}

            remote_url, remote_agent_id, remote_hub_id = row
            # 本地标记撤销
            c.execute("UPDATE team_members SET revoked_at = datetime('now') WHERE id = ?", (member_id,))
            conn.commit()

        # 通知对方撤销
        try:
            import urllib.request
            req = urllib.request.Request(
                f"{remote_url}/api/v1/team/revoke",
                data=json.dumps({"revoked_agent_id": agent_id}).encode(),
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            await asyncio.to_thread(_fetch_url_sync, req, 5)
        except Exception as _exc:
            # 对方离线——撤销队列在 3.5 节设计，当前版本记录到 log 等后续重试
            logger.warning("team silent-except @216: %s", _exc)

        await self._log_event("team_removed", agent_id, {"member_id": member_id, "remote_hub": remote_hub_id})
        return {"status": "removed", "remote_hub_id": remote_hub_id}


    async def _handle_pair_exchange(self, code: str, agent_info: dict) -> dict:
        """处理对方的配对握手——X25519 DH + AES-GCM 加密返回 api_key"""
        with self._db() as conn:
            c = conn.cursor()
            c.execute("SELECT id, hub_id_a, agent_id_a FROM pairing_codes WHERE code = ? AND expires_at > datetime('now') AND used = 0", (code,))
            row = c.fetchone()
            if not row:
                return {"error": "配对码无效"}
            pc_id, hub_id_a, agent_id_a = row

            import secrets
            from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey, X25519PublicKey
            from cryptography.hazmat.primitives.kdf.hkdf import HKDF
            from cryptography.hazmat.primitives import hashes, serialization
            from cryptography.hazmat.primitives.ciphers.aead import AESGCM

            # 生成临时 X25519 密钥对
            private_key = X25519PrivateKey.generate()
            public_bytes = private_key.public_key().public_bytes(
                encoding=serialization.Encoding.Raw, format=serialization.PublicFormat.Raw
            )

            # 解析发起方的 DH 公钥
            initiator_public_hex = agent_info.get("dh_public", "")
            if not initiator_public_hex:
                return {"error": "缺少 DH 公钥"}
            initiator_public = bytes.fromhex(initiator_public_hex)

            # 派生共享密钥
            shared = private_key.exchange(X25519PublicKey.from_public_bytes(initiator_public))
            session_key = HKDF(
                algorithm=hashes.SHA256(), length=32,
                salt=code.encode(), info=b"xingshu-team-pairing-v1",
            ).derive(shared)

            # 生成 api_key 并用 AES-GCM 加密
            api_key = secrets.token_urlsafe(32)
            aesgcm = AESGCM(session_key)
            import os as _os
            nonce = _os.urandom(12)
            encrypted = aesgcm.encrypt(nonce, api_key.encode(), None)

            c.execute("UPDATE pairing_codes SET used = 1, hub_id_b = ? WHERE id = ?", (agent_info.get("agent_id", ""), pc_id))

            # 写入对方到本地 team_members（角色默认 worker——对方发来的 agent_info.role 仅 advisory）
            from datetime import datetime, timezone, timedelta
            now = datetime.now(timezone.utc).isoformat()
            expires = (datetime.now(timezone.utc) + timedelta(days=30)).isoformat()
            # local_agent_id = 发起配对的本地 Agent（pairing_codes.agent_id_a）
            c.execute(
                """INSERT OR IGNORE INTO team_members
                   (local_agent_id, remote_hub_id, remote_hub_url, remote_agent_id, remote_api_key,
                    hostname, user_name, role, department, paired_at, key_expires_at, shared_secret)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
                (agent_id_a,
                 hub_id_a,
                 agent_info.get("remote_hub_url", ""),
                 agent_info.get("agent_id", ""),
                 api_key,
                 agent_info.get("hostname", ""),
                 agent_info.get("user_name", ""),
                 "worker",
                 agent_info.get("department", ""),
                 now, expires, session_key.hex()),  # P2: 落库共享密钥供联邦加密
            )
            # 已存在配对记录时（重配对/更新），同步刷新 shared_secret
            c.execute(
                "UPDATE team_members SET shared_secret = ?, remote_api_key = ? "
                "WHERE local_agent_id = ? AND remote_hub_id = ?",
                (session_key.hex(), api_key, agent_id_a, hub_id_a),
            )
            conn.commit()

        return {
            "dh_public": public_bytes.hex(),
            "encrypted_key": encrypted.hex(),
            "nonce": nonce.hex(),
            "agent_id_a": agent_id_a,
        }



    # ═══════════════════════════════════════════════════════════
    # H4 汇入管道 + E.7 重判定（附录 E v1.4，2026-08-06）
    # ═══════════════════════════════════════════════════════════


