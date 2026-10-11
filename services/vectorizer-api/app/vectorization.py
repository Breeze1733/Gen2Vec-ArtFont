"""
================================================================================
矢量化引擎核心模块 (Vectorization Engine)
================================================================================

本模块实现了艺术字位图到高质量 W3C 标准 SVG 矢量图的端到端转换与保真度评估流水线。

【核心算法原理与数学实现说明】

1. 图像预处理与抗锯齿轮廓边缘增强 (Image Preprocessing & Edge Refinement):
   - 形态学闭开滤波 (Morphological Filtering):
     闭运算: A • B = (A ⊕ B) ⊖ B，利用 3x3 结构元消除字符笔画内部微孔；
     开运算: A ∘ B = (A ⊖ B) ⊕ B，滤除轮廓毛刺与游离噪点。
   - 连通域拓扑几何面积滤波 (Connected Component Analysis with Stats):
     对二值前景掩模进行 8-邻域连通域标记，计算各独立连通块面积 S_i。设定动态面积阈值:
         S_min = max(2, (W × H) / D_min)
     过滤孤立微小飞溅噪点，保证矢量化轮廓的紧凑与连续。
   - 双重自适应高斯软化重映射 (Adaptive Gaussian Antialiasing Softening):
     经膨胀/腐蚀后的边缘进行两级二维高斯滤波:
         α_soft(x, y) = G_σ * α(x, y), 其中 G_σ = (1 / (2πσ²)) exp(-(x² + y²) / (2σ²))
     消除光栅位图离散网格导致的阶梯状阶跃（Stair-stepping / Jaggies）。

2. 基于三次贝塞尔样条 (Cubic Bézier Spline) 的连续路径拟合:
   - 三次贝塞尔曲线参数方程:
         B(t) = (1 - t)³ P₀ + 3(1 - t)² t P₁ + 3(1 - t) t² P₂ + t³ P₃,  t ∈ [0, 1]
     其中 P₀, P₃ 为曲线锚点 (Anchors)，P₁, P₂ 为方向控制点 (Control Points)。
   - 路径拟合优化准则:
     依据曲率特征点自适应划分段落。通过拐角阈值 θ_corner (corner_threshold) 判定转折硬角:
         cos θ = (v₁ · v₂) / (||v₁|| ||v₂||)
     若夹角变化超过阈值则保留为硬锚点，否则在连续导数约束下拟合平滑样条曲线。

3. 语义化图层解耦与拓扑分类算法 (Semantic Layer Decoupling & Topological Grouping):
   - 空间连通度与中心偏离度度量:
     设画布尺寸为 W × H，中心坐标 (C_x, C_y) = (W/2, H/2)，对角线半长 R_diag = √((W/2)² + (H/2)²)。
     对每个独立闭合路径 p，计算其包围盒质心 (c_x, c_y)，归一化中心距离:
         d_center(p) = √((c_x - C_x)² + (c_y - C_y)²) / R_diag
   - 相对面积比率:
         a_rel(p) = (w_p × h_p) / (W × H)
   - 色彩特征感知空间映射 (Perceptual Luminance & Saturation):
         Y(p) = 0.299 R + 0.587 G + 0.114 B,  V(p) = max(R,G,B) / 255
         S(p) = (max(R,G,B) - min(R,G,B)) / max(1, max(R,G,B))
   - 多层级拓扑分类规则:
     ① 投影阴影层 (layer-shadow): (Y < 0.22 或 V < 0.25) 且 a_rel < 0.70，或暗调底衬 (Y < 0.35, S < 0.25, c_y > C_y)；
     ② 散落装饰层 (layer-decorations): d_center > 0.35 且 a_rel < 0.10（外围游离花瓣、叶片、光芒等）；
     ③ 描边外框层 (layer-stroke): 底衬闭合大轮廓 (index=0 且 a_rel > 0.20) 或特征金边/高反差描边；
     ④ 文字主体层 (layer-main-text): 核心字形实体填充与主结构笔画。

4. 综合保真度评估模型 (Multi-dimensional Vector Fidelity Scoring):
   综合空间结构、边缘梯度与感知色彩三维指标计算保真度得分 (Fidelity ∈ [0, 100]):
       Fidelity = 0.50 × SSIM(I_orig, I_vec) + 0.30 × Corr(∇I_orig, ∇I_vec) + 0.20 × Corr(Hist_Lab_orig, Hist_Lab_vec)
   其中 SSIM 使用 11x11 局部窗口，Corr(∇I) 为 Sobel 边缘响应的皮尔逊相关系数。

5. W3C SVG 1.1 / 2.0 规范化与 Dublin Core / JSON-LD 双模元数据规范:
   输出符合标准 XML 语法的矢量代码，显式定义 viewBox 与尺寸，并在 <metadata> 节点内嵌入
   Dublin Core RDF 描述与结构化 JSON-LD 数据，支持主流专业矢量编辑软件无损分层导入。
================================================================================
"""

from __future__ import annotations

import base64
import json
import logging
import math
import os
import re
import sys
import tempfile
import time
import xml.etree.ElementTree as ET
from typing import Any
from xml.dom import minidom

import numpy as np
from PIL import Image

logger = logging.getLogger(__name__)

# 注册标准 XML 命名空间，保证 SVG 导出的前缀整洁规范
ET.register_namespace("", "http://www.w3.org/2000/svg")
ET.register_namespace("inkscape", "http://www.inkscape.org/namespaces/inkscape")
ET.register_namespace("rdf", "http://www.w3.org/1999/02/22-rdf-syntax-ns#")
ET.register_namespace("dc", "http://purl.org/dc/elements/1.1/")
ET.register_namespace("artfont", "https://gen2vec.artfont/schema#")

try:
    import cv2
except Exception:  # pragma: no cover
    cv2 = None

try:
    import resvg_py
except Exception:  # pragma: no cover
    resvg_py = None

try:
    import vtracer
except Exception:  # pragma: no cover
    vtracer = None


# 预设参数表。键含义：cp=color_precision，fs=filter_speckle，ct=corner_threshold，
# lt=length_threshold，ld=layer_difference，scale=上采样倍率。
# 由 clean → ultra 逐步提升细节保留程度，代价是输出体积与路径数量同步增长。
PRESET_CONFIG: dict[str, dict[str, int]] = {
    "clean": {"cp": 4, "fs": 24, "ct": 90, "lt": 16, "ld": 28, "scale": 1},
    "balanced": {"cp": 6, "fs": 10, "ct": 60, "lt": 8, "ld": 16, "scale": 2},
    "detailed": {"cp": 7, "fs": 4, "ct": 45, "lt": 5, "ld": 12, "scale": 2},
    "ultra": {"cp": 8, "fs": 2, "ct": 30, "lt": 3, "ld": 8, "scale": 2},
}

# 追踪前清理配置。键含义：alpha_floor=Alpha 硬阈值下限；min_area_divisor=最小
# 连通块面积的分母（面积阈值 = W×H / 该值，值越大保留的碎块越少）；morph=形态学
# 闭开迭代次数；median=是否中值去噪；solid_alpha=Alpha 是否二值化；smooth_mask=
# 是否平滑掩模；snap_near_white=是否把近白像素吸附为纯白。各预设越靠后越激进。
TRACE_CLEANUP_CONFIG: dict[str, dict[str, int | bool]] = {
    "clean": {
        "alpha_floor": 48,
        "min_area_divisor": 1500,
        "morph": 0,
        "median": True,
        "solid_alpha": True,
        "smooth_mask": True,
        "snap_near_white": False,
    },
    "balanced": {
        "alpha_floor": 24,
        "min_area_divisor": 3600,
        "morph": 0,
        "median": False,
        "solid_alpha": False,
        "smooth_mask": False,
        "snap_near_white": False,
    },
    "detailed": {
        "alpha_floor": 10,
        "min_area_divisor": 12000,
        "morph": 0,
        "median": False,
        "solid_alpha": False,
        "smooth_mask": False,
        "snap_near_white": False,
    },
    "ultra": {
        "alpha_floor": 4,
        "min_area_divisor": 24000,
        "morph": 0,
        "median": False,
        "solid_alpha": False,
        "smooth_mask": False,
        "snap_near_white": False,
    },
}

# 路径 d 属性中的数值提取（含科学计数法），用于快速估算包围盒
_COORD_RE = re.compile(r"[-+]?(?:\d*\.\d+|\d+)(?:[eE][-+]?\d+)?")
# transform="translate(tx, ty)" 的解析正则，用于还原路径的平移偏移
_TRANSLATE_RE = re.compile(r"translate\(\s*([-+]?(?:\d*\.\d+|\d+))\s*,\s*([-+]?(?:\d*\.\d+|\d+))\s*\)")

# 语义图层定义，按 Z-Order 自底向上排列：(图层 id, Inkscape 图层标签)
LAYER_DEFINITIONS: list[tuple[str, str]] = [
    ("layer-shadow", "投影阴影"),
    ("layer-stroke", "描边轮廓"),
    ("layer-main-text", "主体文字"),
    ("layer-decorations", "装饰图形"),
]


def _safe_int(value: Any, default: int) -> int:
    """尽力将 ``value`` 转为 ``int``，失败时返回 ``default``（消化松散的 JSON 参数）。"""
    try:
        return int(value)
    except Exception:
        return default


def _resolve_vector_params(vector: dict[str, Any]) -> dict[str, int | str]:
    """合并预设默认值与显式覆盖，返回裁剪到合法区间的最终参数。

    先用 ``preset`` 选中 ``PRESET_CONFIG`` 中的一组默认值，再用请求里非空的字段
    逐项覆盖，最后对每个参数做区间钳制（如 color_precision ∈ [1, 8]），确保下游
    vtracer 收到的始终是合法值。未知 preset 回退为 ``balanced``。
    """
    preset = str(vector.get("preset", "balanced")).lower()
    if preset not in PRESET_CONFIG:
        preset = "balanced"

    defaults = PRESET_CONFIG[preset]
    cp = _safe_int(vector.get("color_precision"), defaults["cp"])
    fs = _safe_int(vector.get("filter_speckle"), defaults["fs"])
    ct = _safe_int(vector.get("corner_threshold"), defaults["ct"])
    lt = _safe_int(vector.get("length_threshold"), defaults["lt"])
    ld = _safe_int(vector.get("layer_difference"), defaults["ld"])
    scale = _safe_int(vector.get("scale"), defaults["scale"])

    return {
        "preset": preset,
        "cp": max(1, min(8, cp)),
        "fs": max(0, min(64, fs)),
        "ct": max(1, min(180, ct)),
        "lt": max(1, min(64, lt)),
        "ld": max(1, min(64, ld)),
        "scale": max(1, min(4, scale)),
    }


def _png_bytes_to_data_url(png_bytes: bytes) -> str:
    """把 PNG 字节编码为 ``data:image/png;base64,...`` Data URL。"""
    b64 = base64.b64encode(png_bytes).decode("ascii")
    return f"data:image/png;base64,{b64}"


def _svg_to_png_bytes(svg_text: str, width: int | None = None, height: int | None = None) -> bytes:
    """用 resvg-py 把 SVG 文本回渲染为 PNG 字节（用于预览图与保真度评估）。

    Raises:
        RuntimeError: 运行环境未安装 resvg-py。
    """
    if resvg_py is None:
        raise RuntimeError("resvg-py is not installed. Cannot generate preview PNG.")
    return resvg_py.svg_to_bytes(svg_string=svg_text, width=width, height=height)


def _parse_path_bbox(d: str, transform: str = "") -> tuple[float, float, float, float]:
    """从路径 ``d`` 与 ``transform`` 估算包围盒 ``(x1, y1, x2, y2)``。

    这是面向图层分类的轻量近似：直接扫描路径数据中的坐标数值取极值，不解析
    贝塞尔控制点构成的真实曲率边界，因此对曲线路径会有少量偏差（对分类足够）。
    ``transform`` 仅识别 ``translate``，其余变换忽略。
    """
    tx, ty = 0.0, 0.0
    if transform:
        m = _TRANSLATE_RE.search(transform)
        if m:
            tx, ty = float(m.group(1)), float(m.group(2))
    # 提取路径中的所有数值，按「x, y」成对解读并叠加平移偏移
    nums = [float(x) for x in _COORD_RE.findall(d)]
    if len(nums) < 2:
        return tx, ty, tx + 1.0, ty + 1.0
    xs = [nums[i] + tx for i in range(0, len(nums) - 1, 2)]
    ys = [nums[i + 1] + ty for i in range(0, len(nums) - 1, 2)]
    if not xs or not ys:
        return tx, ty, tx + 1.0, ty + 1.0
    return min(xs), min(ys), max(xs), max(ys)


def _parse_fill_rgb(fill_str: str) -> tuple[int, int, int]:
    """把 ``fill`` 颜色字符串解析为 ``(r, g, b)`` 三元组。

    支持 ``#rrggbb`` / ``#rgb`` 十六进制与 ``rgb(...)`` 函数式写法；无法识别时
    返回中性灰 ``(128, 128, 128)`` 作为兜底，保证后续色彩判定不因解析失败中断。
    """
    fill = (fill_str or "").strip()
    if fill.startswith("#"):
        h = fill.lstrip("#")
        # 6 位十六进制：#rrggbb，逐两位解析
        if len(h) == 6:
            try:
                return int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)
            except ValueError:
                pass
        # 3 位十六进制：#rgb，每位重复一次展开为 6 位
        elif len(h) == 3:
            try:
                return int(h[0] * 2, 16), int(h[1] * 2, 16), int(h[2] * 2, 16)
            except ValueError:
                pass
    elif fill.startswith("rgb"):
        # 函数式写法：抽取其中的数字，取前三个作为 R/G/B
        nums = [int(x) for x in re.findall(r"\d+", fill)]
        if len(nums) >= 3:
            return nums[0], nums[1], nums[2]
    return 128, 128, 128


def _classify_svg_path(
    path_elem: ET.Element,
    canvas_w: int,
    canvas_h: int,
    path_idx: int,
    total_paths: int,
) -> str:
    """按几何与色彩特征把单条 SVG 路径归类到语义图层。

    判定依据：包围盒相对面积、包围盒质心到画布中心的归一化距离、填充色的感知
    亮度 / 饱和度，以及路径在整图中的序号。依次匹配「投影阴影 → 散落装饰 →
    描边轮廓 → 主体文字」，命中即返回；均不匹配时归入主体文字层。

    Args:
        path_elem: 待分类的 ``<path>`` 元素。
        canvas_w: 画布宽度（用于归一化）。
        canvas_h: 画布高度。
        path_idx: 路径在文档中的序号（0 表示最底层）。
        total_paths: 路径总数。

    Returns:
        图层 id，取值为 ``LAYER_DEFINITIONS`` 中的键。
    """
    d = path_elem.get("d", "")
    tr = path_elem.get("transform", "")
    fill = path_elem.get("fill", "")

    x1, y1, x2, y2 = _parse_path_bbox(d, tr)
    pw = max(1.0, x2 - x1)
    ph = max(1.0, y2 - y1)
    # 相对面积：包围盒占整幅画布的比例，用于区分大底衬与小装饰
    rel_area = (pw * ph) / float(max(1, canvas_w * canvas_h))

    # 归一化中心距离：质心到画布中心的距离除以半对角线，取值约在 [0, 1]
    cx, cy = canvas_w / 2.0, canvas_h / 2.0
    diag = math.sqrt(cx * cx + cy * cy)
    pcx, pcy = (x1 + x2) / 2.0, (y1 + y2) / 2.0
    d_center = math.sqrt((pcx - cx) ** 2 + (pcy - cy) ** 2) / float(max(1.0, diag))

    # 感知亮度（Rec.601 加权）与 HSV 明度 / 饱和度，用于识别暗调阴影与高饱和金边
    r, g, b = _parse_fill_rgb(fill)
    lum = (0.299 * r + 0.587 * g + 0.114 * b) / 255.0
    max_c, min_c = max(r, g, b), min(r, g, b)
    val = max_c / 255.0
    sat = (max_c - min_c) / float(max(1, max_c))

    # 1. 投影阴影判定 (暗调且位于下方或底层)
    # 极暗或极低明度的中大型色块优先判为阴影，避免被误当作主体笔画
    if (lum < 0.22 or val < 0.25) and rel_area < 0.70:
        return "layer-shadow"
    if lum < 0.35 and sat < 0.25 and pcy > cy and rel_area < 0.50:
        return "layer-shadow"

    # 2. 外部散落装饰判定 (距离中心远、面积小)
    if d_center > 0.35 and rel_area < 0.10:
        return "layer-decorations"

    # 3. 描边轮廓判定 (外围底衬基底 或 特征金边/高反差描边)
    is_gold_edge = r > 180 and g > 140 and b < 90 and sat > 0.35
    is_bright_border = lum > 0.90 and sat < 0.15 and rel_area > 0.10
    is_base_chassis = path_idx == 0 and total_paths >= 3 and rel_area > 0.20
    if is_gold_edge or is_bright_border or is_base_chassis:
        return "layer-stroke"

    # 4. 默认归入主体文字层
    return "layer-main-text"


def _populate_metadata_element(meta_elem: ET.Element, metadata: dict[str, Any]) -> None:
    """向 ``<metadata>`` 元素内填充 Dublin Core RDF 与 JSON-LD 双模元数据。

    RDF 部分以 ``dc:`` 命名空间记录标题、作者、日期、格式，并追加 ``artfont:``
    自定义字段（文字、提示词、seed、预设、保真度、引擎）；同时嵌入一段 schema.org
    ``VisualArtwork`` 的 JSON-LD，便于外部工具机读。纯矢量化（upload）场景不写入
    seed，避免记录无意义的生成参数。
    """
    rdf = ET.SubElement(meta_elem, "{http://www.w3.org/1999/02/22-rdf-syntax-ns#}RDF")
    desc = ET.SubElement(
        rdf,
        "{http://www.w3.org/1999/02/22-rdf-syntax-ns#}Description",
        {"{http://www.w3.org/1999/02/22-rdf-syntax-ns#}about": ""},
    )

    # 从元数据中提取各子块，缺失时退化为空字典以保证字段访问安全
    gen = metadata.get("generation", {})
    params = metadata.get("params", {})
    quality = metadata.get("quality", {})
    stats = metadata.get("stats", {})

    # source_type 缺失时按「是否含提示词 / 文字」推断来源，以区分纯矢量化与文生图
    source_type = metadata.get("source_type") or ("upload" if not gen.get("prompt") and not gen.get("text") else "generated")
    is_pure_vectorize = source_type == "upload"

    text = gen.get("text", "")
    prompt = gen.get("prompt", "")
    seed = gen.get("seed")
    preset = params.get("preset", "balanced")
    fidelity = quality.get("svg_fidelity")
    created_at = metadata.get("created_at") or time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())

    title = str(text or prompt or "ArtFont Vector")

    title_el = ET.SubElement(desc, "{http://purl.org/dc/elements/1.1/}title")
    title_el.text = title

    creator_el = ET.SubElement(desc, "{http://purl.org/dc/elements/1.1/}creator")
    creator_el.text = "Gen2Vec ArtFont System"

    date_el = ET.SubElement(desc, "{http://purl.org/dc/elements/1.1/}date")
    date_el.text = str(created_at)

    format_el = ET.SubElement(desc, "{http://purl.org/dc/elements/1.1/}format")
    format_el.text = "image/svg+xml"

    if text:
        t_el = ET.SubElement(desc, "{https://gen2vec.artfont/schema#}text")
        t_el.text = str(text)
    if prompt:
        p_el = ET.SubElement(desc, "{https://gen2vec.artfont/schema#}prompt")
        p_el.text = str(prompt)
    # 纯矢量化不需要 seed；文生图模式下哪怕 seed 为 0 也记录
    if not is_pure_vectorize and seed is not None:
        s_el = ET.SubElement(desc, "{https://gen2vec.artfont/schema#}seed")
        s_el.text = str(seed)

    pr_el = ET.SubElement(desc, "{https://gen2vec.artfont/schema#}preset")
    pr_el.text = str(preset)

    if fidelity is not None:
        f_el = ET.SubElement(desc, "{https://gen2vec.artfont/schema#}fidelity")
        f_el.text = f"{fidelity:.1f}"

    engine_el = ET.SubElement(desc, "{https://gen2vec.artfont/schema#}engine")
    engine_el.text = str(metadata.get("engine", "vectorizer-api"))

    # JSON-LD 结构化标签
    script = ET.SubElement(meta_elem, "script", {"type": "application/ld+json"})
    parameters = {
        "preset": preset,
        "elapsedMs": stats.get("elapsed_ms"),
    }
    if not is_pure_vectorize and seed is not None:
        parameters["seed"] = seed

    json_summary = {
        "@context": "https://schema.org",
        "@type": "VisualArtwork",
        "name": title,
        "description": prompt,
        "artform": "Vector Art",
        "artMedium": "SVG",
        "generator": {
            "@type": "SoftwareApplication",
            "name": "Gen2Vec ArtFont",
            "version": "1.0.1",
        },
        "fidelityScore": fidelity,
        "parameters": parameters,
    }
    script.text = json.dumps(json_summary, ensure_ascii=False)


def restructure_and_group_svg(
    raw_svg_text: str,
    canvas_width: int,
    canvas_height: int,
    viewbox_width: int | None = None,
    viewbox_height: int | None = None,
    metadata: dict[str, Any] | None = None,
) -> str:
    """重构 vtracer 输出的平铺 SVG，构建 W3C 规范视口、语义解耦图层与内嵌元数据。

    vtracer 的原始输出把所有路径平铺在一层、缺少规范视口与元数据。本函数解析该
    SVG 后：新建带 ``version`` / ``viewBox`` / ``width`` / ``height`` 的根节点，
    注入 ``<metadata>``，按语义把路径分入四个 ``<g>`` 图层（并保证主体文字层非空），
    最后格式化输出带缩进的 XML。

    Args:
        raw_svg_text: vtracer 生成的原始 SVG 文本。
        canvas_width: 输出画布宽度（用户可见尺寸）。
        canvas_height: 输出画布高度。
        viewbox_width: 实际路径坐标系宽度（上采样后为追踪尺寸），缺省取原始尺寸。
        viewbox_height: 实际路径坐标系高度。
        metadata: 需写入 ``<metadata>`` 的结构化元数据。

    Returns:
        重构并格式化后的 SVG 文本；解析失败时原样返回输入。
    """
    try:
        raw_root = ET.fromstring(raw_svg_text.encode("utf-8"))
    except Exception as exc:
        logger.warning("Failed to parse raw SVG XML: %s", exc)
        return raw_svg_text

    # 提取所有绘图路径。优先按 SVG 命名空间查找，查找不到时回退到无命名空间写法，
    # 以兼容不同 vtracer 版本导出的 XML 差异。
    paths = list(raw_root.findall(".//{http://www.w3.org/2000/svg}path"))
    if not paths:
        paths = list(raw_root.findall(".//path"))

    # 保留可能存在的 defs（渐变 / 裁剪等被引用的定义），同样兼容两种命名空间写法
    defs = list(raw_root.findall(".//{http://www.w3.org/2000/svg}defs")) or list(raw_root.findall(".//defs"))

    # 解析实际路径坐标系范围：若发生过上采样，坐标系为 trace 尺寸而非画布尺寸。
    # 调用方显式传入 viewbox 尺寸时优先采用，否则回退到原始 SVG 的 width/height。
    raw_w_str = raw_root.get("width")
    raw_h_str = raw_root.get("height")
    vw = viewbox_width or (int(float(raw_w_str)) if raw_w_str else canvas_width)
    vh = viewbox_height or (int(float(raw_h_str)) if raw_h_str else canvas_height)
    actual_viewbox = raw_root.get("viewBox") or f"0 0 {vw} {vh}"

    new_root = ET.Element(
        "{http://www.w3.org/2000/svg}svg",
        {
            "version": "1.1",
            "viewBox": actual_viewbox,
            "width": str(canvas_width),
            "height": str(canvas_height),
        },
    )

    # 1. 注入 <metadata>：承载 Dublin Core RDF 与 JSON-LD 双模元数据
    meta_elem = ET.SubElement(new_root, "{http://www.w3.org/2000/svg}metadata", {"id": "gen2vec-metadata"})
    if metadata:
        _populate_metadata_element(meta_elem, metadata)

    # 2. 原样搬运原始 defs（渐变、裁剪等定义），避免丢失被引用的资源
    for d_elem in defs:
        new_root.append(d_elem)

    # 3. 语义化图层分类
    grouped_paths: dict[str, list[ET.Element]] = {
        "layer-shadow": [],
        "layer-stroke": [],
        "layer-main-text": [],
        "layer-decorations": [],
    }

    # 3. 逐条路径分类：用追踪坐标系尺寸 vw/vh 做归一化，保证与路径坐标同尺度
    total = len(paths)
    for idx, p in enumerate(paths):
        category = _classify_svg_path(p, vw, vh, idx, total)
        grouped_paths[category].append(p)

    # 兜底：若分类后主体文字层为空，从其它图层借调一条路径，避免出现空的主图层
    if not grouped_paths["layer-main-text"] and paths:
        for cat in ["layer-stroke", "layer-shadow", "layer-decorations"]:
            if grouped_paths[cat]:
                grouped_paths["layer-main-text"].append(grouped_paths[cat].pop(0))
                break

    # 4. 按 LAYER_DEFINITIONS 的 Z-Order 创建 <g> 图层；空图层直接跳过
    for layer_id, label in LAYER_DEFINITIONS:
        layer_items = grouped_paths[layer_id]
        if not layer_items:
            continue
        g_elem = ET.SubElement(
            new_root,
            "{http://www.w3.org/2000/svg}g",
            {
                "id": layer_id,
                "{http://www.inkscape.org/namespaces/inkscape}label": label,
                "{http://www.inkscape.org/namespaces/inkscape}groupmode": "layer",
            },
        )
        for item in layer_items:
            g_elem.append(item)

    # 5. 格式化输出：经 minidom 缩进美化，并剔除全空白行
    xml_bytes = ET.tostring(new_root, encoding="utf-8")
    parsed = minidom.parseString(xml_bytes)
    pretty = parsed.toprettyxml(indent="  ", encoding="utf-8").decode("utf-8")
    lines = [line for line in pretty.splitlines() if line.strip()]
    return "\n".join(lines) + "\n"


def inject_svg_metadata(svg_text: str, metadata: dict[str, Any]) -> str:
    """在已生成的 SVG 文本中安全注入或刷新元数据节点。

    若已存在 ``<metadata>`` 则清空后重填，否则在根节点首位插入新节点。任何解析
    或写入异常都会被捕获并降级为「原样返回」，确保元数据注入失败不会破坏整份 SVG。

    Args:
        svg_text: 目标 SVG 文本。
        metadata: 要注入的结构化元数据。

    Returns:
        注入元数据后的 SVG 文本；失败时返回原始文本。
    """
    try:
        root = ET.fromstring(svg_text.encode("utf-8"))
        meta_elem = None
        for child in list(root):
            tag = child.tag.split("}")[-1] if "}" in child.tag else child.tag
            if tag == "metadata":
                meta_elem = child
                break

        # 无既有 <metadata> 时在根节点首位新建；否则清空原内容后复用该节点
        if meta_elem is None:
            meta_elem = ET.Element("{http://www.w3.org/2000/svg}metadata", {"id": "gen2vec-metadata"})
            root.insert(0, meta_elem)
        else:
            meta_elem.clear()
            meta_elem.attrib["id"] = "gen2vec-metadata"

        _populate_metadata_element(meta_elem, metadata)

        xml_bytes = ET.tostring(root, encoding="utf-8")
        parsed = minidom.parseString(xml_bytes)
        pretty = parsed.toprettyxml(indent="  ", encoding="utf-8").decode("utf-8")
        lines = [line for line in pretty.splitlines() if line.strip()]
        return "\n".join(lines) + "\n"
    except Exception as exc:
        logger.warning("inject_svg_metadata failed: %s", exc)
        return svg_text


def _calculate_svg_fidelity(source_img: Image.Image, preview_png_bytes: bytes) -> float | None:
    """计算 SVG 矢量化还原度（0–100，越高越接近原图）。

    评分策略：

    - 近乎全透明（前景占比 < 0.5%）时直接返回 100，避免无内容可比时产生噪声分；
    - 背景统一填中性灰，使 SSIM 只反映前景区域的结构差异；
    - 对比前对两侧做自适应高斯模糊，并以大窗口 SSIM 计算，对矢量化固有的
      抗锯齿差异保持宽容；
    - 最终得分为 SSIM(0.5) + 边缘梯度相关性(0.3) + 前景色分布相关性(0.2) 的加权和。

    Args:
        source_img: 预处理后的透明底原图。
        preview_png_bytes: SVG 回渲染得到的预览 PNG 字节。

    Returns:
        还原度得分（保留一位小数）；评估过程出错时返回 ``None``。
    """
    try:
        from io import BytesIO

        from skimage.metrics import structural_similarity as ssim
        from scipy.ndimage import gaussian_filter

        # ── 1. 前景掩码 ──────────────────────────────────────────
        # 以 Alpha ≥ 3 作为前景判据，容忍抗锯齿过渡带上的极低透明度像素
        source_rgba = source_img.convert("RGBA")
        alpha = np.array(source_rgba)[:, :, 3]
        fg_mask = alpha >= 3

        # 前景占比过低（近乎全透明）时无有效内容可比，直接判定为完美还原
        fg_ratio = fg_mask.sum() / fg_mask.size
        if fg_ratio < 0.005:
            return 100.0  # 几乎全透明，完美匹配

        # ── 2. 对齐 + 统一背景 ────────────────────────────────────
        # 预览图尺寸可能与原图不一致（viewBox 与画布尺寸解耦），统一缩放到原图尺寸
        source_rgb = source_img.convert("RGB")
        preview_rgba = Image.open(BytesIO(preview_png_bytes)).convert("RGBA")
        if preview_rgba.size != source_rgb.size:
            preview_rgba = preview_rgba.resize(source_rgb.size, Image.Resampling.LANCZOS)
        preview_rgb = preview_rgba.convert("RGB")

        original_np = np.array(source_rgb).astype(np.float64)
        vector_np = np.array(preview_rgb).astype(np.float64)

        # 背景占比适中时把两侧背景统一填成中性灰，使 SSIM 只反映前景差异
        if 0.02 < (1 - fg_ratio) < 0.98:
            neutral = np.array([128.0, 128.0, 128.0], dtype=np.float64)
            original_np[~fg_mask] = neutral
            vector_np[~fg_mask] = neutral

        # ── 3. 高斯预模糊 ────────────────────────────────────────
        # 模糊半径随图像尺寸自适应，用于弱化矢量化抗锯齿带来的高频差异
        h, w = original_np.shape[:2]
        min_dim = min(h, w)
        sigma = max(0.6, min(1.2, min_dim / 800.0))
        original_blur = gaussian_filter(original_np, sigma=(sigma, sigma, 0))
        vector_blur = gaussian_filter(vector_np, sigma=(sigma, sigma, 0))

        # ── 4. SSIM（大窗口，抗局部抖动）───────────────────────────
        # 窗口尺寸受图像短边限制，逐级下调以保证小图也能计算
        if min_dim >= 11:
            win_size = 11
        elif min_dim >= 7:
            win_size = 7
        elif min_dim >= 5:
            win_size = 5
        else:
            win_size = 3

        try:
            ssim_val = ssim(
                original_blur, vector_blur,
                channel_axis=2,
                win_size=win_size,
                data_range=255,
            )
        except TypeError:
            ssim_val = ssim(
                original_blur, vector_blur,
                multichannel=True,
                win_size=win_size,
                data_range=255,
            )
        ssim_val = max(0.0, float(ssim_val))

        # ── 5. 梯度相关性（替代绝对差，容忍边缘微小偏移）──────────
        # 用 Sobel 边缘响应的皮尔逊相关系数衡量轮廓走向一致性
        from skimage.filters import sobel

        edge_orig = sobel(original_blur.mean(axis=2))
        edge_vec = sobel(vector_blur.mean(axis=2))
        edge_corr = np.corrcoef(edge_orig.flat, edge_vec.flat)[0, 1]
        if np.isnan(edge_corr):
            edge_corr = 1.0
        edge_score = max(0.0, float(edge_corr))

        # ── 6. 前景色分布（只看主体区域，排除背景灰）───────────────
        # 在 CIELAB 的 a/b 色度通道上比较前景像素直方图相关性
        try:
            from skimage.color import rgb2lab

            s_lab = rgb2lab(original_blur / 255.0)
            v_lab = rgb2lab(vector_blur / 255.0)
            color_score = 0.0
            for ch in (1, 2):
                s_fg = s_lab[:, :, ch][fg_mask]
                v_fg = v_lab[:, :, ch][fg_mask]
                if len(s_fg) < 50:
                    color_score += 0.5
                    continue
                s_hist, _ = np.histogram(s_fg, bins=64, range=(-128, 128))
                v_hist, _ = np.histogram(v_fg, bins=64, range=(-128, 128))
                corr = np.corrcoef(s_hist, v_hist)[0, 1]
                color_score += max(0.0, corr) / 2.0
        except Exception:
            color_score = ssim_val

        # ── 7. 融合 ──────────────────────────────────────────────
        # 加权融合：结构相似度占主导，边缘与色彩各占次要权重，输出 [0, 100]
        fidelity = ssim_val * 0.50 + edge_score * 0.30 + color_score * 0.20
        score = round(max(0.0, min(100.0, fidelity * 100.0)), 1)
        return score
    except Exception:
        return None


def _remove_small_alpha_components(alpha: np.ndarray, min_area: int) -> np.ndarray:
    """移除 Alpha 掩模中面积小于 ``min_area`` 的孤立连通块。

    采用 8-邻域连通域分析统计各前景块面积，仅保留面积达标者，用于滤除追踪前的
    碎点噪声。未安装 OpenCV 或阈值过小时直接返回原掩模。
    """
    if cv2 is None or min_area <= 1:
        return alpha

    # 二值化后做 8-邻域连通域标记；label 0 恒为背景，故从 1 开始遍历
    mask = (alpha > 0).astype(np.uint8)
    num_labels, labels, stats, _ = cv2.connectedComponentsWithStats(mask, connectivity=8)
    if num_labels <= 1:
        return alpha

    # 累积面积达标的连通块，其余在输出中一并清零
    keep = np.zeros(mask.shape, dtype=bool)
    for label in range(1, num_labels):
        if int(stats[label, cv2.CC_STAT_AREA]) >= min_area:
            keep |= labels == label

    cleaned = alpha.copy()
    cleaned[~keep] = 0
    return cleaned


def _smooth_binary_alpha(alpha: np.ndarray, alpha_floor: int) -> np.ndarray:
    """对 Alpha 掩模做两级高斯模糊 + 阈值，获得平滑的抗锯齿边缘。

    先以较大 sigma 平滑再二值化得到干净轮廓，再用小 sigma 二次软化过渡带；
    掩模外区域强制清零，掩模内低于 ``alpha_floor`` 的弱透明像素抬升到下限，
    避免追踪时产生断续的细碎路径。
    """
    if cv2 is None:
        return alpha

    # 第一级：较大 sigma 模糊后二值化，抹平轮廓上的细小凹凸
    mask = (alpha > 0).astype(np.uint8) * 255
    mask = cv2.GaussianBlur(mask, (0, 0), sigmaX=1.35, sigmaY=1.35)
    _, mask = cv2.threshold(mask, 128, 255, cv2.THRESH_BINARY)

    # 第二级：小 sigma 模糊 + 二值化，得到更柔和的抗锯齿过渡边缘
    mask = cv2.GaussianBlur(mask, (0, 0), sigmaX=0.55, sigmaY=0.55)
    _, mask = cv2.threshold(mask, 128, 255, cv2.THRESH_BINARY)

    # 掩模外清零；掩模内低于下限的弱透明像素抬升到下限，避免追踪出现断点
    out = alpha.copy()
    out[mask == 0] = 0
    out[(mask > 0) & (out < alpha_floor)] = alpha_floor
    return out


def _prepare_trace_input(img: Image.Image, params: dict[str, int | str]) -> Image.Image:
    """为 vtracer 追踪准备输入位图（按预设执行清理）。

    处理顺序：Alpha 硬阈值截断 → 可选中值去噪 → 可选形态学闭开运算 → RGB 双边
    滤波 → 移除小连通块 → 可选掩模平滑 / Alpha 二值化 / 近白吸附。透明区域的 RGB
    统一填白，避免 vtracer 把不可见颜色也当作独立色层追踪。

    Args:
        img: 待清理的 RGBA 图像。
        params: ``_resolve_vector_params`` 产出的参数字典（此处取其中的 ``preset``）。

    Returns:
        清理后的 RGBA 图像。
    """
    rgba = img.convert("RGBA")
    arr = np.array(rgba)
    alpha = arr[:, :, 3].copy()
    preset = str(params["preset"])
    cleanup = TRACE_CLEANUP_CONFIG.get(preset, TRACE_CLEANUP_CONFIG["balanced"])

    # 1. Alpha 硬阈值：低于下限的弱透明像素直接判为背景
    alpha_floor = int(cleanup["alpha_floor"])
    alpha[alpha < alpha_floor] = 0

    if cv2 is not None:
        if bool(cleanup["median"]):
            alpha = cv2.medianBlur(alpha, 3)

        # 可选形态学闭开运算：先闭运算填内部微孔，再开运算去轮廓毛刺
        morph_iterations = int(cleanup["morph"])
        if morph_iterations > 0:
            kernel = np.ones((3, 3), np.uint8)
            mask = (alpha > 0).astype(np.uint8) * 255
            mask = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, kernel, iterations=morph_iterations)
            mask = cv2.morphologyEx(mask, cv2.MORPH_OPEN, kernel, iterations=morph_iterations)
            alpha[mask == 0] = 0
            alpha[(mask > 0) & (alpha < alpha_floor)] = alpha_floor

        # RGB 双边滤波：平滑色彩噪点但保留笔画边缘，避免色层被切碎
        rgb = arr[:, :, :3]
        rgb = cv2.bilateralFilter(rgb, d=5, sigmaColor=32, sigmaSpace=24)
        arr[:, :, :3] = rgb

    # 最小连通块面积随画布尺寸缩放，避免大图上残留碎点
    min_area = max(2, int((rgba.width * rgba.height) / int(cleanup["min_area_divisor"])))
    alpha = _remove_small_alpha_components(alpha, min_area)

    if bool(cleanup["smooth_mask"]):
        alpha = _smooth_binary_alpha(alpha, alpha_floor)

    if bool(cleanup["solid_alpha"]):
        alpha[alpha > 0] = 255

    arr[:, :, 3] = alpha
    # 透明区域 RGB 填白：防止 vtracer 把不可见颜色也追踪成独立色层
    arr[alpha == 0, :3] = 255

    if bool(cleanup["snap_near_white"]) and cv2 is not None:
        visible = alpha > 0
        hsv = cv2.cvtColor(arr[:, :, :3], cv2.COLOR_RGB2HSV)
        near_white = visible & (hsv[:, :, 1] <= 34) & (hsv[:, :, 2] >= 220)
        arr[near_white, :3] = 245

    return Image.fromarray(arr, mode="RGBA")


def _deep_merge_metadata(target: dict[str, Any], source: dict[str, Any]) -> None:
    """把 ``source`` 递归合并进 ``target``（嵌套 dict 逐层合并，其余键直接覆盖）。

    用于把调用方传入的 ``metadata_context`` 叠加到默认元数据模板上，保留模板中
    未被覆盖的默认字段。
    """
    for k, v in source.items():
        # 两侧同为 dict 时递归下探，否则（含类型不一致）直接以 source 覆盖
        if isinstance(v, dict) and isinstance(target.get(k), dict):
            _deep_merge_metadata(target[k], v)
        else:
            target[k] = v


def vectorize_image(
    transparent_image: Image.Image,
    vector: dict[str, Any],
    metadata_context: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """矢量化主流程：清理 →（可选上采样）→ vtracer 追踪 → 重构分层 SVG → 预览与保真度评估。

    主要步骤：

    1. 依据图像尺寸自适应调整上采样倍率（超大图强制降为 1，避免耗时与内存失控）；
    2. 在临时目录写出清理后的 PNG，调用 vtracer 生成原始 SVG；
    3. 组装元数据模板并合并调用方上下文；
    4. ``restructure_and_group_svg`` 重构为标准分层 SVG（含 ``<metadata>``）；
    5. 用 resvg-py 回渲染预览图，计算 SVG 还原度并回填元数据。

    Args:
        transparent_image: 预处理后的透明底 RGBA 图像。
        vector: 矢量化参数字典。
        metadata_context: 需合并进输出元数据的额外上下文（可选）。

    Raises:
        RuntimeError: 运行环境未安装 vtracer，或未找到受支持的转换函数。

    Returns:
        含 ``svg``（矢量文本）、``preview_png``（Data URL）与 ``metadata`` 的字典。
    """
    if vtracer is None:
        raise RuntimeError("vtracer is not installed. Please install dependencies first.")

    t0 = time.perf_counter()
    params = _resolve_vector_params(vector)

    original_width, original_height = transparent_image.size

    # 大图上采样会显著放大追踪耗时与内存占用，故按尺寸逐级限制倍率
    scale = int(params["scale"])
    if original_width > 2400 or original_height > 2400:
        scale = 1
    elif original_width > 1500 or original_height > 1500:
        scale = min(scale, 2)

    # vtracer 仅提供文件级 API，故经临时目录中转输入 PNG 与输出 SVG；
    # with 块退出时临时目录自动删除，无需手动清理
    with tempfile.TemporaryDirectory(prefix="vectorize-api-") as tmp:
        input_png_path = os.path.join(tmp, "input.png")
        output_svg_path = os.path.join(tmp, "output.svg")

        work_img = _prepare_trace_input(transparent_image, params)
        # 先清理再放大、放大后再清理一次：抑制 LANCZOS 上采样引入的插值噪声
        if scale > 1:
            work_img = work_img.resize(
                (int(original_width * scale), int(original_height * scale)),
                Image.Resampling.LANCZOS,
            )
            work_img = _prepare_trace_input(work_img, params)
        work_img.save(input_png_path, "PNG")

        # 兼容 vtracer 绑定的两种函数命名（新版本带 _py 后缀）
        # 追踪参数：彩色模式 + 剪影分层（cutout）+ 样条曲线（spline）。
        # filter_speckle / color_precision / layer_difference / corner_threshold /
        # length_threshold 来自用户预设；max_iterations / splice_threshold /
        # path_precision 为固定的质量调优值，不对外暴露。
        if hasattr(vtracer, "convert_image_to_svg_py"):
            vtracer.convert_image_to_svg_py(
                input_png_path,
                output_svg_path,
                colormode="color",
                hierarchical="cutout",
                mode="spline",
                filter_speckle=int(params["fs"]),
                color_precision=int(params["cp"]),
                layer_difference=int(params["ld"]),
                corner_threshold=int(params["ct"]),
                length_threshold=int(params["lt"]),
                max_iterations=10,
                splice_threshold=45,
                path_precision=4,
            )
        # 旧版绑定使用关键字参数形式的 convert_image_to_svg
        elif hasattr(vtracer, "convert_image_to_svg"):
            vtracer.convert_image_to_svg(
                input_path=input_png_path,
                output_path=output_svg_path,
                colormode="color",
                hierarchical="cutout",
                mode="spline",
                filter_speckle=int(params["fs"]),
                color_precision=int(params["cp"]),
                layer_difference=int(params["ld"]),
                corner_threshold=int(params["ct"]),
                length_threshold=int(params["lt"]),
                max_iterations=10,
                splice_threshold=45,
                path_precision=4,
            )
        else:
            raise RuntimeError("No supported vtracer conversion function found in current binding.")

        trace_w, trace_h = work_img.size

        # 读取 vtracer 写出的原始 SVG 文本（临时目录随 with 块退出自动清理）
        with open(output_svg_path, "r", encoding="utf-8") as f:
            raw_svg_text = f.read()

    elapsed_ms = round((time.perf_counter() - t0) * 1000.0, 2)
    # 追踪坐标系可能因上采样而大于原始画布，viewBox 需按 trace 尺寸填写
    actual_viewbox = f"0 0 {trace_w} {trace_h}"

    # 元数据模板：先给出完整默认结构，再由调用方上下文与后续统计逐步覆盖
    metadata: dict[str, Any] = {
        "engine": "vectorizer-api-split-pipeline",
        "source_type": "",
        "source_channel": "",
        "source_image_name": "",
        "generation": {
            "text": "",
            "prompt": "",
            "negative": "",
            "resolution": "",
            "seed": 0,
        },
        "params": {
            "preset": params["preset"],
            "color_precision": int(params["cp"]),
            "filter_speckle": int(params["fs"]),
            "corner_threshold": int(params["ct"]),
            "length_threshold": int(params["lt"]),
            "layer_difference": int(params["ld"]),
            "scale": scale,
            "trace_cleanup": {
                "alpha_floor": int(TRACE_CLEANUP_CONFIG[str(params["preset"])]["alpha_floor"]),
                "small_component_min_area": max(
                    2,
                    int(
                        (work_img.width * work_img.height)
                        / int(TRACE_CLEANUP_CONFIG[str(params["preset"])]["min_area_divisor"])
                    ),
                ),
            },
        },
        # canvas 记录用户可见尺寸，viewBox 记录实际路径坐标系（可能因上采样而不同）
        "canvas": {
            "width": int(original_width),
            "height": int(original_height),
            "viewBox": actual_viewbox,
        },
        "stats": {
            "elapsed_ms": elapsed_ms,
            "svg_size_kb": 0.0,
            "preview_png_size_kb": 0.0,
        },
        "preprocess": {
            "png_transparency": None,
        },
        "quality": {
            "svg_fidelity": 0.0,
        },
        "created_at": "",
    }

    # 用调用方上下文覆盖默认模板，保留模板中未覆盖的字段
    if metadata_context:
        _deep_merge_metadata(metadata, metadata_context)

    # 重构为包含 <metadata> 与分层 <g> 图层的规范完整 SVG (viewBox 完整覆盖实际路径坐标空间)
    formatted_svg_text = restructure_and_group_svg(
        raw_svg_text,
        canvas_width=original_width,
        canvas_height=original_height,
        viewbox_width=trace_w,
        viewbox_height=trace_h,
        metadata=metadata,
    )
    # 记录最终 SVG 体积（含元数据），供前端展示与体积调优参考
    svg_size_kb = round(len(formatted_svg_text.encode("utf-8")) / 1024.0, 3)
    metadata["stats"]["svg_size_kb"] = svg_size_kb

    # 基于最终完整的 SVG 使用 resvg_py 进行回渲染预览与保真度核验
    preview_png_bytes = _svg_to_png_bytes(formatted_svg_text, width=original_width, height=original_height)
    svg_fidelity = _calculate_svg_fidelity(transparent_image, preview_png_bytes)
    preview_data_url = _png_bytes_to_data_url(preview_png_bytes)

    # 回填保真度评分与预览图体积
    metadata["quality"]["svg_fidelity"] = svg_fidelity
    metadata["stats"]["preview_png_size_kb"] = round(len(preview_png_bytes) / 1024.0, 3)

    return {
        "svg": formatted_svg_text,
        "preview_png": preview_data_url,
        "metadata": metadata,
    }
