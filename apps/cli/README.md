# Gen2Vec CLI — 矢量艺术字生成器 · 自动化验收控制台

## 定位

`gen2vec_cli.exe` 是 **随桌面端安装包交付的自动化验收控制台**，不独立分发。

- **日常使用** → 启动桌面端 GUI，使用图形界面操作。
- **评审验收 / 批量测试 / 脚本自动化** → 使用 CLI。

CLI 不负责启动后端（txt2img-api + vectorizer-api）：使用前需由桌面端或手动启动。`shutdown` 命令仅用于主动关闭已运行的后端。

---

## 运行方式

### 方式 1：安装包自带（推荐，交付验收用）

安装桌面端后，在安装目录下直接运行：

```powershell
cd "$env:LOCALAPPDATA\Programs\矢量艺术字生成器"
.\gen2vec_cli.exe <command> [options]
```

若安装时选择了「为所有用户安装」或自定义目录，请替换为实际安装路径。**此时桌面端已在后台自动启动后端**，CLI 零等待直接执行。

### 方式 2：源码直接运行（开发调试用）

```powershell
# 确保后端已启动

node apps/cli/bin/gen2vec.mjs <command> [options]

# 或通过 npm link
cd apps/cli
npm link
gen2vec_cli <command> [options]
```

---

## 命令参考

### 通用选项

| 选项 | 简写 | 说明 |
|------|:----:|------|
| `--help` | `-h` | 显示帮助 |
| `--wait <秒>` | | 等待后端就绪的最大秒数（默认 0，不等待） |

---

### `pipeline` — 完整流水线（文本 → 位图 → SVG）

最常用的验收命令，一条指令走完全流程。

```powershell
gen2vec_cli.exe pipeline --text "七里香" --prompt "清新国风、墨绿色金边"
gen2vec_cli.exe pipeline --text "Hello" --prompt "neon style" --vector-preset ultra
gen2vec_cli.exe pipeline --text "夏日冰饮 50%" --prompt "清爽蓝白配色" --seed 42
```

| 参数 | 简写 | 说明 | 必填 |
|------|:----:|------|:----:|
| `--text` | `-t` | 艺术字文本 | ✓ |
| `--prompt` | `-p` | 风格提示词 | |
| `--negative` | `-n` | 负面提示词 | |
| `--resolution` | `-r` | 分辨率 (默认: 1024 x 1024) | |
| `--seed` | `-s` | 随机种子 | |
| `--vector-preset` | | 矢量化预设 | |
| `--output-dir` | | 输出目录 (默认: ./outputs) | |
| `--output` | `-o` | 额外输出单个 SVG 路径 | |

---

### `generate` — 生成艺术字位图

```powershell
gen2vec_cli.exe generate --text "你好" --prompt "霓虹风格"
```

| 参数 | 简写 | 说明 | 必填 |
|------|:----:|------|:----:|
| `--text` | `-t` | 艺术字文本 | ✓ |
| `--prompt` | `-p` | 风格提示词 | |
| `--negative` | `-n` | 负面提示词 | |
| `--resolution` | `-r` | 分辨率 | |
| `--seed` | `-s` | 随机种子 | |
| `--output-dir` | | 输出目录 | |
| `--output` | `-o` | 额外输出单个 PNG | |

---

### `vectorize` — 位图矢量化

```powershell
gen2vec_cli.exe vectorize --input artwork.png --preset detailed
gen2vec_cli.exe vectorize -i input.png --preset ultra --preview
```

| 参数 | 简写 | 说明 | 必填 |
|------|:----:|------|:----:|
| `--input` | `-i` | 输入图片路径 | ✓ |
| `--preset` | | 矢量化预设 | |
| `--preview` | | 同时保存预览 PNG | |
| `--output-dir` | | 输出目录 | |
| `--output` | `-o` | 额外输出单个 SVG | |

---

### `batch` — 批量流水线

输入支持 TXT / CSV / JSON。每行一条：

```text
文本 | 风格提示词 | 负面提示词 | seed | resolution
```

以 `#` 开头的行视为注释，会被跳过。

常用：

```powershell
gen2vec_cli.exe batch --input-file tests/acceptance.txt --output-dir ./outputs/cli-batch
gen2vec_cli.exe batch --text "七里香|清新国风`n夏日冰饮 50%|清爽蓝白" --seed 20260605 --seed-step 1
gen2vec_cli.exe batch --input-file batch.csv --no-vectorize
```

| 参数 | 说明 | 必填 |
|------|------|:----:|
| `--input-file` | 批量输入文件 (TXT/CSV/JSON) | (二选一) |
| `--text` | 直接传入一行或多行批量输入 | (二选一) |
| `--negative` | 全局负面提示词 | |
| `--resolution` | 全局分辨率 | |
| `--seed` | 全局随机种子 | |
| `--seed-step` | 每条 seed 递增步长 | |
| `--vector-preset` | 矢量化预设 | |
| `--no-vectorize` | 只生成 original.png，不做矢量化 | |
| `--output-dir` | 输出目录 | |

---

### `env` — 环境信息

显示当前 CLI 版本、后端 URL、health 状态、Node.js 运行时和输出目录配置。

```powershell
gen2vec_cli.exe env
```

---

### `health` — 后端健康检查

```powershell
gen2vec_cli.exe health
# txt2img 服务:    ✓ 正常
# 矢量化服务:     ✓ 正常
```

---

### `shutdown` — 关闭后端

```powershell
gen2vec_cli.exe shutdown
```

---

## 输出结构

CLI 与桌面端使用相同的产物目录结构，两者可以共用、互查。

```text
outputs/
├── task_YYYYMMDD_HHMMSS_mmm_<拼音slug>_seed-<seed>/
│   ├─ original.png            # 文生图原始位图
│   ├─ transparent.png         # 透明背景 PNG
│   ├─ result.svg              # 标准 SVG 矢量图
│   ├─ preview.png             # SVG 回渲染预览
│   ├─ metadata.json           # 结构化元数据
│   ├─ run.log                 # 运行日志
│   └─ workflows/              # ComfyUI 工作流快照
│       ├─ workflow_api.json
│       ├─ nodes.md
│       └─ model_dependencies.json
├── task_YYYYMMDD_HHMMSS_mmm_<拼音slug>_seed-<seed>/
│   └── ...
└── batch_summary.csv          # 批量模式汇总（如有）
```

---

## 环境变量

| 变量 | 默认值 | 说明 |
|------|--------|------|
| `TXT2IMG_BACKEND_URL` | `http://127.0.0.1:9001/api/v1/txt2img` | 文生图服务地址 |
| `VECTORIZER_BACKEND_URL` | `http://127.0.0.1:8000/api/v1/vectorize` | 矢量化服务地址 |
| `TXT2IMG_WORKFLOW` | (空，使用后端降级链) | ComfyUI 工作流名称 |
| `ART_TEXT_OUTPUT_ROOT` | `./outputs` | 产物输出根目录 |

---

## 矢量化预设

| 预设 | 颜色精度 | 斑点过滤 | 拐角阈值 | 长度阈值 | 图层差异 | 缩放 |
|------|:--------:|:--------:|:--------:|:--------:|:--------:|:----:|
| `clean` | 4 | 24 | 90 | 16 | 28 | 1 |
| `balanced` | 6 | 10 | 60 | 8 | 16 | 2 |
| `detailed` | 7 | 4 | 45 | 5 | 12 | 2 |
| `ultra` | 8 | 2 | 30 | 3 | 8 | 2 |
