"""Pydantic 数据契约：文生图接口的请求与响应模型。

``GenerationRequest`` 承载要渲染的文字、风格提示词与生成参数，并通过字段校验器
统一归一化文本与分辨率；``GenerationResponse`` 定义返回给桌面端 / CLI 的结构。
所有长度上限与格式约束集中于此，路由层与生成层据此免于重复校验。
"""

from __future__ import annotations

from typing import Literal, Optional

from pydantic import BaseModel, Field, field_validator

# 输入长度上限。text 是「艺术字要画的字」，远短于风格描述与负面词。
_MAX_TEXT = 60
_MAX_PROMPT = 600
_MAX_NEGATIVE = 600
_MAX_WORKFLOW = 64
_MAX_STYLE = 40
_MAX_RESOLUTION = 32


class GenerationRequest(BaseModel):
    """文生图请求体。

    ``text`` 与 ``prompt`` 分工明确：前者决定「画什么字」，后者决定「画成什么样」。
    ``workflow`` 为空时按文本内容自动路由到候选工作流，显式指定则只尝试该工作流。
    """

    # 要渲染的艺术字内容；允许为空（此时仅出背景 + 风格）
    text: str = Field(
        default="",
        max_length=_MAX_TEXT,
        description="Text content to render (Chinese, English, numbers). Empty is allowed.",
    )
    # 风格描述，可自由书写（含版式要求，不拆字段）
    prompt: str = Field(
        default="",
        max_length=_MAX_PROMPT,
        description="Style description (e.g. 'calligraphy, gold gradient')",
    )
    # 负面提示词：描述不希望出现的缺陷（如断笔、错字）
    negative_prompt: str = Field(default="", max_length=_MAX_NEGATIVE)
    # 期望分辨率，形如 "1024x1024"；后端会按模型家族吸附到官方尺寸
    resolution: str = Field(default="1024 x 1024", max_length=_MAX_RESOLUTION)
    # 随机种子；固定值可复现同一张图
    seed: int = 0
    # style 为保留字段：当前仅记录进 metadata，不参与提示词构造
    style: str = Field(
        default="default",
        max_length=_MAX_STYLE,
        description="Reserved. Currently recorded in metadata only; it does not affect generation.",
    )
    # 产物格式声明；后端统一返回 PNG，SVG 由下游矢量化服务生成
    format: Literal["PNG", "PNG + SVG"] = "PNG"
    # 指定工作流名（不含路径与扩展名）；为空则按文本内容自动路由
    workflow: str = Field(
        default="",
        max_length=_MAX_WORKFLOW,
        description=(
            "Workflow filename (without path/extension), e.g. 'flux_schnell'. "
            "Empty falls back to content-based routing; WORKFLOW_PATH env var overrides both."
        ),
    )

    @field_validator("text")
    @classmethod
    def normalize_text(cls, value: str) -> str:
        """strip 并把内部空白折叠为单空格——提示词以此为准渲染。"""
        return " ".join(value.split())

    @field_validator("prompt", "negative_prompt", "style")
    @classmethod
    def strip_text(cls, value: str) -> str:
        """风格 / 负面 / style 三个字段仅做首尾去空白。"""
        return value.strip()

    @field_validator("resolution")
    @classmethod
    def normalize_resolution(cls, value: str) -> str:
        """校验并归一化分辨率字符串。

        接受 ``"1024 x 1024"`` 等带空格 / 大小写混合写法，统一为 ``"1024x1024"``；
        宽高必须为正整数，否则抛出 ``ValueError`` 由 FastAPI 转为 422 响应。
        """
        normalized = value.strip().lower().replace(" ", "")
        parts = normalized.split("x")
        if len(parts) != 2:
            raise ValueError("resolution format must be like 1024x1024")
        width, height = parts
        if not width.isdigit() or not height.isdigit():
            raise ValueError("resolution width/height must be integer")
        if int(width) <= 0 or int(height) <= 0:
            raise ValueError("resolution width/height must be > 0")
        return normalized


class GenerationResponse(BaseModel):
    """文生图响应体。

    ``image_base64`` 为 ``data:image/png;base64,...`` 形式的产物图，
    ``metadata`` 汇总引擎、参数与降级链追踪字段；``workflow_api`` 与
    ``model_dependencies`` 为提交给 ComfyUI 的工作流快照及其模型依赖（降级到本地
    stub 时为 ``None``）。
    """

    image_base64: str          # data:image/png;base64,... 形式的产物图
    image_name: str            # 由提示词派生的文件名
    metadata: dict             # 引擎、参数与降级链追踪信息
    workflow_api: Optional[dict] = None        # 提交给 ComfyUI 的工作流快照
    model_dependencies: Optional[dict] = None  # 工作流引用的模型清单
