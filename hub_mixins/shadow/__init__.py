# -*- coding: utf-8 -*-
"""hub_mixins/shadow/ —— 包化拆分（3-2c），外部引用 hub_mixins.shadow.X 语义不变。

原 hub_mixins/shadow.py（1395 行）拆为 common/lifecycle/pending/write/merge/writer；
本模块为兼容出口：显式具名 re-export 原模块全部公开名与下划线名。
"""
import collections  # noqa: F401  以下 stdlib 名在原模块 dir() 面内，保留兼容
import datetime  # noqa: F401
import json  # noqa: F401
import logging  # noqa: F401
import os  # noqa: F401
import re  # noqa: F401
import sqlite3  # noqa: F401
import threading  # noqa: F401
import time  # noqa: F401

from .common import (
    _BATCH_INTERVAL,
    _BATCH_SIZE,
    _INDEX_KINDS,
    _MERGE_MIN_AGE_SEC,
    _PEND_DDL,
    _PENDING_MAX_ATTEMPTS,
    _WATCHDOG_INTERVAL,
    _drop_front_matter_field,
    _front_matter,
    _index_rows,
    _index_state,
    _origins_from_trunk,
    _read_worktree,
    _safe_name,
    _strip_front_matter,
    _today,
    _ts_epoch,
    _with_front_matter_field,
    append_index_correction,
    collect_origins,
    logger,
)
from .writer import ShadowWriter
