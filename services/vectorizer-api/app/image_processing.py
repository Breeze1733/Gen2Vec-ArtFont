"""
================================================================================
图像预处理核心模块 (Image Preprocessing Pipeline)
================================================================================

本模块实现了针对艺术字位图的端到端图像质量增强与预处理流水线，为高保真矢量化
(Vectorization Engine) 提供紧凑、去噪、边缘清晰且色彩分层离散的前景位图输入。

【核心算法原理与数学实现说明】

1. 显著性目标检测与深度背景分离算法 (Salient Object Detection & Alpha Matting):
   - 基于深度显著目标检测模型 ISNet-General-Use (Interconnected Saliency Network) / U^2-Net:
     输入归一化艺术字位图 I in R^{3 x H x W}，通过多尺度残差 U 形结构 (ReSQU) 聚合高层语义
     与底层边缘特征，输出逐像素前景置信度概率掩模：
         S(x, y) = sigma(f_theta(I(x, y))) in [0, 1]
   - 贝叶斯 Alpha 抠图与边缘形态学腐蚀优化 (Bayesian / KNN Alpha Matting):
     对于未定过渡区域 Omega_unknown，利用前景颜色 F 与背景颜色 B 的局部先验高斯分布，
     在图像合成约束方程下求解最优透明度通道 alpha：
         I(x, y) = alpha(x, y) F(x, y) + (1 - alpha(x, y)) B(x, y)
     消除半透明羽化光晕，保留字符笔画内部实心像素。

2. 边缘连通域分析与纯白溢出底衬滤除 (Boundary-Connected Component White Removal):
   - 映射至 HSV 感知色彩空间，提取近纯白区域掩模：
         M_white(x, y) = [V(x, y) >= V_thresh] and [S(x, y) <= S_thresh]
   - 进行 8-邻域连通分量标记 (Connected Components Labeling)。遍历图像四边缘边界：
         L_border = Labels(Top) union Labels(Bottom) union Labels(Left) union Labels(Right)
     将与画布外围连通的白色背景标签置为完全透明 (alpha = 0)，防止底衬白边干扰矢量外框识别。

3. 双边保边滤波平滑去噪算法 (Bilateral Edge-Preserving Denoising):
   - 经典高斯滤波会导致文字锐利边缘模糊退化。本模块采用非线性双边滤波 (Bilateral Filter)，
     在空间欧氏距离与色彩辐射距离的双重高斯核联合约束下平滑笔画表面纹理噪点：
         I_filtered(p) = (1 / W_p) sum_{q in Omega} I(q) * g_s(||p - q||) * f_r(||I(p) - I(q)||)
     其中空间邻域高斯核为：
         g_s(||p - q||) = exp(-||p - q||^2 / (2 sigma_s^2))
     色彩辐射高斯核为：
         f_r(||I(p) - I(q)||) = exp(-||I(p) - I(q)||^2 / (2 sigma_r^2))
     归一化权重因子为：
         W_p = sum_{q in Omega} g_s(||p - q||) * f_r(||I(p) - I(q)||)
     有效压制扩散模型生成的伪影颗粒，同时严格保持字符笔画边缘梯度的阶跃锐度。

4. CIELAB 感知色彩空间 K-Means++ 聚类色彩量化 (CIELAB Space K-Means Color Quantization):
   - 传统 RGB 空间的欧氏距离与人眼主观感知色彩差异存在严重非均匀性畸变。
     本算法将前景像素转化至国际照明委员会 CIELAB 色彩空间 (L*, a*, b*)，
     使得欧氏几何距离直接正比于人眼感知色差 Delta E*_{ab}：
         Delta E*_{ab} = sqrt((Delta L*)^2 + (Delta a*)^2 + (Delta b*)^2)
   - 提取不透明前景像素点集 X = {x_i in R^3 | alpha_i > alpha_threshold}。采用 K-Means++ 初始化策略
     设定 k 个主色聚类质心 C = {mu_1, mu_2, ..., mu_k}，迭代优化组内离差平方和 (Inertia / WCSS)：
         J = sum_{j=1}^k sum_{x in S_j} ||x - mu_j||^2,  其中 S_j = {x in X | argmin_m ||x - mu_m|| = j}
   - 将前景像素赋值为最近质心颜色，并重映射回 sRGB 空间，同时严格保留原生平滑 Alpha 通道。
     极大地减少了微小渐变引起的杂散碎块路径，为矢量化输出规整、纯净的分层矢量图层奠定基础。

5. 自适应紧致外接矩形裁剪与亚像素抗锯齿 (Bounding-Box Cropping & Antialiasing Softening):
   - 提取 Alpha > alpha_min 的前景几何包围盒 (Axis-Aligned Bounding Box)：
         AABB = [max(0, x_min - delta), max(0, y_min - delta), min(W, x_max + delta), min(H, y_max + delta)]
     裁剪边缘无用留白，收敛视口并提升后续轮廓拟合计算效率。
   - 采用亚像素高斯软化算子 alpha_soft = G_sigma * alpha，消除光栅离散栅格采样带来的阶梯锯齿 (Jaggies)。
================================================================================
"""

from __future__ import annotations

import base64
import hashlib
import importlib
import io
import os
import sys
from pathlib import Path
from typing import Any

import numpy as np
from PIL import Image

import cv2

REMBG_MODEL_NAME = "isnet-general-use"
REMBG_MODEL_FILENAME = f"{REMBG_MODEL_NAME}.onnx"
REMBG_MODEL_MD5 = "fc16ebd8b0c10d971d3513d564d01e29"

_REMBG_REMOVE = None
_REMBG_NEW_SESSION = None
_REMBG_SESSION = None
_REMBG_MODEL_CHECKED = False


def decode_base64_image(data: str) -> bytes:
    """将 base64 字符串（可带 ``data:`` 前缀）解码为原始字节。

    兼容前端 ``<img src="data:image/png;base64,...">`` 直接透传的写法：
    存在前缀时按第一个逗号切分取出有效载荷，否则整体视为 base64 内容。
    """
    data = data.strip()
    if data.startswith("data:"):
        _, payload = data.split(",", 1)
    else:
        payload = data
    return base64.b64decode(payload)


def image_bytes_to_pil(image_bytes: bytes) -> Image.Image:
    """把图片字节解码为 PIL 图像。

    在此统一兜底解码异常并转换为 ``ValueError``，便于路由层映射为 400 响应；
    ``load()`` 立即读入全部像素，避免文件句柄延迟释放引发的资源问题。

    Raises:
        ValueError: 字节流不是可识别的 PNG/JPG 等图片格式。
    """
    try:
        img = Image.open(io.BytesIO(image_bytes))
        img.load()
        return img
    except Exception as exc:
        raise ValueError("Cannot decode image bytes. Please upload a valid PNG/JPG.") from exc


def pil_to_data_url(img: Image.Image, fmt: str = "PNG") -> str:
    """把 PIL 图像编码为 ``data:<mime>;base64,...`` 形式的 Data URL。

    目前仅区分 PNG 与 JPEG 两种 MIME 类型，其余格式按 JPEG 处理。
    """
    buf = io.BytesIO()
    img.save(buf, format=fmt)
    b64 = base64.b64encode(buf.getvalue()).decode("ascii")
    mime = "image/png" if fmt.upper() == "PNG" else "image/jpeg"
    return f"data:{mime};base64,{b64}"


def pil_to_rgb_np(img: Image.Image) -> np.ndarray:
    """把 PIL 图像转为 ``H×W×3`` 的 RGB ``ndarray``（必要时执行模式转换）。"""
    if img.mode != "RGB":
        img = img.convert("RGB")
    return np.array(img)


def _safe_int(value: Any, default: int) -> int:
    """尽力将 ``value`` 转为 ``int``，失败时返回 ``default``。

    用于消化来自 JSON 的松散参数（可能为字符串、``None`` 或浮点），
    避免因单个非法字段导致整个请求失败。
    """
    try:
        return int(value)
    except Exception:
        return default


def _rembg_model_dir() -> Path:
    """返回 rembg 离线模型所在目录。

    PyInstaller 打包后（``sys.frozen`` 为真）模型位于 EXE 同级的 ``models/rembg``；
    源码运行时则位于 ``services/vectorizer-api/models/rembg``。
    """
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent / "models" / "rembg"
    return Path(__file__).resolve().parents[1] / "models" / "rembg"


def _rembg_model_path() -> Path:
    """返回 rembg 离线模型文件的完整路径。"""
    return _rembg_model_dir() / REMBG_MODEL_FILENAME


def _file_md5(path: Path) -> str:
    """分块计算文件 MD5，返回十六进制摘要。

    采用 1 MiB 分块读取，避免将数百 MB 的 ONNX 模型整体载入内存。
    """
    digest = hashlib.md5()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _ensure_local_rembg_model() -> Path:
    """定位并校验本地 rembg 模型，返回其路径。

    强制使用随包分发的离线模型（不联网下载），并在首次调用时校验 MD5，
    确保模型文件未被截断或替换。校验结果通过 ``_REMBG_MODEL_CHECKED`` 缓存，
    避免每次请求都重算大文件摘要。

    Raises:
        RuntimeError: 模型文件缺失，或校验和与预期不符。
    """
    global _REMBG_MODEL_CHECKED

    model_path = _rembg_model_path()
    if not model_path.is_file():
        raise RuntimeError(
            f"Offline rembg model not found: {model_path}. "
            f"Place {REMBG_MODEL_FILENAME} in {_rembg_model_dir()} before starting vectorizer-api."
        )
    if not _REMBG_MODEL_CHECKED:
        actual_md5 = _file_md5(model_path)
        if actual_md5.lower() != REMBG_MODEL_MD5:
            raise RuntimeError(
                f"Offline rembg model checksum mismatch: {model_path}. "
                f"Expected md5 {REMBG_MODEL_MD5}, got {actual_md5}."
            )
        _REMBG_MODEL_CHECKED = True
    return model_path


def _load_rembg():
    """惰性导入 rembg 及其依赖的 onnxruntime，返回 ``(remove, new_session)``。

    延迟到首次使用才导入，可显著缩短服务冷启动时间；导入结果缓存于模块级变量，
    后续调用直接复用。导入失败时抛出携带原始异常信息的 ``RuntimeError``，便于
    定位缺失的依赖。

    Raises:
        RuntimeError: onnxruntime 或 rembg 未能成功导入。
    """
    global _REMBG_REMOVE, _REMBG_NEW_SESSION

    if _REMBG_REMOVE is not None and _REMBG_NEW_SESSION is not None:
        return _REMBG_REMOVE, _REMBG_NEW_SESSION

    try:
        importlib.import_module("onnxruntime")
    except Exception as exc:
        raise RuntimeError(
            'onnxruntime is not available in the running backend. '
            f"Original error: {type(exc).__name__}: {exc}"
        ) from exc

    try:
        rembg_module = importlib.import_module("rembg")
    except Exception as exc:
        raise RuntimeError(
            'rembg is not available in the running backend. '
            f"Original error: {type(exc).__name__}: {exc}"
        ) from exc

    _REMBG_REMOVE = getattr(rembg_module, "remove")
    _REMBG_NEW_SESSION = getattr(rembg_module, "new_session")
    return _REMBG_REMOVE, _REMBG_NEW_SESSION


def _get_rembg_session():
    """获取（并缓存）rembg 推理会话。

    首次调用时先把 ``U2NET_HOME`` 指向本地模型目录，使 rembg 从离线文件加载
    ``isnet-general-use`` 模型，杜绝运行期联网下载；随后创建并缓存会话实例。

    Raises:
        RuntimeError: 会话初始化失败（通常为缺少 CPU/GPU 运行时支持）。
    """
    global _REMBG_SESSION

    if _REMBG_SESSION is None:
        model_path = _ensure_local_rembg_model()
        os.environ["U2NET_HOME"] = str(model_path.parent)
        _, new_session = _load_rembg()
        try:
            _REMBG_SESSION = new_session(REMBG_MODEL_NAME)
        except Exception as exc:
            raise RuntimeError(
                f"Failed to initialize rembg with local {REMBG_MODEL_NAME} model. "
                'Install CPU support with "pip install rembg[cpu]" or GPU support with "pip install rembg[gpu]".'
            ) from exc
    return _REMBG_SESSION


def remove_background_with_rembg(img: Image.Image) -> Image.Image:
    """使用 rembg 抠除背景，返回带 Alpha 通道的 RGBA 图像。

    优先启用 ``alpha_matting``（贝叶斯抠图）与掩模后处理以获得平滑的边缘过渡；
    若该组合不被当前 rembg 版本支持，则回退到仅 ``post_process_mask`` 的简化调用。
    同时兼容 rembg 返回 PIL 图像或字节流两种实现。

    Raises:
        RuntimeError: rembg 调用失败且回退仍失败，或返回类型不受支持。
    """
    source = img.convert("RGBA")
    session = _get_rembg_session()
    remove, _ = _load_rembg()
    try:
        result = remove(
            source,
            session=session,
            post_process_mask=True,
            alpha_matting=True,
            alpha_matting_foreground_threshold=240,
            alpha_matting_background_threshold=10,
            alpha_matting_erode_size=10,
        )
    except RuntimeError:
        raise
    except Exception:
        try:
            result = remove(source, session=session, post_process_mask=True)
        except Exception as exc:
            raise RuntimeError(f"Failed to remove image background with rembg: {exc}") from exc

    if isinstance(result, Image.Image):
        return result.convert("RGBA")
    if isinstance(result, bytes):
        return image_bytes_to_pil(result).convert("RGBA")
    raise RuntimeError("rembg returned an unsupported image type.")


def remove_edge_connected_white(
    img: Image.Image,
    white_value_threshold: int = 245,
    white_saturation_threshold: int = 20,
) -> Image.Image:
    """将「与画布边缘连通的纯白区域」置为全透明。

    先在 HSV 空间提取近纯白掩模，再做 8-邻域连通域标记，只清除与图像四边相连的
    白色分量——这样既能移除背景底衬白边，又能保留被前景包围的白色笔画内部区域。

    Args:
        img: 输入图像（内部转为 RGBA 处理）。
        white_value_threshold: 判定纯白的 V 通道下限。
        white_saturation_threshold: 判定纯白的 S 通道上限。

    Returns:
        处理后的 RGBA 图像；若不存在白色区域，或白色不与边缘连通，则原样返回。
    """
    rgba = img.convert("RGBA")
    arr = np.array(rgba)
    rgb = arr[:, :, :3]

    hsv = cv2.cvtColor(rgb, cv2.COLOR_RGB2HSV)
    sat = hsv[:, :, 1]
    val = hsv[:, :, 2]

    white_mask = (val >= white_value_threshold) & (sat <= white_saturation_threshold)
    if not np.any(white_mask):
        return rgba

    white_u8 = (white_mask.astype(np.uint8)) * 255
    num_labels, labels = cv2.connectedComponents(white_u8)
    if num_labels <= 1:
        return rgba

    border_labels: set[int] = set()
    border_labels.update(np.unique(labels[0, :]).tolist())
    border_labels.update(np.unique(labels[-1, :]).tolist())
    border_labels.update(np.unique(labels[:, 0]).tolist())
    border_labels.update(np.unique(labels[:, -1]).tolist())
    border_labels.discard(0)
    if not border_labels:
        return rgba

    edge_bg_mask = np.isin(labels, list(border_labels))
    out = arr.copy()
    out[edge_bg_mask, 3] = 0
    return Image.fromarray(out, mode="RGBA")


def denoise_preserve_edges(img: Image.Image) -> Image.Image:
    """对 RGB 通道做双边滤波去噪，同时保持笔画边缘锐利。

    双边滤波在「空间距离」与「色彩差」的双重高斯核约束下平滑表面噪点，
    避免普通高斯模糊造成的边缘退化；Alpha 通道保持原样拼接回去。
    """
    rgba = img.convert("RGBA")
    arr = np.array(rgba)
    rgb = arr[:, :, :3]
    alpha = arr[:, :, 3]

    filtered = cv2.bilateralFilter(rgb, d=5, sigmaColor=40, sigmaSpace=40)
    merged = np.dstack([filtered, alpha])
    return Image.fromarray(merged, mode="RGBA")


def crop_to_subject(img: Image.Image, alpha_threshold: int = 8, padding: int = 8) -> Image.Image:
    """按前景 Alpha 包围盒裁剪图像，并保留 ``padding`` 像素边距。

    收敛视口可去除多余留白，提升后续轮廓拟合效率。若图像全透明（无前景像素）
    则原样返回，避免裁剪出空图。
    """
    rgba = img.convert("RGBA")
    arr = np.array(rgba)
    alpha = arr[:, :, 3]
    ys, xs = np.where(alpha > alpha_threshold)
    if len(xs) == 0 or len(ys) == 0:
        return rgba

    left = max(0, int(xs.min()) - padding)
    top = max(0, int(ys.min()) - padding)
    right = min(rgba.width, int(xs.max()) + padding + 1)
    bottom = min(rgba.height, int(ys.max()) + padding + 1)
    return rgba.crop((left, top, right, bottom))


def preserve_antialias_edges(img: Image.Image) -> Image.Image:
    """软化 Alpha 边缘以保留抗锯齿过渡。

    对 Alpha 通道施加小幅高斯模糊，再与原 Alpha 逐像素取最大值：既补充了半透明
    过渡像素（软化锯齿），又不会削减原有的不透明度。
    """
    rgba = img.convert("RGBA")
    arr = np.array(rgba)
    alpha = arr[:, :, 3]
    soft_alpha = cv2.GaussianBlur(alpha, (0, 0), sigmaX=0.6, sigmaY=0.6)
    arr[:, :, 3] = np.maximum(alpha, soft_alpha).astype(np.uint8)
    return Image.fromarray(arr, mode="RGBA")


def quantize_colors(img: Image.Image, color_count: int) -> Image.Image:
    """
    基于 CIELAB 感知色彩空间与 K-Means++ 聚类的图像主色量化算法。

    【核心步骤】
    1. 提取 Alpha > 8 的有效前景像素集合，避免透明/微透背景对聚类中心产生色彩污染；
    2. 将前景 RGB 色彩映射至非线性人眼均匀感知色彩空间 CIELAB (L*, a*, b*)；
    3. 运行 cv2.kmeans (K-Means++ 初始化)，迭代优化类内欧氏距离平方和：
           min sum_{j=1}^k sum_{x in S_j} ||x - mu_j||^2
    4. 将各像素重映射为其聚类质心颜色，并转换回标准 sRGB 色彩空间；
    5. 严格保留原始平滑 Alpha 通道，输出高保真离散色彩分层图。
    6. 若前景像素数不足或 OpenCV 抛出异常，平滑降级为 PIL MEDIANCUT 中值切割算法。
    """
    rgba = img.convert("RGBA")
    k = max(2, min(64, _safe_int(color_count, 8)))

    if cv2 is not None:
        try:
            arr = np.array(rgba)
            rgb = arr[:, :, :3]
            alpha = arr[:, :, 3]
            fg_mask = alpha > 8

            fg_count = int(np.count_nonzero(fg_mask))
            if fg_count < k:
                return rgba

            fg_rgb = rgb[fg_mask]
            fg_lab = cv2.cvtColor(fg_rgb.reshape(-1, 1, 3), cv2.COLOR_RGB2Lab)
            data = fg_lab.reshape(-1, 3).astype(np.float32)

            criteria = (cv2.TERM_CRITERIA_EPS + cv2.TERM_CRITERIA_MAX_ITER, 20, 1.0)
            attempts = 3
            flags = cv2.KMEANS_PP_CENTERS

            _compactness, labels, centers = cv2.kmeans(data, k, None, criteria, attempts, flags)

            centers_u8 = np.clip(centers, 0, 255).astype(np.uint8)
            quantized_fg_lab = centers_u8[labels.flatten()].reshape(-1, 1, 3)
            quantized_fg_rgb = cv2.cvtColor(quantized_fg_lab, cv2.COLOR_Lab2RGB).reshape(-1, 3)

            out_arr = arr.copy()
            out_arr[fg_mask, :3] = quantized_fg_rgb
            return Image.fromarray(out_arr, mode="RGBA")
        except Exception:
            pass

    # 降级方案：PIL MEDIANCUT
    rgb = rgba.convert("RGB")
    quantized = rgb.quantize(colors=k, method=Image.MEDIANCUT)
    rgb_q = quantized.convert("RGB")
    out = Image.new("RGBA", rgba.size)
    out.paste(rgb_q, (0, 0))
    out.putalpha(rgba.getchannel("A"))
    return out


def calculate_png_transparency(img: Image.Image) -> float:
    """计算透明像素占比（百分比，保留一位小数）。

    以 Alpha 通道均值换算整体透明度，写入元数据用于标记预处理结果的透明程度。
    """
    rgba = img.convert("RGBA")
    alpha = np.array(rgba.getchannel("A"), dtype=np.uint8)
    if alpha.size == 0:
        return 0.0
    return round((1.0 - (float(np.mean(alpha)) / 255.0)) * 100.0, 1)


def preprocess_image(image_bytes: bytes, vector: dict[str, Any]) -> dict[str, Any]:
    """图像预处理主流程：解码 → 抠图 / 去噪 / 裁剪 → 色彩量化。

    关键分支：若原图已自带有效 Alpha 通道（RGBA/LA/PA 模式，或调色板图带
    transparency 信息），则视为「已抠好的图」，跳过 rembg 与重型预处理，直接进入
    色彩量化；否则按需调用 rembg 抠图，再做保边去噪、抗锯齿软化与前景裁剪。
    无论走哪条分支，最后都会执行 CIELAB K-Means 色彩量化并统计透明像素占比。

    Args:
        image_bytes: 原始图片字节。
        vector: 矢量化参数字典（含 ``preset`` / ``color_precision`` /
            ``remove_edge_white_background`` 等键）。

    Returns:
        含 ``transparent_image``（PIL 图）、``transparent_png``（Data URL）、
        ``size``（宽高字典）与 ``png_transparency``（透明占比）的字典。
    """
    img = image_bytes_to_pil(image_bytes)

    # 检测是否已有 alpha 通道（RGBA、LA、PA 模式，或调色板模式带透明度信息）
    has_alpha = (
        (img.mode in ("RGBA", "LA", "PA") and img.getchannel("A").getextrema()[0] < 128)
        or (img.mode == "P" and "transparency" in img.info)
    )

    remove_bg = bool(vector.get("remove_edge_white_background", True))
    preset = str(vector.get("preset", "balanced")).lower()
    preset_color_clusters = {"clean": 6, "balanced": 10, "detailed": 14, "ultra": 16}
    default_clusters = preset_color_clusters.get(preset, 10)
    color_precision = max(2, min(64, _safe_int(vector.get("color_precision"), default_clusters)))

    if has_alpha:
        # 已有 alpha 通道，直接使用原图，跳过背景移除和繁重预处理
        img = img.convert("RGBA")
    else:
        if remove_bg:
            img = remove_background_with_rembg(img)
        else:
            img = img.convert("RGBA")

        img = denoise_preserve_edges(img)
        img = preserve_antialias_edges(img)
        img = crop_to_subject(img)

    # 无论是否已有透明通道，均对主体前景执行颜色聚类
    img = quantize_colors(img, color_precision)

    png_transparency = calculate_png_transparency(img)

    return {
        "transparent_image": img,
        "transparent_png": pil_to_data_url(img, fmt="PNG"),
        "size": {"width": img.width, "height": img.height},
        "png_transparency": png_transparency,
    }
