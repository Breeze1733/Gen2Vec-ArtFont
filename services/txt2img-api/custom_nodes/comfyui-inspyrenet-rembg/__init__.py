"""ComfyUI 自定义节点入口：注册 Inspyrenet 抠图节点。

上游节点的 ``__init__.py`` 直接 ``from Inspyrenet_Rembg import ...``，在部分
ComfyUI 启动方式下会因包搜索路径缺失而导入失败。补丁在导入前把本目录插入
``sys.path``，保证同目录模块可被解析。

``NODE_CLASS_MAPPINGS`` / ``NODE_DISPLAY_NAME_MAPPINGS`` 是 ComfyUI 的节点注册
协议：前者建立「class_type 名 → 实现类」的映射（工作流 JSON 里的 ``class_type``
即取自此处），后者提供面板上展示的显示名。
"""

import sys
import os
# 关键补丁：先注册本目录，再导入同目录下的实现模块
sys.path.insert(0, os.path.dirname(__file__))
from Inspyrenet_Rembg import InspyrenetRembg, InspyrenetRembgAdvanced

# class_type 名 → 实现类；工作流 JSON 中的 class_type 字段与此处键名对应
NODE_CLASS_MAPPINGS = {
    "InspyrenetRembg" : InspyrenetRembg,
    "InspyrenetRembgAdvanced" : InspyrenetRembgAdvanced,
}

# class_type 名 → 面板显示名
NODE_DISPLAY_NAME_MAPPINGS = {
    "InspyrenetRembg": "Inspyrenet Rembg",
    "InspyrenetRembgAdvanced": "Inspyrenet Rembg Advanced"
}
__all__ = ['NODE_CLASS_MAPPINGS', "NODE_DISPLAY_NAME_MAPPINGS"]
