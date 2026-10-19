# 输出样例

本目录存放可直接打开的输出样例，布局与桌面端 / CLI 的标准任务目录一致，每个任务包含：

```text
original.png      文生图原始位图
transparent.png   背景移除后的透明位图
result.svg        矢量化 SVG
preview.png       回渲染预览图
metadata.json     生成参数、工作流与质量指标
run.log           状态与耗时
```

## 样例说明

任务目录遵循标准单图命名格式：

```text
task_<YYYYMMDD_HHMMSS_mmm>_<拼音 slug>_seed-<seed>
```

| 目录 | 输入文本 | 工作流 | SVG 保真度 | 说明 |
| --- | --- | --- | ---: | --- |
| `task_20261019_204723_727_nian-nu-jiao_seed-0` | 念奴娇 | `qwen_image_2512_gguf` | 95.6 | 中文 |
| `task_20261019_204919_658_qing-shan-ji_seed-0` | 青山集 | `qwen_image_2512_gguf` | 93.0 | 中文 |
| `task_20261019_205026_273_ice-crush_seed-0` | ICE CRUSH | `flux_schnell` | 90.0 | 英文 |
| `task_20261019_205136_507_ka-fei-latte-2-0_seed-0` | 咖啡 Latte 2.0 | `test_z_image_turbo` | 99.7 | 中英混排 |

## 提示词

四条样例均为 `seed=0`、输入分辨率 `1024 x 1024`，负面提示词为空。提示词与各任务 `metadata.json` 中的 `generation.prompt` 一致。

| 输入文本 | 提示词 |
| --- | --- |
| 念奴娇 | 三个中文艺术字，霸气狂草，黑白泼墨风格，红色飞扬线条勾勒，金戈铁马之感 |
| 青山集 | 青绿色水墨质感，金色细描边，搭配山形与竹叶装饰 |
| ICE CRUSH | frozen typography, cracked ice shards, translucent blue crystals, polar frost texture |
| 咖啡 Latte 2.0 | 棕色咖啡质感，奶泡纹理，英文字母清晰 |
