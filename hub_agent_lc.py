"""
星枢 Sync Hub — Hub Agent (LangChain)
店长的 AI 助手，通过聊天窗口对话，直接操作系统。
"""
import asyncio
import json, sqlite3
from datetime import datetime, timezone
from typing import Optional
from langchain_openai import ChatOpenAI
from langchain_core.tools import tool
from langchain_core.messages import HumanMessage, AIMessage, SystemMessage
from langchain_core.chat_history import BaseChatMessageHistory
from langchain_core.prompts import ChatPromptTemplate, MessagesPlaceholder

# ============ Memory ============

class SQLiteChatHistory(BaseChatMessageHistory):
    """SQLite 持久化对话历史"""

    def __init__(self, session_id: str, db_path: str):
        self.session_id = session_id
        self.db_path = db_path
        self._ensure_table()

    def _ensure_table(self):
        conn = sqlite3.connect(self.db_path)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS hub_agent_conversations (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id TEXT NOT NULL,
                role TEXT NOT NULL,
                content TEXT NOT NULL,
                created_at TEXT NOT NULL
            )
        """)
        conn.commit()
        conn.close()

    @property
    def messages(self):
        from langchain_core.messages import HumanMessage, AIMessage
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT role, content FROM hub_agent_conversations WHERE session_id = ? ORDER BY id ASC",
            (self.session_id,)
        ).fetchall()
        conn.close()
        msgs = []
        for r in rows:
            if r["role"] == "user":
                msgs.append(HumanMessage(content=r["content"]))
            elif r["role"] == "assistant":
                msgs.append(AIMessage(content=r["content"]))
        return msgs

    def add_messages(self, messages):
        conn = sqlite3.connect(self.db_path)
        now = datetime.now(timezone.utc).isoformat()
        for msg in messages:
            role = "user" if msg.type == "human" else "assistant"
            conn.execute(
                "INSERT INTO hub_agent_conversations (session_id, role, content, created_at) VALUES (?, ?, ?, ?)",
                (self.session_id, role, msg.content, now),
            )
        conn.commit()
        conn.close()

    def clear(self):
        conn = sqlite3.connect(self.db_path)
        conn.execute("DELETE FROM hub_agent_conversations WHERE session_id = ?", (self.session_id,))
        conn.commit()
        conn.close()


# ============ Tools ============

class HubTools:
    """LangChain Tools — 每个 Tool 直接调 Hub 内部方法"""

    def __init__(self, hub, db_path: str):
        self.hub = hub
        self.db_path = db_path

    def _get_db(self):
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def _run_async(self, coro):
        """安全运行异步协程，兼容已有事件循环的环境"""
        try:
            return asyncio.run(coro)
        except RuntimeError:
            try:
                loop = asyncio.get_event_loop()
            except RuntimeError:
                loop = asyncio.new_event_loop()
                asyncio.set_event_loop(loop)
            return loop.run_until_complete(coro)

    @tool
    def query_memories(self, query: str) -> str:
        """搜索记忆池。输入：关键词或 Agent ID。返回：匹配的记忆列表。示例：'色差' 或 'cs-wang'"""
        conn = self._get_db()
        c = conn.cursor()
        if query in self.hub.agents:
            c.execute("SELECT content, tags, owner_agent_id FROM memory_pool WHERE owner_agent_id = ? ORDER BY created_at DESC LIMIT 10", (query,))
        else:
            c.execute("SELECT content, tags, owner_agent_id FROM memory_pool WHERE tags LIKE ? OR content LIKE ? ORDER BY created_at DESC LIMIT 10", (f"%{query}%", f"%{query}%"))
        rows = c.fetchall()
        conn.close()
        if not rows:
            return "没有找到相关记忆。"
        result = []
        for r in rows:
            tags = json.loads(r["tags"]) if r["tags"] else []
            result.append(f"[{r['owner_agent_id']}] {r['content'][:100]} {'🏷'+','.join(tags) if tags else ''}")
        return "\n".join(result)

    @tool
    def query_knowledge(self, query: str) -> str:
        """搜索企业知识库。输入：关键词。返回：匹配的知识条目。"""
        conn = self._get_db()
        c = conn.cursor()
        c.execute("SELECT title, content, category, tags FROM knowledge_base WHERE title LIKE ? OR content LIKE ? OR tags LIKE ? LIMIT 5", (f"%{query}%", f"%{query}%", f"%{query}%"))
        rows = c.fetchall()
        conn.close()
        if not rows:
            return "知识库中没有相关内容。"
        result = []
        for r in rows:
            result.append(f"📚 {r['title']} [{r['category']}]\n{r['content'][:200]}")
        return "\n\n".join(result)

    @tool
    def list_agents(self, _: str = "") -> str:
        """列出所有 Agent 及其在线状态。"""
        agents = self.hub.agents
        if not agents:
            return "没有注册的 Agent。"
        online = sum(1 for a in agents.values() if a.get("status") == "online")
        total = len(agents)
        result = [f"在线 {online}/{total}"]
        for aid, info in agents.items():
            status = "🟢" if info.get("status") == "online" else "🔴"
            result.append(f"  {status} {info.get('agent_name', aid)} ({info.get('role', 'worker')}) - {info.get('department', '')}")
        return "\n".join(result)

    @tool
    def list_tasks(self, status_filter: str = "") -> str:
        """列出任务。输入：状态过滤（pending/assigned/in_progress/completed/failed）或留空查看全部。"""
        conn = self._get_db()
        c = conn.cursor()
        if status_filter and status_filter in ("pending", "assigned", "in_progress", "completed", "failed", "cancelled"):
            c.execute("SELECT task_id, description, status, assigned_agent_id, creator_agent_id FROM tasks WHERE status = ? ORDER BY updated_at DESC LIMIT 15", (status_filter,))
        else:
            c.execute("SELECT task_id, description, status, assigned_agent_id, creator_agent_id FROM tasks ORDER BY updated_at DESC LIMIT 15")
        rows = c.fetchall()
        conn.close()
        if not rows:
            return "没有任务。"
        status_cn = {"pending": "待调度", "assigned": "已分配", "in_progress": "进行中", "completed": "✅完成", "failed": "❌失败", "cancelled": "已取消"}
        result = []
        for r in rows:
            result.append(f"  [{status_cn.get(r['status'], r['status'])}] {r['description'][:60]} → {r['assigned_agent_id'] or '未分配'}")
        return f"共 {len(rows)} 个任务：\n" + "\n".join(result)

    @tool
    def approve_disclosure(self, request_id: str) -> str:
        """批准披露升级请求。输入：request_id。"""
        result = self._run_async(self.hub.approve_disclosure_request(request_id, "hub-agent"))
        if result.get("status") == "approved":
            return f"✅ 已批准披露升级: {result.get('task_id')}"
        return f"❌ 批准失败: {result.get('error', '未知错误')}"

    @tool
    def deny_disclosure(self, request_id: str) -> str:
        """拒绝披露升级请求。输入：request_id。"""
        result = self._run_async(self.hub.deny_disclosure_request(request_id, "hub-agent"))
        if result.get("status") == "denied":
            return f"✕ 已拒绝: {result.get('task_id')}"
        return f"❌ 操作失败: {result.get('error', '未知错误')}"

    @tool
    def create_task(self, description: str) -> str:
        """创建新任务。输入：任务描述。"""
        import hashlib
        task_id = "task-hubagent-" + hashlib.sha256(description.encode()).hexdigest()[:12]
        from models import TaskCreate
        self._run_async(self.hub.create_task(TaskCreate(
            task_id=task_id, description=description,
            creator_agent_id="hub-agent", priority=1
        )))
        return f"✅ 任务已创建: {task_id}\n描述: {description}"

    @tool
    def get_stats(self, _: str = "") -> str:
        """获取系统统计：今日任务数、记忆数、在线人数。"""
        conn = self._get_db()
        c = conn.cursor()
        c.execute("SELECT COUNT(*) FROM tasks WHERE status IN ('in_progress','assigned')")
        active_tasks = c.fetchone()[0]
        c.execute("SELECT COUNT(*) FROM tasks WHERE status = 'completed'")
        done = c.fetchone()[0]
        c.execute("SELECT COUNT(*) FROM memory_pool")
        mems = c.fetchone()[0]
        c.execute("SELECT COUNT(*) FROM knowledge_base")
        kb = c.fetchone()[0]
        conn.close()
        online = sum(1 for a in self.hub.agents.values() if a.get("status") == "online")
        total = len(self.hub.agents)
        return f"📊 今日概况：\n  进行中任务: {active_tasks}\n  已完成: {done}\n  记忆池: {mems} 条\n  知识库: {kb} 条\n  在线 Agent: {online}/{total}"

    @tool
    def ingest_to_wiki(self, title: str, content: str, category: str = "concepts", tags: str = "") -> str:
        """将知识写入 Wiki 知识库。Wiki 沉淀长期文档（政策/流程/最佳实践），知识库存关系图谱节点。
        category: entity(实体/人物/产品) | concept(概念/政策/流程) | comparison(对比分析) | query(问答存档)
        路径经安全校验，仅允许写入 wiki/ 目录内。"""
        import urllib.request, json as _json
        try:
            req = urllib.request.Request(
                f"http://127.0.0.1:3060/api/v1/wiki/ingest",
                data=_json.dumps({
                    "title": title, "content": content, "category": category,
                    "tags": [t.strip() for t in tags.split(",") if t.strip()],
                }).encode(),
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=10) as resp:
                result = _json.loads(resp.read())
            if result.get("status") == "created":
                return f"✅ 已写入 Wiki: [{category}] {title} → {result.get('url', '')}"
            return f"❌ 写入失败: {result.get('error', 'unknown')}"
        except Exception as e:
            return f"❌ Wiki 写入异常: {str(e)[:100]}"


# ============ Agent Chain ============

# SYSTEM_PROMPT 在 create_hub_agent 中动态生成（含公司名）
_DEFAULT_SYSTEM_PROMPT = """你是星枢 Sync Hub 的 AI 助手，服务于{company_name}店长。

你能做什么：
- 📋 查任务进度、分配任务
- 👥 看谁在线、谁在忙
- 🧠 搜索记忆池和知识库
- ✅ 审批和拒绝披露升级请求
- 📊 查看系统统计数据

回复原则：
1. 简洁 — 给具体数字和事实，不说废话
2. 主动 — 发现问题直接说，不用等老板问第二遍
3. 诚实 — 查不到就说查不到，不编造信息
4. 操作有二次确认 — 审批/拒绝前先确认

你是店长的左膀右臂，不是一个通用聊天机器人。"""

MEMORY_KEY = "chat_history"


def create_hub_agent(llm: ChatOpenAI, tools: list, db_path: str, company_name: str = "企业"):
    """创建 LangChain Hub Agent — 返回原始链 + tool map，历史由调用方管理"""
    system_prompt = _DEFAULT_SYSTEM_PROMPT.format(company_name=company_name)
    prompt = ChatPromptTemplate.from_messages([
        ("system", system_prompt),
        MessagesPlaceholder(variable_name=MEMORY_KEY),
        ("human", "{input}"),
    ])

    llm_with_tools = llm.bind_tools(tools)
    chain = prompt | llm_with_tools
    return chain, {t.name: t for t in tools} if tools else {}
