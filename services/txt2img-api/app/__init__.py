"""txt2img-api backend application package.

承载「文本 → 艺术字位图」的文生图服务，模块职责划分如下：

- ``generator``：工作流加载与参数注入、文本分析与提示词合成、ComfyUI 调用、
  工作流降级链与本地 Pillow 兜底；
- ``models``：Pydantic 请求 / 响应契约与输入校验；
- ``main``：FastAPI 应用装配、ComfyUI 子进程生命周期与 HTTP 端点
  （``/healthz`` / ``/shutdown`` / ``/api/v1/txt2img``）。
"""
