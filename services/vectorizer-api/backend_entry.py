"""vectorizer-api 独立进程入口脚本。

PyInstaller 打包为单文件 EXE（``vectorizer-backend.exe``）时以本模块作为启动
入口；开发期则由 ``pyproject.toml`` 的 ``vectorizer-api`` 控制台脚本调用
``app.main.run``。两条路径最终都挂载到同一个 FastAPI ``app`` 实例。
"""

from __future__ import annotations

import argparse

import uvicorn

from app.main import app


def parse_args() -> argparse.Namespace:
    """解析命令行启动参数。

    默认值刻意与 ``app.main.run`` 保持一致（``127.0.0.1:8000``），
    使「直接运行本脚本」与「控制台脚本启动」两种方式行为等价。

    Returns:
        含 ``host`` / ``port`` / ``log_level`` 三个字段的参数命名空间。
    """
    parser = argparse.ArgumentParser(description="Vectorizer API backend launcher")
    parser.add_argument("--host", default="127.0.0.1", help="Bind host, default: 127.0.0.1")
    parser.add_argument("--port", type=int, default=8000, help="Bind port, default: 8000")
    parser.add_argument("--log-level", default="info", help="Uvicorn log level")
    return parser.parse_args()


def main() -> None:
    """解析参数并启动 Uvicorn 服务（阻塞直至进程退出）。"""
    args = parse_args()
    # 固定关闭 reload：打包后的 EXE 无源码热重载需求，开启反而会额外派生监视子进程
    uvicorn.run(app, host=args.host, port=args.port, log_level=args.log_level)


if __name__ == "__main__":
    main()

