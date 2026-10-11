/**
 * vectorize 命令 - 将位图矢量化为 SVG
 *
 * 只走矢量化这一段：把本地 PNG/JPG 交给 vectorizer 后端，产出透明图、
 * SVG 与预览图，并写入标准任务目录。不调用文生图。
 */

import { vectorizeImage } from '../api.mjs'
import { readFileAsBase64, saveSvgToFile, saveBase64ToFile } from '../utils/file.mjs'
import { augmentMetadata, appendTaskIndex, buildRunLog, createCliTask, prepareOutputTask, writeTaskArtifacts } from '../utils/output.mjs'
import path from 'node:path'

/**
 * 矢量化位图
 * @param {Object} args
 * @param {string} args.input - 输入图片路径
 * @param {string} [args.output] - 输出 SVG 路径
 * @param {string} [args.preset] - 矢量化预设
 * @param {boolean} [args.preview] - 是否保存预览 PNG
 * @returns {Promise<{svgPath: string, metadata: Object}>}
 */
export async function run(args) {
  const { input, output, preset = 'balanced', preview = false, outputDir = 'outputs', vector = {} } = args

  // 回显输入路径，便于在批量调用中确认当前处理的是哪张图
  console.log(`正在矢量化: ${input}...`)

  const startedAt = new Date().toISOString()
  // 任务标题取输入文件名，便于在历史列表里辨认；
  // 任务目录名中的文本部分用「去掉扩展名的文件名」，无扩展名时回退为 uploaded-image
  const task = createCliTask({ title: path.basename(input), mode: 'vectorize', startedAt })
  const taskInfo = await prepareOutputTask({ mode: 'vectorize', index: 1, text: path.basename(input, path.extname(input)) || 'uploaded-image', seed: 0, startedAt, outputRoot: outputDir })

  // 读取输入文件
  // 先读成 base64 备用：若后端无法访问该路径（如跨机部署），可回退用内联数据
  const imageBase64 = await readFileAsBase64(input)

  let result
  const t2 = Date.now()
  try {
    // 首选传本地路径：避免大图 base64 在进程间传输的开销
    result = await vectorizeImage({
      imagePath: input,
      imageName: path.basename(input),
      sourceType: 'upload',
      vector: { preset, ...vector },
      timeoutMs: 120000,
    })
  } catch (pathErr) {
    // 路径方式失败（后端与 CLI 不在同一文件系统等）→ 回退 base64 传输
    console.warn(`  路径读取失败，回退 base64: ${pathErr.message}`)
    result = await vectorizeImage({
      imageBase64,
      imageName: path.basename(input),
      sourceType: 'upload',
      vector: { preset, ...vector },
      timeoutMs: 120000,
    })
  }
  const stage2Duration = Date.now() - t2

  // usesTxt2Img: false —— 纯矢量化不写 workflows/* 快照，元数据里 workflow_paths 也为 null
  const metadata = augmentMetadata(result.metadata, { task, taskInfo, modeName: 'vectorize', seed: 0, usesTxt2Img: false })
  await writeTaskArtifacts({
    outputRoot: taskInfo.outputRoot,
    taskDir: taskInfo.taskDir,
    taskName: taskInfo.taskName,
    paths: taskInfo.paths,
    // 矢量化产物：原图 + 透明图 + 预览 + SVG（不含位图生成阶段）
    artifacts: {
      original: imageBase64,
      transparent: result.transparent_png,
      preview: result.preview_png || result.png,
      svg: result.svg,
    },
    metadata,
    runLog: buildRunLog({ task, taskInfo, modeName: 'vectorize', stage2Duration, status: 'success', usesTxt2Img: false }),
    usesTxt2Img: false,
  })

  console.log(`✓ 任务目录: ${taskInfo.taskDir}`)
  console.log(`  SVG: ${taskInfo.paths.svg}`)
  console.log(`  preview: ${taskInfo.paths.preview}`)

  // --output 指定时额外导出一份 SVG 到该路径（任务目录内的那份仍然保留）
  if (output) {
    await saveSvgToFile(result.svg, output)
    console.log(`✓ 额外 SVG 已保存: ${output}`)
  }

  // 可选：保存预览 PNG
  // 指定了 --output 时，预览图放在其旁边并加 -preview 后缀；否则用任务目录里的默认路径
  if (preview && result.preview_png) {
    const previewPath = output
      ? (/\.svg$/i.test(output) ? output.replace(/\.svg$/i, '-preview.png') : `${output}-preview.png`)
      : taskInfo.paths.preview
    await saveBase64ToFile(result.preview_png, previewPath)
    console.log(`✓ 预览已保存: ${previewPath}`)
  }

  // 输出质量信息
  // 后端若未计算质量指标则显示 N/A，不影响产物
  if (result.metadata?.quality) {
    const q = result.metadata.quality
    console.log(`  轮廓偏差: ${q.contour_deviation?.toFixed(4) || 'N/A'}`)
    console.log(`  颜色数量: ${q.color_count || 'N/A'}`)
  }

  // ── 写入 tasks-index.json ──
  // 索引写入失败不影响主流程（产物已落盘），仅告警
  try {
    await appendTaskIndex(outputDir, {
      id: task.id,
      mode: 'vectorize',
      title: path.basename(input),
      time: startedAt,
      status: '完成',
      taskDir: taskInfo.taskDir,
      paths: taskInfo.paths,
      inputParams: { mode: 'vectorize', preset, ...vector },
    })
  } catch (indexErr) {
    console.warn(`  ⚠ 写入任务索引失败: ${indexErr.message}`)
  }

  return { svgPath: taskInfo.paths.svg, taskDir: taskInfo.taskDir, metadata }
}
