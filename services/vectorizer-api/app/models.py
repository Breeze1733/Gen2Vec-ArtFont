"""Pydantic 数据契约：矢量化接口的请求模型、参数配置与响应模型。

四个模型分别对应「矢量化参数配置」（``VectorConfig``）、「文生图产物引用」
（``GeneratedImageRef``）以及接口的入参 / 出参（``VectorizeRequest`` /
``VectorizeResponse``）。所有字段约束（取值范围、字面量枚举、默认值）集中于此，
路由层与算法层据此免于重复校验。
"""

from __future__ import annotations

from typing import Any, Literal, Optional

from pydantic import BaseModel, Field, field_validator


class VectorConfig(BaseModel):
    """矢量化参数配置（4 种预设 + 6 个可单独覆盖的底层参数）。

    预设为底层参数提供一组经验默认值；显式传入的字段会覆盖预设对应项。
    字段为 ``None`` 表示「沿用预设值」，因此在算法层由 ``_resolve_vector_params`` 回填。
    """

    preset: Literal["clean", "balanced", "detailed", "ultra"] = "balanced"
    # 颜色精度：调色板量化层级数，值越大保留的色彩层次越多
    color_precision: int | None = Field(default=None, ge=1, le=8)
    # 斑点过滤：忽略小于该面积的色块，值越大输出路径越少
    filter_speckle: int | None = Field(default=None, ge=0, le=64)
    # 拐角阈值：判定硬角的切线夹角（度），越小越易保留锐利转折
    corner_threshold: int | None = Field(default=None, ge=1, le=180)
    # 长度阈值：短于此长度的路径段被平滑合并
    length_threshold: int | None = Field(default=None, ge=1, le=64)
    # 层差：相邻颜色层合并的容差，值越大色阶越少
    layer_difference: int | None = Field(default=None, ge=1, le=64)
    # 上采样倍率：追踪前放大位图，缓解边缘锯齿
    scale: int | None = Field(default=None, ge=1, le=4, description="upscale factor")
    # 是否计算 SVG 还原度（SSIM 等指标），关闭可缩短响应时间
    evaluate_quality: bool = True
    # 是否滤除与画布边缘连通的纯白底衬
    remove_edge_white_background: bool = True
    # 纯白判定：HSV 明度下限
    white_value_threshold: int = Field(default=245, ge=200, le=255)
    # 纯白判定：HSV 饱和度上限
    white_saturation_threshold: int = Field(default=20, ge=0, le=80)


class GeneratedImageRef(BaseModel):
    """文生图产物引用，用于「生成 → 矢量化」的流水线交接。

    三种定位方式按优先级尝试：``image_base64``（内联）> ``file_path``（本地路径）
    > ``artifact_id``（当前后端未实现，仅作占位）。
    """

    artifact_id: Optional[str] = None
    image_base64: Optional[str] = None
    file_path: Optional[str] = None


class VectorizeRequest(BaseModel):
    """矢量化请求体。

    ``source_type`` 区分两种来源：``upload`` 为用户直接上传的位图；
    ``generated`` 为文生图链路产出的位图。位图本体可通过 ``image_path``
    （本地路径，桌面端 / CLI 首选，避免大体积 base64 传输）或
    ``image_base64`` / ``generated_image`` 提供。
    """

    source_type: Literal["upload", "generated"] = "upload"
    text: str = ""
    prompt: str = ""
    negative: str = ""
    resolution: str = "1024 x 1024"
    format: str = "PNG + SVG"
    seed: Optional[int] = None
    vector: VectorConfig = Field(default_factory=VectorConfig)
    image_base64: Optional[str] = None
    image_path: Optional[str] = None
    image_name: Optional[str] = None
    generated_image: Optional[GeneratedImageRef] = None

    @field_validator("seed", mode="before")
    @classmethod
    def validate_seed(cls, value: Any) -> Optional[int]:
        """把空串 / 非数字的 seed 统一归一为 ``None``，避免前端表单空值触发校验错误。"""
        if value is None or value == "":
            return None
        try:
            return int(value)
        except (ValueError, TypeError):
            return None

    @field_validator("resolution")
    @classmethod
    def validate_resolution(cls, value: str) -> str:
        """校验并归一化分辨率字符串。

        接受 ``"1024 x 1024"`` 等带空格 / 大小写混合写法，统一为 ``"1024x1024"``；
        宽高必须为正整数，否则抛出 ``ValueError``，由 FastAPI 转换为 422 响应。
        """
        # Normalize resolution like "1024 x 1024" -> "1024x1024".
        value = value.strip().lower().replace(" ", "")
        parts = value.split("x")
        if len(parts) != 2:
            raise ValueError("resolution format must be like 1024x1024")
        width, height = parts
        if not (width.isdigit() and height.isdigit()):
            raise ValueError("resolution width/height must be integer")
        if int(width) <= 0 or int(height) <= 0:
            raise ValueError("resolution width/height must be > 0")
        return value


class VectorizeResponse(BaseModel):
    """矢量化响应体。

    ``transparent_png`` 为预处理后的透明底 PNG，``preview_png`` 为 SVG 回渲染
    预览图（``png`` 字段为其兼容别名），``svg`` 为最终矢量文本，
    ``metadata`` 汇总生成参数、耗时与保真度等结构化信息。
    """

    transparent_png: str
    preview_png: str
    png: str
    svg: str
    metadata: dict
