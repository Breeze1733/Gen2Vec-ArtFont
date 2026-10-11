"""HTTP 层冒烟测试：验证 FastAPI 端点契约（状态码与响应结构）。

不依赖真实 ComfyUI：生成端点在无后端时会自然降级到本地 stub，因此断言只覆盖
「始终成立」的响应形状，而非具体像素内容。
"""
from fastapi.testclient import TestClient

from app.main import app

# 复用同一个 TestClient，避免每个用例重复构建应用
client = TestClient(app)


def test_healthz() -> None:
    """健康探针应返回 200 与固定的服务标识。"""
    response = client.get("/healthz")
    assert response.status_code == 200
    assert response.json() == {"ok": True, "service": "txt2img-api"}


def test_generate_returns_image_and_metadata() -> None:
    """生成端点应返回合法的 PNG data URL，并在元数据中回显请求参数。"""
    # 未传 text，走「背景 + 风格」路径；seed 固定便于断言
    response = client.post(
        "/api/v1/txt2img",
        json={
            "prompt": "晨曦之城",
            "negative_prompt": "模糊, 断裂",
            "resolution": "1024 x 1024",
            "seed": 42,
            "style": "neon",
            "format": "PNG",
        },
    )

    assert response.status_code == 200
    payload = response.json()
    # 产物必须是可直接用于 <img src> 的 PNG data URL
    assert payload["image_base64"].startswith("data:image/png;base64,")
    assert payload["image_name"].endswith(".png")
    # 元数据需回显请求中的关键参数
    assert payload["metadata"]["prompt"] == "晨曦之城"
    assert payload["metadata"]["seed"] == 42
