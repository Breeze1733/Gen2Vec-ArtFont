"""FR3 intelligent vectorization backend package.

承载「位图 → SVG」矢量化后端的全部实现，模块职责划分如下：

- ``image_processing``：背景分离、保边去噪、裁剪与 CIELAB 色彩量化等预处理；
- ``vectorization``：vtracer 路径追踪、语义图层解耦、保真度评估与 SVG 元数据注入；
- ``models``：Pydantic 请求 / 响应契约与输入校验；
- ``main``：FastAPI 应用装配与 HTTP 端点（``/healthz`` / ``/shutdown`` / ``/api/v1/vectorize``）。
"""

