"""文生图生成核心模块（工作流路由 → 提示词合成 → ComfyUI 调用 → 降级兜底）。

整体流程：

1. **文本分析**：``_analyze_text`` 把输入拆成汉字 / 拉丁片段 / 数字 / 符号，并判定
   ``chinese`` / ``english`` / ``mixed`` 脚本类型——它是路由与提示词模板共用的唯一真相源；
2. **工作流路由**：``generate_artwork`` 按脚本类型选择候选工作流降级链，逐个尝试至成功；
3. **提示词合成**：``_plan_prompt`` 依据工作流所属模型家族（flux / zimage / qwen_image）
   选取 ``RenderProfile``，按固定段序渲染正向串；当工作流的负面 conditioning 不生效时
   （cfg=1 或无负面文本节点），把负面词「语义反演」成正向引导；
4. **参数注入**：``_patch_workflow`` 顺采样器连线定位正负向文本节点，注入提示词、分辨率与种子；
5. **提交与轮询**：``_call_comfyui_api`` 提交工作流并轮询 history，下载产物；
6. **降级兜底**：任一步失败或 ComfyUI 不可达时，``_local_stub_generate`` 用 Pillow 生成
   带诊断信息的占位图，保证接口始终有响应。

术语约定：

- **脚本（script）**：文本的书写系统分类，取值 ``chinese`` / ``english`` / ``mixed``；
- **profile**：提示词模板家族，与工作流所用模型绑定，决定段序与子句措辞；
- **residue**：原文去掉全部汉字后的剩余串，用于避免汉字被渲染两遍；
- **反演（inversion）**：把「否定」语义改写为「肯定」引导子句。
"""

from __future__ import annotations

import base64
import copy
import hashlib
import io
import json
import logging
import os
import random
import re
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from functools import lru_cache
from pathlib import Path
from typing import Any, Callable, Optional

import httpx
from PIL import Image, ImageDraw, ImageFont

from .models import GenerationRequest

logger = logging.getLogger(__name__)


# ── 环境变量名与默认值 ──
_ENV_COMFYUI_HOST = "COMFYUI_HOST"            # ComfyUI 服务地址
_ENV_POLL_TIMEOUT = "COMFYUI_POLL_TIMEOUT"    # 轮询结果的最大等待秒数
_ENV_POLL_INTERVAL = "COMFYUI_POLL_INTERVAL"  # 轮询间隔（秒）
_ENV_WORKFLOW_PATH = "WORKFLOW_PATH"          # 指定工作流 JSON 的绝对路径

_DEFAULT_COMFYUI_HOST = "http://127.0.0.1:8188"
_DEFAULT_POLL_TIMEOUT = 900    # 15 分钟，覆盖大模型冷启动 + 采样耗时
_DEFAULT_POLL_INTERVAL = 1.0


@dataclass(frozen=True)
class GenerationArtifact:
    """一次生成的完整产物（不可变）。

    ``workflow_api`` 与 ``model_dependencies`` 仅在成功走通 ComfyUI 时填充，
    降级到本地 stub 时为 ``None``。
    """

    image_base64: str
    image_name: str
    metadata: dict
    workflow_api: Optional[dict] = None
    model_dependencies: Optional[dict] = None


# ── Workflow helpers ──


def _resolve_workflow_path(workflow_name: str = "") -> Path:
    """Return absolute path to the workflow JSON template.

    Priority:
      1. WORKFLOW_PATH env var (highest, for dev overrides)
      2. ``workflow_name`` from request → ``workflows/{name}.json``
      3. Default: ``workflows/flux_schnell.json``
    """
    # 1. 环境变量优先级最高，供开发期临时覆盖工作流
    env_path = os.environ.get(_ENV_WORKFLOW_PATH)
    if env_path:
        return Path(env_path).expanduser().resolve()

    # __file__ = .../app/generator.py → .parents[1] = .../txt2img-api/
    # This is correct in both dev (project checkout) and PyInstaller-frozen
    # layouts because frozen bundles lay out the package as `app/*.py` rooted
    # at the service directory alongside the `workflows/` data folder.
    project_root = Path(__file__).resolve().parents[1]
    workflows_dir = project_root / "workflows"

    # 2. 请求指定了工作流名 → workflows/{name}.json（自动补 .json 后缀）
    if workflow_name:
        name = workflow_name if workflow_name.endswith(".json") else f"{workflow_name}.json"
        return workflows_dir / name

    # 3. 均未指定时的默认工作流
    return workflows_dir / "flux_schnell.json"


def _load_workflow(path: Path) -> dict:
    """Load and validate a ComfyUI API-format workflow JSON.

    必须是 API 格式：顶层为 ``{node_id: node}`` 的扁平 dict，每个 node 至少含
    ``class_type`` 与 ``inputs``，否则后续无法安全地注入参数。
    """
    if not path.exists():
        raise FileNotFoundError(f"Workflow file not found: {path}")

    raw = path.read_text(encoding="utf-8")
    workflow = json.loads(raw)

    # 顶层必须是 dict（node_id -> node），否则不是 API 格式工作流
    if not isinstance(workflow, dict):
        raise ValueError("Workflow must be a JSON object (dict of node_id -> node)")

    # 逐个节点校验必要字段，尽早暴露格式问题而非留到注入阶段才崩
    for node_id, node in workflow.items():
        if not isinstance(node, dict) or "class_type" not in node:
            raise ValueError(f"Node {node_id!r} is missing 'class_type' — not an API-format workflow.")
        if "inputs" not in node:
            raise ValueError(f"Node {node_id!r} is missing 'inputs' — not an API-format workflow.")

    return workflow


def _find_nodes_by_class(workflow: dict, class_type: str) -> list[tuple[str, dict]]:
    """扫描工作流中所有节点，返回匹配 ``class_type`` 的 ``(node_id, node_data)`` 列表。

    用于定位需要注入参数的目标节点（如 SaveImage、EmptyLatentImage、KSampler）。
    """

    return [(nid, node) for nid, node in workflow.items() if node.get("class_type") == class_type]


# ── 文本分析：路由与提示词模板共用的唯一真相源 ──

_CJK_RE = re.compile(r"[一-鿿]")              # 汉字（CJK 统一表意文字基本区）
_LATIN_RUN_RE = re.compile(r"[A-Za-z]+")      # 连续拉丁字母片段
_DIGIT_RUN_RE = re.compile(r"\d+(?:[.,]\d+)*")  # 数字串，允许小数点 / 千分位
_WS_RE = re.compile(r"\s+")                    # 连续空白，用于折叠为单空格

# residue 两端需要剥离的包围分隔符（避免 ", 100% ," 这类残留）
_RESIDUE_TRIM = " \t,，、;；:：.。"

# 会被提示词单独点名的符号集。空白与字母数字不属于符号。
_NOTABLE_SYMBOLS = frozenset(
    "%.,:;!?&@#+-=/*~^|$¥€￥°·…—'\"()[]{}「」『』《》【】"
)


@dataclass(frozen=True)
class TextAnalysis:
    """一次拆分出汉字 / 拉丁 / 数字 / 符号，供路由与提示词模板共用。

    ``residue``（原文去掉全部汉字后的剩余串）是修复「文字被渲染两遍」的关键：
    它**永远不含汉字**，因此可以安全地被「同时包含」子句引用。
    """

    raw: str
    stripped: str
    hanzi: tuple[str, ...]
    latin_runs: tuple[str, ...]
    digit_runs: tuple[str, ...]
    symbols: tuple[str, ...]
    script: str

    @property
    def has_hanzi(self) -> bool:
        """是否包含汉字。"""
        return bool(self.hanzi)

    @property
    def has_latin(self) -> bool:
        """是否包含拉丁字母片段。"""
        return bool(self.latin_runs)

    @property
    def has_digits(self) -> bool:
        """是否包含数字串。"""
        return bool(self.digit_runs)

    @property
    def has_symbols(self) -> bool:
        """是否包含被点名的符号。"""
        return bool(self.symbols)

    @property
    def is_empty(self) -> bool:
        """归一化后是否为空串。"""
        return not self.stripped

    @property
    def hanzi_count(self) -> int:
        """汉字个数，用于在提示词里声明精确字数。"""
        return len(self.hanzi)

    @property
    def max_latin_run(self) -> int:
        """最长拉丁片段长度。旧模板用 ``[a-zA-Z]{2,}`` 判定，会漏掉长度为 1 的片段。"""
        return max((len(run) for run in self.latin_runs), default=0)

    @property
    def hanzi_spaced(self) -> str:
        """汉字逐字空格拆分——强制模型一个字一个字画，降低连笔与漏字。"""
        return " ".join(self.hanzi)

    @property
    def symbols_spaced(self) -> str:
        """符号以空格分隔，便于模板逐个点名。"""
        return " ".join(self.symbols)

    @property
    def residue(self) -> str:
        """原文去掉全部汉字后的剩余串（已归一化空白与两端分隔符）。

        ``"咪哄之风 98% hey you"`` -> ``"98% hey you"``
        ``"咖啡 Latte 2.0"``       -> ``"Latte 2.0"``
        ``"单依纯 X"``             -> ``"X"``
        ``"满庭芳"``               -> ``""``
        """
        s = _CJK_RE.sub("", self.stripped)
        s = _WS_RE.sub(" ", s).strip()
        return s.strip(_RESIDUE_TRIM)


@lru_cache(maxsize=256)
def _analyze_text(text: str) -> TextAnalysis:
    """把原始文本拆解为 :class:`TextAnalysis`。

    纯函数且字段全为 str/tuple，故可哈希，用 ``lru_cache`` 缓存重复输入。
    脚本判定只统计汉字与拉丁字母：数字、标点、空白不参与，因此无字母的输入
    （纯数字 / 符号 / 空）与纯英文一样归入 ``english``。
    """
    stripped = _WS_RE.sub(" ", text.strip())

    hanzi = tuple(_CJK_RE.findall(stripped))
    latin_runs = tuple(_LATIN_RUN_RE.findall(stripped))
    digit_runs = tuple(_DIGIT_RUN_RE.findall(stripped))
    # dict.fromkeys 兼顾「去重」与「保持首次出现顺序」
    symbols = tuple(dict.fromkeys(ch for ch in stripped if ch in _NOTABLE_SYMBOLS))

    # 脚本判定：汉字与拉丁字母同时存在才算 mixed，只有汉字算 chinese
    if hanzi and latin_runs:
        script = "mixed"
    elif hanzi:
        script = "chinese"
    else:
        # 纯拉丁，以及无字母（纯数字/符号/空）—— 与既有路由契约一致
        script = "english"

    return TextAnalysis(
        raw=text,
        stripped=stripped,
        hanzi=hanzi,
        latin_runs=latin_runs,
        digit_runs=digit_runs,
        symbols=symbols,
        script=script,
    )


# ── Prompt template engine ──

# 背景抑制前缀 — 要求模型生成干净、易抠图的纯色背景
_FLUX_BG_SUPPRESS = (
    "plain solid background, simple clean backdrop, "
    "no complex scene, no landscape, no environment, "
    "no indoor background, no outdoor background, "
    "no textured surface, no patterned backdrop, "
    "isolated on solid color"
)

_ZIMAGE_BG_SUPPRESS = (
    "纯色背景，简洁干净的背景，"
    "无复杂场景，无风景，无环境，"
    "无室内背景，无室外背景，"
    "无纹理背景，无图案背景"
)

# 文本艺术字常见缺陷的默认负面提示词
_DEFAULT_NEGATIVE = (
    "broken strokes, missing strokes, wrong characters, garbled text, "
    "duplicate characters, repeated characters, extra character, wrong character count, "
    "deformed text, blurry text, low quality, jpeg artifacts, "
    "watermark, text signature, "
    "messy background, cluttered layout, "
    "complex background, busy background, detailed background, "
    "scenery background, landscape background, environmental background, "
    "indoor scene, outdoor scene, gradient background, patterned texture, "
    "photographic background, realistic setting"
)


def _detect_workflow_model(workflow: dict) -> str:
    """Detect which model family a workflow targets.

    Returns 'flux' if the workflow uses DualCLIPLoader + CLIPTextEncodeFlux,
    'zimage' if it uses CLIPLoader(type=lumina2),
    'qwen_image' if it uses CLIPLoader(type=qwen_image), otherwise 'unknown'.
    """
    # 依据加载器节点类型反推模型家族，决定后续用哪套提示词模板与分辨率表
    for node in workflow.values():
        ct = node.get("class_type", "")
        if ct == "DualCLIPLoader":
            return "flux"
        if ct == "CLIPLoader":
            clip_type = node.get("inputs", {}).get("type", "")
            if clip_type == "lumina2":
                return "zimage"
            if clip_type == "qwen_image":
                return "qwen_image"
    return "unknown"


# ── Profile 化的提示词渲染 ──
#
# 单一渲染器 + 数据化 profile：渲染流程零分支，两个模型家族的差异全部落在数据上。

# 段落顺序。布局由用户在 ``prompt`` 里自由书写（不拆字段），
# 因此「把风格段提前到内容约束之前」就是让用户意图排在固定约束前面的落地方式。
_SECTION_ORDER = ("background", "style", "layout", "content", "accuracy", "inversion")

# 版式意图关键词：命中则发中性版式段，不覆盖用户自己写的排布要求。
_LAYOUT_INTENT_RE = re.compile(
    r"布局|版式|排列|排版|居中|中间|错落|上下|左右|左上|右上|左下|右下|"
    r"角落|底层|顶层|位置|大小|倾斜|横排|竖排|"
    r"layout|arrang|centered|centred|vertical|stacked|baseline|align",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class PromptRule:
    """一条提示词子句规则。

    ``section`` 决定它落在哪一段；``fires`` 决定它是否触发；
    ``suppresses`` 列出它触发时需要屏蔽的同表其他 key——例如 residue
    已经把数字点名，就不必再单独强调数字。
    """

    section: str
    key: str
    fires: Callable[[TextAnalysis], bool]
    template: str
    suppresses: tuple[str, ...] = ()


@dataclass(frozen=True)
class RenderProfile:
    """一个模型家族的提示词渲染配置。

    ``lang`` 决定反演词表取英文还是中文子句；``layout`` 为默认版式段，
    ``layout_neutral`` 在用户已表达版式意图时替代之；``rules`` 为该家族的
    全部子句规则（按段归组）。
    """

    key: str
    lang: str          # 'en' | 'zh' —— 同时决定反演词表取哪一语言的子句
    joiner: str
    bg_suppress: str
    layout: str
    layout_neutral: str
    rules: tuple[PromptRule, ...]


@dataclass(frozen=True)
class RenderContext:
    """一次渲染所需的全部输入。

    把「分析结果 + 风格 + profile + 版式意图 + 反演子句」打包为一个上下文，
    使渲染函数保持无状态、易测试。
    """

    analysis: TextAnalysis
    style_prompt: str
    profile: RenderProfile
    layout_intent_detected: bool = False
    inverted_clauses: tuple[str, ...] = ()


# Flux 家族（英文模板）：连接符 ", "，规则子句为英文。
# 段序见 _SECTION_ORDER；规则按段归组，fires 决定触发条件，suppresses 决定互斥。
_FLUX_PROFILE = RenderProfile(
    key="flux",
    lang="en",
    joiner=", ",
    bg_suppress=_FLUX_BG_SUPPRESS,
    layout=(
        "layout: centered composition, single horizontal line of lettering "
        "on one baseline, even character spacing, front-facing flat view, "
        "generous margins"
    ),
    layout_neutral="layout: follow the arrangement described in the style description above",
    rules=(
        # 汉字：逐字空格拆分并声明精确字数
        PromptRule(
            "content", "hanzi", lambda a: a.has_hanzi,
            'Chinese text "{hanzi_spaced}", exactly {hanzi_count} characters',
        ),
        # 汉字之外的内容（拉丁 / 数字 / 符号）由 residue 一次性点名
        PromptRule(
            "content", "residue", lambda a: a.has_hanzi and bool(a.residue),
            'alongside additional characters "{residue}"',
            suppresses=("digits", "symbols"),
        ),
        # 无汉字时直接点名整串（此时不存在汉字被画两遍的风险）
        PromptRule(
            "content", "literal", lambda a: not a.has_hanzi and bool(a.stripped),
            'text "{stripped}"',
        ),
        PromptRule(
            "content", "digits", lambda a: a.has_digits,
            "accurate digits, no repeated digits",
        ),
        PromptRule(
            "content", "symbols", lambda a: a.has_symbols,
            'includes the symbol "{symbols_spaced}" drawn accurately',
        ),
        # 准确性约束：针对汉字字形完整性的兜底
        PromptRule(
            "accuracy", "hanzi_accuracy", lambda a: a.has_hanzi,
            "accurate strokes, complete radicals, no missing character, "
            "no extra character, no repeated character",
        ),
        # 准确性约束：针对拉丁字形质量
        PromptRule(
            "accuracy", "latin_accuracy", lambda a: a.has_latin,
            "crisp letterforms, perfect typography, no duplicate letters",
        ),
    ),
)

# Z-Image / Qwen-Image 家族（中文模板）：连接符 "，"。
# 与 Flux 的差异全部落在数据上——渲染流程本身零分支。
_ZIMAGE_PROFILE = RenderProfile(
    key="zimage",
    lang="zh",
    joiner="，",
    bg_suppress=_ZIMAGE_BG_SUPPRESS,
    layout="版式：文字居中排布，单行水平排列，字距均匀，正面平视视角，四周留白充足",
    layout_neutral="版式：遵循上述风格描述中给出的排布方式",
    rules=(
        # 汉字：逐字空格拆分并声明精确字数
        PromptRule(
            "content", "hanzi", lambda a: a.has_hanzi,
            '文字内容为"{hanzi_spaced}"，不多不少正好{hanzi_count}个字',
        ),
        # 汉字之外的内容由 residue 一次性点名（residue 恒不含汉字）
        PromptRule(
            "content", "residue", lambda a: a.has_hanzi and bool(a.residue),
            '同时包含"{residue}"',
            suppresses=("digits", "symbols"),
        ),
        # 无汉字时直接点名整串
        PromptRule(
            "content", "literal", lambda a: not a.has_hanzi and bool(a.stripped),
            '文字内容为"{stripped}"',
        ),
        PromptRule(
            "content", "digits", lambda a: a.has_digits,
            "数字大小比例正确，清晰可辨，不重不漏",
        ),
        PromptRule(
            "content", "symbols", lambda a: a.has_symbols,
            '包含符号"{symbols_spaced}"，形状准确、清晰可辨',
        ),
        # 准确性约束：汉字字形完整性
        PromptRule(
            "accuracy", "hanzi_accuracy", lambda a: a.has_hanzi,
            "不丢字不缺字，不重字不多字，每个字笔画完整结构正确",
        ),
        # 准确性约束：拉丁与数字字形质量
        PromptRule(
            "accuracy", "latin_accuracy", lambda a: a.has_latin,
            "字母与数字大小比例正确、清晰可辨，不重复不遗漏",
        ),
    ),
)

# 家族 → profile。查表取代 if/elif，顺带消除「未知 model 落到函数末尾返回 None」的隐患。
_MODEL_PROFILE: dict[str, RenderProfile] = {
    "flux": _FLUX_PROFILE,
    "zimage": _ZIMAGE_PROFILE,
    "qwen_image": _ZIMAGE_PROFILE,
    "unknown": _FLUX_PROFILE,
}


def _detect_layout_intent(style_prompt: str) -> bool:
    """用户是否已在风格描述里表达了版式意图。

    命中则发中性版式段，避免默认版式覆盖用户自己的排布要求。

    Args:
        style_prompt: 用户风格描述。

    Returns:
        命中版式关键词返回 ``True``。
    """
    return bool(_LAYOUT_INTENT_RE.search(style_prompt))


def _render_rules(section: str, ctx: RenderContext) -> list[str]:
    """渲染指定段内所有「触发」的规则子句。

    规则按 profile 定义顺序遍历：命中 ``suppresses`` 的 key 会被跳过，``fires``
    为假的不参与；某条规则命中后，其 ``suppresses`` 集合加入抑制集，影响后续同表
    规则（例如 residue 已点名数字，就不再单独强调数字）。
    """
    analysis = ctx.analysis
    suppressed: set[str] = set()
    parts: list[str] = []
    for rule in ctx.profile.rules:
        if rule.section != section or rule.key in suppressed:
            continue
        if not rule.fires(analysis):
            continue
        parts.append(
            rule.template.format(
                hanzi_spaced=analysis.hanzi_spaced,
                hanzi_count=analysis.hanzi_count,
                residue=analysis.residue,
                stripped=analysis.stripped,
                symbols_spaced=analysis.symbols_spaced,
            )
        )
        suppressed.update(rule.suppresses)
    return parts


def _render_section(key: str, ctx: RenderContext) -> list[str]:
    """渲染单个段落，返回子句列表（可能为空）。

    各段生成方式不同：背景段发固定抑制串、风格段回显用户输入、版式段按意图在
    默认 / 中性之间二选一、内容与约束段走规则引擎、反演段回显已算好的正向子句。
    """
    profile = ctx.profile
    if key == "background":
        # 固定背景抑制串，两个 profile 各自维护
        return [profile.bg_suppress]
    if key == "style":
        # 回显用户风格描述；为空则不产出该段
        style = ctx.style_prompt.strip()
        return [style] if style else []
    if key == "layout":
        if ctx.analysis.is_empty:
            return []       # 没有文字就无需版式引导
        # 用户已表达版式意图则发中性段，否则发默认版式
        return [profile.layout_neutral if ctx.layout_intent_detected else profile.layout]
    if key in ("content", "accuracy"):
        # 内容与约束段由规则引擎按触发条件生成
        return _render_rules(key, ctx)
    if key == "inversion":
        # 负面词反演得到的正向子句
        return list(ctx.inverted_clauses)
    return []


def _render_prompt(ctx: RenderContext) -> str:
    """按 ``_SECTION_ORDER`` 逐段渲染，用 profile 的连接符拼成完整正向串。"""
    parts: list[str] = []
    for key in _SECTION_ORDER:
        parts.extend(_render_section(key, ctx))
    return ctx.profile.joiner.join(p for p in parts if p)


def _render_for(
    text: str,
    style_prompt: str,
    profile: RenderProfile,
    inverted_clauses: tuple[str, ...] = (),
) -> str:
    """组装渲染上下文并输出正向提示词（供内部与测试复用的便捷入口）。

    Args:
        text: 要渲染的艺术字内容。
        style_prompt: 用户风格描述。
        profile: 目标模型家族的渲染配置。
        inverted_clauses: 负面词反演得到的正向子句，默认无。

    Returns:
        拼装好的正向提示词。
    """
    ctx = RenderContext(
        analysis=_analyze_text(text),
        style_prompt=style_prompt,
        profile=profile,
        layout_intent_detected=_detect_layout_intent(style_prompt),
        inverted_clauses=inverted_clauses,
    )
    return _render_prompt(ctx)


def _build_flux_prompt(text: str, style_prompt: str) -> str:
    """构造 Flux.1 家族的英文正向提示词。

    模板只负责文字准确性约束（背景抑制、字数、笔画完整性），视觉风格全部来自
    用户传入的 ``style_prompt``。

    Args:
        text: 要渲染的艺术字内容。
        style_prompt: 用户风格描述。

    Returns:
        拼装好的英文正向提示词。
    """
    return _render_for(text, style_prompt, _FLUX_PROFILE)


def _build_zimage_prompt(text: str, style_prompt: str) -> str:
    """构造 Z-Image / Qwen-Image 家族的中文正向提示词。

    与 :func:`_build_flux_prompt` 职责相同，仅 profile 不同（中文模板与连接符）。

    Args:
        text: 要渲染的艺术字内容。
        style_prompt: 用户风格描述。

    Returns:
        拼装好的中文正向提示词。
    """
    return _render_for(text, style_prompt, _ZIMAGE_PROFILE)


# Qwen-Image 官方支持的 7 种分辨率
_QWEN_RESOLUTIONS = [
    (1328, 1328),   # 1:1
    (1664, 928),    # 16:9
    (928, 1664),    # 9:16
    (1472, 1104),   # 4:3
    (1104, 1472),   # 3:4
    (1584, 1056),   # 3:2
    (1056, 1584),   # 2:3
]

# Flux official resolutions — all dimensions divisible by 64 (transformer requirement)
_FLUX_RESOLUTIONS = [
    (1024, 1024),   # 1:1
    (1344, 768),    # 16:9
    (768, 1344),    # 9:16
    (1152, 896),    # 4:3
    (896, 1152),    # 3:4
    (1216, 832),    # 3:2
    (832, 1216),    # 2:3
]


def _map_qwen_resolution(w: int, h: int) -> tuple[int, int]:
    """把请求分辨率映射到最接近的 Qwen-Image 官方尺寸。

    Qwen-Image 仅在 7 种官方分辨率下训练，使用非标准尺寸会明显劣化画质，
    故按宽高比取最接近者。

    Args:
        w: 请求宽度。
        h: 请求高度。

    Returns:
        最接近的官方 ``(宽, 高)``。
    """
    # 按宽高比最接近者匹配官方尺寸，避免非标准分辨率导致画质劣化
    ratio = w / h
    return min(_QWEN_RESOLUTIONS, key=lambda r: abs(r[0] / r[1] - ratio))


def _map_flux_resolution(w: int, h: int) -> tuple[int, int]:
    """把请求分辨率映射到最接近的 Flux 官方尺寸。

    Flux 要求所有边长均为 64 的整数倍，故同样按宽高比吸附到官方尺寸表。

    Args:
        w: 请求宽度。
        h: 请求高度。

    Returns:
        最接近的官方 ``(宽, 高)``。
    """
    # 同理匹配 Flux 官方尺寸（所有边长均为 64 的整数倍，满足 transformer 要求）
    ratio = w / h
    return min(_FLUX_RESOLUTIONS, key=lambda r: abs(r[0] / r[1] - ratio))


def _build_negative_prompt(user_negative: str = "") -> str:
    """合并用户负面提示词与艺术字专用默认负面词。

    Args:
        user_negative: 用户提供的负面词，可为空。

    Returns:
        合并后的负面串；用户未提供时即为默认负面词。
    """
    # 默认负面词在前、用户负面词在后，两者以 ", " 连接（后续按 ", " 切子句）
    if user_negative.strip():
        return f"{_DEFAULT_NEGATIVE}, {user_negative.strip()}"
    return _DEFAULT_NEGATIVE


# ── 负面 conditioning 能力探测 ──
#
# 负面提示词并非在所有工作流上都真正生效：
#   * cfg = 1 时采样器忽略负面 conditioning（flux_schnell 与 test_z_image_turbo 都是）
#   * test_z_image_turbo 结构上就没有负面文本节点（负向由 ConditioningZeroOut 从正向派生）
# 所以必须同时看 cfg 与节点结构，任一不满足都判定为「不生效」。

_SAMPLER_CLASSES = ("KSampler", "KSamplerAdvanced")
_TEXT_ENCODE_CLASSES = ("CLIPTextEncode", "CLIPTextEncodeFlux")

# conditioning 直通节点：class_type -> 用于上溯的输入字段名
_CONDITIONING_PASSTHROUGH = {"ConditioningZeroOut": "conditioning"}

# cfg <= 该值视为负面 conditioning 失效
_CFG_NEGATIVE_MIN = 1.05

_BLOCKER_ORDER = ("no_sampler", "derived_from_positive", "no_negative_text_node", "cfg_too_low")


@dataclass(frozen=True)
class _ConditioningResolution:
    positive_node_id: Optional[str]
    negative_node_id: Optional[str]      # 能承载负面文本的节点；None 表示无处可写
    negative_chain_class: Optional[str]  # negative 槽位直接指向的 class_type
    derived_from_positive: bool


@dataclass(frozen=True)
class NegativeCapability:
    has_sampler: bool
    cfg: Optional[float]
    positive_node_id: Optional[str]
    negative_node_id: Optional[str]
    negative_chain_class: Optional[str]
    derived_from_positive: bool
    effective: bool
    reason: str
    blockers: tuple[str, ...]


def _node_sort_key(node_id: str) -> tuple[int, ...]:
    """数字感知的节点排序键：``"57:27" -> (57, 27)``、``"9" -> (9,)``。

    纯字符串排序会得出 ``"57:27" < "9"``，与人类直觉相反。
    """
    # 抽出 id 中的所有数字段作为排序键；无数字时退化为 (0,) 保证可比
    return tuple(int(n) for n in re.findall(r"\d+", node_id)) or (0,)


def _ref_node_id(ref: Any) -> Optional[str]:
    """解析 ``[node_id, slot]`` 形式的连线引用，返回节点 id。

    ComfyUI 的连线既可能是 ``["4", 0]`` 这样的列表，也可能是裸字符串；
    无法识别时返回 ``None``。
    """
    if isinstance(ref, (list, tuple)) and ref:
        return str(ref[0])
    if isinstance(ref, str):
        return ref
    return None


def _resolve_conditioning_nodes(workflow: dict) -> _ConditioningResolution:
    """解析正向 / 负向文本节点。

    主路径顺 ``KSampler.positive`` / ``.negative`` 连线解析——**不依赖 JSON 字典插入序**，
    否则一次重新导出就可能把正负向互换。解析时沿 conditioning 直通节点上溯，判断负向
    是否由正向派生；无可用采样器连线时，回退到「数字感知的 id 序」取前两个文本编码节点。

    Args:
        workflow: API 格式工作流 dict。

    Returns:
        正向 / 负向节点 id、负向链路 class_type 与是否由正向派生。
    """
    for node in workflow.values():
        if node.get("class_type") not in _SAMPLER_CLASSES:
            continue
        inputs = node.get("inputs", {})
        pos_id = _ref_node_id(inputs.get("positive"))
        neg_id = _ref_node_id(inputs.get("negative"))
        if pos_id is None or neg_id is None:
            continue

        neg_chain_class = workflow.get(neg_id, {}).get("class_type")

        # 沿负向链路穿过 conditioning 直通节点，直到文本编码节点或断层
        cursor: Optional[str] = neg_id
        # seen 防止环形连线导致死循环；若上溯撞到正向节点，说明负向由正向派生
        seen: set[str] = set()
        derived = False
        while cursor and cursor not in seen:
            if cursor == pos_id:
                derived = True
                break
            seen.add(cursor)
            node_c = workflow.get(cursor, {})
            ctype = node_c.get("class_type", "")
            if ctype in _TEXT_ENCODE_CLASSES:
                break
            step = _CONDITIONING_PASSTHROUGH.get(ctype)
            if not step:
                cursor = None
                break
            cursor = _ref_node_id(node_c.get("inputs", {}).get(step))

        negative_text_node: Optional[str] = None
        if not derived and cursor and workflow.get(cursor, {}).get("class_type") in _TEXT_ENCODE_CLASSES:
            negative_text_node = cursor

        positive_text_node = (
            pos_id if workflow.get(pos_id, {}).get("class_type") in _TEXT_ENCODE_CLASSES else None
        )

        return _ConditioningResolution(
            positive_node_id=positive_text_node,
            negative_node_id=negative_text_node,
            negative_chain_class=neg_chain_class,
            derived_from_positive=derived,
        )

    # 回退：无采样器连线可用时，按数字感知 id 序取前两个文本编码节点
    text_nodes = sorted(
        (nid for nid, n in workflow.items() if n.get("class_type") in _TEXT_ENCODE_CLASSES),
        key=_node_sort_key,
    )
    return _ConditioningResolution(
        positive_node_id=text_nodes[0] if text_nodes else None,
        negative_node_id=text_nodes[1] if len(text_nodes) > 1 else None,
        negative_chain_class=None,
        derived_from_positive=False,
    )


def _probe_negative_capability(workflow: dict) -> NegativeCapability:
    """判定该工作流能否真正兑现负面提示词。

    同时考察 cfg 与节点结构：cfg=1 时采样器忽略负面 conditioning；负向若由
    ``ConditioningZeroOut`` 从正向派生，则结构上根本没有负面文本节点。任一条件
    不满足都判定为「不生效」，并记录具体阻断原因。

    Args:
        workflow: API 格式工作流 dict。

    Returns:
        含是否生效、cfg、正负向节点与阻断原因的能力描述。
    """
    res = _resolve_conditioning_nodes(workflow)
    sampler = next(
        (n for n in workflow.values() if n.get("class_type") in _SAMPLER_CLASSES), None
    )
    cfg_raw = (sampler or {}).get("inputs", {}).get("cfg")
    cfg = float(cfg_raw) if isinstance(cfg_raw, (int, float)) else None

    # 逐条收集阻断原因；只要存在任意一条，负面 conditioning 即判为不生效
    blockers: list[str] = []
    if sampler is None:
        blockers.append("no_sampler")
    if res.derived_from_positive:
        blockers.append("derived_from_positive")
    elif res.negative_node_id is None:
        blockers.append("no_negative_text_node")
    if cfg is None or cfg <= _CFG_NEGATIVE_MIN:
        blockers.append("cfg_too_low")

    # effective 为「无任何阻断原因」；reason 取首个命中的阻断项作为主因
    return NegativeCapability(
        has_sampler=sampler is not None,
        cfg=cfg,
        positive_node_id=res.positive_node_id,
        negative_node_id=res.negative_node_id,
        negative_chain_class=res.negative_chain_class,
        derived_from_positive=res.derived_from_positive,
        effective=not blockers,
        reason=next((b for b in _BLOCKER_ORDER if b in blockers), "ok"),
        blockers=tuple(blockers),
    )


# ── 负面词语义反演 ──
#
# cfg=1 的工作流无法用负面 conditioning，改用「否定 -> 肯定」把负面词转成正向引导。
# 硬规则：进入正向串的字符串**只能**来自下方词表的字面量，用户负面词本身永不注入
# （把 "broken strokes" 放进正向串等于邀请模型画断裂笔画）。

@dataclass(frozen=True)
class InversionRule:
    """一条「否定 -> 肯定」反演规则。

    ``match`` 里的英文项须为小写（按小写干草堆匹配），中文项原样。
    ``covered_by`` 非空表示该语义已由正向的某一段承担，只记账、不注入。
    ``clause_en`` / ``clause_zh`` 内**禁止出现否定词**——否则等于把缺陷写进正向串。
    """

    key: str
    match: tuple[str, ...]
    clause_en: str
    clause_zh: str
    covered_by: Optional[str] = None


@dataclass(frozen=True)
class InversionResult:
    """一次负面词反演的结果。

    ``matched`` / ``covered`` 分别记录命中的规则与已被既有正向段覆盖的规则；
    ``unmapped_terms`` 为词表未覆盖的用户负面词——只上报、绝不注入正向串。
    """

    clauses: tuple[str, ...]         # 实际注入的正向子句
    matched: tuple[str, ...]         # 命中的规则 key
    covered: tuple[str, ...]         # 命中但被既有正向段覆盖
    unmapped_terms: tuple[str, ...]  # 词表未覆盖的用户词，只上报、绝不注入


_INVERSION_RULES: tuple[InversionRule, ...] = (
    # —— 已被背景抑制段覆盖，只记账不注入 ——
    # 复杂 / 杂乱背景：已由背景抑制段承担，只记账
    InversionRule(
        key="complex_background",
        match=("complex background", "busy background", "detailed background",
               "messy background", "cluttered layout", "杂乱背景", "复杂背景"),
        clause_en="clean empty background",
        clause_zh="干净空旷的背景",
        covered_by="background",
    ),
    # 风景 / 实景背景：同上，由背景抑制段承担
    InversionRule(
        key="scenery_background",
        match=("scenery background", "landscape background", "environmental background",
               "photographic background", "realistic setting", "风景背景", "环境背景"),
        clause_en="abstract flat backdrop",
        clause_zh="抽象平面底",
        covered_by="background",
    ),
    # 室内 / 室外场景：同上，由背景抑制段承担
    InversionRule(
        key="indoor_outdoor_scene",
        match=("indoor scene", "outdoor scene", "室内场景", "室外场景"),
        clause_en="pure graphic backdrop",
        clause_zh="纯图形底",
        covered_by="background",
    ),
    # 渐变 / 图案背景：同上，由背景抑制段承担
    InversionRule(
        key="gradient_pattern_bg",
        match=("gradient background", "patterned texture", "渐变背景", "图案背景"),
        clause_en="uniform single-color fill",
        clause_zh="均匀纯色填充",
        covered_by="background",
    ),
    # —— 已被 accuracy 段覆盖，只记账不注入 ——
    # 断笔 / 缺笔：已由 accuracy 段承担，只记账
    InversionRule(
        key="broken_missing_strokes",
        match=("broken strokes", "missing strokes", "断笔", "缺笔", "笔画断裂"),
        clause_en="each glyph fully formed with complete closed strokes",
        clause_zh="每个字笔画完整、结构闭合",
        covered_by="accuracy",
    ),
    # 错字 / 乱码：已由 accuracy 段承担，只记账
    InversionRule(
        key="wrong_garbled_text",
        match=("wrong characters", "garbled text", "错字", "乱码"),
        clause_en="every glyph is a correct, legible character",
        clause_zh="每个字都是正确可读的汉字",
        covered_by="accuracy",
    ),
    # 重字 / 多字 / 字数错误：已由 accuracy 段承担，只记账
    InversionRule(
        key="duplicate_characters",
        match=("duplicate characters", "repeated characters", "extra character",
               "wrong character count", "重字", "多字", "重复字"),
        clause_en="exactly the specified character count, each glyph appears once",
        clause_zh="字数与指定完全一致，每个字只出现一次",
        covered_by="accuracy",
    ),
    # —— 需要真正注入的正向引导 ——
    # 模糊 / 低质 / 畸变：正向无对应段，需真正注入清晰度引导
    InversionRule(
        key="blurry_low_quality",
        match=("blurry text", "low quality", "jpeg artifacts", "deformed text",
               "模糊", "低清晰度", "畸变"),
        clause_en="sharp high-resolution edges, crisp vector-clean outlines",
        clause_zh="边缘锐利、高清、矢量感清晰的字形",
    ),
    # 水印 / 署名：正向无对应段，注入「只有字形本身」的引导
    InversionRule(
        key="watermark",
        match=("watermark", "text signature", "水印", "署名", "落款"),
        clause_en="clean unmarked typography, only the lettering itself",
        clause_zh="画面只有字形本身，干净利落",
    ),
    # 人物 / 动物等具象元素：正向无对应段，注入「纯字形构图」的引导
    InversionRule(
        key="figurative_elements",
        match=("person", "people", "human", "face", "hand", "animal",
               "人物", "人脸", "动物"),
        clause_en="only typography in frame, pure lettering composition",
        clause_zh="画面主体只有文字，纯字形构图",
    ),
)

_NEGATIVE_TERM_SPLIT_RE = re.compile(r"[,，、;；\n]+")   # 负面词的分隔符（中英标点 + 换行）
_COMPARE_NORM_RE = re.compile(r"[\s,，、;；:：.。]+")      # 比较前需剔除的空白与标点


def _split_negative_terms(source: str) -> tuple[str, ...]:
    """按中英文逗号 / 分号 / 换行切分负面词串，并丢弃空白项。"""
    return tuple(t.strip() for t in _NEGATIVE_TERM_SPLIT_RE.split(source) if t.strip())


def _normalize_for_compare(text: str) -> str:
    """归一化用于「是否已存在」比较的文本：转小写并剔除空白与标点。"""
    return _COMPARE_NORM_RE.sub("", text.lower())


def _rule_matches(rule: InversionRule, haystack: str) -> bool:
    """判断规则是否命中：任一匹配词出现在（已小写的）干草堆中即为命中。"""
    return any(term in haystack for term in rule.match)


def _invert_negative(
    negative_source: str,
    profile: RenderProfile,
    already_present: str = "",
) -> InversionResult:
    """把负面词表内的词反演成正向子句。

    未命中的词只上报、不注入。已由正向段覆盖的词只记账、不重复注入。
    """
    # 无负面词则无需求反演，直接返回空结果
    terms = _split_negative_terms(negative_source)
    if not terms:
        return InversionResult((), (), (), ())

    haystack = negative_source.lower()
    norm_present = _normalize_for_compare(already_present)
    use_en = profile.lang == "en"

    clauses: list[str] = []
    matched: list[str] = []
    covered: list[str] = []
    seen_clauses: set[str] = set()

    for rule in _INVERSION_RULES:
        if not _rule_matches(rule, haystack):
            continue
        matched.append(rule.key)
        if rule.covered_by is not None:
            covered.append(rule.key)
            continue

        # 同一子句只注入一次；若正向串里已含等价内容，也跳过
        clause = rule.clause_en if use_en else rule.clause_zh
        norm = _normalize_for_compare(clause)
        if norm in seen_clauses or (norm_present and norm in norm_present):
            continue
        seen_clauses.add(norm)
        clauses.append(clause)

    # 未被任何规则覆盖的负面词：仅上报，绝不注入正向串
    unmapped = tuple(
        term for term in terms
        if not any(_rule_matches(rule, term.lower()) for rule in _INVERSION_RULES)
    )

    return InversionResult(tuple(clauses), tuple(matched), tuple(covered), unmapped)


# ── 提示词方案 ──

# clip_l 是 CLIP-L 编码器（77 token），只收关键段的短摘要；全长给 t5xxl。
_CLIP_L_SECTIONS = ("background", "content", "style")
_CLIP_L_MAX_CHARS = 220


@dataclass(frozen=True)
class PromptPlan:
    """一次渲染的完整产物：正向 / 负向串与各自的 clip_l 摘要在此定下来。"""

    analysis: TextAnalysis
    profile_key: str
    positive_prompt: str
    negative_prompt: str
    clip_l_positive: str
    clip_l_negative: str
    inversion: InversionResult
    negative_strategy: str          # 'negative-conditioning' | 'positive-inversion'
    capability: Optional[NegativeCapability]
    layout_intent_detected: bool
    clip_l_token_estimate: int


def _estimate_clip_tokens(text: str) -> int:
    """保守估算 CLIP BPE 的 token 数。

    英文按词、数字按串、其余非空白字符各计 1——这是偏保守的上界近似，
    用于判断 ``clip_l`` 是否存在截断风险（其上限为 77）。
    """
    return len(re.findall(r"[A-Za-z]+|\d+|[^\sA-Za-z\d]", text))


def _truncate_at_boundary(text: str, limit: int, joiner: str) -> str:
    """按子句边界截断文本，绝不切断半个词。

    Args:
        text: 待截断文本。
        limit: 字符数上限。
        joiner: 子句连接符，据此切分子句。

    Returns:
        不超过 ``limit`` 的最长子句序列；单子句即超长时退回硬截断。
    """
    if len(text) <= limit:
        return text
    # 逐个子句累加，一旦超出上限立即停止，避免切断半个词
    parts = text.split(joiner)
    out = ""
    for part in parts:
        candidate = part if not out else f"{out}{joiner}{part}"
        if len(candidate) > limit:
            break
        out = candidate
    # 单个子句就超长时退回硬截断，保证至少返回非空结果
    return out or parts[0][:limit]


def _render_clip_l(ctx: RenderContext) -> str:
    """为 CLIP-L 渲染短摘要。

    只取关键段（背景 / 内容 / 风格）的摘要，并按子句边界截断；t5xxl 收全长，
    两者内容不再相等。
    """
    # 只取关键段的摘要，再按子句边界截断到 CLIP-L 的长度上限
    parts: list[str] = []
    for key in _CLIP_L_SECTIONS:
        parts.extend(_render_section(key, ctx))
    joined = ctx.profile.joiner.join(p for p in parts if p)
    return _truncate_at_boundary(joined, _CLIP_L_MAX_CHARS, ctx.profile.joiner)


def _plan_prompt(
    request: GenerationRequest,
    model: str,
    capability: Optional[NegativeCapability] = None,
) -> PromptPlan:
    """渲染正向 / 负向提示词，并决定负面策略。

    ``capability`` 为 None 表示未知——按「不生效」处理，把用户意图反演成正向引导，
    避免静默丢失。

    Args:
        request: 生成请求（提供文字、风格与负面词）。
        model: 工作流所属模型家族，决定选用哪个 ``RenderProfile``。
        capability: 负面 conditioning 能力探测结果；None 表示未知。

    Returns:
        含正向 / 负向串、clip_l 摘要、反演结果与策略标记的完整方案。
    """
    profile = _MODEL_PROFILE.get(model, _FLUX_PROFILE)
    analysis = _analyze_text(request.text)
    negative_prompt = _build_negative_prompt(request.negative_prompt)

    # capability 为 None（未知）或判定不生效时，改用正向反演兜住用户意图
    use_inversion = capability is None or not capability.effective
    layout_intent = _detect_layout_intent(request.prompt)

    # 先渲染不含反演子句的基础正向串，供「是否已覆盖」判断使用
    base_ctx = RenderContext(
        analysis=analysis,
        style_prompt=request.prompt,
        profile=profile,
        layout_intent_detected=layout_intent,
    )
    base_positive = _render_prompt(base_ctx)

    inversion = (
        _invert_negative(negative_prompt, profile, already_present=base_positive)
        if use_inversion
        else InversionResult((), (), (), ())
    )

    # 有反演子句时重新渲染以并入 inversion 段；否则直接复用基础串
    if inversion.clauses:
        ctx = RenderContext(
            analysis=analysis,
            style_prompt=request.prompt,
            profile=profile,
            layout_intent_detected=layout_intent,
            inverted_clauses=inversion.clauses,
        )
        positive_prompt = _render_prompt(ctx)
    else:
        ctx = base_ctx
        positive_prompt = base_positive

    # clip_l 只收关键段摘要；负面串按 ", " 切子句后同样截断到长度上限
    clip_l_positive = _render_clip_l(ctx)
    return PromptPlan(
        analysis=analysis,
        profile_key=profile.key,
        positive_prompt=positive_prompt,
        negative_prompt=negative_prompt,
        clip_l_positive=clip_l_positive,
        # 负面串由 _build_negative_prompt 用 ", " 连接，故按 ", " 切子句
        clip_l_negative=_truncate_at_boundary(negative_prompt, _CLIP_L_MAX_CHARS, ", "),
        inversion=inversion,
        negative_strategy="positive-inversion" if use_inversion else "negative-conditioning",
        capability=capability,
        layout_intent_detected=layout_intent,
        clip_l_token_estimate=_estimate_clip_tokens(clip_l_positive),
    )


# prompt_synthesis 元数据结构的版本号，消费方可据此判断字段兼容性
_SYNTHESIS_VERSION = 1


def _build_synthesis_block(plan: PromptPlan) -> dict[str, Any]:
    """记录「实际注入了什么」，供验收与追溯。

    ``positive_prompt`` 是权威值——必须与产物 ``workflow_api.json`` 中对应节点
    的内容逐字相等；``positive_sha256`` 为其短哈希，便于快速比对。

    Args:
        plan: 已渲染完成的提示词方案。

    Returns:
        可直接并入产物 metadata 的 ``prompt_synthesis`` 块。
    """
    cap = plan.capability
    # 记录实际生效的 profile、文本画像与注入串，供验收核对
    return {
        "version": _SYNTHESIS_VERSION,
        "applied": True,
        "profile": plan.profile_key,
        "section_order": list(_SECTION_ORDER),
        # 文本画像：脚本类型、归一化文本与各成分拆解，便于复现与排错
        "text_profile": plan.analysis.script,
        "normalized_text": plan.analysis.stripped,
        "hanzi_count": plan.analysis.hanzi_count,
        "residue": plan.analysis.residue,
        "symbols": list(plan.analysis.symbols),
        "layout_intent_detected": plan.layout_intent_detected,
        "positive_prompt": plan.positive_prompt,
        "positive_sha256": hashlib.sha256(plan.positive_prompt.encode("utf-8")).hexdigest()[:16],
        # clip_l 只有 flux 家族在用（CLIPTextEncodeFlux 的双文本输入）。
        # 其余家族写的是单 text 输入，报告「截断风险」会误导。
        "clip_l_used": plan.profile_key == "flux",
        "clip_l": plan.clip_l_positive,
        "clip_l_token_estimate": plan.clip_l_token_estimate,
        "clip_l_truncation_risk": (
            plan.clip_l_token_estimate > 70 if plan.profile_key == "flux" else None
        ),
        # 负面策略与写入位置：inversion 表示走正向反演，否则写进负面节点
        "negative_strategy": plan.negative_strategy,
        "negative_written_to": cap.negative_node_id if cap else None,
        "negative_capability": (
            {
                "effective": cap.effective,
                "reason": cap.reason,
                "blockers": list(cap.blockers),
                "cfg": cap.cfg,
                "negative_node_id": cap.negative_node_id,
                "negative_chain_class": cap.negative_chain_class,
                "derived_from_positive": cap.derived_from_positive,
            }
            if cap
            else None
        ),
        # 反演明细：命中的规则、被覆盖的规则、实际注入的子句与未映射词
        "inversion": {
            "matched": list(plan.inversion.matched),
            "covered": list(plan.inversion.covered),
            "injected_clauses": list(plan.inversion.clauses),
            "unmapped_terms": list(plan.inversion.unmapped_terms),
        },
    }


def _build_stub_synthesis_block(request: GenerationRequest) -> dict[str, Any]:
    """为 stub 产物构造同名的 prompt_synthesis 块。

    stub 不构造提示词，但仍发出同名键，保证消费方可无条件读取而不必判空。
    """
    return {
        "version": _SYNTHESIS_VERSION,
        "applied": False,
        "text_profile": _analyze_text(request.text).script,
        "note": "local stub 不构造提示词，未提交 ComfyUI",
    }


def _log_prompt_plan(plan: PromptPlan, workflow_name: str) -> None:
    """记录一次提示词方案的摘要，并对被丢弃 / 已覆盖的负面词给出告警。"""
    logger.info(
        "prompt plan: workflow=%s profile=%s text=%s strategy=%s negative_effective=%s",
        workflow_name,
        plan.profile_key,
        plan.analysis.script,
        plan.negative_strategy,
        plan.capability.effective if plan.capability else None,
    )
    if plan.inversion.unmapped_terms:
        logger.warning(
            "负面词不在反演词表内，已丢弃且不注入：%s", list(plan.inversion.unmapped_terms)
        )
    if plan.inversion.covered:
        logger.info("负面词已由既有正向段覆盖，不重复注入：%s", list(plan.inversion.covered))


def _write_text_node(node: dict, text: str, clip_l: str) -> None:
    """把提示词写入指定文本节点。

    ``CLIPTextEncodeFlux`` 有 ``clip_l`` 与 ``t5xxl`` 两个文本输入，分别写入
    短摘要与全长；普通文本节点只有一个 ``text`` 输入。
    """
    if node.get("class_type") == "CLIPTextEncodeFlux":
        # t5xxl 容量大，收全长；clip_l 仅 77 token，收短摘要
        node["inputs"]["t5xxl"] = text
        node["inputs"]["clip_l"] = clip_l
    else:
        node["inputs"]["text"] = text


def _patch_workflow(
    workflow: dict,
    request: GenerationRequest,
    *,
    plan: Optional[PromptPlan] = None,
) -> dict:
    """Deep-copy workflow and inject user parameters.

    Patching rules:
      - 文本节点：顺 KSampler 的 ``positive`` / ``negative`` 连线解析
        （无采样器时按数字感知 id 序回退），不再依赖 JSON 字典插入序
      - CLIPTextEncodeFlux：``t5xxl`` 收全长、``clip_l`` 收短摘要
      - EmptyLatentImage / EmptySD3LatentImage → width, height
      - KSampler / KSamplerAdvanced → seed

    ``plan`` 可由调用方预先算好以避免重复渲染；为 None 时内部计算。
    """
    # 深拷贝：绝不修改调用方传入的原始工作流模板
    patched = copy.deepcopy(workflow)

    model = _detect_workflow_model(patched)
    if plan is None:
        plan = _plan_prompt(request, model, _probe_negative_capability(patched))

    # ── 文本节点（只写解析出的两个，不再「第 2 个起全灌负面」）──
    resolved = _resolve_conditioning_nodes(patched)
    if resolved.positive_node_id and resolved.positive_node_id in patched:
        _write_text_node(
            patched[resolved.positive_node_id], plan.positive_prompt, plan.clip_l_positive
        )
    if resolved.negative_node_id and resolved.negative_node_id in patched:
        # 即便 capability 判定不生效也照写：保持 workflow_api 快照的历史可比性，
        # 且将来若调高 cfg，负面立即恢复生效。不生效的事实由 metadata 记录。
        _write_text_node(
            patched[resolved.negative_node_id], plan.negative_prompt, plan.clip_l_negative
        )

    # ── EmptyLatentImage / EmptySD3LatentImage (resolution) ──
    # 两种潜空间节点都覆盖，兼容不同工作流的导出习惯
    latent_nodes = _find_nodes_by_class(patched, "EmptyLatentImage")
    latent_nodes += _find_nodes_by_class(patched, "EmptySD3LatentImage")
    w, h = _parse_resolution(request.resolution)
    # qwen / flux 需吸附到各自的官方尺寸，其余家族沿用请求值
    if model == "qwen_image":
        w, h = _map_qwen_resolution(w, h)
    elif model == "flux":
        w, h = _map_flux_resolution(w, h)
    for _nid, node in latent_nodes:
        node["inputs"]["width"] = w
        node["inputs"]["height"] = h

    # ── KSampler (seed) ──
    # 采样器种类同样有两种，一并写入种子
    sampler_nodes = _find_nodes_by_class(patched, "KSampler")
    sampler_nodes += _find_nodes_by_class(patched, "KSamplerAdvanced")
    for _nid, node in sampler_nodes:
        node["inputs"]["seed"] = request.seed

    return patched


# ── Warmup ──


def warmup_comfyui_connection() -> None:
    """预热 ComfyUI 连接，避免首个生成请求承担冷启动开销。

    以短超时探测 ``/system_stats``；连不上属正常（ComfyUI 可能仍在启动），
    因此失败静默忽略。
    """
    host = os.environ.get(_ENV_COMFYUI_HOST, _DEFAULT_COMFYUI_HOST).rstrip("/")
    try:
        # 短超时探测：连不上也不报错，ComfyUI 可能仍在启动中
        with httpx.Client(timeout=5.0) as client:
            resp = client.get(f"{host}/system_stats")
            if resp.status_code == 200:
                logger.info("ComfyUI connection verified at %s", host)
    except Exception:
        pass  # ComfyUI may still be starting — that's fine


# ── ComfyUI API interaction ──


def _call_comfyui_api(
    request: GenerationRequest, submitted_workflow: dict
) -> Optional[GenerationArtifact]:
    """Submit an **already patched** workflow to ComfyUI, poll, return artifact.

    调用方负责 patch（见 :func:`_patch_workflow`）——本函数不再内部重复 patch。

    Returns None on any failure (connection, timeout, execution error)
    so the caller can fall back to the local stub.
    """
    # 三项连接参数均可由环境变量覆盖，便于长任务或本地快速测试
    host = os.environ.get(_ENV_COMFYUI_HOST, _DEFAULT_COMFYUI_HOST).rstrip("/")
    timeout = int(os.environ.get(_ENV_POLL_TIMEOUT, str(_DEFAULT_POLL_TIMEOUT)))
    interval = float(os.environ.get(_ENV_POLL_INTERVAL, str(_DEFAULT_POLL_INTERVAL)))

    try:
        with httpx.Client(timeout=30.0) as client:
            # 1. 提交工作流，取回 prompt_id 作为后续轮询的句柄
            client_id = str(uuid.uuid4())
            patched = submitted_workflow
            submit_payload = {"prompt": patched, "client_id": client_id}

            submit_resp = client.post(f"{host}/prompt", json=submit_payload)
            # 提交失败（工作流非法等）→ 返回 None，交由上层尝试下一个工作流
            if submit_resp.is_error:
                logger.warning("ComfyUI /prompt returned %s: %s", submit_resp.status_code, submit_resp.text[:200])
                return None
            prompt_id = submit_resp.json().get("prompt_id")
            if not prompt_id:
                logger.warning("ComfyUI /prompt returned no prompt_id: %s", submit_resp.text[:200])
                return None

            # 2. 轮询 history 直至该 prompt 标记完成或超时
            deadline = time.monotonic() + timeout
            history = None
            while time.monotonic() < deadline:
                hist_resp = client.get(f"{host}/history/{prompt_id}")
                if hist_resp.status_code == 200:
                    data = hist_resp.json()
                    entry = data.get(prompt_id)
                    if entry and entry.get("status", {}).get("completed"):
                        history = entry
                        break
                time.sleep(interval)

            # 超时同样返回 None：生成可能仍在进行，但调用方不应无限等待
            if history is None:
                logger.warning("ComfyUI timeout waiting for prompt %s after %ss", prompt_id[:8], timeout)
                return None  # timeout

            # 3. 从 SaveImage 节点的输出中取出产物文件名等信息
            save_nodes = _find_nodes_by_class(patched, "SaveImage")
            # 无 SaveImage 节点就取不到产物文件名，直接判为失败
            if not save_nodes:
                logger.warning("No SaveImage node found in patched workflow")
                return None
            save_node_id = save_nodes[0][0]

            outputs = history.get("outputs", {})
            node_outputs = outputs.get(save_node_id, {})
            images = node_outputs.get("images", [])
            # 节点执行完成但没有产出图片，说明采样被跳过或出错
            if not images:
                logger.warning("No images in output node %s; available outputs: %s",
                               save_node_id, list(outputs.keys()))
                return None

            img_info = images[0]
            filename = img_info["filename"]
            subfolder = img_info.get("subfolder", "")
            img_type = img_info.get("type", "output")

            # 4. 通过 /view 拉取实际图片字节
            view_resp = client.get(
                f"{host}/view",
                params={"filename": filename, "type": img_type, "subfolder": subfolder},
            )
            view_resp.raise_for_status()
            image_bytes = view_resp.content

            # 5. 编码为 data URL，供前端直接作为 <img src> 使用
            b64 = base64.b64encode(image_bytes).decode("ascii")
            image_base64 = f"data:image/png;base64,{b64}"

            # 6. 组装元数据（文件名由提示词派生，保证可读性）
            slug = re.sub(r"[^\w一-鿿]+", "-", request.prompt.strip(), flags=re.UNICODE).strip("-")
            image_name = f"{slug or 'txt2img-generated'}.png"
            w, h = _parse_resolution(request.resolution)

            metadata: dict[str, Any] = {
                "engine": "comfyui",
                "prompt": request.prompt,
                "negative_prompt": request.negative_prompt,
                "resolution": request.resolution,
                "seed": request.seed,
                "style": request.style,
                "format": request.format,
                "canvas": {"width": w, "height": h},
                "comfyui_prompt_id": prompt_id,
                "generated_at": datetime.now(timezone.utc).isoformat(),
                "artifact": {"image_name": image_name, "byte_length": len(image_bytes)},
            }

            return GenerationArtifact(image_base64=image_base64, image_name=image_name, metadata=metadata)

    except Exception as exc:
        # 连接失败、超时、解析错误等一律吞掉并返回 None，保证上层能降级
        logger.warning("ComfyUI generation failed: %s", exc)
        return None


# ── Local stub (fallback) ──


def _local_stub_generate(
    request: GenerationRequest, attempted_workflows: list[str] | None = None
) -> GenerationArtifact:
    """ComfyUI 不可用时的本地兜底：用 Pillow 生成随机渐变 + 提示词文字。

    产物并非真实艺术字，仅用于保证接口可用与可诊断：``engine=local-studio``、
    ``fallback_tier=-1`` 是降级标志。种子取请求的 ``seed``，为 0 时由提示词派生，
    从而保证同一输入可复现同一张占位图。

    Args:
        request: 原始生成请求。
        attempted_workflows: 已尝试过的工作流名列表，写入元数据便于排错。

    Returns:
        带降级标志的 :class:`GenerationArtifact`。
    """
    from hashlib import sha256

    w, h = _parse_resolution(request.resolution)

    # seed=0 时由提示词哈希派生确定性种子，保证同输入同输出
    seed_value = request.seed or int.from_bytes(sha256(request.prompt.encode()).digest()[:8], "big")
    rng = random.Random(seed_value)

    def _random_color():
        """用当前 rng 生成一个随机 RGB 三元组。"""
        return tuple(int(c * 255) for c in (rng.random(), rng.random(), rng.random()))

    top = _random_color()
    bottom = _random_color()

    image = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)
    # 逐行在顶色与底色之间做线性插值，形成垂直渐变背景
    for y in range(h):
        ratio = y / max(1, h - 1)
        color = tuple(int(top[i] * (1 - ratio) + bottom[i] * ratio) for i in range(3))
        draw.line((0, y, w, y), fill=color)

    # 字体尽力而为：系统无 arial 时退回 PIL 内置位图字体，不影响流程
    try:
        font = ImageFont.truetype("arial.ttf", size=max(20, w // 24))
    except Exception:
        font = ImageFont.load_default()
    # 在左上角绘制提示词（截断到 80 字符），作为「这不是真实产物」的可见提示
    draw.text((16, 16), request.prompt[:80], font=font, fill=(255, 255, 255))

    buf = io.BytesIO()
    image.convert("RGB").save(buf, format="PNG")
    png_bytes = buf.getvalue()
    image_base64 = "data:image/png;base64," + base64.b64encode(png_bytes).decode("ascii")

    slug = re.sub(r"[^\w一-鿿]+", "-", request.prompt.strip(), flags=re.UNICODE).strip("-")
    image_name = f"{slug or 'txt2img-generated'}.png"

    # 元数据保留与真实产物同名的键，并额外标注降级来源
    metadata: dict[str, Any] = {
        "engine": "local-studio",
        "prompt": request.prompt,
        "negative_prompt": request.negative_prompt,
        "resolution": request.resolution,
        "seed": seed_value,
        "style": request.style,
        "format": request.format,
        "canvas": {"width": w, "height": h},
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "artifact": {"image_name": image_name, "byte_length": len(png_bytes)},
        # fallback_tier=-1 是「已降级到本地 stub」的判定标志
        "fallback_tier": -1,
        "workflow_used": "",
        "attempted_workflows": attempted_workflows or [],
        "prompt_synthesis": _build_stub_synthesis_block(request),
    }

    return GenerationArtifact(image_base64=image_base64, image_name=image_name, metadata=metadata)


# ── Resolution parser ──


def _parse_resolution(resolution: str) -> tuple[int, int]:
    """解析 ``"1024x1024"`` 或 ``"1024 x 1024"`` 为 ``(宽, 高)``。

    任何非法输入（格式错误、非正整数）都回退到 ``(1024, 1024)``，
    使调用方无需处理异常。
    """
    parts = resolution.strip().lower().replace(" ", "").split("x")
    if len(parts) != 2:
        return 1024, 1024
    try:
        w, h = int(parts[0]), int(parts[1])
        return (w, h) if w > 0 and h > 0 else (1024, 1024)
    except ValueError:
        return 1024, 1024


# ── Public entry point ──


# 工作流降级链：按内容类型选择优先级
_CHINESE_FALLBACK = ["qwen_image_2512_gguf", "test_z_image_turbo"]
_ENGLISH_FALLBACK = ["flux_schnell", "test_z_image_turbo"]
_MIXED_FALLBACK = ["test_z_image_turbo"]

_ROUTE_CHAINS: dict[str, list[str]] = {
    "chinese": _CHINESE_FALLBACK,
    "english": _ENGLISH_FALLBACK,
    "mixed": _MIXED_FALLBACK,
}


def _classify_text(text: str) -> str:
    """按字母构成把文本分为 'chinese' / 'english' / 'mixed'。

    只统计汉字与拉丁字母；数字、标点、符号、空白一律不参与判定，
    因此 ``冰川 100%`` 归为纯中文。无字母的输入（纯数字/符号/空）
    并入 'english'。

    判定规则由 :class:`TextAnalysis` 承载——路由与提示词模板共用同一份分析，
    不再各持一个正则。
    """
    return _analyze_text(text).script


def _extract_model_dependencies(workflow: dict, workflow_name: str = "") -> dict:
    """扫描工作流中的加载器节点，提取其引用的模型文件清单。

    结果按类别（checkpoints / unets / clip / vae / loras）去重排序，作为运行时
    模型依赖快照写入响应，便于结合交付文档核验模型来源。

    Args:
        workflow: 已 patch 的工作流。
        workflow_name: 工作流名，写入结果便于溯源。

    Returns:
        分类的模型依赖快照 dict。
    """
    models: dict[str, list[str]] = {
        "checkpoints": [],
        "unets": [],
        "clip": [],
        "vae": [],
        "loras": [],
    }

    # Class-type → input-field mapping for known loader nodes
    LOADER_FIELDS: dict[str, str] = {
        "CheckpointLoaderSimple": "ckpt_name",
        "UNETLoader": "unet_name",
        "UnetLoaderGGUF": "unet_name",
        "CLIPLoader": "clip_name",
        "DualCLIPLoader": "clip_name1",  # primary model; clip_name2 also captured
        "VAELoader": "vae_name",
        "LoraLoaderModelOnly": "lora_name",
    }

    # 扫描所有加载器节点，收集其引用的模型文件名（末尾统一去重）
    for node in workflow.values():
        ct = node.get("class_type", "")
        field = LOADER_FIELDS.get(ct)
        if field:
            model_name = node.get("inputs", {}).get(field, "")
            if model_name and isinstance(model_name, str):
                if ct == "DualCLIPLoader":
                    models["clip"].append(model_name)
                    clip2 = node.get("inputs", {}).get("clip_name2", "")
                    if clip2 and isinstance(clip2, str):
                        models["clip"].append(clip2)
                elif ct == "LoraLoaderModelOnly":
                    models["loras"].append(model_name)
                elif ct in ("UNETLoader", "UnetLoaderGGUF", "CheckpointLoaderSimple"):
                    target = "unets" if ct != "CheckpointLoaderSimple" else "checkpoints"
                    models[target].append(model_name)
                elif ct == "VAELoader":
                    models["vae"].append(model_name)
                elif ct == "CLIPLoader":
                    models["clip"].append(model_name)

    return {
        "checkpoints": sorted(set(models["checkpoints"])),
        "unets": sorted(set(models["unets"])),
        "clip": sorted(set(models["clip"])),
        "vae": sorted(set(models["vae"])),
        "loras": sorted(set(models["loras"])),
        "workflow_name": workflow_name,
        "note": "运行时模型依赖快照；请结合交付文档中的模型清单核验。",
    }


def generate_artwork(request: GenerationRequest) -> GenerationArtifact:
    """依次尝试工作流降级链，全部失败则用本地 Pillow stub。

    路由规则：

    - 用户显式指定了 ``workflow`` → 只尝试那一个；
    - 纯中文 → Qwen-Image → Z-Image；
    - 纯英文（含无字母的纯数字 / 符号）→ Flux → Z-Image；
    - 中英混排 → Z-Image（单层，无兜底）。

    Args:
        request: 生成请求。

    Returns:
        生成产物；成功时带 ``workflow_api`` 与模型依赖快照，降级时带 ``fallback_tier=-1``。
    """
    workflows_to_try: list[str]
    if request.workflow:
        workflows_to_try = [request.workflow]
    else:
        workflows_to_try = list(_ROUTE_CHAINS[_classify_text(request.text)])

    # 依次尝试候选工作流；任一成功即返回，全部失败才降级到本地 stub
    for idx, name in enumerate(workflows_to_try):
        try:
            # 加载模板 → 探测模型家族与负面能力 → 渲染提示词 → 注入参数 → 提交
            workflow_path = _resolve_workflow_path(name)
            workflow = _load_workflow(workflow_path)
            model = _detect_workflow_model(workflow)
            capability = _probe_negative_capability(workflow)
            plan = _plan_prompt(request, model, capability)
            patched = _patch_workflow(workflow, request, plan=plan)
            result = _call_comfyui_api(request, patched)
            if result is not None:
                logger.info("Workflow '%s' succeeded (tier=%d)", name, idx)
                _log_prompt_plan(plan, name)
                result.metadata["fallback_tier"] = idx
                result.metadata["workflow_used"] = name
                result.metadata["attempted_workflows"] = workflows_to_try
                result.metadata["prompt_synthesis"] = _build_synthesis_block(plan)
                model_deps = _extract_model_dependencies(patched, workflow_name=name)
                return GenerationArtifact(
                    image_base64=result.image_base64,
                    image_name=result.image_name,
                    metadata=result.metadata,
                    workflow_api=patched,
                    model_dependencies=model_deps,
                )
            logger.warning("Workflow '%s' returned None, trying next", name)
        except Exception as exc:
            # 单个工作流失败不致命，记录后继续尝试降级链中的下一个
            logger.warning("Workflow '%s' failed: %s", name, exc)
            continue

    # 降级链全部失败 → 用本地 stub 兜底，保证接口始终有响应
    return _local_stub_generate(request, attempted_workflows=workflows_to_try)
