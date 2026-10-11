"""txt2img-api HTTP 服务入口（FastAPI 应用装配与 ComfyUI 生命周期管理）。

对外暴露三个端点：

- ``GET  /healthz``：桌面端启动自检用的健康探针；
- ``POST /shutdown``：Electron / 启动管理器退出前调用的优雅关闭接口；
- ``POST /api/v1/txt2img``：核心业务端点，委托 ``generator.generate_artwork`` 生成位图。

此外还负责在启动时（可选）自动拉起随包分发的 ComfyUI 便携版子进程，并注册
``atexit`` 与信号处理器，确保进程退出时一并回收 ComfyUI。
"""

from __future__ import annotations

import atexit
import configparser
import logging
import os
import shutil
import signal
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path

import uvicorn
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from .generator import generate_artwork, warmup_comfyui_connection
from .models import GenerationRequest, GenerationResponse

logger = logging.getLogger(__name__)

# 存储 ComfyUI 子进程引用，用于 Ctrl+C 时自动清理
_comfyui_proc: subprocess.Popen | None = None


def _cleanup_comfyui() -> None:
    """退出时强制终止 ComfyUI 子进程树。

    幂等操作：未启动或已自行退出的进程直接跳过。由 ``atexit`` 与信号处理器
    共同触发，确保无论正常退出还是被中断都能回收 ComfyUI。
    """
    global _comfyui_proc
    # 未启动或已自行退出则无需处理
    if _comfyui_proc is None or _comfyui_proc.poll() is not None:
        return
    logger.info("正在关闭 ComfyUI (pid %d)...", _comfyui_proc.pid)
    if sys.platform == "win32":
        # Windows 上 ComfyUI 会派生多层子进程，须用 /T 连同进程树一并终止
        subprocess.run(
            ["taskkill", "/F", "/T", "/PID", str(_comfyui_proc.pid)],
            capture_output=True,
        )
    else:
        _comfyui_proc.terminate()
        try:
            _comfyui_proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            _comfyui_proc.kill()
            _comfyui_proc.wait(timeout=3)
    logger.info("ComfyUI 已关闭")


atexit.register(_cleanup_comfyui)

# Ctrl+C 时先清理再退出（Windows 上 atexit 也生效，这里是双保险）
def _signal_handler(signum: int, frame: object) -> None:
    """收到 SIGINT / SIGTERM 时先回收 ComfyUI 子进程再退出。"""
    _cleanup_comfyui()
    sys.exit(0)


signal.signal(signal.SIGINT, _signal_handler)
if hasattr(signal, "SIGTERM"):
    signal.signal(signal.SIGTERM, _signal_handler)


# ComfyUI portable bundle lives as a sibling of the running EXE (frozen) or
# the service checkout root (dev). Keeping it out of `_MEIPASS` lets users
# drop a 60 GB bundle next to the EXE without bloating the frozen binary.
_ENV_COMFYUI_LAUNCHER_BAT = "COMFYUI_LAUNCHER_BAT"      # 自定义启动脚本覆盖项
_ENV_COMFYUI_NETWORK_MODE = "COMFYUI_NETWORK_MODE"      # ComfyUI-Manager 联网模式
_DEFAULT_NETWORK_MODE = "offline"                        # 默认离线，加快冷启动
_COMFYUI_PORTABLE_DIRNAME = "ComfyUI_windows_portable_nvidia"  # 便携包外层目录名
_COMFYUI_PORTABLE_INNER = "ComfyUI_windows_portable"    # 便携包内层目录名

# FastAPI 应用实例：title / version 会体现在自动生成的 OpenAPI 文档中
app = FastAPI(
    title="txt2img-api",
    version="1.0.1",
    description="Text-to-image generation backend for the Development-Training workspace.",
)

# 桌面端（Electron renderer）与 CLI 均以本机 HTTP 访问，来源不固定，故放开 CORS
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/healthz")
def healthz() -> dict:
    """健康探针：供桌面端在启动阶段判断后端是否已就绪。"""
    return {"ok": True, "service": "txt2img-api"}


@app.post("/shutdown")
def shutdown() -> dict:
    """优雅关闭服务。

    Electron / 启动管理器在退出前调用此接口，确保后端进程正常终止。
    在独立线程中延迟退出，以便 HTTP 响应能先返回给调用方。
    """

    def _shutdown() -> None:
        import time as _time
        _time.sleep(0.2)  # 等待 HTTP 响应发送完成
        os._exit(0)

    # 用 daemon 线程 + os._exit 强退：绕过 uvicorn 信号处理，保证进程彻底退出
    threading.Thread(target=_shutdown, daemon=True, name="shutdown").start()
    return {"ok": True, "message": "Shutting down..."}


@app.post("/api/v1/txt2img", response_model=GenerationResponse)
def generate(payload: GenerationRequest) -> GenerationResponse:
    """核心业务端点：文本 → 艺术字位图。

    生成逻辑（工作流路由、提示词合成、降级链）全部委托 ``generate_artwork``；
    本函数只负责把内部 ``GenerationArtifact`` 映射为对外响应模型。
    """
    artifact = generate_artwork(payload)
    return GenerationResponse(
        image_base64=artifact.image_base64,
        image_name=artifact.image_name,
        metadata=artifact.metadata,
        workflow_api=artifact.workflow_api,
        model_dependencies=artifact.model_dependencies,
    )


def run() -> None:
    """Entry point for ``txt2img-api`` console script (used by uv / pip)."""
    # reload 固定关闭：生产 / 打包场景无源码热重载需求，开启会额外派生监视子进程
    uvicorn.run(app, host="0.0.0.0", port=9001, reload=False)


# ── ComfyUI lifecycle ──


def _get_executable_dir() -> Path:
    """返回「运行体」所在目录，用于定位同级的 ComfyUI 便携包。

    * 打包后（PyInstaller）：``txt2img-backend.exe`` 所在目录；
    * 开发态：``txt2img-api/`` 服务目录（``pyproject.toml`` 所在处）。

    两种布局都把 ComfyUI 便携包放在该目录旁，因此用同一函数统一解析。
    """
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    # __file__ = .../app/main.py → parents[1] = .../txt2img-api/
    return Path(__file__).resolve().parents[1]


def _is_port_open(host: str, port: int, timeout: float = 0.6) -> bool:
    """探测目标主机端口是否可建立 TCP 连接（用于判断 ComfyUI 是否就绪）。"""
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except Exception:
        return False


def _has_nvidia_gpu() -> bool:
    """检测本机是否存在可用的 NVIDIA GPU（依赖 nvidia-smi）。

    用于决定 ComfyUI 以 GPU 加速模式还是 CPU 模式启动。探测失败一律按
    「无独显」处理，保证不会因显卡问题阻断服务。
    """
    nvidia_smi = shutil.which("nvidia-smi")
    if nvidia_smi is None:
        return False
    try:
        result = subprocess.run(
            [nvidia_smi],
            capture_output=True,
            timeout=3.0,
        )
        return result.returncode == 0
    except Exception:
        return False


def _ensure_comfyui_offline_config(portable_dir: Path) -> None:
    """启动前把 ComfyUI-Manager 的 ``network_mode`` 改为离线，跳过联网检查。

    读取（若存在）``config.ini``，仅更新 ``network_mode`` 一个键后回写，不触碰
    其它配置。目标模式由 ``COMFYUI_NETWORK_MODE`` 环境变量控制，默认 ``offline``；
    离线模式可显著缩短冷启动时间。

    Args:
        portable_dir: ComfyUI 便携包的内层目录。
    """
    network_mode = os.environ.get(_ENV_COMFYUI_NETWORK_MODE, _DEFAULT_NETWORK_MODE)
    config_path = portable_dir / "ComfyUI" / "user" / "__manager" / "config.ini"

    config = configparser.ConfigParser()
    # 目标目录可能尚不存在（首次运行），先确保父目录存在
    config_path.parent.mkdir(parents=True, exist_ok=True)
    if config_path.exists():
        config.read(str(config_path), encoding="utf-8")

    if not config.has_section("default"):
        config.add_section("default")

    # 仅在值确实变化时才回写，避免每次启动都改写配置文件
    old = config.get("default", "network_mode", fallback=None)
    if old != network_mode:
        config.set("default", "network_mode", network_mode)
        with open(str(config_path), "w", encoding="utf-8") as f:
            config.write(f)
        logger.info(
            "ComfyUI-Manager network_mode set to %r (was %r)", network_mode, old
        )


def _build_comfyui_command(portable_dir: Path) -> list[str]:
    """用便携包自带的嵌入式 Python 构造 ComfyUI 启动命令。

    刻意绕过官方 ``.bat`` 启动脚本，以便直接注入 ``--disable-api-nodes`` 等参数。

    Args:
        portable_dir: ComfyUI 便携包的内层目录。

    Raises:
        FileNotFoundError: 便携包内未找到嵌入式 Python。

    Returns:
        可直接交给 ``subprocess`` 的命令参数列表。
    """
    # 便携包自带嵌入式 Python，直接用它启动可完全掌控命令行参数
    python_exe = portable_dir / "python_embeded" / "python.exe"
    if not python_exe.exists():
        raise FileNotFoundError(f"Embedded Python not found: {python_exe}")

    # -s 忽略用户 site-packages；--disable-auto-launch 阻止自动打开浏览器；
    # --disable-api-nodes 关闭远程 API 节点，纯本地运行更安全
    cmd: list[str] = [
        str(python_exe),
        "-s",
        os.path.join("ComfyUI", "main.py"),
        "--windows-standalone-build",
        "--disable-auto-launch",
        "--disable-api-nodes",
    ]

    # 有 NVIDIA 独显时启用 fp16 累加加速，否则退回 CPU 模式
    if _has_nvidia_gpu():
        cmd.extend(["--fast", "fp16_accumulation"])
    else:
        cmd.append("--cpu")

    return cmd


def _launch_and_wait(
    cmd: Path | list[str],
    host: str,
    port: int,
    timeout: float,
    cwd: Path,
) -> None:
    """启动 ComfyUI 子进程并轮询直至其 API 可访问（或超时）。

    ``cmd`` 既可以是 ``.bat`` 路径（需 shell），也可以是参数列表（直接执行）。
    启动失败仅记录日志、不抛出，以便服务照常提供降级能力。

    Args:
        cmd: 启动命令（``Path`` 或参数列表）。
        host: ComfyUI 监听地址。
        port: ComfyUI 监听端口。
        timeout: 等待就绪的最大秒数。
        cwd: 子进程工作目录。
    """
    global _comfyui_proc
    try:
        if isinstance(cmd, Path):
            # .bat launcher — needs shell
            proc = subprocess.Popen(
                [str(cmd)],
                shell=True,
                cwd=str(cwd),
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0,
            )
        else:
            # Direct command list
            proc = subprocess.Popen(
                cmd,
                shell=False,
                cwd=str(cwd),
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0,
            )

        _comfyui_proc = proc  # 保存引用，供 Ctrl+C 时清理

        # 轮询端口直到可连接或超时；ComfyUI 冷启动通常需要数十秒
        start = time.monotonic()
        while time.monotonic() - start < timeout:
            if _is_port_open(host, port, timeout=0.6):
                logger.info(
                    "ComfyUI is ready at %s:%d (pid %d)", host, port, proc.pid
                )
                return
            time.sleep(1.0)

        logger.warning("Timed out waiting for ComfyUI at %s:%d", host, port)
    except Exception:
        # 启动失败不阻断服务：后续生成请求会自动降级到本地 stub
        logger.exception("Failed to start ComfyUI")


def _start_comfyui_background(
    host: str = "127.0.0.1",
    port: int = 8188,
    timeout: float = 120.0,
) -> None:
    """在后台线程中启动 ComfyUI 并等待其就绪（不阻塞 FastAPI 启动）。

    两条路径：

    * 若设置了 ``COMFYUI_LAUNCHER_BAT``，直接使用该 ``.bat`` 启动（运维逃生通道）；
    * 否则调用便携包内的 ``python_embeded/python.exe`` 并附加 ``--disable-api-nodes``，
      同时把 ComfyUI-Manager 的 ``config.ini`` 改为 ``network_mode = offline`` 以加速冷启动。

    便携包缺失时仅告警并跳过，不影响服务本身启动。

    Args:
        host: ComfyUI 监听地址。
        port: ComfyUI 监听端口。
        timeout: 等待就绪的最大秒数。
    """
    # ── COMFYUI_LAUNCHER_BAT override (escape hatch) ──
    # 运维逃生通道：设置了 COMFYUI_LAUNCHER_BAT 就完全交给该脚本启动
    override = os.environ.get(_ENV_COMFYUI_LAUNCHER_BAT, "").strip()
    if override:
        bat_path = Path(override).expanduser().resolve()
        if not bat_path.exists():
            logger.warning(
                "COMFYUI_LAUNCHER_BAT is set but file not found: %s", bat_path
            )
            return
        logger.info("Starting ComfyUI via override launcher: %s", bat_path)
        thread = threading.Thread(
            target=_launch_and_wait,
            args=(bat_path, host, port, timeout, bat_path.parent),
            daemon=True,
            name="comfyui-launcher",
        )
        thread.start()
        return

    # ── Default: locate portable bundle ──
    # 期望布局：<EXE 同级>/ComfyUI_windows_portable_nvidia/ComfyUI_windows_portable/
    portable_dir = (
        _get_executable_dir() / _COMFYUI_PORTABLE_DIRNAME / _COMFYUI_PORTABLE_INNER
    )
    if not portable_dir.is_dir():
        logger.warning(
            "ComfyUI portable bundle not found at %s — skipping auto-start. "
            "Place the %s/ folder next to the running EXE "
            "(or set %s to a custom .bat).",
            portable_dir,
            _COMFYUI_PORTABLE_DIRNAME,
            _ENV_COMFYUI_LAUNCHER_BAT,
        )
        return

    # ── Patch ComfyUI-Manager config for fast offline startup ──
    try:
        _ensure_comfyui_offline_config(portable_dir)
    except Exception:
        logger.exception("Failed to patch ComfyUI-Manager config — continuing anyway")

    # ── Build command line directly (skip .bat for control over flags) ──
    try:
        cmd = _build_comfyui_command(portable_dir)
        logger.info("Starting ComfyUI: %s", " ".join(cmd))
    except FileNotFoundError as exc:
        logger.warning("Cannot build ComfyUI command: %s", exc)
        return

    # 后台线程启动，不阻塞 FastAPI 启动流程；daemon 保证随主进程退出
    thread = threading.Thread(
        target=_launch_and_wait,
        args=(cmd, host, port, timeout, portable_dir),
        daemon=True,
        name="comfyui-launcher",
    )
    thread.start()


@app.on_event("startup")
def _on_startup() -> None:
    """应用启动钩子：按需自动拉起 ComfyUI，并预热连接。"""
    # AUTO_START_COMFYUI=0 时跳过自动启动，供用户手动管理 ComfyUI 进程
    if os.environ.get("AUTO_START_COMFYUI", "1") == "1":
        _start_comfyui_background()

    # Pre-warm: try connecting to an already-running ComfyUI so the
    # first generate call doesn't pay the cold-start penalty.
    warmup_comfyui_connection()