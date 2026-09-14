"""Alembic env — 读取项目 CONFIG.DB_PATH（可用 SYNC_HUB_DB 环境变量覆盖，硬等式验证用空库）。

星枢约定（P2, D5）：
- Alembic 只做结构迁移，手动触发（无启动自动 migrate）
- 硬等式验收：空库 `alembic upgrade head` 的 schema 与现库逐表一致
"""
import os
import sys
from logging.config import fileConfig

from sqlalchemy import engine_from_config
from sqlalchemy import pool

from alembic import context

# 让项目根可导入（读取 models.CONFIG.DB_PATH）
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

# this is the Alembic Config object, which provides
# access to the values within the .ini file in use.
config = context.config

# Interpret the config file for Python logging.
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# 数据库 URL：优先 SYNC_HUB_DB（硬等式验证空库），否则 CONFIG.DB_PATH
db_path = os.environ.get("SYNC_HUB_DB") or ""
if not db_path:
    try:
        from models import CONFIG
        db_path = CONFIG.DB_PATH
    except Exception:
        db_path = "./sync_hub.db"
db_url = "sqlite:///" + os.path.abspath(db_path).replace("\\", "/")
config.set_main_option("sqlalchemy.url", db_url)

target_metadata = None


def run_migrations_offline() -> None:
    """Run migrations in 'offline' mode."""
    url = config.get_main_option("sqlalchemy.url")
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )

    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """Run migrations in 'online' mode."""
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )

    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
        )

        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
