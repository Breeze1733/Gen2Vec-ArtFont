"""矢量化后端 HTTP 服务入口（FastAPI 应用装配与路由）。

对外暴露三个端点：

- ``GET  /healthz``：桌面端启动自检用的健康探针；
- ``POST /shutdown``：Electron / 启动管理器退出前调用的优雅关闭接口；
- ``POST /api/v1/vectorize``：核心业务端点，完成「位图 → 透明底 PNG → SVG → 预览图」。

本模块只做「取图 → 调算法 → 组装响应」的编排，具体图像算法位于
``image_processing`` 与 ``vectorization`` 两个子模块。
"""

from __future__ import annotations

import os
import threading
from datetime import datetime, timezone
from pathlib import Path

import uvicorn
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware

from .models import VectorizeRequest, VectorizeResponse
from .image_processing import decode_base64_image, preprocess_image
from .vectorization import inject_svg_metadata, vectorize_image

# FastAPI 应用实例：title / version 会体现在自动生成的 OpenAPI 文档中
app = FastAPI(
    title="Vectorizer API",
    version="1.0.1",
    description="Bitmap-to-SVG vectorization backend service based on OpenCV + scikit-image + vtracer + svgwrite.",
)

# 桌面端（Electron renderer）与 CLI 均以本机 HTTP 访问，来源不固定，故放开 CORS
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


def _read_image_path(file_path: str, channel: str, image_name: str | None = None) -> tuple[bytes, str | None, str]:
    """从本地路径读取位图字节。

    桌面端 / CLI 在「生成 → 矢量化」交接时优先走本地路径，以避免在进程间
    传递大体积 base64 字符串。``channel`` 用于在错误信息中区分图片来源
    （如 ``upload:image_path`` / ``generated:file_path``），便于前端定位问题。

    Args:
        file_path: 位图文件的本地路径，支持 ``~`` 展开。
        channel: 来源渠道标识，仅用于错误提示。
        image_name: 显式指定的图片名；为空时回退为文件名。

    Raises:
        HTTPException: 路径不存在或读取失败时返回 400。

    Returns:
        ``(字节内容, 图片名, 渠道标识)`` 三元组。
    """
    # Read bitmap bytes from a local path. Desktop/CLI use this path-first handoff
    # to avoid sending large base64 payloads between generation and vectorization.
    path = Path(file_path).expanduser()
    if not path.exists() or not path.is_file():
        raise HTTPException(status_code=400, detail=f"{channel} does not exist or is not a file.")
    try:
        return path.read_bytes(), image_name or path.name, channel
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"Failed to read {channel}: {exc}") from exc


def _resolve_vectorize_image(payload: VectorizeRequest) -> tuple[bytes, str | None, str]:
    """解析请求中的位图来源，返回统一的 ``(字节内容, 图片名, 渠道标识)``。

    按 ``source_type`` 分支并遵循固定的优先级：

    - ``upload``：``image_path`` > ``image_base64``；
    - ``generated``：``image_path`` > ``image_base64`` > ``generated_image``
      （其内部再按 ``image_base64`` > ``file_path`` 尝试）。

    所有分支均校验缺失或非法的输入并抛出 400，确保下游算法只处理合法字节流。

    Raises:
        HTTPException: 未提供可用图片来源，或 base64 解码失败。
    """
    # Resolve bitmap bytes from either user upload or generated-pipeline source.
    # upload 分支：本地路径优先，其次内联 base64
    if payload.source_type == "upload":
        if payload.image_path:
            return _read_image_path(payload.image_path, "upload:image_path", payload.image_name)
        if not payload.image_base64:
            raise HTTPException(status_code=400, detail="source_type=upload requires image_base64 or image_path.")
        try:
            return decode_base64_image(payload.image_base64), payload.image_name, "upload:image_base64"
        except Exception as exc:
            raise HTTPException(status_code=400, detail=f"Invalid image_base64: {exc}") from exc

    # generated 分支：本地路径优先，其次顶层 base64，最后退回 generated_image 对象
    if payload.image_path:
        return _read_image_path(payload.image_path, "generated:image_path", payload.image_name)

    if payload.image_base64:
        try:
            return (
                decode_base64_image(payload.image_base64),
                payload.image_name,
                "generated:image_base64",
            )
        except Exception as exc:
            raise HTTPException(status_code=400, detail=f"Invalid image_base64: {exc}") from exc

    generated = payload.generated_image
    if not generated:
        raise HTTPException(
            status_code=400,
            detail="source_type=generated requires image_base64 or generated_image object.",
        )

    if generated.image_base64:
        try:
            return (
                decode_base64_image(generated.image_base64),
                payload.image_name or generated.artifact_id,
                "generated:image_base64",
            )
        except Exception as exc:
            raise HTTPException(status_code=400, detail=f"Invalid generated.image_base64: {exc}") from exc

    if generated.file_path:
        return _read_image_path(
            generated.file_path,
            "generated:file_path",
            payload.image_name or generated.artifact_id,
        )

    if generated.artifact_id:
        raise HTTPException(
            status_code=400,
            detail=(
                "generated.artifact_id is not supported by vectorizer-api. "
                "Please provide generated.image_base64 or generated.file_path."
            ),
        )

    raise HTTPException(
        status_code=400,
        detail="generated_image must provide one of: image_base64, file_path, artifact_id.",
    )


@app.get("/healthz")
def healthz() -> dict:
    """健康探针：供桌面端在启动阶段判断后端是否已就绪。"""
    return {"ok": True, "service": "vectorizer-api"}


@app.post("/shutdown")
def shutdown() -> dict:
    """优雅关闭服务。

    Electron / 启动管理器在退出前调用此接口，确保后端进程正常终止。
    在独立线程中延迟退出，以便 HTTP 响应能先返回给调用方。
    """

    def _shutdown() -> None:
        # 延迟 0.2s 让响应先发回，再用 os._exit 强制终止整个进程
        import time as _time
        _time.sleep(0.2)  # 等待 HTTP 响应发送完成
        os._exit(0)

    # 使用 daemon 线程 + os._exit 强退：绕过 uvicorn 信号处理，保证进程彻底退出
    threading.Thread(target=_shutdown, daemon=True, name="shutdown").start()
    return {"ok": True, "message": "Shutting down..."}


@app.post("/api/v1/vectorize", response_model=VectorizeResponse)
def vectorize(payload: VectorizeRequest) -> VectorizeResponse:
    """核心业务端点：位图 → 透明底 PNG → SVG → 预览图。

    编排流程：解析图片来源 → 预处理（抠图 / 去噪 / 量化）→ 矢量追踪 →
    回填业务元数据并注入 SVG → 组装响应。算法层抛出的异常按类型映射为
    HTTP 状态码：``ValueError`` → 400（入参问题）、``RuntimeError`` → 422
    （依赖缺失等可预期失败）、其余 → 500。
    """
    # Main FR3 endpoint: preprocess -> transparent PNG -> SVG -> preview PNG.
    image_bytes, source_image_name, source_channel = _resolve_vectorize_image(payload)

    try:
        # 先做图像预处理（抠图 / 去噪 / 量化），再交给矢量化引擎追踪路径
        processed = preprocess_image(image_bytes=image_bytes, vector=payload.vector.model_dump())
        output = vectorize_image(transparent_image=processed["transparent_image"], vector=payload.vector.model_dump())
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except Exception as exc:  # pragma: no cover
        raise HTTPException(status_code=500, detail=f"Vectorization failed: {exc}") from exc

    # 回填来源与生成参数等业务元数据；纯上传模式不记录 seed（无生成语义）
    output["metadata"].update(
        {
            "source_type": payload.source_type,
            "source_channel": source_channel,
            "source_image_name": source_image_name,
            "generation": {
                "text": payload.text or "",
                "prompt": payload.prompt or "",
                "negative": payload.negative or "",
                "resolution": payload.resolution or "",
                "seed": payload.seed if payload.seed is not None else 0,
            }
            if payload.source_type != "upload"
            else {
                "text": payload.text or "",
                "prompt": payload.prompt or "",
                "negative": payload.negative or "",
                "resolution": payload.resolution or "",
            },
            "preprocess": {
                "transparent_size": processed["size"],
                "png_transparency": processed.get("png_transparency"),
            },
            "created_at": datetime.now(timezone.utc).isoformat(),
        }
    )
    # 确保最终完整的业务元数据同步注入至 SVG 的 <metadata> 节点中
    output["svg"] = inject_svg_metadata(output["svg"], output["metadata"])
    # SVG 体积需在注入元数据后重新统计，否则会漏算元数据本身的字节数
    output["metadata"]["stats"]["svg_size_kb"] = round(len(output["svg"].encode("utf-8")) / 1024.0, 3)

    # png 与 preview_png 取同一值：保留 png 旧字段名以兼容既有调用方
    response_payload = {
        "transparent_png": processed["transparent_png"],
        "preview_png": output["preview_png"],
        "png": output["preview_png"],
        "svg": output["svg"],
        "metadata": output["metadata"],
    }
    return VectorizeResponse(**response_payload)


def run() -> None:
    """Entry point for the ``vectorizer-api`` console script."""
    # 固定绑定回环地址：本服务仅供本机桌面端 / CLI 调用，不对外暴露
    uvicorn.run(app, host="127.0.0.1", port=8000, reload=False)
