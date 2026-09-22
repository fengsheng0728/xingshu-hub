#!/usr/bin/env python3
"""
星枢 Hub — 打包脚本
构建: python build_hub.py
输出: dist/星枢 Hub.exe（单文件）+ dist/dashboard_dist/（新控制台构建产物）

2026-09-21 打包轮修正两处：
  ① 删掉「把 chroma_db 注入 spec」的旧步骤——那是 2026-09-06 之前的口径，
     锚点注释已被移除（现在 spec 明确写着**不携带 config/ 与 chroma_db/**，
     理由：config 含生产 hub_token、chroma_db 是生产记忆索引，跟包分发=泄露）。
     该步骤自锚点消失后一直是空操作，留着会误导下一个人。
  ② 补「把 hub_ui 的构建产物 dashboard_dist/ 拷到 dist/ 同目录」——
     exe 内不含 dashboard_dist（spec 的 datas 只带 dashboard + examples/showcase，
     该决策见 sync_hub.spec 注释，改它=改打包决策，须在 commit 里说明），
     而新控制台（12 页，含身份供给）要求 exe 同目录存在 dashboard_dist/index.html。
     不拷 = 交付出去的包双击只有旧首页。
"""
import os
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).parent
DIST = ROOT / "dist"
EXE = DIST / "星枢 Hub.exe"


def run(cmd):
    print(f"  > {cmd}")
    subprocess.run(cmd, shell=True, check=True, cwd=str(ROOT))


def main():
    os.chdir(str(ROOT))

    # 1. 清理旧构建
    if EXE.exists():
        EXE.unlink()
    for d in ["build", "__pycache__"]:
        p = ROOT / d
        if p.exists():
            shutil.rmtree(p)

    # 2. PyInstaller 构建（打包内容 = sync_hub.spec 的 datas/hiddenimports，不在此处改 spec）
    print("=== PyInstaller 打包 ===")
    run("pyinstaller sync_hub.spec --noconfirm --clean")

    # 3. 新控制台构建产物：拷到 exe 同目录（exe 内不含它，见文件头 ②）
    ui_src = ROOT / "dashboard_dist"
    ui_dst = DIST / "dashboard_dist"
    if (ui_src / "index.html").is_file():
        if ui_dst.exists():
            shutil.rmtree(ui_dst)
        shutil.copytree(ui_src, ui_dst)
        print(f"=== 已带上新控制台: {ui_dst}（{len(list(ui_dst.rglob('*')))} 项）===")
    else:
        print("[WARN] 未找到 dashboard_dist/index.html —— 打包版将只有旧首页。"
              "先在 hub_ui/ 跑 `npm install && npm run build`（产物落到 ../dashboard_dist）再打包。")

    # 4. 创建启动脚本（用 bytes 写，避免文本模式把 \n 再翻成 \r\n 变成 \r\r\n）
    print("=== 创建启动脚本 ===")
    (DIST / "启动 Hub.bat").write_bytes(
        '@echo off\r\n'
        'title 星枢 Sync Hub\r\n'
        'cd /d "%~dp0"\r\n'
        'echo 星枢 Sync Hub 启动中...\r\n'
        'chcp 65001 >nul\r\n'
        'echo 浏览器访问: http://localhost:3060\r\n'
        'echo.\r\n'
        '"星枢 Hub.exe"\r\n'
        'pause\r\n'.encode("gbk")
    )

    # 5. 确认输出
    if EXE.exists():
        size_mb = EXE.stat().st_size / (1024 * 1024)
        print(f"\n✅ 打包完成: {EXE}")
        print(f"   大小: {size_mb:.0f} MiB")
        print(f"   目录: {DIST}（含 dashboard_dist/ 与 启动 Hub.bat，双击 bat 或 exe 即可）")
    else:
        print("\n❌ 打包失败: exe 未生成")
        sys.exit(1)


if __name__ == "__main__":
    main()
