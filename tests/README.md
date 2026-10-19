# 测试集与 CLI 验收

本目录提供面向评审验收的标准测试集与 Windows 原生验收脚本。脚本会先把测试集规范化为 CLI 支持的 `文本 | 风格提示词 | 负面提示词 | seed | resolution` 文本格式，再调用安装包交付的 `gen2vec_cli.exe batch`，最后核验输出产物是否满足赛题要求。

## 测试集

| 套件 | 文件 | 条数 | 用途 |
| --- | --- | ---: | --- |
| acceptance | `tests/fixtures/acceptance.txt` | 120 | 标准批量验收，满足赛题「≥100 条」要求 |

`acceptance.txt` 是单一夹具：文件顶部是 3 条以 `#route` 加空格开头的路由用例供默认模式的场景 2，后面是 120 条正文。

```text
#route 文本 | 风格提示词 | 负面提示词 | seed | resolution | 期望工作流 | 期望文本分类
...
文本 | 风格提示词
...
```

安装包中的 `tests\` 目录会进一步精简，只包含：

```text
tests/
├─ acceptance.txt
├─ run-acceptance.ps1
└─ run-acceptance.bat
```

## 运行方式

确保桌面端已启动两个后端，或手动启动 `txt2img-api:9001` 和 `vectorizer-api:8000` 后执行。评审可直接双击 `.bat`，也可以在 PowerShell 中运行 `.ps1`：

```powershell
.\tests\run-acceptance.ps1
```

对应的双击入口：

```bat
tests\run-acceptance.bat
```

脚本会自动在安装目录、仓库根目录和 `apps\cli\dist\` 中查找 `gen2vec_cli.exe`。如果评审把脚本放在其他位置，可显式指定：

```powershell
.\tests\run-acceptance.ps1 -CliPath "C:\Program Files\矢量艺术字生成器\gen2vec_cli.exe"
```

默认输出写入桌面端同一个 `outputs\` 根目录。CLI 会创建一个批量任务目录 `outputs\task_..._batch-run_...`，历史索引只写入根目录的 `outputs\tasks-index.json`。

## 核验内容

脚本会检查：

- `batch_summary.csv` 行数、失败条数、状态字段与输入文本一致性。
- 每个任务目录是否包含 `original.png`、`transparent.png`、`result.svg`、`preview.png`、`metadata.json`、`run.log` 和核心 `workflows/*` 文件。
- PNG 是否可读、默认尺寸是否为 `1664 x 928`；如果 Qwen 工作流映射到官方 16:9 尺寸 `1664 x 928`，记录为警告但不判失败。
- SVG 是否具备基础 XML 结构和可编辑矢量元素，且不允许 `<image>` 或 base64 位图伪 SVG；缺少 `viewBox` 或 `<g>` 分组记录为警告。
- `metadata.json` 是否为合法 JSON，`run.log` 是否包含状态与耗时字段。
- 单条端到端耗时与矢量化耗时默认只记录，不设硬阈值；需要性能门槛时可显式传入 `-MaxE2eMs` 或 `-MaxVectorMs`。
- 批量汇总 CSV 会包含 `generation_ms`、`vector_ms`、`png_transparency`、`svg_fidelity`，并保留 `run_log_path` 便于追溯每条任务日志。

常用调试参数：

```powershell
# 只打印场景计划，不解析夹具、不调用后端
.\tests\run-acceptance.ps1 -DryRun

# 打印包含完整套件的场景计划，不解析夹具、不调用后端
.\tests\run-acceptance.ps1 -Full -DryRun

# 核验已经跑完的一份 batch_summary.csv
.\tests\run-acceptance.ps1 -VerifyOnly outputs\task_...\batch_summary.csv

# 不做矢量化，只验 original.png、metadata、日志和汇总表
.\tests\run-acceptance.ps1 -NoVectorize
```
