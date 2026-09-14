"""统一依赖出口（D-7 / 3-2a）。

本模块是 ``models`` / ``db`` 两个叶子模块对全仓的**显式具名 re-export 出口**。
规则：

- **禁止通配 import**：全仓 ``from models import *`` / ``from db import *`` 已清零，
  ruff 门禁 F403 零容忍（见 ruff.toml，D-7/3-2a 加入）。消费方一律
  ``from deps import <名字>``（或按需直接从 ``models``/``db`` 具名导入）。
- **为什么放在仓库根、而不是 ``routes_common`` 下**：``routes_common.py`` 属
  路由层（import 了 ``auth_provider``）；而 ``hub_mixins/**``、``hub_core.py``
  是核心层。若依赖出口挂在 ``routes_common`` 下，核心层就要反向依赖路由层
  （连带执行 ``routes_common`` → 拉 auth_provider），制造分层倒置。
  ``deps.py`` 只依赖 ``models``/``db`` 两个叶子模块，对任何层都是"向下依赖"。
- **D-10（3-1a db 门面）将以本模块为接缝**：届时 db 访问收口只改这一处 +
  热点路径。
"""

from models import (
    CONFIG,
    PUBLIC_DOMAIN,
    TASK_TRANSITIONS,
    AgentRegistration,
    DisclosureLevel,
    DisclosureRequest,
    DisclosureRules,
    DisclosureScope,
    HubAgentConfig,
    KnowledgeEntry,
    MemoryBatchOp,
    MemoryEntry,
    SemanticSearchRequest,
    SessionArchiveRequest,
    SessionHandoffRequest,
    TaskCreate,
    TaskStatus,
)

from db import (
    SCHEMA_VERSION,
    logger,
    check_windows_firewall,
    get_embedding_provider,
    get_lan_ips,
    init_db,
    row_to_dict,
)
