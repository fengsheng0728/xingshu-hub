#!/usr/bin/env python3
"""
星枢 Hub — 打包脚本
构建: python build_hub.py
输出: dist/星枢 Hub/
"""
import os, sys, shutil, subprocess
from pathlib import Path

ROOT = Path(__file__).parent
DIST = ROOT / "dist" / "星枢 Hub"

def run(cmd):
    print(f"  > {cmd}")
    subprocess.run(cmd, shell=True, check=True, cwd=str(ROOT))

def main():
    os.chdir(str(ROOT))

    # 1. 清理旧构建
    dist_exe = ROOT / "dist" / "星枢 Hub.exe"
    if dist_exe.exists():
        dist_exe.unlink()
    for d in ["build", "__pycache__"]:
        p = ROOT / d
        if p.exists():
            shutil.rmtree(p)

    # 2. 确保 spec 文件中 chroma_db 目录存在
    spec_content = ROOT / "sync_hub.spec"
    chroma_dir = ROOT / "chroma_db"
    if chroma_dir.exists():
        # 更新 spec: 添加 chroma_db 到 datas
        lines = spec_content.read_text(encoding="utf-8").split("\n")
        new_lines = []
        for line in lines:
            new_lines.append(line)
            if "# ChromaDB 数据目录（如果存在）" in line:
                new_lines.append(f"        ('chroma_db', 'chroma_db'),")
        spec_content.write_text("\n".join(new_lines), encoding="utf-8")

    # 3. PyInstaller 构建
    print("=== PyInstaller 打包 ===")
    run("pyinstaller sync_hub.spec --noconfirm --clean")

    # 4. 创建启动脚本
    print("=== 创建启动脚本 ===")
    launch_script = ROOT / "dist" / "启动 Hub.bat"
    launch_script.write_text(
        '@echo off\r\n'
        'title 星枢 Sync Hub\r\n'
        'cd /d "%~dp0"\r\n'
        'echo 星枢 Sync Hub 启动中...\r\n'
        'echo 浏览器访问: http://localhost:3060\r\n'
        'echo.\r\n'
        '"星枢 Hub.exe"\r\n'
        'pause\r\n',
        encoding="gbk"
    )

    # 5. 确认输出
    exe = ROOT / "dist" / "星枢 Hub.exe"
    if exe.exists():
        size_mb = exe.stat().st_size / (1024 * 1024)
        print(f"\n✅ 打包完成: {exe}")
        print(f"   大小: {size_mb:.0f} MB")
        print(f"   启动: 双击 dist/星枢 Hub/星枢 Hub.exe")
    else:
        print("\n❌ 打包失败: exe 未生成")
        sys.exit(1)

if __name__ == "__main__":
    main()
