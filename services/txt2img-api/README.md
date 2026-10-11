# txt2img-api

文生图生成后端。接收文字内容与风格提示词，通过 ComfyUI 工作流生成艺术字位图，返回 PNG data URL 与结构化元数据。

**职责边界**：本服务只负责**生成位图**；位图到 SVG 的矢量化由 [`vectorizer-api`](../vectorizer-api/README.md) 独立完成。

---

## 架构

```text
POST /api/v1/txt2img
  │
  ├─ 文本分析 ──→ 工作流路由 ──→ 提示词合成
  │
  ├─ ComfyUI 可用 ──→ 提交工作流 → 轮询 history → 下载图片 → base64 返回
  │
  └─ ComfyUI 不可达 ──→ Pillow 降级引擎（渐变背景 + 文字）
```

- **ComfyUI 模式**：`POST /prompt` 提交 → 轮询 `GET /history/{id}` → `GET /view` 下载图片
- **降级模式**：Pillow 本地生成，保证接口始终有响应

---

## 接口

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| `GET` | `/healthz` | 健康检查 |
| `POST` | `/shutdown` | 关闭后端进程 |
| `POST` | `/api/v1/txt2img` | 文生图 |

桌面端与 CLI 默认调用 `http://127.0.0.1:9001/api/v1/txt2img`。

### `GET /healthz`

```json
{ "ok": true, "service": "txt2img-api" }
```

### `POST /shutdown`

关闭服务进程，返回确认信息。

### `POST /api/v1/txt2img`

#### 请求体

```json
{
  "text": "七里香",
  "prompt": "清新国风、墨绿色金边、植物叶片装饰",
  "negative_prompt": "缺字, 错字, 笔画断裂",
  "resolution": "1024 x 1024",
  "seed": 42,
  "style": "default",
  "format": "PNG",
  "workflow": ""
}
```

| 字段 | 类型 | 默认 | 约束 | 说明 |
| --- | --- | --- | --- | --- |
| `text` | string | `""` | ≤ 60 | 要渲染的文字内容；允许为空（仅生成背景与风格） |
| `prompt` | string | `""` | ≤ 600 | 风格描述 |
| `negative_prompt` | string | `""` | ≤ 600 | 负面提示词 |
| `resolution` | string | `1024 x 1024` | ≤ 32 | 分辨率，格式 `宽x高` |
| `seed` | int | `0` | — | 随机种子，固定可复现 |
| `style` | string | `default` | ≤ 40 | **保留字段**，仅记录到元数据，不影响生成 |
| `format` | enum | `PNG` | `PNG` / `PNG + SVG` | 输出格式 |
| `workflow` | string | `""` | ≤ 64 | 工作流文件名（不含路径与扩展名）；为空时按文本内容路由 |

输入规范化：`text` 会 strip 并把内部连续空白折叠为单空格；`prompt` / `negative_prompt` / `style` 仅 strip。

#### 响应

```json
{
  "image_base64": "data:image/png;base64,...",
  "image_name": "qi-li-xiang.png",
  "metadata": {
    "engine": "comfyui",
    "canvas": { "width": 1024, "height": 1024 },
    "comfyui_prompt_id": "xxx-xxx-xxx",
    "generated_at": "2026-05-16T12:00:00+00:00",
    "artifact": { "image_name": "qi-li-xiang.png", "byte_length": 123456 },
    "prompt_synthesis": { "positive_prompt": "...", "negative_strategy": "positive-inversion" }
  },
  "workflow_api": { "...": "实际执行的工作流快照" },
  "model_dependencies": { "...": "运行时扫描到的模型引用" }
}
```

| 字段 | 说明 |
| --- | --- |
| `image_base64` | PNG 的 data URL，可直接用于 `<img src="...">` |
| `image_name` | 根据文字内容自动生成的文件名 |
| `metadata.engine` | 实际使用的引擎：`comfyui` 或 `local-studio`（降级） |
| `metadata.prompt_synthesis` | 提示词合成的权威记录，`positive_prompt` 与实际注入工作流的内容逐字相等 |
| `workflow_api` | 本次实际执行的工作流快照，可据此复现 |
| `model_dependencies` | 扫描到的模型引用，便于审查模型来源 |

> `engine=local-studio`、`fallback_tier=-1`、无 `comfyui_prompt_id` 是**降级标志**——自动化验收可据此判断是否真正跑通了 ComfyUI。

---

## 快速开始

### 依赖

| 组件 | 要求 |
| --- | --- |
| Python | 3.13+ |
| uv | 推荐（也可用 pip） |
| ComfyUI | 可选，缺失时降级 |

### 安装与启动

```powershell
cd services\txt2img-api
uv sync
uv run txt2img-api
```

服务监听 `0.0.0.0:9001`。

没有 uv 时：

```powershell
pip install -r requirements.txt
uvicorn app.main:app --host 0.0.0.0 --port 9001 --reload
```

### 测试

```powershell
uv run pytest
```

---

## 配置

全部通过环境变量控制：

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `AUTO_START_COMFYUI` | `1` | 设为 `0` 禁用 ComfyUI 自动启动 |
| `COMFYUI_HOST` | `http://127.0.0.1:8188` | ComfyUI 服务地址 |
| `COMFYUI_POLL_TIMEOUT` | `900` | 轮询结果的最大等待秒数 |
| `COMFYUI_POLL_INTERVAL` | `1.0` | 轮询间隔（秒） |
| `WORKFLOW_PATH` | 空 | 指定工作流 JSON 的绝对路径，**优先级高于请求中的 `workflow`** |
| `COMFYUI_LAUNCHER_BAT` | 空 | 使用自定义 ComfyUI 启动脚本 |
| `COMFYUI_NETWORK_MODE` | `offline` | 写入 ComfyUI-Manager 配置，减少启动时联网检查 |

工作流选择优先级：`WORKFLOW_PATH` > 请求 `workflow` > 按文本内容自动路由。

---

## ComfyUI 集成

### 自动启动

启动时若检测到仓库内的便携版 ComfyUI，会以**无窗口、无浏览器**模式在后台拉起：

```text
services/txt2img-api/
└── ComfyUI_windows_portable_nvidia/
    └── ComfyUI_windows_portable/
        └── python_embeded/python.exe
            └── ComfyUI/main.py
```

启动参数 `--fast fp16_accumulation`，输出定向到 `DEVNULL`。

手动启动 ComfyUI 时，设 `AUTO_START_COMFYUI=0` 运行本服务。

### 交互流程

```text
txt2img-api                          ComfyUI
  ├─ POST /prompt ──────────────────→│  提交工作流 JSON
  │  ← { prompt_id }                 │
  ├─ GET /history/{prompt_id} ──────→│  轮询完成状态
  │  ← { completed: true }           │
  ├─ GET /view?filename=... ────────→│  下载图片
  │  ← png bytes                     │
```

---

## 工作流

工作流 JSON 位于 `workflows/`，格式为 ComfyUI **API 格式**（扁平 dict，每个节点含 `class_type` 与 `inputs`）。

| 文件 | 适用文本 | 基座模型 |
| --- | --- | --- |
| `qwen_image_2512_gguf.json` | 纯中文 | Qwen-Image-2512（GGUF 量化） |
| `flux_schnell.json` | 纯英文 | FLUX.1-schnell |
| `test_z_image_turbo.json` | 中英混排 / 降级候选 | Z-Image-Turbo |

路由与降级链：

| 文本类型 | 优先级 1 | 优先级 2 | 兜底 |
| --- | --- | --- | --- |
| 纯中文 | `qwen_image_2512_gguf` | `test_z_image_turbo` | Pillow stub |
| 纯英文 | `flux_schnell` | `test_z_image_turbo` | Pillow stub |
| 中英混排 | `test_z_image_turbo` | — | Pillow stub |

### 参数注入

工作流节点按 `class_type` 自动识别并注入参数：

| 节点 class_type | 注入参数 |
| --- | --- |
| `CLIPTextEncode` | `inputs.text` |
| `CLIPTextEncodeFlux` | `inputs.t5xxl`（全长提示词）+ `inputs.clip_l`（短摘要，≤220 字符，按子句边界截断） |
| `EmptyLatentImage` / `EmptySD3LatentImage` | `inputs.width`、`inputs.height` |
| `KSampler` / `KSamplerAdvanced` | `inputs.seed` |

**正负向节点的定位**：顺 `KSampler` 的 `inputs.positive` / `inputs.negative` 连线解析，并沿 `ConditioningZeroOut` 一类 conditioning 直通节点上溯；**不按节点在 JSON 中的出现顺序**，否则从 ComfyUI 重新导出一次就可能让正负向互换。没有可用采样器连线时，回退到「数字感知的节点 id 序」取前两个文本编码节点。解析只改写这两个节点，其余文本节点不动。

---

## 提示词子系统

用户输入不直接提交给模型，而是经「文本分析 → profile 渲染 → 负面词处理」三步包装。算法细节见[算法原理说明](../../docs/算法原理说明.md#六文生图侧的关键算法)。

### 文本分析

`TextAnalysis` 一次拆分汉字 / 拉丁片段 / 数字 / 符号，**路由与模板共用同一份分析**。关键属性 `residue` = 原文去掉全部汉字后的剩余串：

```text
"咪哄之风 98% hey you"  →  "98% hey you"
"咖啡 Latte 2.0"        →  "Latte 2.0"
"满庭芳"                →  ""
```

### profile 化渲染

单一渲染器 + 数据化 profile（`_FLUX_PROFILE` / `_ZIMAGE_PROFILE`），渲染流程零分支。段落顺序：

```text
background → style → layout → content → accuracy → inversion
```

用户把版式意图写在 `prompt` 里，因此风格段被提前到内容约束之前。若 `prompt` 中已出现版式关键词（`居中`、`右下角`、`layout`、`aligned` 等），layout 段改发中性表述，不覆盖用户自己的排布要求。

### 负面提示词处理

**负面 conditioning 并非在所有工作流上都生效**：

| 工作流 | cfg | 负面文本节点 | 是否生效 |
| --- | ---: | --- | :---: |
| `qwen_image_2512_gguf` | 2.5 | 节点 `5` | ✅ |
| `flux_schnell` | 1.0 | 节点 `9` | ❌ cfg=1 时采样器忽略负面 conditioning |
| `test_z_image_turbo` | 1.0 | 无 | ❌ 负向由 `ConditioningZeroOut` 从正向派生 |

能力探测对 **cfg 与节点结构做与门判定**：只看 cfg 会漏判「cfg 正常但负向指向 ConditioningZeroOut」，只看节点会漏判 flux。

- **生效** → 照常写入负面 conditioning
- **不生效** → 走双语词表的**否定 → 肯定反演**：命中词表的负面词转成正向子句（`模糊` → `边缘锐利、高清、矢量感清晰的字形`）；**未命中的词只记录在 metadata 的 `inversion.unmapped_terms`，绝不注入**

---

## 本地降级引擎

ComfyUI 不可达时自动降级为 Pillow 生成：按 `seed` 生成随机渐变背景并在左上角绘制提示词文字，返回标准 PNG data URL。

降级确保前端在 ComfyUI 未就绪时仍能得到正常响应。**降级产物不是真实艺术字**，验收时应检查 `metadata.engine`。

---

## 项目结构

```text
services/txt2img-api/
├── app/
│   ├── main.py            FastAPI 应用、路由、ComfyUI 生命周期管理
│   ├── models.py          Pydantic 请求 / 响应模型
│   └── generator.py       核心生成逻辑（文本分析、提示词合成、ComfyUI 客户端、降级 stub）
├── workflows/             ComfyUI 工作流 JSON（API 格式）
├── custom_nodes/          自定义节点补丁源（configure-comfyui.ps1 从这里复制到 ComfyUI）
├── scripts/
│   └── build-backend-exe.ps1   PyInstaller 打包脚本
├── tests/                 pytest 测试
├── txt2img_entry.py       PyInstaller 打包入口
├── pyproject.toml / uv.lock / requirements.txt
└── pytest.ini
```

---

## 打包为 EXE

```powershell
.\scripts\build-backend-exe.ps1                  # 自动检测：有 uv 用 uv，否则用 python
.\scripts\build-backend-exe.ps1 -Toolchain uv
.\scripts\build-backend-exe.ps1 -Toolchain python
```

产物：`services/txt2img-api/dist/txt2img-backend.exe`

工作流 JSON 通过 `--add-data` 以**相对路径**打进 EXE；便携版 ComfyUI（约 60 GB）**不打包**，需在运行时与 EXE 同级放置。

---

## 相关源码

| 文件 | 说明 |
| --- | --- |
| `app/main.py` | FastAPI 路由、ComfyUI 进程生命周期、启动验收 |
| `app/models.py` | 请求 / 响应模型与输入约束 |
| `app/generator.py` | 文本分析、工作流路由、提示词合成、ComfyUI 客户端、降级 stub |
| `scripts/build-backend-exe.ps1` | PyInstaller 打包脚本 |

---

## 相关文档

| 文档 | 内容 |
| --- | --- |
| [算法原理说明](../../docs/算法原理说明.md) | 文本分析、三路路由、提示词合成的算法细节 |
| [参数配置说明](../../docs/参数配置说明.md) | 请求字段、环境变量、工作流降级链 |
| [模型与节点依赖清单](../../docs/模型与节点依赖清单.md) | 模型来源、版本、许可与部署位置 |
| [安装部署说明](../../docs/安装部署说明.md) | 交付包部署与源码部署两条路径 |

## 与 monorepo 的关联

- 桌面端与 CLI 通过 HTTP 调用本服务 `http://127.0.0.1:9001/api/v1/txt2img`
- 生成的位图交由 [`vectorizer-api`](../vectorizer-api/README.md)（端口 8000）矢量化
