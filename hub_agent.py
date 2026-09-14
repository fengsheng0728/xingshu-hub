"""
星枢 Sync Hub — Hub Agent（LLM 驱动）
"""
import logging
logger = logging.getLogger("xingshu.hub_agent")

import json
import os
import re
import sqlite3
from datetime import datetime, timezone
import httpx
from pydantic import BaseModel


from deps import DisclosureRules, HubAgentConfig
from db import logger

# ============ 企业配置辅助 ============

def _get_company_name() -> str:
    """从 config.yaml 读取公司名称，默认 '企业'"""
    try:
        import yaml, os
        config_dir = os.environ.get("SYNC_HUB_CONFIG_DIR", "./config")
        with open(os.path.join(config_dir, "config.yaml"), "r", encoding="utf-8") as f:
            cfg = yaml.safe_load(f) or {}
        return cfg.get("business", {}).get("company_name", "企业")
    except (FileNotFoundError, yaml.YAMLError, KeyError) as e:
        logger.debug(f"读取公司名失败 ({type(e).__name__})，使用默认值")
        return "企业"


# ============ Hub Agent（LLM 驱动的披露审计引擎） ==========



class HubAgent:
    """
    Hub 内部 LLM Agent — 根据配置的披露规则，自动审计披露升级请求。

    职责：
      - 存储 LLM provider 配置（provider/key/model/api_base）
      - 存储披露审计规则（自然语言）
      - 接收披露请求 → 调 LLM → 返回审核建议（approve/deny/need_info）
      - 可选自动批准（auto_approve=True 时 LLM approve 则直接执行）
    """

    DEFAULT_RULES = """你是星枢 Hub 的披露审计 Agent，服务于{company_name}客服团队。
你的职责：审查客服 Agent 发起的披露升级请求，判断是否应该批准查看更多客户信息。

业务背景：
- 客服团队处理公司产品/服务的售前咨询和售后投诉
- 客户信息包括：购买记录、历史投诉、沟通记录、个人联系方式
- 披露升级意味着客服可以查看更完整的客户档案

判断原则：
1. 理由涉及「需要了解客户历史」「查看过往沟通记录」「确认之前处理方案」→ 批准
2. 理由涉及「客户投诉升级」「涉及赔偿/退款」「需要完整信息才能处理」→ 批准
3. 理由模糊如「不够」「再看看」「多了解」→ 拒绝（要求补充具体理由）
4. 理由与任务明显无关 → 拒绝
5. 其他无法判断的 → need_info（转人工）

返回严格 JSON（不要其他内容）：
{"decision": "approve|deny|need_info", "reason": "一句话理由（中文）", "risk_level": "low|medium|high"}"""

    KNOWLEDGE_SYSTEM_PROMPT = """你是{company_name}的企业知识库编辑助手。根据标题生成专业的知识条目。

企业背景：{company_name}是一家公司，需要知识库来统一回答标准和政策。

返回严格 JSON（不要其他内容）：
{
  "title": "原标题",
  "content": "详细内容（2-3段，专业简洁，适合客服查阅）",
  "category": "product|policy|faq|process|general",
  "tags": ["标签1", "标签2", "标签3"],
  "importance": 0.5-1.0
}

分类规则：
- product: 产品规格、型号、材质、安装要求、使用说明、常见问题
- policy: 公司政策、售后规则、退换货流程、保修条款、赔偿标准
- faq: 客户常见问题及标准应答
- process: 工作流程、操作步骤、工单处理规范
- general: 其他通用知识

内容要求：
- 面向客服人员，语言简洁易懂
- 包含具体数据和操作指引，不要空话
- 与卫浴行业相关"""

    def __init__(self, db_path: str):
        self.db_path = db_path
        self._auto_configure_from_env()

    def _auto_configure_from_env(self):
        """从环境变量自动配置 LLM（启动即用，无需 Dashboard 手动配置）"""
        api_key = os.environ.get("SYNC_HUB_LLM_API_KEY", "").strip()
        if not api_key:
            return

        existing = self._get_config()
        if existing.get("api_key"):
            return  # 已有配置，不覆盖

        provider = os.environ.get("SYNC_HUB_LLM_PROVIDER", "deepseek").strip()
        model = os.environ.get("SYNC_HUB_LLM_MODEL", "deepseek-chat").strip()
        base_url = os.environ.get("SYNC_HUB_LLM_BASE_URL", "").strip()

        self._set_config({
            "provider": provider,
            "api_key": api_key,
            "api_base": base_url,
            "model": model,
            "temperature": "0.3",
            "enabled": "true",
            "auto_approve": "false",
        })
        logger.info(f"Hub Agent 从环境变量自动配置: provider={provider} model={model}")

    def _get_db(self):
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def _get_config(self) -> dict:
        """获取当前 Hub Agent 配置"""
        conn = self._get_db()
        c = conn.cursor()
        c.execute("SELECT key, value FROM hub_agent_config")
        config = {row["key"]: row["value"] for row in c.fetchall()}
        conn.close()
        return config

    def _set_config(self, updates: dict):
        """批量更新配置"""
        conn = self._get_db()
        c = conn.cursor()
        now = datetime.now(timezone.utc).isoformat()
        for k, v in updates.items():
            c.execute(
                "INSERT OR REPLACE INTO hub_agent_config (key, value, updated_at) VALUES (?, ?, ?)",
                (k, str(v), now),
            )
        conn.commit()
        conn.close()

    def configure(self, cfg: HubAgentConfig) -> dict:
        """配置 LLM provider"""
        self._set_config({
            "provider": cfg.provider,
            "api_key": cfg.api_key,
            "api_base": cfg.api_base,
            "model": cfg.model,
            "temperature": str(cfg.temperature),
            "enabled": str(cfg.enabled).lower(),
            "auto_approve": str(cfg.auto_approve).lower(),
        })
        if hasattr(self, "_lc_chain"):
            del self._lc_chain
        if hasattr(self, "_tool_map"):
            del self._tool_map
        return {"status": "ok", "message": "Hub Agent 配置已更新"}

    def set_disclosure_rules(self, rules: DisclosureRules) -> dict:
        """设置披露审计规则"""
        self._set_config({"disclosure_rules": rules.rules})
        return {"status": "ok", "message": "披露规则已更新"}

    def get_config(self) -> dict:
        """获取当前完整配置（api_key 脱敏）"""
        cfg = self._get_config()
        if cfg.get("api_key"):
            cfg["api_key"] = cfg["api_key"][:8] + "..." + cfg["api_key"][-4:] if len(cfg["api_key"]) > 12 else "***"
        return cfg

    def is_configured(self) -> bool:
        cfg = self._get_config()
        return bool(cfg.get("enabled") == "true" and cfg.get("api_key") and cfg.get("model"))

    async def test_connection(self) -> dict:
        """测试 LLM 连接"""
        cfg = self._get_config()
        if not cfg.get("api_key"):
            return {"status": "error", "error": "未配置 API Key"}

        try:
            async with httpx.AsyncClient(timeout=15.0) as client:
                base = cfg.get("api_base") or self._default_base(cfg.get("provider", "openai"))
                headers = {
                    "Authorization": f"Bearer {cfg['api_key']}",
                    "Content-Type": "application/json",
                }
                payload = {
                    "model": cfg.get("model", "gpt-4o-mini"),
                    "messages": [{"role": "user", "content": "ping"}],
                    "max_tokens": 5,
                }
                resp = await client.post(f"{base}/chat/completions", json=payload, headers=headers)
                if resp.status_code == 200:
                    data = resp.json()
                    return {"status": "ok", "model": cfg["model"], "provider": cfg.get("provider"),
                            "response": data["choices"][0]["message"]["content"] if data.get("choices") else "(empty)"}
                else:
                    return {"status": "error", "error": f"HTTP {resp.status_code}: {resp.text[:200]}"}
        except Exception as e:
            return {"status": "error", "error": str(e)}

    async def audit_disclosure(self, task_id: str, agent_id: str, reason: str, task_desc: str = "") -> dict:
        """
        审计披露升级请求。

        返回: {"decision": "approve|deny|need_info", "reason": "...", "risk_level": "low|medium|high"}
        """
        cfg = self._get_config()
        if not self.is_configured():
            return {"decision": "need_info", "reason": "Hub Agent 未配置或未启用", "risk_level": "unknown"}

        rules = cfg.get("disclosure_rules", self.DEFAULT_RULES.format(company_name=_get_company_name()))

        user_prompt = f"""披露升级请求：
- 任务ID: {task_id}
- 任务描述: {task_desc or '(未提供)'}
- 申请人: {agent_id}
- 申请理由: {reason}

请根据披露规则判断是否批准此请求。"""

        try:
            async with httpx.AsyncClient(timeout=30.0) as client:
                base = cfg.get("api_base") or self._default_base(cfg.get("provider", "openai"))
                headers = {
                    "Authorization": f"Bearer {cfg['api_key']}",
                    "Content-Type": "application/json",
                }
                payload = {
                    "model": cfg.get("model", "gpt-4o-mini"),
                    "messages": [
                        {"role": "system", "content": rules},
                        {"role": "user", "content": user_prompt},
                    ],
                    "temperature": float(cfg.get("temperature", "0.3")),
                    "max_tokens": 200,
                }
                resp = await client.post(f"{base}/chat/completions", json=payload, headers=headers)
                if resp.status_code == 200:
                    data = resp.json()
                    raw = data["choices"][0]["message"]["content"]
                    # 尝试解析 JSON
                    import re
                    match = re.search(r'\{[^}]+\}', raw)
                    if match:
                        result = json.loads(match.group())
                        return {
                            "decision": result.get("decision", "need_info"),
                            "reason": result.get("reason", raw),
                            "risk_level": result.get("risk_level", "medium"),
                            "raw": raw,
                        }
                    return {"decision": "need_info", "reason": raw, "risk_level": "medium", "raw": raw}
                else:
                    return {"decision": "need_info", "reason": f"LLM 调用失败 HTTP {resp.status_code}", "risk_level": "unknown"}
        except Exception as e:
            return {"decision": "need_info", "reason": f"LLM 调用异常: {e}", "risk_level": "unknown"}

    async def auto_complete_knowledge(self, title: str) -> dict:
        """用 LLM 自动补全知识条目：标题 → 内容/分类/标签"""
        cfg = self._get_config()
        if not self.is_configured():
            return {"status": "error", "error": "Hub Agent 未配置 LLM"}

        system_prompt = self.KNOWLEDGE_SYSTEM_PROMPT.format(company_name=_get_company_name())

        try:
            async with httpx.AsyncClient(timeout=30.0) as client:
                base = cfg.get("api_base") or self._default_base(cfg.get("provider", "openai"))
                headers = {"Authorization": f"Bearer {cfg['api_key']}", "Content-Type": "application/json"}
                payload = {
                    "model": cfg.get("model", "gpt-4o-mini"),
                    "messages": [
                        {"role": "system", "content": system_prompt},
                        {"role": "user", "content": f"标题：{title}"},
                    ],
                    "temperature": 0.5, "max_tokens": 600,
                }
                resp = await client.post(f"{base}/chat/completions", json=payload, headers=headers)
                if resp.status_code == 200:
                    data = resp.json()
                    raw = data["choices"][0]["message"]["content"]
                    import re
                    match = re.search(r'\{[^{}]*\}(?:\s*\{[^{}]*\})*', raw.replace('\n', ' '))
                    if match:
                        result = json.loads(match.group())
                        return {"status": "ok", "entry": result}
                    return {"status": "ok", "entry": {"title": title, "content": raw, "category": "general", "tags": [], "importance": 0.5}}
                return {"status": "error", "error": f"LLM 错误 HTTP {resp.status_code}"}
        except Exception as e:
            return {"status": "error", "error": str(e)}

    async def extract_knowledge_from_memories(self, limit: int = 10) -> dict:
        """从记忆池中提取高频主题，建议生成知识条目"""
        conn = self._get_db()
        c = conn.cursor()

        # 统计高频标签（json.loads 解码 Unicode 转义）
        c.execute("SELECT tags, content, owner_agent_id FROM memory_pool WHERE tags != '' AND tags IS NOT NULL")
        tag_counts = {}
        tag_samples = {}  # tag -> {content, agent_id}
        for row in c.fetchall():
            try:
                tags = json.loads(row["tags"])
                for tag in tags:
                    tag_counts[tag] = tag_counts.get(tag, 0) + 1
                    if tag not in tag_samples:
                        tag_samples[tag] = {
                            "content": row["content"][:200] if row["content"] else "",
                            "owner_agent_id": row["owner_agent_id"],
                        }
            except Exception as _exc:
                logger.debug("hub_agent silent-except @316: %s", _exc)

        # 取 TOP tags
        top_tags = sorted(tag_counts.items(), key=lambda x: x[1], reverse=True)[:limit]

        suggestions = []
        for tag, count in top_tags:
            sample = tag_samples.get(tag, {})
            suggestions.append({
                "title": f"{tag}相关",
                "suggested_content": sample.get("content", ""),
                "suggested_category": "faq" if count > 5 else "general",
                "suggested_tags": [tag],
                "importance": min(1.0, count / 10),
                "frequency": count,
                "source_agent": sample.get("owner_agent_id", ""),
            })

        conn.close()
        return {"status": "ok", "suggestions": suggestions}

    def _default_base(self, provider: str) -> str:
        """获取各 provider 的默认 API base URL"""
        bases = {
            "openai": "https://api.openai.com/v1",
            "deepseek": "https://api.deepseek.com/v1",
            "qianwen": "https://dashscope.aliyuncs.com/compatible-mode/v1",
            "zhipu": "https://open.bigmodel.cn/api/paas/v4",
            "minimax": "https://api.minimax.chat/v1",
        }
        return bases.get(provider, "https://api.openai.com/v1")