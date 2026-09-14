"""集成层 — Connector 适配器契约（hub-ui-redesign.md §7.2）+ 凭证加密

每个连接器 = connectors/ 目录下一个文件，实现本模块 Connector 协议，
并暴露模块级函数 get_connector() -> Connector 实例。注册表自动发现。
"""
import hashlib
import os
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Callable, Dict, Iterator, List, Optional, Protocol


# ───────────────────────── 数据类型 ─────────────────────────

@dataclass
class TestResult:
    """test_connection 结果（⑩页"测试连接"按钮）"""
    ok: bool
    detail: str = ""
    latency_ms: float = 0.0


@dataclass
class RawRecord:
    """适配器从外部系统拿到的原始记录（未映射）"""
    source_id: str                    # 外部系统内主键
    entity_hint: str                  # 适配器建议的实体类型（customer/order/...）
    payload: Dict[str, Any]           # 原始字段
    updated_at: str = ""              # 外部系统侧更新时间（ISO）


@dataclass
class CanonicalEntity:
    """规范实体（§7.3 统一实体映射）— 跨系统同一概念先映射到它再入管道"""
    entity_type: str                  # customer / employee / order / invoice / voucher / product
    source_system: str                # 溯源：来源系统（= connector.name）
    source_id: str                    # 溯源：外部主键
    title: str                        # 展示标题
    content: str                      # 进 E 管道的正文（自然语言化）
    fields: Dict[str, Any] = field(default_factory=dict)   # 结构化字段（检索/映射编辑用）
    dept: str = ""                    # 数据域标签（附录 K：dept/project）
    trust_level: str = "external"     # taint：集成数据恒为 external
    updated_at: str = ""


@dataclass
class HubEvent:
    """出站事件（Hub → 外部系统）"""
    event_type: str
    payload: Dict[str, Any]
    created_at: str = ""


# ───────────────────────── Connector 契约 ─────────────────────────

class Connector(Protocol):
    """适配器契约。实现本协议 + 模块级 get_connector() 即完成注册。"""

    name: str                         # "yonyou_u8" / "example_connector"
    display_name: str                 # 展示名（"用友 U8"）
    category: str                     # erp / crm / finance / oa / other

    def configure(self, cfg: Dict[str, Any]) -> None:
        """写入连接参数。cfg 中密钥字段已被注册表解密为明文，仅驻内存。"""
        ...

    def test_connection(self) -> TestResult:
        """⑩页"测试连接"按钮"""
        ...

    def pull(self, since: Optional[datetime]) -> Iterator[RawRecord]:
        """定时/手动增量拉取。since=None 表示全量。"""
        ...

    def handle_webhook(self, payload: Dict[str, Any]) -> List[RawRecord]:
        """实时推送（可选；不支持则返回空列表）"""
        return []

    def map_entity(self, raw: RawRecord, mapping: Optional[Dict[str, str]] = None) -> CanonicalEntity:
        """字段映射：外部字段 → 规范实体。mapping=server config 字段映射表（可视化编辑，不硬编码）"""
        ...

    def push_outbound(self, event: HubEvent) -> None:
        """出站（可选）。门框期不会被直调——统一入口先过审批门。"""
        ...


class BaseConnector:
    """可选基类：提供契约可选方法的默认实现。
    适配器可继承它只写必要方法（Protocol 的默认实现不会被结构化子类继承）。"""

    name: str = ""
    display_name: str = ""
    category: str = "other"

    def handle_webhook(self, payload: Dict[str, Any]) -> List[RawRecord]:
        return []

    def push_outbound(self, event: HubEvent) -> None:
        pass


# ───────────────────────── 凭证加解密（AES-GCM，链外主密钥） ─────────────────────────

_SECRET_HINTS = ("secret", "token", "password", "passwd", "apikey", "api_key", "private")
_KEY_FILE = os.path.join("integration_keys", "master.key")


def is_secret_field(field_name: str) -> bool:
    f = field_name.lower()
    return any(h in f for h in _SECRET_HINTS)


def _master_key() -> bytes:
    """链外主密钥：./integration_keys/master.key（32B 随机，首次自动生成）。
    不入库、不入审计链；遗失则已存密文不可解（需重新配置连接器）。"""
    if os.path.exists(_KEY_FILE):
        with open(_KEY_FILE, "rb") as f:
            return f.read()
    os.makedirs(os.path.dirname(_KEY_FILE), exist_ok=True)
    key = os.urandom(32)
    with open(_KEY_FILE, "wb") as f:
        f.write(key)
    try:
        os.chmod(_KEY_FILE, 0o600)
    except OSError:
        pass  # Windows 上 chmod 语义有限，目录已尽量收权
    return key


def encrypt_config_secrets(cfg: Dict[str, Any]) -> Dict[str, Any]:
    """配置落库前：密钥字段 → enc:<nonce_hex>:<ct_hex>，其余明文。"""
    from fed_crypto import encrypt_payload
    out = {}
    for k, v in cfg.items():
        if v and isinstance(v, str) and is_secret_field(k) and not v.startswith("enc:"):
            enc = encrypt_payload(_master_key(), v.encode("utf-8"))
            out[k] = f"enc:{enc['nonce']}:{enc['ciphertext']}"
        else:
            out[k] = v
    return out


def decrypt_config_secrets(cfg: Dict[str, Any]) -> Dict[str, Any]:
    """配置出库后：enc: 字段还原明文（仅驻内存，供 configure() 使用）。
    注意：不复用 fed_crypto.decrypt_payload——它带传输信道防重放 nonce 缓存，
    同一密文二次解密会被拒；凭证解密是本地存储场景，直接用 AESGCM。"""
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    out = {}
    for k, v in cfg.items():
        if isinstance(v, str) and v.startswith("enc:"):
            try:
                _, nonce, ct = v.split(":", 2)
                aesgcm = AESGCM(_master_key())
                out[k] = aesgcm.decrypt(bytes.fromhex(nonce), bytes.fromhex(ct), None).decode("utf-8")
            except Exception:
                out[k] = ""  # 解密失败 = 主密钥已轮换 → 视为未配置，fail-closed
        else:
            out[k] = v
    return out


def redact_config(cfg: Dict[str, Any]) -> Dict[str, Any]:
    """出 API 响应前：密钥字段一律掩码（无论明文/密文）。"""
    return {k: ("***" if is_secret_field(k) and v else v) for k, v in cfg.items()}


def config_fingerprint(cfg: Dict[str, Any]) -> str:
    """配置指纹（审计用，不含任何值）"""
    keys = ",".join(sorted(cfg.keys()))
    return hashlib.sha256(keys.encode()).hexdigest()[:12]
