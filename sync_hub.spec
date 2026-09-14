# -*- mode: python ; coding: utf-8 -*-
# 星枢 Sync Hub — PyInstaller 打包配置
# 构建: pyinstaller sync_hub.spec
# 输出: dist/星枢 Hub/星枢 Hub.exe

import os
from PyInstaller.utils.hooks import collect_data_files, collect_submodules

HIDDEN = [
    'fastapi', 'uvicorn', 'uvicorn.loops', 'uvicorn.protocols',
    'aiohttp', 'websockets',
    'langchain', 'langchain_core', 'langchain_openai', 'langchain.agents',
    'sklearn', 'sklearn.feature_extraction.text',
    'sentence_transformers',
    'numpy', 'httpx', 'yaml', 'pydantic', 'requests',
    'sqlite3', 'hashlib', 'json', 'secrets',
]
# chromadb 1.5.9 惰性导入 telemetry.posthog 等(2026-09-06 实测漏收集→ChromaDB 初始化失败→
# 打包版静默降级, 语义搜索不可用); collect_submodules 全量收集防同类遗漏
HIDDEN += collect_submodules('chromadb')

a = Analysis(
    ['main.py'],
    pathex=[],
    binaries=[],
    datas=[
        # Dashboard 和静态资源（showcase 属 examples/ 演示展示资产，D-6 出仓后仍在包内）
        ('dashboard', 'dashboard'),
        ('examples/showcase', 'examples/showcase'),
        # ⚠️ 2026-09-06: 不再携带 config/ 与 chroma_db/——
        #   ① config.yaml 含生产 hub_token/注册语义, 打进包=部署凭据可被解包提取;
        #   ② chroma_db 是生产记忆索引, 跟包分发=隐私泄露。
        #   exe 运行时 config_dir 默认 ./config(相对 cwd, 用户自备或 SYNC_HUB_CONFIG_DIR
        #   覆盖), 缺失时 Config 用内置默认值(open 注册); chroma 缺失走 CD-016 降级/自动重建。
    ],
    hiddenimports=HIDDEN,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        'tkinter', 'matplotlib', 'pandas', 'PIL', 'curses',
        'IPython', 'jupyter', 'notebook',
        'torch', 'tensorflow',
    ],
    noarchive=False,
    optimize=0,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name='星枢 Hub',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=True,  # 显示控制台窗口，方便查看日志
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
