import { transliterateChinese as pinyinTransliterate, safeTaskSlug as pinyinSlug } from './pinyin.mjs'
/**
 * CLI 输出目录工具（与桌面端产物布局保持一致）。
 *
 * 桌面端会写出固定的任务产物：original.png、transparent.png、result.svg、
 * preview.png、metadata.json、run.log、workflows/* 以及批量汇总
 * batch_summary.csv。CLI 沿用同一套布局，好处是：批量跑出的结果可以直接用
 * 桌面端的历史面板查看，两端的目录结构与元数据 schema 无需再做转换。
 *
 * 本模块负责三件事：
 *   1. 目录与文件名的生成（含中文转写、时间戳、重名避让）；
 *   2. 各类产物（图片 / SVG / 元数据 / 日志 / 工作流快照）的落盘；
 *   3. 任务索引 tasks-index.json 与批量汇总 CSV 的追加维护。
 */

import fsSync from 'node:fs'
import { appendFile, mkdir, readFile, writeFile } from 'node:fs/promises'
import path from 'node:path'

/**
 * 矢量化预设表（与 vectorizer-api 的 PRESET_CONFIG 保持一致）。
 *
 * 6 个底层参数共同决定细节保留程度：color_precision（颜色层次）、
 * filter_speckle（碎块过滤）、corner_threshold（硬角判定）、
 * length_threshold（短段合并）、layer_difference（色层合并容差）、
 * scale（追踪前上采样倍率）。越靠后的预设保留细节越多、产物越大。
 */
export const VECTOR_PRESETS = {
  clean: {
    preset: 'clean',
    color_precision: 4,
    filter_speckle: 24,
    corner_threshold: 90,
    length_threshold: 16,
    layer_difference: 28,
    scale: 1,
  },
  balanced: {
    preset: 'balanced',
    color_precision: 6,
    filter_speckle: 10,
    corner_threshold: 60,
    length_threshold: 8,
    layer_difference: 16,
    scale: 2,
  },
  detailed: {
    preset: 'detailed',
    color_precision: 7,
    filter_speckle: 4,
    corner_threshold: 45,
    length_threshold: 5,
    layer_difference: 12,
    scale: 2,
  },
  ultra: {
    preset: 'ultra',
    color_precision: 8,
    filter_speckle: 2,
    corner_threshold: 30,
    length_threshold: 3,
    layer_difference: 8,
    scale: 2,
  },
}

/**
 * 解析输出根目录，优先级：显式参数 > 环境变量 > CWD 下的 outputs/。
 * @param {string} [outputRoot] - 显式指定的根目录
 * @returns {string} 绝对路径
 */
export function resolveOutputRoot(outputRoot = '') {
  // 1. 函数参数优先级最高
  if (outputRoot) return path.resolve(outputRoot)
  // 2. 环境变量次之
  if (process.env.ART_TEXT_OUTPUT_ROOT) return path.resolve(process.env.ART_TEXT_OUTPUT_ROOT)
  // 3. 默认：相对于 CWD 的 outputs/
  //    开发时从项目根运行 → 项目根/outputs/
  //    打包后 exe 在安装目录 → 安装目录/outputs/
  return path.resolve('outputs')
}

/**
 * 把批量序号格式化为 3 位零填充字符串（如 7 → "007"）。
 * 非法或非正数一律按 1 处理，保证目录名稳定。
 * @param {number|string} index
 * @returns {string}
 */
export function padTaskIndex(index) {
  const safeIndex = Number.isFinite(Number(index)) && Number(index) > 0 ? Number(index) : 1
  return String(Math.trunc(safeIndex)).padStart(3, '0')
}

/** 中文转拼音（转发到 pinyin 模块，此处仅作为本模块的统一出口）。 */
export function transliterateChinese(text) {
  return pinyinTransliterate(text)
}

/** 生成安全文件名 slug（转发到 pinyin 模块）。 */
export function safeTaskSlug(text) {
  return pinyinSlug(text)
}

/**
 * 组装任务目录名：task_<时间戳>[_<序号>]_<slug>_seed-<种子>。
 * 批量模式在时间戳后追加 3 位序号，避免同一秒内多条任务同名。
 * @returns {string} 目录名（不含父路径）
 */
export function buildTaskDirectoryName({ startedAt = null, mode = 'single', index = 1, text = 'art-text', seed = 0 } = {}) {
  const taskId = mode === 'batch' ? `${formatTaskStartedAt(startedAt)}_${padTaskIndex(index)}` : formatTaskStartedAt(startedAt)
  return `task_${taskId}_${safeTaskSlug(text)}_seed-${seed || 0}`
}

/**
 * 把时间格式化为 YYYYMMDD_HHMMSS_mmm 的紧凑时间戳（文件名安全、可字典序排序）。
 * 传入非法时间时回退为当前时刻，避免生成 "NaN" 目录名。
 * @param {string|Date|null} startedAt
 * @returns {string}
 */
export function formatTaskStartedAt(startedAt) {
  const date = startedAt ? new Date(startedAt) : new Date()
  const validDate = Number.isNaN(date.getTime()) ? new Date() : date
  const pad = (value, width = 2) => String(value).padStart(width, '0')
  return [
    validDate.getFullYear(),
    pad(validDate.getMonth() + 1),
    pad(validDate.getDate()),
    '_',
    pad(validDate.getHours()),
    pad(validDate.getMinutes()),
    pad(validDate.getSeconds()),
    '_',
    pad(validDate.getMilliseconds(), 3),
  ].join('')
}

/**
 * 准备一个任务目录并返回其上下文信息。
 *
 * 同名目录已存在时追加 _2、_3… 后缀避让，保证同一秒内重复执行也不会互相覆盖。
 * @param {Object} [options]
 * @param {string} [options.outputRoot] - 输出根目录
 * @param {string} [options.mode] - 任务模式（single | batch | vectorize）
 * @param {number} [options.index] - 批量序号
 * @param {string} [options.text] - 用于生成 slug 的文本
 * @param {number} [options.seed] - 随机种子
 * @param {string|null} [options.startedAt] - 开始时间
 * @param {string|null} [options.summaryDir] - 汇总 CSV 的存放目录
 * @returns {Promise<Object>} 任务信息（含 paths 映射）
 */
export async function prepareOutputTask({
  outputRoot = '',
  mode = 'single',
  index = 1,
  text = 'art-text',
  seed = 0,
  startedAt = null,
  summaryDir = null,
} = {}) {
  const root = resolveOutputRoot(outputRoot)
  await mkdir(root, { recursive: true })

  const baseName = buildTaskDirectoryName({ startedAt, mode, index, text, seed })
  let taskName = baseName
  let taskDir = path.join(root, taskName)
  // 重名避让计数器：从 2 开始，首次重名得到 _2
  let counter = 2

  while (fsSync.existsSync(taskDir)) {
    taskName = `${baseName}_${counter}`
    taskDir = path.join(root, taskName)
    counter += 1
  }

  await mkdir(taskDir, { recursive: true })
  return buildTaskInfo({ outputRoot: root, taskDir, taskName, mode, index, seed, summaryDir })
}

/**
 * 由任务目录推导出全部产物的标准路径。
 *
 * 集中在一处定义路径规则，保证「写产物」与「读产物」两端始终一致；
 * 批量汇总 CSV 默认放在输出根目录（除非显式指定 summaryDir）。
 * @returns {Object} { outputRoot, taskDir, taskName, mode, index, seed, paths }
 */
export function buildTaskInfo({ outputRoot, taskDir, taskName, mode, index, seed, summaryDir }) {
  const summaryRoot = summaryDir || (mode === 'batch-batch' ? taskDir : outputRoot)
  // 全部产物路径在此集中定义，写与读共用同一份规则
  const paths = {
    original: path.join(taskDir, 'original.png'),
    transparent: path.join(taskDir, 'transparent.png'),
    svg: path.join(taskDir, 'result.svg'),
    preview: path.join(taskDir, 'preview.png'),
    metadata: path.join(taskDir, 'metadata.json'),
    log: path.join(taskDir, 'run.log'),
    workflows: path.join(taskDir, 'workflows'),
    workflowApi: path.join(taskDir, 'workflows', 'workflow_api.json'),
    workflowNodes: path.join(taskDir, 'workflows', 'nodes.md'),
    modelDependencies: path.join(taskDir, 'workflows', 'model_dependencies.json'),
    summary: path.join(summaryRoot, 'batch_summary.csv'),
    summaryDir: summaryRoot,
  }
  return { outputRoot, taskDir, taskName, mode, index, seed, paths }
}

/**
 * 解析 data URL，返回其承载的字节。
 * 同时支持 base64 与百分号编码两种形式；格式不匹配返回 null。
 */
function parseDataUrl(dataUrl) {
  const match = /^data:(.+?)(;base64)?,(.*)$/s.exec(dataUrl)
  if (!match) return null
  const isBase64 = !!match[2]
  const data = match[3]
  return isBase64 ? Buffer.from(data, 'base64') : Buffer.from(decodeURIComponent(data), 'utf8')
}

/**
 * 把任意形态的产物数据统一转换为 Buffer。
 *
 * 处理顺序：
 *   - 空值 → null（调用方据此跳过落盘）
 *   - Buffer / 对象 → 原样或 JSON 序列化
 *   - 文本类产物（isText）→ 直接 UTF-8 编码
 *   - data URL → 解析出原始字节
 *   - 键名形如图片（original/transparent/preview/png）→ 按裸 base64 解码
 *   - 其余 → 当作文本处理
 * @param {*} data
 * @param {string} [key] - 产物键名，用于推断数据形态
 * @param {boolean} [isText] - 是否强制按文本处理
 * @returns {Buffer|null}
 */
function bufferForArtifact(data, key = '', isText = false) {
  if (data === undefined || data === null || data === '') return null
  if (Buffer.isBuffer(data)) return data
  if (typeof data === 'object') return Buffer.from(JSON.stringify(data, null, 2), 'utf8')

  const str = String(data)
  if (isText) return Buffer.from(str, 'utf8')

  const dataUrlBuffer = str.startsWith('data:') ? parseDataUrl(str) : null
  if (dataUrlBuffer) return dataUrlBuffer

  // 无 data: 前缀但键名表明是图片 → 按裸 base64 解码
  const imageKeys = ['original', 'transparent', 'preview', 'png']
  const lowerKey = String(key).toLowerCase()
  if (imageKeys.some((name) => lowerKey.includes(name))) {
    return Buffer.from(str.replace(/^data:[^;]+;base64,/, ''), 'base64')
  }

  return Buffer.from(str, 'utf8')
}

/**
 * 落盘单个产物文件；数据为空时跳过并返回 null。
 * 会自动创建父目录，返回写入的路径供调用方登记。
 */
async function writeArtifactFile(filePath, data, key = '', isText = false) {
  const buffer = bufferForArtifact(data, key, isText)
  if (!buffer) return null
  await mkdir(path.dirname(filePath), { recursive: true })
  await writeFile(filePath, buffer)
  return filePath
}

/**
 * 转义 CSV 字段：含引号 / 逗号 / 换行时用双引号包裹，并把内部引号翻倍。
 */
function csvEscape(value) {
  const text = value === undefined || value === null ? '' : String(value)
  if (/[",\n\r]/.test(text)) return `"${text.replace(/"/g, '""')}"`
  return text
}

/**
 * 从元数据中抽取 4 项质量指标，缺失值统一归一为空串（便于写入 CSV 与日志）。
 * @returns {{generation_ms:*, vector_ms:*, png_transparency:*, svg_fidelity:*}}
 */
function extractQualityMetrics(metadata = {}) {
  const generationMs = metadata?.generation?.duration_ms
  const vectorMs = metadata?.stats?.elapsed_ms
  const normalizedGenerationMs = generationMs === undefined || generationMs === null ? '' : generationMs
  const normalizedVectorMs = vectorMs === undefined || vectorMs === null ? '' : vectorMs
  const pngTransparency = metadata?.preprocess?.png_transparency
  const svgFidelity = metadata?.quality?.svg_fidelity
  return {
    generation_ms: normalizedGenerationMs,
    vector_ms: normalizedVectorMs,
    png_transparency: pngTransparency === undefined || pngTransparency === null ? '' : pngTransparency,
    svg_fidelity: svgFidelity === undefined || svgFidelity === null ? '' : svgFidelity,
  }
}

/**
 * 把质量指标以 key=value 形式补进 run.log。
 *
 * 采用「存在则原地替换、不存在则追加」的 upsert 语义：这样两阶段写日志时
 * （先生成、后矢量化）后写的指标能覆盖前一次的空值，而不会出现重复行。
 */
function appendQualityMetricsToRunLog(runLog = '', metadata = {}) {
  if (!runLog) return runLog
  const metrics = extractQualityMetrics(metadata)
  const upsertMetric = (text, key, value) => {
    const line = `${key}=${value}`
    const pattern = new RegExp(`(^|\\n)${key}=.*(?=\\n|$)`)
    if (pattern.test(text)) {
      return text.replace(pattern, (match, prefix) => `${prefix}${line}`)
    }
    return `${text}\n${line}`
  }
  return [
    ['generation_ms', metrics.generation_ms],
    ['vector_ms', metrics.vector_ms],
    ['png_transparency', metrics.png_transparency],
    ['svg_fidelity', metrics.svg_fidelity],
  ].reduce((text, [key, value]) => upsertMetric(text, key, value), runLog)
}

/**
 * 向批量汇总 CSV 追加一行。
 *
 * 首次写入时先输出表头，之后只追加数据行；列顺序由 columns 固定，
 * 保证多次运行追加到同一文件时列对齐。
 * @param {string} summaryPath - CSV 文件路径
 * @param {Object} [row] - 一行数据，键名与 columns 对应
 */
export async function appendBatchSummaryCsv(summaryPath, row = {}) {
  // 固定的列顺序（也是表头顺序）
  const columns = [
    'task_id',
    'task_name',
    'mode',
    'status',
    'text',
    'prompt',
    'seed',
    'resolution',
    'task_dir',
    'run_log_path',
    'generation_ms',
    'vector_ms',
    'png_transparency',
    'svg_fidelity',
    'error',
  ]
  await mkdir(path.dirname(summaryPath), { recursive: true })
  const exists = fsSync.existsSync(summaryPath)
  const line = columns.map((key) => csvEscape(row[key])).join(',')
  const content = `${exists ? '' : `${columns.join(',')}\n`}${line}\n`
  await appendFile(summaryPath, content, 'utf8')
}

/**
 * 构造工作流快照三件套（workflow_api.json / nodes.md / model_dependencies.json）。
 *
 * 调用方若已从后端拿到真实的 workflowApi / modelDependencies，则原样采用；
 * 否则退化为基于 metadata 的「运行快照」，保证产物结构始终齐全。
 */
function buildWorkflowArtifacts({ metadata = {}, taskName = '', seed = 0, mode = 'single', workflowApi = null, modelDependencies = null }) {
  if (workflowApi || modelDependencies) {
    return {
      workflowApi: workflowApi || {
        task_name: taskName,
        mode,
        seed,
        workflow: metadata?.generation?.workflow || metadata?.workflow || metadata?.engine || 'local-runtime',
        prompt: metadata?.generation?.prompt || metadata?.prompt || '',
        text: metadata?.generation?.text || '',
        resolution: metadata?.generation?.resolution || '',
        captured_at: new Date().toISOString(),
      },
      workflowNodes: `# 工作流节点说明\n\n- 任务：${taskName}\n- 模式：${mode}\n- 文生图：生成位图 original.png\n- 矢量化：生成 transparent.png、result.svg、preview.png\n\n> 当前文件为 CLI 运行快照；后续可由 txt2img-api 返回完整 ComfyUI 节点清单。\n`,
      modelDependencies: modelDependencies || {
        engine: metadata?.engine || metadata?.generation?.engine || 'unknown',
        workflow: metadata?.workflow || metadata?.generation?.workflow || null,
        style: metadata?.style || metadata?.generation?.style || null,
        note: '运行时模型依赖快照；请结合交付文档中的模型清单核验。',
      },
    }
  }

  return {
    workflowApi: {
      task_name: taskName,
      mode,
      seed,
      workflow: metadata?.generation?.workflow || metadata?.workflow || metadata?.engine || 'local-runtime',
      prompt: metadata?.generation?.prompt || metadata?.prompt || '',
      text: metadata?.generation?.text || '',
      resolution: metadata?.generation?.resolution || '',
      captured_at: new Date().toISOString(),
    },
    workflowNodes: `# 工作流节点说明\n\n- 任务：${taskName}\n- 模式：${mode}\n- 文生图：生成位图 original.png\n- 矢量化：生成 transparent.png、result.svg、preview.png\n\n> 当前文件为 CLI 运行快照；后续可由 txt2img-api 返回完整 ComfyUI 节点清单。\n`,
    modelDependencies: {
      engine: metadata?.engine || metadata?.generation?.engine || 'unknown',
      workflow: metadata?.workflow || metadata?.generation?.workflow || null,
      style: metadata?.style || metadata?.generation?.style || null,
      note: '运行时模型依赖快照；请结合交付文档中的模型清单核验。',
    },
  }
}

/**
 * 把一次任务的全部产物写入任务目录（CLI 输出的核心出口）。
 *
 * 写入内容按需触发：
 *   - 图片/SVG：artifacts 里给了才写；
 *   - metadata.json：metadata 非 null 才写（pipeline 阶段 1 会显式传 null 跳过）；
 *   - run.log：runLog 非空才写，并自动补上质量指标；
 *   - workflows/*：仅 usesTxt2Img 为真时写；
 *   - 批量汇总 CSV：给了 summaryRow 才追加。
 *
 * @param {Object} options
 * @param {string} options.taskDir - 任务目录（必填）
 * @param {Object} [options.paths] - 覆盖默认产物路径
 * @param {Object} [options.artifacts] - 待写入的产物数据（original/transparent/svg/preview）
 * @param {Object|null} [options.metadata] - 元数据；传 null 表示本次不写
 * @param {string} [options.runLog] - 运行日志文本
 * @param {boolean} [options.usesTxt2Img] - 是否涉及文生图（决定是否写工作流快照）
 * @param {Object|null} [options.workflowArtifacts] - 后端返回的工作流与模型依赖
 * @param {Object|null} [options.summaryRow] - 需要追加到批量汇总 CSV 的行
 * @returns {Promise<Object>} 任务目录、路径映射与实际写入的文件列表
 */
export async function writeTaskArtifacts({
  outputRoot,
  taskDir,
  taskName,
  paths = {},
  artifacts = {},
  metadata = {},
  runLog = '',
  usesTxt2Img = false,
  workflowArtifacts = null,
  summaryRow = null,
} = {}) {
  if (!taskDir || typeof taskDir !== 'string') {
    throw new Error('taskDir is required')
  }

  const root = path.resolve(outputRoot || path.dirname(taskDir))
  const resolvedTaskDir = path.resolve(taskDir)
  await mkdir(resolvedTaskDir, { recursive: true })

  const targetPaths = {
    original: paths.original || path.join(resolvedTaskDir, 'original.png'),
    transparent: paths.transparent || path.join(resolvedTaskDir, 'transparent.png'),
    svg: paths.svg || path.join(resolvedTaskDir, 'result.svg'),
    preview: paths.preview || path.join(resolvedTaskDir, 'preview.png'),
    metadata: paths.metadata || path.join(resolvedTaskDir, 'metadata.json'),
    log: paths.log || path.join(resolvedTaskDir, 'run.log'),
    workflows: paths.workflows || path.join(resolvedTaskDir, 'workflows'),
    workflowApi: paths.workflowApi || path.join(resolvedTaskDir, 'workflows', 'workflow_api.json'),
    workflowNodes: paths.workflowNodes || path.join(resolvedTaskDir, 'workflows', 'nodes.md'),
    modelDependencies: paths.modelDependencies || path.join(resolvedTaskDir, 'workflows', 'model_dependencies.json'),
    summary: paths.summary || path.join(root, 'batch_summary.csv'),
    summaryDir: paths.summaryDir || root,
  }

  // 逐个写四类图形产物；缺失的键会被 writeArtifactFile 自动跳过
  const savedFiles = []
  for (const [key, filePath] of [
    ['original', targetPaths.original],
    ['transparent', targetPaths.transparent],
    ['svg', targetPaths.svg],
    ['preview', targetPaths.preview],
  ]) {
    // SVG 是文本，其余按二进制/base64 处理
    const saved = await writeArtifactFile(filePath, artifacts[key], key, key === 'svg')
    if (saved) savedFiles.push(saved)
  }

  // metadata 显式传 null 表示「本次不写」（用于两阶段流程的中间态）
  const shouldWriteMetadata = metadata !== undefined && metadata !== null
  const metadataPayload = shouldWriteMetadata
    ? {
        ...(metadata || {}),
        schema_version: metadata?.schema_version || 1,
        task_name: metadata?.task_name || taskName || path.basename(resolvedTaskDir),
        output_dir: resolvedTaskDir,
        paths: {
          original: 'original.png',
          transparent: 'transparent.png',
          svg: 'result.svg',
          preview: 'preview.png',
          metadata: 'metadata.json',
          log: 'run.log',
        },
        workflow_paths: usesTxt2Img
          ? {
              workflow_api: 'workflows/workflow_api.json',
              nodes: 'workflows/nodes.md',
              model_dependencies: 'workflows/model_dependencies.json',
            }
          : null,
      }
    : null

  if (shouldWriteMetadata) {
    const saved = await writeArtifactFile(targetPaths.metadata, metadataPayload, 'metadata', true)
    if (saved) savedFiles.push(saved)
  }

  if (runLog) {
    const saved = await writeArtifactFile(targetPaths.log, appendQualityMetricsToRunLog(runLog, metadataPayload || {}), 'log', true)
    if (saved) savedFiles.push(saved)
  }

  if (usesTxt2Img) {
    // 兼容 camelCase 与 snake_case 两种来源命名
    const wfApi = workflowArtifacts?.workflowApi || workflowArtifacts?.workflow_api
    const wfDeps = workflowArtifacts?.modelDependencies || workflowArtifacts?.model_dependencies
    // 键数 > 2 才认为是「真实工作流」（而非占位快照）
    const hasRealData = wfApi && typeof wfApi === 'object' && Object.keys(wfApi).length > 2
    const fallbackWorkflow = buildWorkflowArtifacts({
      metadata: metadataPayload || {},
      taskName,
      seed: metadataPayload?.generation?.seed,
      mode: metadataPayload?.mode,
      workflowApi: wfApi,
      modelDependencies: wfDeps,
    })
    const workflow = hasRealData ? { ...fallbackWorkflow, ...workflowArtifacts } : fallbackWorkflow

    const workflowApiSaved = await writeArtifactFile(targetPaths.workflowApi, workflow.workflowApi || workflow.workflow_api || {}, 'workflowApi', true)
    const workflowNodesSaved = await writeArtifactFile(targetPaths.workflowNodes, workflow.workflowNodes || workflow.nodes || '', 'workflowNodes', true)
    const modelDepsSaved = await writeArtifactFile(targetPaths.modelDependencies, workflow.modelDependencies || workflow.model_dependencies || {}, 'modelDependencies', true)
    savedFiles.push(...[workflowApiSaved, workflowNodesSaved, modelDepsSaved].filter(Boolean))
  }

  // 批量模式：把本行追加到汇总 CSV，并补齐质量指标与默认路径
  if (summaryRow) {
    const summaryTarget = summaryRow.summary_path || targetPaths.summary
    if (summaryTarget) {
      await appendBatchSummaryCsv(summaryTarget, {
        ...extractQualityMetrics(metadataPayload || {}),
        ...summaryRow,
        task_name: summaryRow.task_name || taskName || path.basename(resolvedTaskDir),
        task_dir: summaryRow.task_dir || resolvedTaskDir,
        run_log_path: summaryRow.run_log_path || targetPaths.log,
      })
    }
  }

  return { outputRoot: root, taskDir: resolvedTaskDir, taskName: taskName || path.basename(resolvedTaskDir), paths: targetPaths, filePaths: savedFiles }
}

/**
 * 创建一个 CLI 任务对象（进程内使用，不落盘）。
 * id 取毫秒时间戳，与桌面端任务 id 规则一致，便于历史列表合并去重。
 */
export function createCliTask({ title = '', mode = 'single', startedAt = new Date().toISOString() } = {}) {
  return {
    id: Date.now(),
    title,
    startedAt,
    time: new Date(startedAt).toLocaleTimeString('zh-CN', { hour12: false }),
    status: 'running',
    mode,
  }
}

/**
 * 由元数据判定任务状态。
 *   - local-studio 引擎 → 'stub'（文生图已降级为本地占位图）
 *   - fallback_tier > 0 → 'degraded'（首选的 ComfyUI 工作流失败，换了备选）
 *   - 其余 → 'success'
 */
export function resolveStatus(metadata) {
  if (!metadata) return 'success'
  if (metadata.engine === 'local-studio') return 'stub'
  if (metadata.fallback_tier > 0) return 'degraded'
  return 'success'
}

/**
 * 生成 run.log 的 key=value 文本内容。
 *
 * 采用 key=value 而非自由文本，便于用 grep / 脚本直接解析；空 error 行会被
 * 过滤掉，避免日志里出现无意义的 `error=`。
 * @returns {string} 多行文本
 */
export function buildRunLog({
  task,
  taskInfo,
  modeName,
  text = '',
  prompt = '',
  seed = 0,
  stage1Duration = 0,
  stage2Duration = 0,
  status = 'success',
  error = '',
  usesTxt2Img = false,
  engine = '',
  metadata = {},
} = {}) {
  const paths = taskInfo?.paths || {}
  const metrics = extractQualityMetrics(metadata)
  return [
    `task_id=${task?.id || ''}`,
    `task_name=${taskInfo?.taskName || ''}`,
    `mode=${modeName}`,
    `text=${text}`,
    `prompt=${prompt}`,
    `seed=${seed}`,
    `generation_ms=${stage1Duration}`,
    `vector_ms=${stage2Duration}`,
    `uses_txt2img=${usesTxt2Img}`,
    `engine=${engine}`,
    `png_transparency=${metrics.png_transparency}`,
    `svg_fidelity=${metrics.svg_fidelity}`,
    `task_dir=${taskInfo?.taskDir || ''}`,
    `original_path=${paths.original || ''}`,
    `transparent_path=${paths.transparent || ''}`,
    `result_svg_path=${paths.svg || ''}`,
    `preview_path=${paths.preview || ''}`,
    `metadata_path=${paths.metadata || ''}`,
    `run_log_path=${paths.log || ''}`,
    `status=${status}`,
    error ? `error=${error}` : '',
  ].filter(Boolean).join('\n')
}

/**
 * 用任务上下文补全元数据，产出最终写入 metadata.json 的对象。
 *
 * 原则是「后端给的值优先」：generation 块里的 text/prompt/seed 只有缺失时才用
 * 命令行入参兜底；paths / workflow_paths 则统一改写为相对任务目录的路径，
 * 便于整个任务目录被整体搬移后仍然自洽。
 */
export function augmentMetadata(metadata, { task, taskInfo, modeName, text = '', prompt = '', seed = 0, usesTxt2Img = false } = {}) {
  return {
    ...(metadata || {}),
    schema_version: metadata?.schema_version || 1,
    task_id: String(task?.id || ''),
    task_name: taskInfo?.taskName || '',
    mode: modeName,
    output_dir: taskInfo?.taskDir || '',
    generation: {
      ...(metadata?.generation || {}),
      text: metadata?.generation?.text || text,
      prompt: metadata?.generation?.prompt || prompt,
      seed: metadata?.generation?.seed ?? seed,
    },
    paths: taskInfo?.paths
      ? {
          original: taskInfo.paths.original,
          transparent: taskInfo.paths.transparent,
          svg: taskInfo.paths.svg,
          preview: taskInfo.paths.preview,
          metadata: taskInfo.paths.metadata,
          log: taskInfo.paths.log,
        }
      : {
          original: 'original.png',
          transparent: 'transparent.png',
          svg: 'result.svg',
          preview: 'preview.png',
          metadata: 'metadata.json',
          log: 'run.log',
        },
    workflow_paths: usesTxt2Img
      ? {
          workflow_api: 'workflows/workflow_api.json',
          nodes: 'workflows/nodes.md',
          model_dependencies: 'workflows/model_dependencies.json',
        }
      : null,
  }
}

/**
 * 从 metadata.json 或 run.log 中获取缩略图路径
 */
function findThumbnailPath(taskDir) {
  const preview = path.join(taskDir, 'preview.png')
  const original = path.join(taskDir, 'original.png')
  return fsSync.existsSync(preview) ? preview
    : fsSync.existsSync(original) ? original
    : ''
}

/**
 * 向输出根目录的 tasks-index.json 追加一条任务索引记录。
 * Desktop 端在启动时读取此文件，将 CLI 产生的任务合并到历史列表中。
 *
 * @param {string} outputRoot - 输出根目录（绝对路径）
 * @param {Object} entry - 索引条目
 * @param {number|string} entry.id - 任务 ID（Date.now()）
 * @param {string} entry.mode - 任务模式 (single|batch|vectorize)
 * @param {string} entry.title - 任务标题
 * @param {string} entry.time - 任务时间
 * @param {string} entry.status - 状态文本
 * @param {string} entry.taskDir - 任务目录绝对路径
 * @param {Object} entry.paths - 产物路径映射
 * @param {number} [entry.itemsCount] - 批量任务总数
 * @param {number} [entry.succeeded] - 批量任务成功数
 * @param {number} [entry.failed] - 批量任务失败数
 */
export async function appendTaskIndex(outputRoot, entry) {
  const root = resolveOutputRoot(outputRoot)
  const indexPath = path.join(root, 'tasks-index.json')

  let index = []
  try {
    const existing = await readFile(indexPath, 'utf8')
    index = JSON.parse(existing.replace(/^\uFEFF/, ''))
  } catch {
    // 文件不存在或解析失败，从空列表开始
  }

  // 去重：相同 id 的记录覆盖
  const existingIdx = index.findIndex(e => String(e.id) === String(entry.id))
  const indexEntry = {
    id: entry.id,
    mode: entry.mode || 'single',
    title: entry.title || '',
    time: entry.time || new Date().toISOString(),
    status: entry.status || '完成',
    taskDir: entry.taskDir,
    outputRoot: root,
    paths: entry.paths || {},
    inputParams: entry.inputParams || { mode: entry.mode || 'single' },
    itemsCount: entry.itemsCount || 0,
    succeeded: entry.succeeded || 0,
    failed: entry.failed || 0,
    thumb: entry.thumb || '',
  }

  if (existingIdx >= 0) {
    index[existingIdx] = indexEntry
  } else {
    index.push(indexEntry)
  }

  // 保留最近 200 条，避免索引无限膨胀
  const trimmed = index.slice(-200)
  await mkdir(root, { recursive: true })
  await writeFile(indexPath, JSON.stringify(trimmed, null, 2), 'utf8')
}

/**
 * 构造批量汇总 CSV 的一行。
 *
 * 显式传入的 generationMs / vectorMs 会覆盖元数据里的对应值——批量场景下
 * 这两个耗时由命令层实测，比后端自报更贴近端到端体验。
 * @returns {Object} 键名与 appendBatchSummaryCsv 的 columns 对应
 */
export function buildSummaryRow({
  task,
  taskInfo,
  modeName,
  status = 'success',
  text = '',
  prompt = '',
  seed = 0,
  resolution = '',
  error = '',
  metadata = {},
  generationMs = null,
  vectorMs = null,
} = {}) {
  // 把实测耗时合并进元数据的对应字段后再统一抽取，保证取值逻辑只有一处
  const metrics = extractQualityMetrics({
    ...(metadata || {}),
    generation: {
      ...(metadata?.generation || {}),
      ...(generationMs !== null && generationMs !== undefined ? { duration_ms: generationMs } : {}),
    },
    stats: {
      ...(metadata?.stats || {}),
      ...(vectorMs !== null && vectorMs !== undefined ? { elapsed_ms: vectorMs } : {}),
    },
  })
  return {
    task_id: String(task?.id || ''),
    task_name: taskInfo?.taskName || '',
    mode: modeName,
    status,
    text,
    prompt,
    seed,
    resolution,
    task_dir: taskInfo?.taskDir || '',
    run_log_path: taskInfo?.paths?.log || '',
    generation_ms: metrics.generation_ms,
    vector_ms: metrics.vector_ms,
    png_transparency: metrics.png_transparency,
    svg_fidelity: metrics.svg_fidelity,
    error,
  }
}
