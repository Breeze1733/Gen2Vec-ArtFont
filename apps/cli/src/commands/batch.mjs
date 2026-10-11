/**
 * batch 命令 - 批量生成艺术字（逐条处理，容错执行）
 *
 * 输入格式：
 *   TXT:  每行一条：文本 | 风格提示词 | 负面提示词 | seed
 *   CSV:  text,prompt,negative,seed,resolution
 *   JSON: [{ "text": "...", "prompt": "..." }]
 *
 * 设计要点：
 *   - **逐条容错**：任一条失败只记录该条的错误，不中断整批；每条都写入
 *     独立任务目录，并追加一行到 batch_summary.csv。
 *   - **格式自适应**：自动识别 JSON / 带表头的 CSV / 无表头的分隔行，
 *     分隔符按行内容在 `|`、制表符、逗号之间择一。
 *   - **字段别名宽松**：text/title/word/content 等写法都接受，降低输入门槛。
 */

import { generateArtBitmap, vectorizeImage } from '../api.mjs'
import {
  appendTaskIndex,
  augmentMetadata,
  buildRunLog,
  buildSummaryRow,
  createCliTask,
  prepareOutputTask,
  resolveStatus,
  writeTaskArtifacts,
} from '../utils/output.mjs'
import { readFile } from 'node:fs/promises'

/** 把 stdin 全部读入并解码为 UTF-8 字符串（用于 `--input -`）。 */
async function readStdin() {
  const chunks = []
  for await (const chunk of process.stdin) {
    chunks.push(Buffer.from(chunk))
  }
  return Buffer.concat(chunks).toString('utf8')
}

/**
 * 按分隔符切分一行，支持双引号包裹与 `""` 转义。
 *
 * 手写而非用现成库，是为了同时兼容 `|`、制表符、逗号三种分隔符，
 * 且保持零第三方依赖。引号内的分隔符不会被当作分隔点。
 * @param {string} line
 * @param {string} [delimiter]
 * @returns {string[]} 已 trim 的字段数组
 */
function splitDelimitedLine(line, delimiter = ',') {
  const values = []
  let current = ''
  let quoted = false

  for (let i = 0; i < line.length; i++) {
    const char = line[i]
    const next = line[i + 1]
    if (char === '"' && quoted && next === '"') {
      // 引号内的 "" 表示一个字面双引号
      current += '"'
      i += 1
    } else if (char === '"') {
      // 切换「是否在引号内」状态
      quoted = !quoted
    } else if (char === delimiter && !quoted) {
      values.push(current.trim())
      current = ''
    } else {
      current += char
    }
  }

  values.push(current.trim())
  return values
}

/**
 * 把一条原始输入（字符串或对象）归一化为标准批次条目。
 *
 * 接受多种字段别名（text/title/word/content、prompt/style/description、
 * negative/negative_prompt），空值统一为 undefined 以便上层回退到全局默认值。
 */
function normalizeBatchItem(item, index = 0) {
  // 纯字符串输入视为只有文本
  if (typeof item === 'string') {
    return { text: item.trim(), prompt: '', negative: '', seed: undefined, resolution: undefined }
  }

  const raw = item || {}
  return {
    text: String(raw.text ?? raw.title ?? raw.word ?? raw.content ?? '').trim(),
    prompt: String(raw.prompt ?? raw.style ?? raw.description ?? '').trim(),
    negative: String(raw.negative ?? raw.negative_prompt ?? '').trim(),
    seed: raw.seed === undefined || raw.seed === '' ? undefined : Number(raw.seed),
    resolution: raw.resolution ? String(raw.resolution).trim() : undefined,
    index: raw.index ?? index + 1,
  }
}

/**
 * 解析 JSON 输入：既接受顶层数组，也接受 `{ items: [...] }` 包装。
 * 过滤掉既无 text 又无 prompt 的空条目。
 */
function parseJsonInput(content) {
  const parsed = JSON.parse(content)
  const rows = Array.isArray(parsed) ? parsed : (Array.isArray(parsed.items) ? parsed.items : [])
  return rows.map(normalizeBatchItem).filter((item) => item.text || item.prompt)
}

/**
 * 解析文本输入（TXT / CSV）。
 *
 * 自动判断两件事：
 *   1. 首行是否为表头（同时含 text 与 prompt 关键词即视为表头）；
 *   2. 分隔符是什么（按行内是否出现 `|`、制表符、逗号依次判定）。
 * 有表头时按表头名映射字段，否则按「文本 | 提示词 | 负面 | seed | 分辨率」的位置约定。
 */
function parseTextInput(content) {
  // 按行切分并丢弃空行（用户常在末尾留空行）
  const lines = content.split(/\r?\n/).map((line) => line.trim()).filter(Boolean)
  if (lines.length === 0) return []

  const first = lines[0].toLowerCase()
  const hasHeader = /\btext\b/.test(first) && /\bprompt\b/.test(first)
  const rows = hasHeader ? lines.slice(1) : lines
  // 表头行的分隔符决定列名解析方式；数据行各自再判一次，容忍混用
  const headerDelimiter = lines[0].includes('|') ? '|' : (lines[0].includes('\t') ? '\t' : ',')
  const headers = hasHeader ? splitDelimitedLine(lines[0], headerDelimiter).map((name) => name.trim().toLowerCase()) : []

  return rows.map((line, index) => {
    // 数据行各自再判一次分隔符，容忍同一文件里混用 | 与 ,
    const delimiter = line.includes('|') ? '|' : (line.includes('\t') ? '\t' : ',')
    const parts = splitDelimitedLine(line, delimiter)

    // 有表头：按列名组装记录对象
    if (headers.length > 0) {
      const record = {}
      headers.forEach((key, i) => {
        record[key] = parts[i] || ''
      })
      return normalizeBatchItem(record, index)
    }

    // 无表头：按固定位置取值
    return normalizeBatchItem({
      text: parts[0] || '',
      prompt: parts[1] || '',
      negative: parts[2] || '',
      seed: parts[3] || undefined,
      resolution: parts[4] || undefined,
    }, index)
  }).filter((item) => item.text || item.prompt)
}

/**
 * 读取批量输入内容，支持三种来源：
 *   --input-file <path>  文件
 *   --input -            标准输入（管道）
 *   --input @<path>      以 @ 前缀指向文件
 *   其它                 直接当作内联文本
 */
async function readBatchContent({ input = '', inputFile = '' } = {}) {
  if (inputFile) return readFile(inputFile, 'utf8')
  if (input === '-') return readStdin()
  if (input && input.startsWith('@')) return readFile(input.slice(1), 'utf8')
  return input || ''
}

/** 读取内容并按首字符判断是 JSON 还是文本，再交给对应解析器。 */
async function parseBatchInput(options) {
  // 统一 trim：既用于判空，也避免首字符判断被前导空白干扰
  const content = (await readBatchContent(options)).trim()
  if (!content) return []
  // 以 [ 或 { 开头即认为是 JSON
  if (content.startsWith('[') || content.startsWith('{')) return parseJsonInput(content)
  return parseTextInput(content)
}

/**
 * 调用矢量化，路径方式失败时自动回退到内联 base64。
 *
 * 批量场景下位图刚由本地写出，优先传路径可避免大图 base64 传输；
 * 但若后端无法访问该路径（跨机部署等），用内存里的 base64 兜底。
 */
async function vectorizeWithPathFallback(payload, fallbackImageBase64) {
  try {
    return await vectorizeImage(payload)
  } catch (pathErr) {
    if (!fallbackImageBase64) throw pathErr
    console.warn(`  路径读取失败，回退 base64: ${pathErr.message}`)
    return vectorizeImage({
      ...payload,
      imagePath: undefined,
      generatedImage: undefined,
      imageBase64: fallbackImageBase64,
    })
  }
}

/**
 * 运行批量生成
 * @param {Object} args
 * @param {string} [args.input] - 批量输入（每行一条）或 @file / -
 * @param {string} [args.inputFile] - 批量输入文件
 * @param {string} [args.negative] - 全局负面提示词
 * @param {string} [args.resolution] - 分辨率
 * @param {number} [args.seed] - 全局随机种子
 * @param {number} [args.seedStep] - 每条 seed 递增步长（默认 0，和 desktop 对齐）
 * @param {string} [args.vectorPreset] - 矢量化预设
 * @param {Object} [args.vector] - 矢量化参数
 * @param {boolean} [args.vectorize] - 是否同时矢量化（默认 true）
 * @param {string} [args.outputDir] - 输出目录
 * @returns {Promise<{count: number, succeeded: number, failed: number, results: Array}>}
 */
export async function run(args) {
  // 解构带默认值：negative/resolution/seed 作为「条目未指定时」的全局兜底
  const {
    input,
    inputFile,
    negative = '',
    resolution = '1024 x 1024',
    seed: baseSeed = 0,
    seedStep = 0,
    vectorPreset,
    vector = {},
    // 重命名以避免与「矢量化结果」变量混淆
    vectorize: doVectorize = true,
    outputDir = 'outputs',
  } = args

  console.log('正在解析批量输入...\n')
  const items = await parseBatchInput({ input, inputFile })

  if (items.length === 0) {
    console.error('错误: 未找到有效的输入条目')
    process.exit(1)
  }

  // 批量任务自身也建一个目录：所有条目的子目录与汇总 CSV 都放在它下面，
  // 便于整批一次性打包或清理
  const startedAt = new Date().toISOString()
  const task = createCliTask({ title: '批量任务', mode: 'batch', startedAt })
  const batchTaskInfo = await prepareOutputTask({ mode: 'batch-batch', index: 0, text: 'batch-run', seed: baseSeed, startedAt, outputRoot: outputDir })
  const batchSummaryDir = batchTaskInfo.taskDir
  const summaryPath = batchTaskInfo.paths.summary

  console.log(`共 ${items.length} 条待处理`)
  console.log(`汇总目录: ${batchSummaryDir}`)
  console.log(`汇总 CSV: ${summaryPath}\n`)

  // 逐条结果与成功/失败计数：供收尾汇总与返回值使用
  const results = []
  let succeeded = 0
  let failed = 0

  // ── 逐条处理：单条失败只记账，不中断整批 ──
  for (let i = 0; i < items.length; i++) {
    const item = items[i]
    const text = item.text || ''
    const prompt = item.prompt || ''
    // 条目级参数优先，缺失时回退到命令行给的全局默认值
    const itemNegative = item.negative || negative
    const itemResolution = item.resolution || resolution
    // 种子优先级：条目自带 > baseSeed + 序号×步长（步长为 0 时全部同种子）
    const seed = item.seed ?? (Number(baseSeed) + i * Number(seedStep || 0))
    const itemTaskInfo = await prepareOutputTask({
      mode: 'batch',
      index: i + 1,
      text,
      seed,
      startedAt,
      outputRoot: batchSummaryDir,
      summaryDir: batchSummaryDir,
    })

    // 先登记「进行中」的结果对象，成功/失败时再原地更新其状态
    const result = {
      index: i + 1,
      text,
      prompt,
      seed,
      status: 'running',
      taskDir: itemTaskInfo.taskDir,
      pngPath: itemTaskInfo.paths.original,
      // 不做矢量化时该字段留空，避免指向不存在的文件
      svgPath: doVectorize ? itemTaskInfo.paths.svg : '',
      metadata: null,
      error: '',
    }

    console.log(`[${i + 1}/${items.length}] 正在处理: "${text || prompt}"`)
    // 阶段耗时与中间结果提到 try 外声明：catch 分支也要用它们写失败产物
    let stage1Duration = 0
    let stage2Duration = 0
    let genResult = null
    let workflowArtifacts = null

    try {
      // ── 阶段 1：文生图 ──
      console.log('  → 生成位图...')
      const t1 = Date.now()
      genResult = await generateArtBitmap({
        text,
        prompt,
        negative: itemNegative,
        resolution: itemResolution,
        seed,
        format: 'PNG',
      })
      stage1Duration = Date.now() - t1
      workflowArtifacts = {
        workflowApi: genResult.workflow_api || null,
        modelDependencies: genResult.model_dependencies || null,
      }

      // 先落位图（metadata 传 null 表示稍后统一写），让中途失败也至少留下原图
      await writeTaskArtifacts({
        outputRoot: itemTaskInfo.outputRoot,
        taskDir: itemTaskInfo.taskDir,
        taskName: itemTaskInfo.taskName,
        paths: itemTaskInfo.paths,
        artifacts: { original: genResult.png },
        metadata: null,
        runLog: buildRunLog({
          task,
          taskInfo: itemTaskInfo,
          modeName: 'batch',
          text,
          prompt,
          seed,
          stage1Duration,
          status: doVectorize ? 'vectorizing' : 'generated',
          usesTxt2Img: true,
          engine: genResult.metadata?.engine || '',
        }),
        usesTxt2Img: true,
        workflowArtifacts,
      })
      console.log(`  ✓ original: ${itemTaskInfo.paths.original}`)

      let metadata
      // 状态先按文生图结果判定，矢量化成功后再沿用
      let batchStatus = resolveStatus(genResult.metadata)
      if (doVectorize) {
        // ── 阶段 2：矢量化 ──
        console.log('  → 矢量化...')
        const vectorConfig = { preset: vectorPreset || vector.preset || 'balanced', ...vector }
        const t2 = Date.now()
        // 传入 genResult.png 作为回退数据：路径方式失败时可直接用内存中的图
        const vecResult = await vectorizeWithPathFallback({
          sourceType: 'generated',
          text,
          prompt,
          negative: itemNegative,
          resolution: itemResolution,
          format: 'PNG + SVG',
          seed,
          generatedImage: { file_path: itemTaskInfo.paths.original },
          imageName: genResult.image_name || `${text || `batch-${i + 1}`}-orig.png`,
          vector: vectorConfig,
          timeoutMs: 120000,
        }, genResult.png)
        stage2Duration = Date.now() - t2

        // 把阶段 1 的耗时补进元数据：矢量化返回的 generation 块默认没有该字段
        if (vecResult.metadata) {
          vecResult.metadata.generation = vecResult.metadata.generation || {}
          vecResult.metadata.generation.duration_ms = stage1Duration
        }

        // 用两阶段的完整信息重写产物（覆盖阶段 1 那次只含 original 的写入）
        metadata = augmentMetadata(vecResult.metadata, { task, taskInfo: itemTaskInfo, modeName: 'batch', text, prompt, seed, usesTxt2Img: true })
        await writeTaskArtifacts({
          outputRoot: itemTaskInfo.outputRoot,
          taskDir: itemTaskInfo.taskDir,
          taskName: itemTaskInfo.taskName,
          paths: itemTaskInfo.paths,
          artifacts: {
            original: genResult.png,
            transparent: vecResult.transparent_png,
            preview: vecResult.preview_png || vecResult.png,
            svg: vecResult.svg,
          },
          metadata,
          runLog: buildRunLog({
            task,
            taskInfo: itemTaskInfo,
            modeName: 'batch',
            text,
            prompt,
            seed,
            stage1Duration,
            stage2Duration,
            status: batchStatus,
            usesTxt2Img: true,
            engine: genResult.metadata?.engine || '',
          }),
          usesTxt2Img: true,
          summaryRow: {
            ...buildSummaryRow({
              task,
              taskInfo: itemTaskInfo,
              modeName: 'batch',
              status: batchStatus,
              text,
              prompt,
              seed,
              resolution: itemResolution,
              metadata,
              generationMs: stage1Duration,
              vectorMs: stage2Duration,
            }),
            summary_path: summaryPath,
          },
          workflowArtifacts,
        })
        console.log(`  ✓ SVG: ${itemTaskInfo.paths.svg}`)
      } else {
        // --no-vectorize：只产位图，状态标记为 generated 以区分「没做矢量化」
        metadata = augmentMetadata(genResult.metadata, { task, taskInfo: itemTaskInfo, modeName: 'batch', text, prompt, seed, usesTxt2Img: true })
        batchStatus = batchStatus === 'success' ? 'generated' : batchStatus
        await writeTaskArtifacts({
          outputRoot: itemTaskInfo.outputRoot,
          taskDir: itemTaskInfo.taskDir,
          taskName: itemTaskInfo.taskName,
          paths: itemTaskInfo.paths,
          artifacts: { original: genResult.png },
          metadata,
          runLog: buildRunLog({
            task,
            taskInfo: itemTaskInfo,
            modeName: 'batch',
            text,
            prompt,
            seed,
            stage1Duration,
            status: batchStatus,
            usesTxt2Img: true,
            engine: genResult.metadata?.engine || '',
          }),
          usesTxt2Img: true,
          summaryRow: {
            ...buildSummaryRow({
              task,
              taskInfo: itemTaskInfo,
              modeName: 'batch',
              status: batchStatus,
              text,
              prompt,
              seed,
              resolution: itemResolution,
              metadata,
              generationMs: stage1Duration,
              vectorMs: stage2Duration,
            }),
            summary_path: summaryPath,
          },
          workflowArtifacts,
        })
      }

      // 本条成功：更新结果对象与计数器
      result.status = 'success'
      result.metadata = metadata
      succeeded++
    } catch (err) {
      // 单条失败：记账 + 落一份含错误的产物目录，然后继续下一条
      result.status = 'failed'
      result.error = err.message
      failed++
      console.error(`  ✗ 失败: ${err.message}`)

      // 失败目录同样写元数据与日志，便于事后定位；位图存在时一并保留
      const metadata = augmentMetadata({ error: result.error, generation: genResult?.metadata || {} }, { task, taskInfo: itemTaskInfo, modeName: 'batch', text, prompt, seed, usesTxt2Img: true })
      await writeTaskArtifacts({
        outputRoot: itemTaskInfo.outputRoot,
        taskDir: itemTaskInfo.taskDir,
        taskName: itemTaskInfo.taskName,
        paths: itemTaskInfo.paths,
        artifacts: genResult?.png ? { original: genResult.png } : {},
        metadata,
        runLog: buildRunLog({
          task,
          taskInfo: itemTaskInfo,
          modeName: 'batch',
          text,
          prompt,
          seed,
          stage1Duration,
          stage2Duration,
          status: 'failed',
          error: result.error,
          usesTxt2Img: true,
          engine: genResult?.metadata?.engine || '',
        }),
        usesTxt2Img: true,
        summaryRow: {
          ...buildSummaryRow({
            task,
            taskInfo: itemTaskInfo,
            modeName: 'batch',
            status: 'failed',
            text,
            prompt,
            seed,
            resolution: itemResolution,
            error: result.error,
            generationMs: stage1Duration,
            vectorMs: stage2Duration,
          }),
          summary_path: summaryPath,
        },
        workflowArtifacts,
      })
    }

    // 无论成功失败都入结果列表，并空一行分隔下一条的输出
    results.push(result)
    console.log('')
  }

  // ── 收尾汇总 ──
  console.log('═'.repeat(40))
  console.log('批量处理完成:')
  console.log(`  总数:      ${items.length}`)
  console.log(`  成功:      ${succeeded}`)
  console.log(`  失败:      ${failed}`)
  console.log(`  汇总 CSV:  ${summaryPath}`)

  // 有失败项时集中列出，方便一眼看到需要重跑哪些
  const failedItems = results.filter((result) => result.status === 'failed')
  if (failedItems.length > 0) {
    console.log('\n失败条目:')
    for (const failedItem of failedItems) {
      const label = failedItem.text || `第 ${failedItem.index} 条`
      console.log(`  [${failedItem.index}] ${label}: ${failedItem.error}`)
    }
  }

  // ── 写入 tasks-index.json ──
  // 索引写入失败不影响主流程（产物已落盘），仅告警
  try {
    await appendTaskIndex(outputDir, {
      id: task.id,
      mode: 'batch',
      title: `批量任务 (${items.length} 条)`,
      time: startedAt,
      // 三态：全成功「完成」、部分成功「部分完成」、全失败「失败」
      status: failed > 0 ? (succeeded > 0 ? `部分完成（${succeeded}/${items.length}）` : '失败') : '完成',
      taskDir: batchTaskInfo.taskDir,
      paths: { summary: summaryPath, summaryDir: batchSummaryDir },
      inputParams: { mode: 'batch', batch: items.length > 10 ? `${items.length} items` : items.map(i => i.text).join(', '), negative, resolution, seed: baseSeed, seedStep },
      itemsCount: items.length,
      succeeded,
      failed,
    })
  } catch (indexErr) {
    console.warn(`  ⚠ 写入任务索引失败: ${indexErr.message}`)
  }

  return { count: items.length, succeeded, failed, results, summaryPath, outputRoot: batchTaskInfo.outputRoot }
}
