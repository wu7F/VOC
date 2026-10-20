# -*- coding: utf-8 -*-
"""VOC洞察平台 · 打包为独立 exe

    python build_exe.py

产物：dist/VOC洞察平台/VOC洞察平台.exe
运行时不需要装 Python；数据默认落在 exe 同级的 VOCData/。
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
NAME = "VOC洞察平台"


def main() -> int:
    try:
        import PyInstaller  # noqa: F401
    except ImportError:
        print("缺少 PyInstaller，请先安装：pip install pyinstaller")
        return 1

    sep = ";" if sys.platform == "win32" else ":"
    cmd = [
        sys.executable, "-m", "PyInstaller",
        "--noconfirm", "--clean",
        "--name", NAME,
        "--console",                      # 需要看日志、Ctrl+C 停止
        "--onedir",                       # onedir 启动快、报错好排查，体积可接受
        # 前端静态资源（含 1MB 的 echarts）打进包内
        "--add-data", f"{ROOT / 'web'}{sep}web",
        # uvicorn 的部分子模块是动态导入的，必须显式声明
        "--hidden-import", "uvicorn.logging",
        "--hidden-import", "uvicorn.loops.auto",
        "--hidden-import", "uvicorn.protocols.http.auto",
        "--hidden-import", "uvicorn.protocols.websockets.auto",
        "--hidden-import", "uvicorn.lifespan.on",
        "--collect-all", "pandas",
        "--collect-all", "openpyxl",
        # 排除用不到的重家伙伙，显著减小体积
        "--exclude-module", "matplotlib",
        "--exclude-module", "scipy",
        "--exclude-module", "notebook",
        "--exclude-module", "IPython",
        "--exclude-module", "pytest",
        str(ROOT / "web" / "serve.py"),
    ]

    print("打包中（约 1~3 分钟）…\n")
    r = subprocess.run(cmd, cwd=str(ROOT))
    if r.returncode != 0:
        return r.returncode

    dist = ROOT / "dist" / NAME
    print("\n完成。产物：")
    print(f"  {dist}")
    print("\n使用：")
    print(f'  set VOC_PASSWORD=你的管理员口令        (必设，否则远程只读)')
    print(f'  set VOC_GUEST_PASSWORD=给客户看的口令   (可选)')
    print(f'  "{dist / (NAME + ".exe")}"')
    print("\n数据默认落在 exe 同级的 VOCData/，整个目录一起拷走即可迁移。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())