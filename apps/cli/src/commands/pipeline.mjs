/**
 * pipeline 命令 - 完整流水线：文本 → 位图 → SVG
 *
 * 一次调用串起两段后端能力：先由 txt2img 生成位图，再交给 vectorizer 矢量化，
 * 产物（原图 / 透明图 / 预览 / SVG）统一写入同一个标准任务目录。
 *
 * 两阶段各自计时并写入 run.log，便于区分耗时瓶颈在生成还是矢量化。
 */

import { generateArtBitmap, vectorizeImage } from '../api.mjs'
import { saveBase64ToFile, saveSvgToFile } from '../utils/file.mjs'
import { augmentMetadata, appendTaskIndex, buildRunLog, createCliTask, prepareOutputTask, resolveStatus, writeTaskArtifacts } from '../utils/output.mjs'

/**
 * 运行完整流水线
 * @param {Object} args
 * @param {string} args.text - 艺术字文本
 * @param {string} [args.prompt] - 风格提示词
 * @param {string} [args.negative] - 负面提示词
 * @param {string} [args.resolution] - 分辨率
 * @param {number} [args.seed] - 随机种子
 * @param {string} [args.vectorPreset] - 矢量化预设
 * @param {string} [args.outputDir] - 输出目录
 * @returns {Promise<{pngPath: string, svgPath: string, metadata: Object}>}
 */
export async function run(args) {
  // 解构带默认值，保证后续逻辑无需再判空
  const { text, prompt = '', negative = '', resolution = '1024 x 1024', seed = 0, vectorPreset, outputDir = 'outputs', output, vector = {} } = args

  // 先回显关键参数，便于用户在长时间等待前确认设置无误
  console.log(`正在生成艺术字: "${text}"...`)
  console.log(`  矢量化预设: ${vectorPreset || 'balanced'}`)

  // 先建立任务上下文：两个阶段共用同一 task / taskInfo，产物落在同一目录
  const startedAt = new Date().toISOString()
  const task = createCliTask({ title: text || '艺术字流水线', mode: 'single', startedAt })
  const taskInfo = await prepareOutputTask({ mode: 'single', index: 1, text, seed, startedAt, outputRoot: outputDir })

  // ── 阶段 1：文生图 ──
  const t1 = Date.now()
  const genResult = await generateArtBitmap({
    text,
    prompt,
    negative,
    resolution,
    seed,
    format: 'PNG',
  })
  const stage1Duration = Date.now() - t1
  // 判定本阶段是否真正跑通 ComfyUI（降级到 stub 时状态会不同）
  const stage1Status = resolveStatus(genResult.metadata)
  // 工作流快照留到阶段 2 写最终元数据时复用，故先存下来
  const workflowArtifacts = {
    workflowApi: genResult.workflow_api || null,
    modelDependencies: genResult.model_dependencies || null,
  }

  // 先把位图落盘：既让用户尽早看到中间产物，也让阶段 2 能直接传文件路径而非 base64。
  // run.log 此时的状态标记为 'vectorizing'，矢量化完成后再整体重写。
  await writeTaskArtifacts({
    outputRoot: taskInfo.outputRoot,
    taskDir: taskInfo.taskDir,
    taskName: taskInfo.taskName,
    paths: taskInfo.paths,
    artifacts: { original: genResult.png },
    // 此处先不写元数据（null），等两阶段跑完后统一写入完整元数据
    metadata: null,
    runLog: buildRunLog({
      task,
      taskInfo,
      modeName: 'single',
      text,
      prompt,
      seed,
      stage1Duration,
      status: 'vectorizing',
      usesTxt2Img: true,
      engine: genResult.metadata?.engine || '',
    }),
    usesTxt2Img: true,
    workflowArtifacts,
  })

  // ── 阶段 2：矢量化 ──
  // 预设优先级：--vector-preset 参数 > vector.preset > 默认 balanced；
  // 其余 6 个底层参数由 vector 对象透传，未传的项由后端沿用预设值
  // （下同：向量配置在所有命令里都遵循这一优先级规则）
  const vectorConfig = { preset: vectorPreset || vector.preset || 'balanced', ...vector }
  let vecResult
  const t2 = Date.now()
  try {
    vecResult = await vectorizeImage({
      sourceType: 'generated',
      text,
      prompt,
      negative,
      resolution,
      format: 'PNG + SVG',
      seed,
      // 首选传本地文件路径：避免把整张图 base64 后再传输（下方 catch 会回退 base64）
      generatedImage: { file_path: taskInfo.paths.original },
      imageName: genResult.image_name || `${text || 'art'}-orig.png`,
      vector: vectorConfig,
      timeoutMs: 120000,
    })
  } catch (pathErr) {
    // 路径方式失败（后端与 CLI 不在同一文件系统等）→ 回退用内存里的 base64
    console.warn(`  路径读取失败，回退 base64: ${pathErr.message}`)
    vecResult = await vectorizeImage({
      sourceType: 'generated',
      text,
      prompt,
      negative,
      resolution,
      format: 'PNG + SVG',
      seed,
      imageBase64: genResult.png,
      imageName: genResult.image_name || `${text || 'art'}-orig.png`,
      vector: vectorConfig,
      timeoutMs: 120000,
    })
  }
  const stage2Duration = Date.now() - t2

  // 把阶段 1 的耗时补进元数据：矢量化返回的 generation 块默认没有该字段
  if (vecResult.metadata) {
    vecResult.metadata.generation = vecResult.metadata.generation || {}
    vecResult.metadata.generation.duration_ms = stage1Duration
  }

  // 用两阶段的完整信息重写任务元数据与产物（覆盖阶段 1 那次只含 original 的写入）
  const metadata = augmentMetadata(vecResult.metadata, { task, taskInfo, modeName: 'single', text, prompt, seed, usesTxt2Img: true })
  await writeTaskArtifacts({
    outputRoot: taskInfo.outputRoot,
    taskDir: taskInfo.taskDir,
    taskName: taskInfo.taskName,
    paths: taskInfo.paths,
    artifacts: {
      original: genResult.png,
      transparent: vecResult.transparent_png,
      preview: vecResult.preview_png || vecResult.png,
      svg: vecResult.svg,
    },
    metadata,
    runLog: buildRunLog({
      task,
      taskInfo,
      modeName: 'single',
      text,
      prompt,
      seed,
      stage1Duration,
      stage2Duration,
      status: stage1Status,
      usesTxt2Img: true,
      engine: genResult.metadata?.engine || '',
    }),
    usesTxt2Img: true,
    workflowArtifacts,
  })

  // 打印任务目录与四类产物路径，便于用户直接打开查看
  console.log(`✓ 任务目录: ${taskInfo.taskDir}`)
  console.log(`  original: ${taskInfo.paths.original}`)
  console.log(`  SVG: ${taskInfo.paths.svg}`)
  console.log(`  preview: ${taskInfo.paths.preview}`)

  // --output 指定时额外导出 SVG 与位图两份；任务目录内的那份仍然保留
  if (output) {
    await saveSvgToFile(vecResult.svg, output)
    // 位图路径由 SVG 路径推导：把 .svg 换成 .png，无扩展名则直接追加
    const pngOutput = /\.svg$/i.test(output) ? output.replace(/\.svg$/i, '.png') : `${output}.png`
    await saveBase64ToFile(genResult.png, pngOutput)
    console.log(`✓ 额外输出: ${output}`)
  }

  // 输出元数据摘要：仅当对应字段存在时才打印，避免出现无意义的 N/A
  if (metadata?.generation) {
    console.log(`  生成耗时: ${stage1Duration}ms`)
  }
  if (metadata?.quality) {
    const q = metadata.quality
    console.log(`  轮廓偏差: ${q.contour_deviation?.toFixed(4) || 'N/A'}`)
  }

  // ── 写入 tasks-index.json ──
  // 索引写入失败不影响主流程（产物已落盘），仅告警
  try {
    await appendTaskIndex(outputDir, {
      id: task.id,
      mode: 'single',
      title: text || '艺术字流水线',
      time: startedAt,
      status: '完成',
      taskDir: taskInfo.taskDir,
      paths: taskInfo.paths,
      inputParams: { mode: 'single', text, prompt, negative, resolution, seed, vector: vectorConfig },
    })
  } catch (indexErr) {
    console.warn(`  ⚠ 写入任务索引失败: ${indexErr.message}`)
  }

  return { pngPath: taskInfo.paths.original, svgPath: taskInfo.paths.svg, taskDir: taskInfo.taskDir, metadata }
}
