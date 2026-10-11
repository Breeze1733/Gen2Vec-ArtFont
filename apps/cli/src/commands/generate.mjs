/**
 * generate 命令 - 生成艺术字位图
 *
 * 只走文生图这一段：调用 txt2img 后端拿到 PNG，随后把产物与元数据写入
 * 标准任务目录（original.png / metadata.json / run.log）并追加任务索引。
 * 不涉及矢量化，需要 SVG 时请用 pipeline 命令。
 */

import { generateArtBitmap } from '../api.mjs'
import { saveBase64ToFile } from '../utils/file.mjs'
import { augmentMetadata, appendTaskIndex, buildRunLog, createCliTask, prepareOutputTask, resolveStatus, writeTaskArtifacts } from '../utils/output.mjs'

/**
 * 生成艺术字位图
 * @param {Object} args
 * @param {string} args.text - 艺术字文本
 * @param {string} [args.prompt] - 风格提示词
 * @param {string} [args.negative] - 负面提示词
 * @param {string} [args.resolution] - 分辨率
 * @param {number} [args.seed] - 随机种子
 * @param {string} [args.output] - 输出文件路径
 * @returns {Promise<{pngPath: string, metadata: Object}>}
 */
export async function run(args) {
  // 解构带默认值，保证后续逻辑无需再判空
  const { text, prompt = '', negative = '', resolution = '1024 x 1024', seed = 0, output, outputDir = 'outputs' } = args

  console.log(`正在生成艺术字: "${text}"...`)

  // 建立任务上下文：标题回退为「艺术字位图」，避免空文本时目录名缺少可读部分
  const startedAt = new Date().toISOString()
  const task = createCliTask({ title: text || '艺术字位图', mode: 'generate', startedAt })
  // 单独计时第一阶段（文生图），后续写入 run.log 便于定位耗时瓶颈
  const t1 = Date.now()
  const result = await generateArtBitmap({
    text,
    prompt,
    negative,
    resolution,
    seed,
    format: 'PNG',
  })
  const stage1Duration = Date.now() - t1

  // 显式指定 --output 时走「单文件直出」：只落一张 PNG，不建任务目录
  if (output) {
    await saveBase64ToFile(result.png, output)
    console.log(`✓ PNG 已保存: ${output}`)
    return { pngPath: output, metadata: result.metadata }
  }

  // 默认路径：建立标准任务目录，把产物、元数据与运行日志一并写入
  const taskInfo = await prepareOutputTask({ mode: 'generate', index: 1, text, seed, startedAt, outputRoot: outputDir })
  const status = resolveStatus(result.metadata)
  const metadata = augmentMetadata(result.metadata, { task, taskInfo, modeName: 'generate', text, prompt, seed, usesTxt2Img: true })
  await writeTaskArtifacts({
    outputRoot: taskInfo.outputRoot,
    taskDir: taskInfo.taskDir,
    taskName: taskInfo.taskName,
    paths: taskInfo.paths,
    // generate 只有原图，没有透明图 / SVG / 预览
    artifacts: { original: result.png },
    metadata,
    runLog: buildRunLog({
      task,
      taskInfo,
      modeName: 'generate',
      text,
      prompt,
      seed,
      stage1Duration,
      status,
      usesTxt2Img: true,
      // 记录实际引擎（comfyui / local-studio），便于识别是否发生降级
      engine: result.metadata?.engine || '',
    }),
    usesTxt2Img: true,
    // 快照提交给 ComfyUI 的工作流与模型依赖，供复现与审查
    workflowArtifacts: {
      workflowApi: result.workflow_api || null,
      modelDependencies: result.model_dependencies || null,
    },
  })

  console.log(`✓ 任务目录: ${taskInfo.taskDir}`)
  console.log(`  original: ${taskInfo.paths.original}`)
  console.log(`  metadata: ${taskInfo.paths.metadata}`)

  if (result.metadata) {
    console.log(`  生成耗时: ${stage1Duration}ms`)
  }

  // ── 写入 tasks-index.json ──
  // 索引写入失败不影响主流程（产物已落盘），仅告警
  try {
    await appendTaskIndex(outputDir, {
      id: task.id,
      mode: 'generate',
      title: text || '艺术字位图',
      time: startedAt,
      status: '完成',
      taskDir: taskInfo.taskDir,
      paths: taskInfo.paths,
      inputParams: { mode: 'single', text, prompt, negative, resolution, seed },
    })
  } catch (indexErr) {
    console.warn(`  ⚠ 写入任务索引失败: ${indexErr.message}`)
  }

  return { pngPath: taskInfo.paths.original, taskDir: taskInfo.taskDir, metadata }
}
