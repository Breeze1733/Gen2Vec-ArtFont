"""Entry point used by PyInstaller when building ``txt2img-backend.exe``.

Mirrors ``services/vectorizer-api/backend_entry.py``: pass the FastAPI
``app`` object directly to ``uvicorn.run`` so PyInstaller can statically
follow the reference and bundle the whole ``app`` package — no string-based
import lookup, no risk of missing ``--hidden-import`` flags.

Usage from source::

    python txt2img_entry.py --host 127.0.0.1 --port 9001

Usage as a frozen binary (Windows)::

    txt2img-backend.exe --host 127.0.0.1 --port 9001
"""
from __future__ import annotations

import argparse

import uvicorn

from app.main import app


def parse_args() -> argparse.Namespace:
    """解析命令行启动参数。

    默认绑定 ``0.0.0.0:9001``，与 ``app.main.run`` 保持一致；桌面端以本机
    回环地址访问，绑定通配地址不影响本地使用，同时便于同机其它工具调试。

    Returns:
        含 ``host`` / ``port`` / ``log_level`` 三个字段的参数命名空间。
    """
    parser = argparse.ArgumentParser(description="txt2img API backend launcher")
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=9001)
    parser.add_argument("--log-level", default="info")
    return parser.parse_args()


def main() -> None:
    """解析参数并启动 Uvicorn 服务（阻塞直至进程退出）。"""
    args = parse_args()
    # 固定关闭 reload：打包后的 EXE 无源码热重载需求，开启反而会额外派生监视子进程
    uvicorn.run(app, host=args.host, port=args.port, log_level=args.log_level)


if __name__ == "__main__":
    main()
