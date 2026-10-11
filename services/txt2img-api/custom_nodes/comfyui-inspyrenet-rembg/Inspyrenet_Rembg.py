"""ComfyUI 自定义节点：基于 Inspyrenet 的 AI 抠图（背景移除）。

上游节点为 ``john-mnz/ComfyUI-Inspyrenet-Rembg``，本文件是其**补丁后版本**，
由 ``scripts/configure-comfyui.ps1`` 覆盖安装到 ComfyUI 的 ``custom_nodes/`` 目录。

补丁要点：
1. 在 import ``transparent_background`` **之前**设置环境变量并 monkey-patch
   numba，规避 Windows MAX_PATH 限制导致的缓存写入失败；
2. 模型目录指向 ComfyUI 内置的 ``models/inspyrenet``，避免首次运行联网下载。

节点输出 ``(IMAGE, MASK)``：IMAGE 为 RGBA 抠图结果，MASK 取其 Alpha 通道，
供下游矢量化工作流使用。
"""

import os
import numpy as np
import torch
from PIL import Image

# ═══════════════════════════════════════════════════════════════════
# 环境变量 + numba monkey-patch 必须在 import transparent_background
# 之前执行，否则 numba 往 site-packages __pycache__ 写内联缓存时
# 路径超 Windows MAX_PATH (260) → FileNotFoundError
# ═══════════════════════════════════════════════════════════════════

# 由本文件位置反推 ComfyUI 根目录：
#   <ComfyUI>/custom_nodes/comfyui-inspyrenet-rembg/Inspyrenet_Rembg.py
# 两次 dirname 后即得到 <ComfyUI>，模型统一放在其 models/inspyrenet 下。
_CUSTOM_NODE_DIR = os.path.dirname(os.path.abspath(__file__))
_COMFYUI_DIR = os.path.dirname(os.path.dirname(_CUSTOM_NODE_DIR))
_INSPYRENET_MODEL_DIR = os.path.join(_COMFYUI_DIR, "models", "inspyrenet")

# 模型目录（避免首次运行从 GitHub 下载）
if "TRANSPARENT_BACKGROUND_FILE_PATH" not in os.environ:
    os.environ["TRANSPARENT_BACKGROUND_FILE_PATH"] = _INSPYRENET_MODEL_DIR

# numba 全局缓存目录（仅影响显式 API 缓存，不影响 pymatting 内联缓存）
if "NUMBA_CACHE_DIR" not in os.environ:
    os.environ["NUMBA_CACHE_DIR"] = os.path.join(_INSPYRENET_MODEL_DIR, ".numba_cache")

# ══ monkey-patch: 禁用 numba 内联文件缓存 ══
# pymatting 源码中 @njit(cache=True) 会往源码旁 __pycache__/ 写缓存，
# 该路径极易超 Windows MAX_PATH。NUMBA_CACHE_DIR 无法重定向内联缓存，
# 因此必须在 import 前将 numba.njit/jit 的 cache 参数强制改为 False。
# 副作用：首次 import 时 JIT 编译结果不持久化，下次进程重启重新编译。
try:
    import numba as _numba
    # 保留原始实现，供包装函数回退调用
    _orig_njit = _numba.njit
    _orig_jit = _numba.jit

    def _njit_nocache(*args, **kwargs):
        """``numba.njit`` 的包装：无论调用方是否传 cache，都强制 cache=False。"""
        kwargs.pop("cache", None)
        return _orig_njit(*args, **kwargs, cache=False)

    def _jit_nocache(*args, **kwargs):
        """``numba.jit`` 的包装：同上，强制关闭磁盘缓存。"""
        kwargs.pop("cache", None)
        return _orig_jit(*args, **kwargs, cache=False)

    _numba.njit = _njit_nocache
    _numba.jit = _jit_nocache
except Exception:
    pass  # numba 不可用时静默跳过，不影响 transparent_background 降级

# 注意：以下 import 必须在上述 monkey-patch 之后，否则补丁不生效
from transparent_background import Remover
from tqdm import tqdm


# Tensor to PIL
def tensor2pil(image):
    """把 ComfyUI 的 IMAGE 张量转为 PIL 图像。

    张量取值域为 [0, 1]，先放大到 [0, 255] 并裁剪到合法范围，再转 uint8；
    ``squeeze()`` 去掉批次维度（本节点按单张处理）。
    """
    return Image.fromarray(np.clip(255. * image.cpu().numpy().squeeze(), 0, 255).astype(np.uint8))

# Convert PIL to Tensor
def pil2tensor(image):
    """把 PIL 图像转回 ComfyUI 的 IMAGE 张量。

    先归一化到 [0, 1]，再升维补上批次维度，与 ComfyUI 的 IMAGE 约定一致。
    """
    return torch.from_numpy(np.array(image).astype(np.float32) / 255.0).unsqueeze(0)

class InspyrenetRembg:
    """基础抠图节点：以默认阈值移除背景，输出 RGBA 图与 Alpha 掩模。"""

    def __init__(self):
        pass
    
    @classmethod
    def INPUT_TYPES(s):
        """声明节点输入：待处理图像 + 是否启用 TorchScript JIT 加速。"""
        return {
            "required": {
                "image": ("IMAGE",),
                "torchscript_jit": (["default", "on"],)
            },
        }

    # ComfyUI 节点协议：输出类型 / 执行入口方法名 / 面板分类
    RETURN_TYPES = ("IMAGE", "MASK")
    FUNCTION = "remove_background"
    CATEGORY = "image"

    def remove_background(self, image, torchscript_jit):
        """逐张抠图并拼回批次，返回 (RGBA 图, Alpha 掩模)。"""
        # jit=True 会先做 TorchScript 编译，首次较慢但后续推理更快
        if (torchscript_jit == "default"):
            remover = Remover()
        else:
            remover = Remover(jit=True)
        img_list = []
        for img in tqdm(image, "Inspyrenet Rembg"):
            mid = remover.process(tensor2pil(img), type='rgba')
            out =  pil2tensor(mid)
            img_list.append(out)
        img_stack = torch.cat(img_list, dim=0)
        # 第 4 个通道即 Alpha，直接作为掩模输出
        mask = img_stack[:, :, :, 3]
        return (img_stack, mask)
        
class InspyrenetRembgAdvanced:
    """进阶抠图节点：额外暴露阈值参数，可调节前景判定严格程度。"""

    def __init__(self):
        pass
    
    @classmethod
    def INPUT_TYPES(s):
        """声明节点输入：图像 + 阈值(0~1) + 是否启用 TorchScript JIT。"""
        return {
            "required": {
                "image": ("IMAGE",),
                "threshold": ("FLOAT", {"default": 0.5, "min": 0.0, "max": 1.0, "step": 0.01}),
                "torchscript_jit": (["default", "on"],)
            },
        }

    # ComfyUI 节点协议：输出类型 / 执行入口方法名 / 面板分类
    RETURN_TYPES = ("IMAGE", "MASK")
    FUNCTION = "remove_background"
    CATEGORY = "image"

    def remove_background(self, image, torchscript_jit, threshold):
        """逐张抠图并拼回批次；``threshold`` 透传给 Remover 控制前景判定。"""
        # jit=True 会先做 TorchScript 编译，首次较慢但后续推理更快
        if (torchscript_jit == "default"):
            remover = Remover()
        else:
            remover = Remover(jit=True)
        img_list = []
        for img in tqdm(image, "Inspyrenet Rembg"):
            mid = remover.process(tensor2pil(img), type='rgba', threshold=threshold)
            out =  pil2tensor(mid)
            img_list.append(out)
        img_stack = torch.cat(img_list, dim=0)
        # 第 4 个通道即 Alpha，直接作为掩模输出
        mask = img_stack[:, :, :, 3]
        return (img_stack, mask)
