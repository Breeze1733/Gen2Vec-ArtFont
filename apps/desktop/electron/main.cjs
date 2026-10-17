const { app, BrowserWindow, ipcMain, dialog, net, shell, Notification } = require('electron')
const path = require('path')
const fs = require('fs/promises')
const fsSync = require('fs')
const { spawn, spawnSync } = require('child_process')
const netModule = require('net')

// ── 模块级状态 ──
let splashWin = null
let mainWin = null
let txt2imgProc = null
let vectorizerProc = null
let engineDownloadProc = null
let modelDownloadProc = null
let startupState = { ready: false, modelsReady: false, backendsRunning: false, modelsSkipped: false, engineSkipped: false }
let startupError = null

// 强制 Windows 使用高性能独立显卡（NVIDIA/AMD），避免默认分配集显导致 WebGL 检测不到独显
// 必须在 app.whenReady() 之前调用
// 注：部分 Electron 版本下 commandLine 可能未就绪，包裹 try/catch 作为尽力而为的优化
try {
  if (app && app.commandLine) {
    app.commandLine.appendSwitch('force_high_performance_gpu')
  }
} catch (_) {
  // 非关键路径，静默忽略
}

const VECTORIZER_BACKEND_URL = process.env.VECTORIZER_BACKEND_URL || 'http://127.0.0.1:8000/api/v1/vectorize'
const TXT2IMG_BACKEND_URL = process.env.TXT2IMG_BACKEND_URL || 'http://127.0.0.1:9001/api/v1/txt2img'

/**
 * 由业务接口 URL 推导出对应的关闭接口地址（同 host/port，路径换成 /shutdown）。
 * URL 非法时返回 null，调用方会过滤掉。
 */
function deriveShutdownUrl(apiUrl) {
  try {
    const u = new URL(apiUrl)
    u.pathname = '/shutdown'
    return u.toString()
  } catch {
    return null
  }
}

const SHUTDOWN_URLS = [
  deriveShutdownUrl(TXT2IMG_BACKEND_URL),
  deriveShutdownUrl(VECTORIZER_BACKEND_URL),
].filter(Boolean)

// ── 后端路径解析 ──

/**
 * 返回后端资源目录（含两个 EXE 与各 .ps1 脚本）。
 * 打包态为 resources/backend；开发态返回 null —— 开发模式不自动管理后端进程。
 */
function getBackendDir() {
  if (app.isPackaged) {
    return path.join(process.resourcesPath, 'backend')
  }
  // 开发模式：不自动管理后端，由开发者手动启动
  return null
}

/**
 * 枚举验收脚本可能所在的路径。
 *
 * 打包后 tests/ 与安装目录的相对关系会因 NSIS 布局而异（同级、上级等），
 * 因此同时登记多个候选，由 resolveAcceptanceScriptPath 逐个探测取第一个存在的。
 */
function getAcceptanceScriptCandidates() {
  const candidates = []

  if (app.isPackaged) {
    const installDir = path.dirname(process.execPath)
    candidates.push(path.join(installDir, 'tests', 'run-acceptance.bat'))
    candidates.push(path.join(process.resourcesPath, '..', 'tests', 'run-acceptance.bat'))
    candidates.push(path.join(process.resourcesPath, 'tests', 'run-acceptance.bat'))
  }

  candidates.push(path.resolve(__dirname, '..', '..', '..', 'tests', 'run-acceptance.bat'))
  candidates.push(path.resolve(process.cwd(), 'tests', 'run-acceptance.bat'))
  candidates.push(path.resolve(process.cwd(), '..', '..', 'tests', 'run-acceptance.bat'))

  return [...new Set(candidates)]
}

function resolveAcceptanceScriptPath() {
  for (const candidate of getAcceptanceScriptCandidates()) {
    if (fsSync.existsSync(candidate)) {
      return candidate
    }
  }
  throw new Error('未找到 run-acceptance.bat。请确认安装目录下存在 tests\\run-acceptance.bat。')
}

// ── 端口检测 ──

/**
 * 探测本机某端口是否已被占用。
 * 做法是尝试监听：能监听到说明空闲（随即关闭），报错说明已被占用。
 * @param {number} port
 * @returns {Promise<boolean>} true 表示端口被占用
 */
function isPortInUse(port) {
  return new Promise((resolve) => {
    const server = netModule.createServer()
    // 监听报错 → 端口已被别的进程占用
    server.once('error', () => resolve(true))
    server.once('listening', () => {
      // 能监听 → 端口空闲；立刻释放探测用的临时 server
      server.close()
      resolve(false)
    })
    server.listen(port, '127.0.0.1')
  })
}

// ── ComfyUI 引擎管理 ──

/** 返回 ComfyUI 便携版的内层目录（引擎解压后的实际根目录）。 */
function getComfyUIPortableDir(backendDir) {
  return path.join(backendDir, 'ComfyUI_windows_portable_nvidia', 'ComfyUI_windows_portable')
}

/**
 * 判断 ComfyUI 引擎是否已就绪。
 * 以 `ComfyUI/main.py` 作为哨兵文件；开发模式下不管理引擎，直接视为就绪。
 */
function isComfyUIReady(backendDir) {
  if (!backendDir) return true // 开发模式跳过
  const sentinel = path.join(getComfyUIPortableDir(backendDir), 'ComfyUI', 'main.py')
  return fsSync.existsSync(sentinel)
}

/**
 * 把下载脚本上报的任务名映射为其落盘文件路径，用于轮询文件大小推算进度。
 * 同一文件在脚本里可能以「别名」或「带扩展名」两种名字上报，故两者都登记。
 */
function getEngineDownloadTrackPath(backendDir, taskName) {
  const taskPaths = {
    '7za.exe': path.join(backendDir, '7za920.zip'),
    'ComfyUI_portable': path.join(backendDir, 'ComfyUI_windows_portable_nvidia.7z'),
    'ComfyUI_windows_portable_nvidia.7z': path.join(backendDir, 'ComfyUI_windows_portable_nvidia.7z'),
    'ComfyUI-GGUF': path.join(backendDir, 'ComfyUI-GGUF.zip'),
    'ComfyUI-GGUF.zip': path.join(backendDir, 'ComfyUI-GGUF.zip'),
    'ComfyUI-Inspyrenet-Rembg': path.join(backendDir, 'ComfyUI-Inspyrenet-Rembg.zip'),
    'ComfyUI-Inspyrenet-Rembg.zip': path.join(backendDir, 'ComfyUI-Inspyrenet-Rembg.zip'),
  }
  return taskPaths[taskName] || null
}

/**
 * 调用 PowerShell 脚本下载并解压 ComfyUI 引擎（含两个自定义节点）。
 *
 * 进度反馈机制：脚本以 `ENGINEDL:` 前缀输出结构化事件行（TOTAL/START/DONE/
 * SKIP/ERROR/COMPLETE），本函数解析这些行驱动进度回调；由于脚本不报告字节级
 * 进度，额外用 2 秒定时器轮询下载文件的大小来推算速度与 ETA。
 *
 * 完成时会校验三处哨兵：ComfyUI/main.py、GGUF 节点、Inspyrenet 节点，
 * 任一缺失都视为失败——避免「脚本退出码为 0 但产物不全」的假成功。
 *
 * @param {string} backendDir - 后端资源目录（含各 .ps1 脚本）
 * @param {Function} [onProgress] - 进度回调
 * @returns {Promise<{ok:number, skip:number, fail:number}>}
 */
function downloadComfyUIEngine(backendDir, onProgress) {
  return new Promise((resolve, reject) => {
    const ps1Path = path.join(backendDir, 'download-comfyui-engine.ps1')
    if (!fsSync.existsSync(ps1Path)) {
      reject(new Error(`未找到引擎下载脚本: ${ps1Path}`))
      return
    }

    if (onProgress) {
      onProgress({ step: 1, phase: 'downloading', message: '正在准备下载推理引擎...', percent: -1, fileIndex: 0, totalFiles: 0 })
    }

    const proc = spawn('powershell.exe', [
      '-NoProfile',
      '-NonInteractive',
      '-ExecutionPolicy', 'Bypass',
      '-File', ps1Path,
      '-Electron',
      '-DestDir', backendDir
    ], {
      cwd: backendDir,
      stdio: ['ignore', 'pipe', 'pipe'],
      windowsHide: true,
    })
    engineDownloadProc = proc

    // 脚本事件驱动的计数与状态
    let totalFiles = 0
    let completedFiles = 0
    let failedFiles = 0
    let currentFileName = ''
    let currentFileSize = ''
    let finalResult = null
    // stdout 按行切分，跨 chunk 的半行留到下次拼接
    let stdoutBuffer = ''
    // 文件大小轮询跟踪（与模型下载一致）
    let trackedFiles = []
    let startedDownloading = false

    /** 解析脚本输出的 `ENGINEDL:` 事件行并更新计数、触发进度回调。 */
    const handleEngineOutputLines = (lines) => {
      for (const line of lines) {
        if (!line.startsWith('ENGINEDL:')) continue
        const parts = line.substring(9).split('|')
        const type = parts[0]

        switch (type) {
          // 脚本先告知总数，进度条据此换算整体百分比
          case 'TOTAL':
            totalFiles = parseInt(parts[1], 10) || 0
            break
          // 开始下载某个组件：登记到跟踪列表以便轮询其大小
          case 'START':
            currentFileName = parts[1] || ''
            currentFileSize = parts[2] || ''
            startedDownloading = true
            // 添加到文件大小跟踪列表
            const engineFilePath = getEngineDownloadTrackPath(backendDir, currentFileName)
            if (engineFilePath) {
              let initialSize = 0
              try { if (fsSync.existsSync(engineFilePath)) initialSize = fsSync.statSync(engineFilePath).size } catch (_) {}
              trackedFiles.push({
                name: currentFileName,
                fullPath: engineFilePath,
                lastSize: initialSize,
                lastTime: Date.now(),
                remoteSize: 0
              })
            }
            if (onProgress) {
              onProgress({
                step: 1, phase: 'downloading',
                message: `正在下载 (${completedFiles + 1}/${totalFiles || '?'}) ${currentFileName}`,
                percent: totalFiles > 0 ? Math.round((completedFiles / totalFiles) * 100) : -1,
                fileIndex: completedFiles + 1,
                totalFiles,
                fileName: currentFileName,
                fileSize: currentFileSize
              })
            }
            break
          // 组件完成或已存在被跳过：两者都推进整体进度，并移出跟踪列表
          case 'DONE':
          case 'SKIP':
            completedFiles++
            const doneName = parts[1] || currentFileName
            trackedFiles = trackedFiles.filter(f => f.name !== doneName)
            if (onProgress) {
              onProgress({
                step: 1, phase: 'downloading',
                message: type === 'SKIP' ? `跳过: ${doneName}` : `完成: ${doneName}`,
                percent: totalFiles > 0 ? Math.round((completedFiles / totalFiles) * 100) : -1,
                fileIndex: completedFiles,
                totalFiles,
                fileName: doneName,
                speed: '',
                eta: '',
                filePercent: -1
              })
            }
            break
          // 单个组件失败：仍计入已完成数（否则总进度永远到不了 100%），另计失败数
          case 'ERROR':
            completedFiles++
            failedFiles++
            const errName = parts[1] || currentFileName
            if (onProgress) {
              onProgress({
                step: 1, phase: 'downloading',
                message: `下载失败: ${errName}`,
                detail: parts[2] ? `错误: ${parts.slice(2).join(':')}` : '',
                percent: totalFiles > 0 ? Math.round((completedFiles / totalFiles) * 100) : -1,
                fileIndex: completedFiles,
                totalFiles,
                fileName: errName
              })
            }
            break
          // 脚本收尾：汇总 ok/skip/fail，并作为最终结果留存
          case 'COMPLETE':
            const ok = parseInt(parts[1], 10) || 0
            const skip = parseInt(parts[2], 10) || 0
            const fail = parseInt(parts[3], 10) || 0
            finalResult = { ok, skip, fail }
            if (onProgress) {
              onProgress({
                step: 1,
                phase: fail > 0 ? 'error' : 'complete',
                message: fail > 0 ? `引擎下载失败 (${fail} 个组件失败)` : '推理引擎就绪',
                percent: fail > 0 ? 0 : 100,
                fileIndex: totalFiles || completedFiles,
                totalFiles: totalFiles || completedFiles,
                result: finalResult
              })
            }
            break
          case 'READY':
            break
        }
      }
    }

    // 按行消费 stdout：最后一段可能是半行，留到下次拼接
    proc.stdout.on('data', (chunk) => {
      stdoutBuffer += chunk.toString()
      const lines = stdoutBuffer.split(/\r?\n/)
      stdoutBuffer = lines.pop() || ''
      handleEngineOutputLines(lines.filter(Boolean))
    })

    // 每 2 秒轮询文件大小，推算速度/ETA/百分比
    // 脚本不报告字节级进度，只能靠「文件大小的增量」反推下载速率
    const progressInterval = setInterval(() => {
      if (trackedFiles.length === 0) return

      let totalSpeed = ''
      let totalEta = ''
      let maxFilePct = -1

      for (const tf of trackedFiles) {
        try {
          if (fsSync.existsSync(tf.fullPath)) {
            const currentSize = fsSync.statSync(tf.fullPath).size
            const now = Date.now()
            const elapsedSecs = (now - tf.lastTime) / 1000

            if (elapsedSecs >= 1.5 && currentSize > tf.lastSize) {
              const bytesPerSec = (currentSize - tf.lastSize) / elapsedSecs
              if (tf.remoteSize <= 0) {
                const sizeStr = currentFileSize || ''
                if (sizeStr.includes('GB')) {
                  tf.remoteSize = parseFloat(sizeStr) * 1024 * 1024 * 1024
                } else if (sizeStr.includes('MB')) {
                  tf.remoteSize = parseFloat(sizeStr) * 1024 * 1024
                } else if (sizeStr.includes('KB')) {
                  tf.remoteSize = parseFloat(sizeStr) * 1024
                }
              }

              const speedMBs = bytesPerSec / (1024 * 1024)
              if (speedMBs >= 0.01) {
                totalSpeed = `${speedMBs.toFixed(1)} MB/s`
                if (tf.remoteSize > 0 && currentSize > 0) {
                  const remaining = tf.remoteSize - currentSize
                  if (remaining > 0 && bytesPerSec > 0) {
                    const etaSec = Math.round(remaining / bytesPerSec)
                    if (etaSec >= 3600) {
                      totalEta = `${Math.floor(etaSec / 3600)}h${Math.floor((etaSec % 3600) / 60)}m`
                    } else if (etaSec >= 60) {
                      totalEta = `${Math.floor(etaSec / 60)}m${etaSec % 60}s`
                    } else {
                      totalEta = `${etaSec}s`
                    }
                  }
                }
                if (tf.remoteSize > 0) {
                  maxFilePct = Math.min(100, Math.round((currentSize / tf.remoteSize) * 100))
                }
              }
              // 记录本次采样点，作为下次算增量的基准
              tf.lastSize = currentSize
              tf.lastTime = now
            } else if (tf.lastSize === 0 && currentSize > 0) {
              // 首次看到文件出现：只建立基准，不产生速度（此时算不出增量）
              tf.lastSize = currentSize
              tf.lastTime = now
            }
          }
        } catch (_) { /* 文件可能被锁定，跳过 */ }
      }

      if (startedDownloading && onProgress) {
        onProgress({
          step: 1, phase: 'downloading',
          message: `正在下载 (${completedFiles + 1}/${totalFiles || '?'}) ${currentFileName}`,
          percent: totalFiles > 0 ? Math.round((completedFiles / totalFiles) * 100) : -1,
          fileIndex: completedFiles + 1,
          totalFiles,
          fileName: currentFileName,
          fileSize: currentFileSize,
          speed: totalSpeed,
          eta: totalEta,
          filePercent: maxFilePct
        })
      }
    }, 2000)

    // stderr 仅记录，不参与进度解析（脚本的正常输出走 stdout）
    proc.stderr.on('data', (chunk) => {
      console.warn('[download-comfyui-engine stderr]', chunk.toString())
    })

    // 进程级失败（如 powershell 无法启动）：清理定时器与全局引用后报错
    proc.on('error', (err) => {
      clearInterval(progressInterval)
      if (engineDownloadProc === proc) engineDownloadProc = null
      reject(new Error(`启动引擎下载脚本失败: ${err.message}`))
    })

    proc.on('close', (code) => {
      clearInterval(progressInterval)
      if (engineDownloadProc === proc) engineDownloadProc = null
      // 冲刷残留的半行，避免最后一条事件被丢掉
      if (stdoutBuffer.trim()) {
        handleEngineOutputLines([stdoutBuffer.trim()])
        stdoutBuffer = ''
      }
      // 脚本异常退出且没有逐项失败记录 → 整体失败
      if (code !== 0 && failedFiles === 0) {
        reject(new Error(`引擎下载脚本异常退出 (退出码: ${code})`))
        return
      }
      const result = finalResult || {
        ok: code === 0 && failedFiles === 0 && totalFiles > 0 ? totalFiles : completedFiles - failedFiles,
        skip: 0,
        fail: failedFiles
      }
      if (!finalResult && code === 0 && failedFiles === 0 && onProgress) {
        onProgress({
          step: 1,
          phase: 'complete',
          message: '推理引擎就绪',
          percent: 100,
          fileIndex: totalFiles || completedFiles,
          totalFiles: totalFiles || completedFiles,
          result
        })
      }
      if (failedFiles > 0 && onProgress && !finalResult) {
        onProgress({
          step: 1,
          phase: 'error',
          message: `引擎下载失败 (${failedFiles} 个组件失败)`,
          percent: 0,
          fileIndex: completedFiles,
          totalFiles: totalFiles || completedFiles
        })
      }
      // 三处哨兵校验：脚本退出码为 0 不代表产物齐全，必须逐项确认
      // 验证 sentinel + GGUF
      if (!isComfyUIReady(backendDir)) {
        reject(new Error('引擎下载后未找到 ComfyUI/main.py'))
        return
      }
      // 验证 GGUF 也存在
      const ggufSentinel = path.join(getComfyUIPortableDir(backendDir), 'ComfyUI', 'custom_nodes', 'ComfyUI-GGUF', 'nodes.py')
      if (!fsSync.existsSync(ggufSentinel)) {
        reject(new Error('引擎下载后未找到 ComfyUI-GGUF custom node'))
        return
      }
      const inspyrenetDir = path.join(getComfyUIPortableDir(backendDir), 'ComfyUI', 'custom_nodes', 'ComfyUI-Inspyrenet-Rembg')
      const inspyrenetReady = fsSync.existsSync(inspyrenetDir) && fsSync
        .readdirSync(inspyrenetDir, { withFileTypes: true })
        .some((entry) => entry.isFile() && entry.name.endsWith('.py'))
      if (!inspyrenetReady) {
        reject(new Error('引擎下载后未找到 ComfyUI-Inspyrenet-Rembg custom node'))
        return
      }
      resolve(result)
    })
  })
}

// ── 模型管理 ──

function getComfyUIModelsDir(backendDir) {
  return path.join(getComfyUIPortableDir(backendDir), 'ComfyUI', 'models')
}

// 模型清单：subdir 必须是 ComfyUI 实际解析到的目录。
// diffusion_models 组的搜索顺序是 [models/unet, models/diffusion_models]（unet 在前），
// 同名文件会被 unet/ 遮蔽，所以 flux 放 diffusion_models，不能放 unet。
// 遗留目录里若留下截断的副本，会静默顶掉这里检查的完整文件（2026-06 曾因此使 Flux 完全不可用）。
const REQUIRED_MODEL_FILES = [
  ['diffusion_models', 'z_image_turbo_bf16.safetensors', 10 * 1024 ** 3, '12.3 GB'],
  ['diffusion_models', 'flux1-schnell-fp8-e4m3fn.safetensors', 10 * 1024 ** 3, '11.9 GB'],
  ['text_encoders', 'qwen_2.5_vl_7b_fp8_scaled.safetensors', 8 * 1024 ** 3, '9.4 GB'],
  ['diffusion_models', 'qwen-image-2512-Q3_K_M.gguf', 7 * 1024 ** 3, '9 GB'],
  ['text_encoders', 'qwen_3_4b.safetensors', 6 * 1024 ** 3, '8.0 GB'],
  ['text_encoders', 't5xxl_fp8_e4m3fn.safetensors', 4 * 1024 ** 3, '4.9 GB'],
  ['loras', 'Qwen-Image-Lightning-4steps-V1.0.safetensors', 1 * 1024 ** 3, '1.7 GB'],
  ['vae', 'ae.safetensors', 200 * 1024 ** 2, '335 MB'],
  ['vae', 'qwen_image_vae.safetensors', 150 * 1024 ** 2, '254 MB'],
  ['text_encoders', 'clip_l.safetensors', 100 * 1024 ** 2, '246 MB'],
  ['inspyrenet/.transparent-background', 'ckpt_base.pth', 150 * 1024 ** 2, '170 MB'],
]

/**
 * 把 REQUIRED_MODEL_FILES 展开为完整的文件路径对象列表。
 * completePath 指向下载脚本写入的「完成标记」，可用于区分「下载中」与「已完成」。
 */
function getRequiredModelPaths(backendDir) {
  const modelsDir = getComfyUIModelsDir(backendDir)
  return REQUIRED_MODEL_FILES.map(([subdir, filename, minBytes, sizeLabel]) => {
    const filePath = path.join(modelsDir, subdir, filename)
    return { subdir, filename, filePath, completePath: `${filePath}.complete`, minBytes, sizeLabel }
  })
}

/**
 * 找出缺失或体积不足的模型文件。
 *
 * 判定标准是「存在且不小于预期体积」——只判断存在会把下载中断产生的
 * 半截文件误认为完整，因此体积下限是必要的第二重校验。
 * @returns {Promise<Array>} 缺失模型列表（空数组表示齐全）
 */
async function getMissingModels(backendDir) {
  if (!backendDir) return []
  const missing = []
  for (const model of getRequiredModelPaths(backendDir)) {
    try {
      const stat = await fs.stat(model.filePath)
      if (!stat.isFile() || stat.size < model.minBytes) missing.push(model)
    } catch {
      // 文件不存在（stat 抛错）同样计入缺失
      missing.push(model)
    }
  }
  return missing
}

/** 判断模型是否齐全；开发模式不管理模型，直接返回 true。 */
async function checkModelsExist(backendDir) {
  if (!backendDir) return true // 开发模式跳过
  return (await getMissingModels(backendDir)).length === 0
}

/**
 * 调用 PowerShell 脚本下载全部 AI 模型（约 30 GB）。
 *
 * 进度机制与引擎下载一致：解析脚本输出的结构化事件行 + 轮询文件大小推算速率。
 * @param {string} backendDir - 后端资源目录
 * @param {Function} [onProgress] - 进度回调
 * @returns {Promise<Object>} 下载结果统计
 */
function downloadModels(backendDir, onProgress) {
  return new Promise((resolve, reject) => {
    const ps1Path = path.join(backendDir, 'download-models.ps1')
    if (!fsSync.existsSync(ps1Path)) {
      if (onProgress) {
        onProgress({ step: 2, phase: 'error', message: '未找到模型下载脚本，请重新安装应用', percent: 0 })
      }
      reject(new Error(`未找到模型下载脚本: ${ps1Path}`))
      return
    }

    if (onProgress) {
      onProgress({ step: 2, phase: 'downloading', message: '正在准备下载...', percent: -1, fileIndex: 0, totalFiles: 0 })
    }

    const proc = spawn('powershell.exe', [
      '-NoProfile',
      '-NonInteractive',
      '-ExecutionPolicy', 'Bypass',
      '-File', ps1Path,
      '-Electron'
    ], {
      cwd: backendDir,
      stdio: ['ignore', 'pipe', 'pipe'],
      windowsHide: true,
    })
    modelDownloadProc = proc

    let totalFiles = 0
    let completedFiles = 0
    let failedFiles = 0
    let currentFileName = ''
    let currentFileSize = ''
    let currentSubdir = ''
    let finalResult = null

    // 已开始下载的模型文件跟踪（用于轮询子进度）
    let trackedFiles = [] // [{ name, subdir, fullPath, lastSize, lastTime, remoteSize }]
    let startedDownloading = false
    let stdoutBuffer = ''

    /** 解析脚本输出的 `MODELDL:` 事件行并更新计数、触发进度回调。 */
    const handleDownloadOutputLines = (lines) => {
      for (const line of lines) {
        if (!line.startsWith('MODELDL:')) continue
        const parts = line.substring(8).split('|')
        const type = parts[0]

        switch (type) {
          case 'TOTAL':
            totalFiles = parseInt(parts[1], 10) || 0
            break
          // 开始下载某个模型：登记到跟踪列表（需同时知道子目录才能拼出路径）
          case 'START':
            currentFileName = parts[1] || ''
            currentSubdir = parts[2] || ''
            currentFileSize = parts[3] || ''
            startedDownloading = true
            // 添加到跟踪列表
            if (currentFileName && currentSubdir) {
              const modelsDir = getComfyUIModelsDir(backendDir)
              const fullPath = path.join(modelsDir, currentSubdir, currentFileName)
              let initialSize = 0
              try { if (fsSync.existsSync(fullPath)) initialSize = fsSync.statSync(fullPath).size } catch (_) {}
              trackedFiles.push({
                name: currentFileName,
                subdir: currentSubdir,
                fullPath,
                lastSize: initialSize,
                lastTime: Date.now(),
                remoteSize: 0
              })
            }
            if (onProgress) {
              onProgress({
                step: 2, phase: 'downloading',
                message: `正在下载 (${completedFiles + 1}/${totalFiles || '?'}) ${currentFileName}`,
                percent: totalFiles > 0 ? Math.round((completedFiles / totalFiles) * 100) : -1,
                fileIndex: completedFiles + 1,
                totalFiles,
                fileName: currentFileName,
                fileSize: currentFileSize
              })
            }
            break
          // 组件完成或已存在被跳过：两者都推进整体进度，并移出跟踪列表
          case 'DONE':
          case 'SKIP':
            completedFiles++
            // 从跟踪列表中移除
            const doneName = parts[1] || currentFileName
            trackedFiles = trackedFiles.filter(f => f.name !== doneName)
            if (onProgress) {
              onProgress({
                step: 2, phase: 'downloading',
                message: type === 'SKIP' ? `跳过: ${doneName}` : `完成: ${doneName}`,
                percent: totalFiles > 0 ? Math.round((completedFiles / totalFiles) * 100) : -1,
                fileIndex: completedFiles,
                totalFiles,
                fileName: doneName,
                speed: '',
                eta: '',
                filePercent: -1
              })
            }
            break
          // 单个模型失败：仍计入已完成数（否则总进度到不了 100%），另计失败数
          case 'ERROR':
            completedFiles++
            failedFiles++
            const errName = parts[1] || currentFileName
            trackedFiles = trackedFiles.filter(f => f.name !== errName)
            if (onProgress) {
              onProgress({
                step: 2, phase: 'downloading',
                message: `下载失败: ${errName}`,
                detail: parts[4] ? `错误: ${parts.slice(4).join(':')}` : '',
                percent: totalFiles > 0 ? Math.round((completedFiles / totalFiles) * 100) : -1,
                fileIndex: completedFiles,
                totalFiles,
                fileName: errName,
                errorCode: parts[3] || '0'
              })
            }
            break
          case 'COMPLETE':
            trackedFiles = []
            const ok = parseInt(parts[1], 10) || 0
            const skip = parseInt(parts[2], 10) || 0
            const fail = parseInt(parts[3], 10) || 0
            finalResult = { ok, skip, fail }
            if (onProgress) {
              onProgress({
                step: 2, phase: 'complete',
                message: fail > 0 ? `模型下载完成 (${ok} 成功, ${fail} 失败)` : '模型下载完成',
                percent: 100,
                fileIndex: totalFiles || completedFiles,
                totalFiles: totalFiles || completedFiles,
                speed: '',
                eta: '',
                filePercent: -1,
                result: finalResult
              })
            }
            break
          // 这些只是信息提示，不触发进度推送
          case 'CHECK':
          case 'RESUME':
            break
          // 握手类事件无需处理（脚本已就绪 / 引擎校验通过）
          case 'READY':
          case 'ENGINE_OK':
            break
        }
      }
    }

    proc.stdout.on('data', (chunk) => {
      stdoutBuffer += chunk.toString()
      const lines = stdoutBuffer.split(/\r?\n/)
      stdoutBuffer = lines.pop() || ''
      handleDownloadOutputLines(lines.filter(Boolean))
    })

    // 每 2 秒轮询文件大小，推算速度/ETA/百分比
    // 脚本不报告字节级进度，只能靠「文件大小的增量」反推下载速率
    const progressInterval = setInterval(() => {
      if (trackedFiles.length === 0) return

      const modelsDir = getComfyUIModelsDir(backendDir)
      let totalSpeed = ''
      let totalEta = ''
      let maxFilePct = -1

      for (const tf of trackedFiles) {
        try {
          if (fsSync.existsSync(tf.fullPath)) {
            const currentSize = fsSync.statSync(tf.fullPath).size
            const now = Date.now()
            const elapsedSecs = (now - tf.lastTime) / 1000

            // 间隔不足 1.5 秒或文件没长，说明采样窗口太短或下载停滞，跳过本次测算
            if (elapsedSecs >= 1.5 && currentSize > tf.lastSize) {
              const bytesPerSec = (currentSize - tf.lastSize) / elapsedSecs
              // 尝试获取远程总大小
              // 脚本只给「12.3 GB」这类可读字符串，首次采样时解析为字节数并缓存
              if (tf.remoteSize <= 0) {
                const sizeStr = currentFileSize || ''
                if (sizeStr.includes('GB')) {
                  tf.remoteSize = parseFloat(sizeStr) * 1024 * 1024 * 1024
                } else if (sizeStr.includes('MB')) {
                  tf.remoteSize = parseFloat(sizeStr) * 1024 * 1024
                }
              }

              const speedMBs = bytesPerSec / (1024 * 1024)
              if (speedMBs >= 0.01) {
                totalSpeed = `${speedMBs.toFixed(1)} MB/s`
                // ETA
                if (tf.remoteSize > 0 && currentSize > 0) {
                  const remaining = tf.remoteSize - currentSize
                  if (remaining > 0 && bytesPerSec > 0) {
                    const etaSec = Math.round(remaining / bytesPerSec)
                    if (etaSec >= 3600) {
                      totalEta = `${Math.floor(etaSec / 3600)}h${Math.floor((etaSec % 3600) / 60)}m`
                    } else if (etaSec >= 60) {
                      totalEta = `${Math.floor(etaSec / 60)}m${etaSec % 60}s`
                    } else {
                      totalEta = `${etaSec}s`
                    }
                  }
                }
                // 文件内百分比
                if (tf.remoteSize > 0) {
                  maxFilePct = Math.min(100, Math.round((currentSize / tf.remoteSize) * 100))
                }
              }
              // 记录本次采样点，作为下次算增量的基准
              tf.lastSize = currentSize
              tf.lastTime = now
            } else if (tf.lastSize === 0 && currentSize > 0) {
              // 首次看到文件出现：只建立基准，不产生速度（此时算不出增量）
              tf.lastSize = currentSize
              tf.lastTime = now
            }
          }
        } catch (_) { /* 文件可能被锁定，跳过 */ }
      }

      if (startedDownloading && onProgress) {
        onProgress({
          step: 2, phase: 'downloading',
          message: `正在下载 (${completedFiles + 1}/${totalFiles || '?'}) ${currentFileName}`,
          percent: totalFiles > 0 ? Math.round((completedFiles / totalFiles) * 100) : -1,
          fileIndex: completedFiles + 1,
          totalFiles,
          fileName: currentFileName,
          fileSize: currentFileSize,
          speed: totalSpeed,
          eta: totalEta,
          filePercent: maxFilePct,
          subdir: currentSubdir
        })
      }
    }, 2000)

    proc.stderr.on('data', (chunk) => {
      console.warn('[download-models stderr]', chunk.toString())
    })

    proc.on('error', (err) => {
      clearInterval(progressInterval)
      if (modelDownloadProc === proc) modelDownloadProc = null
      reject(new Error(`启动下载脚本失败: ${err.message}`))
    })

    proc.on('close', (code) => {
      clearInterval(progressInterval)
      if (modelDownloadProc === proc) modelDownloadProc = null
      // 冲刷残留的半行，避免最后一条事件被丢掉
      if (stdoutBuffer.trim()) {
        handleDownloadOutputLines([stdoutBuffer.trim()])
        stdoutBuffer = ''
      }
      if (code !== 0 && failedFiles === 0 && !finalResult) {
        // 脚本自身退出非零但无具体文件错误
        reject(new Error(`模型下载脚本异常退出 (退出码: ${code})`))
        return
      }
      const result = finalResult || {
        ok: code === 0 && failedFiles === 0 && totalFiles > 0 ? totalFiles : completedFiles - failedFiles,
        skip: 0,
        fail: failedFiles
      }
      if (!finalResult && code === 0 && failedFiles === 0 && onProgress) {
        onProgress({
          step: 2,
          phase: 'complete',
          message: '模型下载完成',
          percent: 100,
          fileIndex: totalFiles || completedFiles,
          totalFiles: totalFiles || completedFiles,
          speed: '',
          eta: '',
          filePercent: -1,
          result
        })
      }
      resolve(result)
    })
  })
}

// ── 后端进程生命周期 ──

/**
 * 启动一个后端 EXE 子进程。
 * 无窗口运行；stdout/stderr 保留管道便于主进程记录日志。
 * @param {string} exePath - 后端可执行文件路径
 * @param {string} cwd - 工作目录（后端需要在此找到 workflows/ 等资源）
 * @returns {import('child_process').ChildProcess}
 */
function spawnBackend(exePath, cwd) {
  if (!fsSync.existsSync(exePath)) {
    throw new Error(`未找到后端程序: ${exePath}`)
  }

  const proc = spawn(exePath, [], {
    cwd,
    stdio: ['ignore', 'pipe', 'pipe'],
    windowsHide: true,
  })

  // 启动失败（如文件损坏）只记录，由后续的健康检查判定超时
  proc.on('error', (err) => {
    console.error(`[backend] ${path.basename(exePath)} 进程错误:`, err.message)
  })

  return proc
}

/**
 * 终止进程树。
 * Windows 上必须用 taskkill /T —— 后端会派生 uvicorn worker 等子进程，
 * 只杀父进程会留下孤儿进程继续占用端口。
 */
function killProcessTree(proc) {
  if (!proc || proc.killed || !proc.pid) return
  try {
    if (process.platform === 'win32') {
      spawnSync('taskkill.exe', ['/PID', String(proc.pid), '/T', '/F'], {
        stdio: 'ignore',
        windowsHide: true,
      })
    } else {
      proc.kill('SIGTERM')
    }
  } catch (_) {
    // 兜底：taskkill 不可用时退回普通 kill
    try { proc.kill() } catch {}
  }
}

/**
 * 轮询健康检查端点，直到服务就绪或超时。
 *
 * 每次探测用 3 秒短超时，避免单次请求挂住整个轮询；等待期间按已用时间
 * 上报 0–99% 的进度（成功时才补到 100%）。
 * @returns {Promise<{ready:boolean, service?:string, error?:string}>}
 */
async function waitForHealthz(url, timeoutMs = 30000, onProgress) {
  const deadline = Date.now() + timeoutMs
  let lastError = null

  while (Date.now() < deadline) {
    try {
      const elapsed = timeoutMs - (deadline - Date.now())
      const percent = Math.min(99, Math.round((elapsed / timeoutMs) * 100))

      const resp = await fetch(url, { signal: AbortSignal.timeout(3000) })
      if (resp.ok) {
        const body = await resp.json().catch(() => ({}))
        if (onProgress) {
          onProgress({ phase: 'waiting', message: `${body.service || '服务'} 已就绪`, percent: 100 })
        }
        return { ready: true, service: body.service || 'unknown' }
      }
    } catch (err) {
      lastError = err
      if (onProgress) {
        const elapsed = timeoutMs - (deadline - Date.now())
        const percent = Math.min(99, Math.round((elapsed / timeoutMs) * 100))
        onProgress({ phase: 'waiting', message: '等待服务就绪...', percent })
      }
    }
    await new Promise(r => setTimeout(r, 1000))
  }

  return { ready: false, error: lastError?.message || '服务启动超时' }
}

/**
 * 探测指定端口上的后端健康状态，并核对它是否为预期服务。
 *
 * `matches` 区分「端口被占但是别的程序」与「本应用的后端已在运行」——
 * 前者需要报错提示用户，后者可以直接复用。
 * @returns {Promise<{running:boolean, matches:boolean, service:string|null}>}
 */
async function checkBackendHealth(port, expectedService) {
  try {
    const resp = await fetch(`http://127.0.0.1:${port}/healthz`, { signal: AbortSignal.timeout(2000) })
    if (resp.ok) {
      const body = await resp.json().catch(() => ({}))
      if (body.service === expectedService) {
        return { running: true, matches: true, service: body.service }
      }
      return { running: true, matches: false, service: body.service || 'unknown' }
    }
  } catch { /* 端口无响应 */ }
  return { running: false, matches: false, service: null }
}

/**
 * 启动两个后端并等待其就绪（矢量化 8000 优先，随后文生图 9001）。
 *
 * 每个后端都遵循同一套三分支逻辑：
 *   1. 端口上已是本应用的后端 → 复用，不重复拉起；
 *   2. 端口被其它程序占用 → 直接报错（避免静默连到错误的服务）；
 *   3. 端口空闲 → 拉起进程并轮询 /healthz 直到就绪。
 *
 * 开发模式（backendDir 为空）下不管理后端，由开发者自行启动。
 * @returns {Promise<{txt2imgReady:boolean, vectorizerReady:boolean}>}
 */
async function startBackends(backendDir, onProgress) {
  if (!backendDir) {
    // 开发模式：不做任何事
    if (onProgress) {
      onProgress({ step: 3, phase: 'done', message: '开发模式，后端由开发者手动管理', percent: 100 })
    }
    return { txt2imgReady: true, vectorizerReady: true }
  }

  // ── 矢量化后端 (端口 8000) ──
  if (onProgress) {
    onProgress({ step: 3, phase: 'starting_vectorizer', message: '正在启动矢量化服务...', percent: 10 })
  }

  const vecExe = path.join(backendDir, 'vectorizer-backend.exe')

  // 检查端口是否已被占用
  // 三种结果：本应用后端已在跑（复用）/ 别的程序占了（报错）/ 空闲（拉起）
  const vecHealth = await checkBackendHealth(8000, 'vectorizer-api')
  if (vecHealth.running && vecHealth.matches) {
    if (onProgress) {
      onProgress({ step: 3, phase: 'waiting_vectorizer', message: '矢量化服务已在运行，复用现有进程', percent: 50 })
    }
  } else if (vecHealth.running && !vecHealth.matches) {
    throw new Error('端口 8000 已被其他程序占用，请关闭后重试')
  } else {
    try {
      vectorizerProc = spawnBackend(vecExe, backendDir)
      const vecResult = await waitForHealthz('http://127.0.0.1:8000/healthz', 30000, (p) => {
        if (onProgress) {
          onProgress({ step: 3, phase: 'waiting_vectorizer', message: p.message, percent: 20 + Math.round(p.percent * 0.3) })
        }
      })
      if (!vecResult.ready) {
        throw new Error(`矢量化服务启动超时: ${vecResult.error}`)
      }
    } catch (err) {
      throw new Error(`矢量化服务启动失败: ${err.message}`)
    }
  }

  if (onProgress) {
    onProgress({ step: 3, phase: 'starting_txt2img', message: '正在启动文生图服务...', percent: 55 })
  }

  // ── 文生图后端 (端口 9001) ──
  const txt2imgExe = path.join(backendDir, 'txt2img-backend.exe')

  // 检查端口是否已被占用（判定逻辑与矢量化后端一致）
  const t2iHealth = await checkBackendHealth(9001, 'txt2img-api')
  if (t2iHealth.running && t2iHealth.matches) {
    if (onProgress) {
      onProgress({ step: 3, phase: 'waiting_txt2img', message: '文生图服务已在运行，复用现有进程', percent: 85 })
    }
  } else if (t2iHealth.running && !t2iHealth.matches) {
    throw new Error('端口 9001 已被其他程序占用，请关闭后重试')
  } else {
    try {
      txt2imgProc = spawnBackend(txt2imgExe, backendDir)
      const t2iResult = await waitForHealthz('http://127.0.0.1:9001/healthz', 30000, (p) => {
        if (onProgress) {
          onProgress({ step: 3, phase: 'waiting_txt2img', message: p.message, percent: 55 + Math.round(p.percent * 0.4) })
        }
      })
      if (!t2iResult.ready) {
        throw new Error(`文生图服务启动超时: ${t2iResult.error}`)
      }
    } catch (err) {
      throw new Error(`文生图服务启动失败: ${err.message}`)
    }
  }

  if (onProgress) {
    onProgress({ step: 3, phase: 'done', message: '所有服务已就绪', percent: 100 })
  }

  // 标记到全局启动状态，供渲染进程查询
  startupState.backendsRunning = true
  return { txt2imgReady: true, vectorizerReady: true }
}

// ── 启动编排 ──

/**
 * 启动编排总入口：按顺序完成引擎检查、模型检查、后端启动三个阶段。
 * 各阶段的进度通过 splash 窗口的 `splash:progress` 通道推送给启动页。
 * @param {import('electron').BrowserWindow} splashWin - 启动页窗口
 */
async function runStartupSequence(splashWin) {
  const backendDir = getBackendDir()

  // 向 splash 发送进度
  // 加 isDestroyed 判断：用户可能已关闭启动页，此时发送会抛错
  const sendProgress = (data) => {
    if (splashWin && !splashWin.isDestroyed()) {
      splashWin.webContents.send('splash:progress', data)
    }
  }

  try {
    // ── 步骤 1: 检查 / 下载 ComfyUI 引擎 ──
    // 三条分支：无脚本（打包不完整）→ 降级；引擎缺失 → 询问用户；已就绪 → 直接过
    currentStartupStep = 1
    sendProgress({ step: 1, phase: 'checking', message: '正在检查推理引擎...', percent: -1 })

    if (backendDir) {
      const enginePs1 = path.join(backendDir, 'download-comfyui-engine.ps1')
      if (!fsSync.existsSync(enginePs1)) {
        sendProgress({ step: 1, phase: 'done', message: '未包含引擎下载脚本，使用本地降级引擎', percent: 100 })
        startupState.engineSkipped = true
      } else if (!isComfyUIReady(backendDir)) {
        sendProgress({
          step: 1, phase: 'prompt',
          message: '需要下载并配置推理引擎ComfyUI',
          percent: 0,
          promptLabel: '跳过',
          promptConfirm: '开始下载',
          promptDetail: '下载后即可使用 GPU 推理生成艺术字'
        })

        const action = await waitForSplashAction()
        if (action === 'download') {
          await downloadComfyUIEngine(backendDir, sendProgress)
          startupState.engineSkipped = false
        } else {
          sendProgress({ step: 1, phase: 'done', message: '已跳过引擎下载，文生图使用本地降级引擎', percent: 100 })
          startupState.engineSkipped = true
        }
      } else {
        sendProgress({ step: 1, phase: 'done', message: '推理引擎就绪', percent: 100 })
      }
    } else {
      sendProgress({ step: 1, phase: 'done', message: '推理引擎就绪 (开发模式)', percent: 100 })
    }

    // ── 步骤 2: 检查 / 下载模型 ──
    currentStartupStep = 2
    sendProgress({ step: 2, phase: 'checking', message: '正在检查 AI 模型...', percent: -1 })

    // 引擎被跳过时模型也无从加载，直接连带跳过（避免白跑一遍体积校验）
    if (startupState.engineSkipped) {
      sendProgress({ step: 2, phase: 'done', message: '引擎未安装，跳过模型检查', percent: 100 })
      startupState.modelsSkipped = true
    } else {
      const missingModels = backendDir ? await getMissingModels(backendDir) : []
      if (missingModels.length > 0 && backendDir) {
        const ps1Path = path.join(backendDir, 'download-models.ps1')
        if (!fsSync.existsSync(ps1Path)) {
          sendProgress({ step: 2, phase: 'done', message: '未包含模型下载脚本，使用本地降级引擎', percent: 100 })
          startupState.modelsSkipped = true
        } else {
          sendProgress({
            step: 2, phase: 'prompt',
            message: '需要下载本地 AI 模型',
            percent: 0,
            promptLabel: '跳过',
            promptConfirm: '开始下载',
            promptDetail: '下载脚本会自动跳过已存在的模型'
          })

          const action = await waitForSplashAction()
          if (action === 'download') {
            const result = await downloadModels(backendDir, sendProgress)
            startupState.modelsReady = result.fail === 0
            startupState.modelsSkipped = false
          } else {
            sendProgress({ step: 2, phase: 'done', message: '已跳过模型下载，文生图使用本地降级引擎', percent: 100 })
            startupState.modelsSkipped = true
            startupState.modelsReady = false
          }
        }
      } else {
        sendProgress({ step: 2, phase: 'done', message: 'AI 模型就绪', percent: 100 })
        startupState.modelsReady = true
      }
    }

    // ── 步骤 3: 启动后端服务 ──
    currentStartupStep = 3
    sendProgress({ step: 3, phase: 'starting', message: '正在启动后端服务...', percent: -1 })

    await startBackends(backendDir, sendProgress)

    // ── 步骤 4: 完成 ──
    sendProgress({ step: 4, phase: 'complete', message: '正在启动应用...', percent: 100 })
    startupState.ready = true

    return { success: true }

  } catch (err) {
    startupError = err.message
    sendProgress({
      step: currentStartupStep,
      phase: 'error',
      message: err.message,
      percent: 0
    })
    return { success: false, error: err.message }
  }
}

// 当前启动步骤（错误时使用）
let currentStartupStep = 1

// 等待 splash 窗口中用户的操作
// 用 EventEmitter 把「启动流程」与「用户点击」解耦：流程侧 await 一个 Promise，
// 点击侧通过 IPC 触发事件，无需把 resolve 回调层层透传
const EventEmitter = require('events')
class SplashActionEmitter extends EventEmitter {}
const splashActions = new SplashActionEmitter()
const SPLASH_ACTION_EVENT = 'splash-action'

/**
 * 挂起等待用户在启动页上做出选择（下载 / 跳过）。
 * @returns {Promise<string>} 用户选择的 action 标识
 */
function waitForSplashAction() {
  return new Promise((resolve) => {
    // once：只消费第一个动作，避免后续误触再次唤醒已结束的等待
    splashActions.once(SPLASH_ACTION_EVENT, (action) => {
      resolve(action)
    })
  })
}

ipcMain.on('splash:action', (_event, data) => {
  // data 可以是 { action: 'xxx' } 对象或 'xxx' 字符串（preload 传的是对象）
  let action
  if (typeof data === 'string') {
    action = data
  } else if (data && typeof data === 'object') {
    action = data.action
  }

  if (action) {
    // 窗口控制类动作就地处理，不进入启动流程的等待队列
    if (action === 'minimize-window') {
      const win = BrowserWindow.fromWebContents(_event.sender)
      if (win && !win.isDestroyed()) win.minimize()
      return
    }

    // Generic download/skip/retry actions go to waitForSplashAction
    // 退出应用需要走完整清理流程，不能直接 process.exit
    if (action === 'exit-app') {
      forceQuit()
    } else {
      splashActions.emit(SPLASH_ACTION_EVENT, action)
    }
  }
})

// ── 窗口管理 ──

/**
 * 解析应用图标路径，优先 PNG（Linux/macOS），回退 ICO（Windows）。
 * 两者都不存在时返回 undefined，交给 Electron 使用默认图标。
 */
function getAppIcon() {
  const iconPng = path.join(__dirname, '..', 'assets', 'icon.png')
  if (fsSync.existsSync(iconPng)) return iconPng
  const iconIco = path.join(__dirname, '..', 'assets', 'icon.ico')
  if (fsSync.existsSync(iconIco)) return iconIco
  return undefined
}

/**
 * 创建启动页窗口（无边框、置顶、不可缩放）。
 * show: false + ready-to-show 再显示，避免白屏闪烁。
 */
function createSplashWindow() {
  const win = new BrowserWindow({
    width: 520,
    height: 580,
    resizable: false,
    frame: false,
    alwaysOnTop: true,
    center: true,
    show: false,
    backgroundColor: '#f7f3ea',
    icon: getAppIcon(),
    // 与主窗口一致的安全配置：contextIsolation 开启、禁用 nodeIntegration
    webPreferences: {
      preload: path.join(__dirname, 'preload.cjs'),
      contextIsolation: true,
      nodeIntegration: false,
    },
  })

  win.loadFile(path.join(__dirname, 'splash.html'))

  win.once('ready-to-show', () => {
    win.show()
  })

  return win
}

/**
 * 创建主窗口。
 * 有 VITE_DEV_SERVER_URL 时加载开发服务器并打开 DevTools；否则加载打包产物。
 */
function createMainWindow() {
  const win = new BrowserWindow({
    width: 1080,
    height: 760,
    minWidth: 920,
    minHeight: 640,
    title: '矢量艺术字生成器',
    show: false,
    icon: getAppIcon(),
    webPreferences: {
      preload: path.join(__dirname, 'preload.cjs'),
      contextIsolation: true,
      nodeIntegration: false
    }
  })

  // 窗口准备好后最大化显示
  win.once('ready-to-show', () => {
    win.maximize()
    win.show()
  })

  if (process.env.VITE_DEV_SERVER_URL) {
    win.loadURL(process.env.VITE_DEV_SERVER_URL)
    win.webContents.openDevTools({ mode: 'detach' })
    return win
  }

  win.loadFile(path.join(__dirname, '../dist/index.html'))
  return win
}

/** 创建主窗口并登记到模块级引用，窗口关闭时清空以便重建。 */
function showMainWindow() {
  mainWin = createMainWindow()
  mainWin.on('closed', () => {
    mainWin = null
  })
  return mainWin
}

/**
 * 解析 data URL 为字节。
 * 不是 data URL 时按普通文本处理（用于容忍调用方直接传裸内容）。
 */
function parseDataUrl(dataUrl) {
  const match = /^data:(.+?)(;base64)?,(.*)$/s.exec(dataUrl)
  if (!match) {
    return Buffer.from(dataUrl, 'utf8')
  }

  const isBase64 = !!match[2]
  const data = match[3]
  return isBase64 ? Buffer.from(data, 'base64') : Buffer.from(decodeURIComponent(data), 'utf8')
}

/**
 * 解析产物输出根目录，优先级：环境变量 > 按运行形态推导的默认目录。
 * 开发态落在项目根的 outputs/，打包态落在安装目录的 outputs/
 * （与 gen2vec_cli.exe 同级，便于用户找到 CLI 与桌面端的共同产物）。
 */
function getOutputRoot() {
  if (process.env.ART_TEXT_OUTPUT_ROOT) {
    return path.resolve(process.env.ART_TEXT_OUTPUT_ROOT)
  }

  const appPath = app.getAppPath()
  // 开发模式下 appPath 通常是 apps/desktop，按项目根目录输出到 outputs/
  // 打包后 resources/app.asar 上一级是 resources/，再上一级是安装目录
  // 统一回到项目根目录或安装目录
  if (!app.isPackaged && path.basename(appPath) === 'desktop') {
    return path.resolve(appPath, '..', '..', 'outputs')
  }

  // 打包后：安装目录下的 outputs/（与 gen2vec_cli.exe 同级）
  const installDir = path.dirname(process.execPath)
  return path.resolve(installDir, 'outputs')
}

/** 批量序号格式化为 3 位零填充（非法或非正数按 1 处理）。 */
function padTaskIndex(index) {
  const safeIndex = Number.isFinite(Number(index)) && Number(index) > 0 ? Number(index) : 1
  return String(Math.trunc(safeIndex)).padStart(3, '0')
}

const { transliterateChinese, safeTaskSlug } = require('./pinyin.cjs')

/**
 * 组装任务目录名：task_<时间戳>[_<序号>]_<slug>_seed-<种子>。
 * 规则与 apps/cli/src/utils/output.mjs 保持一致，保证两端产物可互相识别。
 */
function buildTaskDirectoryName({ startedAt = null, mode = 'single', index = 1, text = 'art-text', seed = 0 } = {}) {
  const taskId = mode === 'batch' ? `${formatTaskStartedAt(startedAt)}_${padTaskIndex(index)}` : formatTaskStartedAt(startedAt)
  return `task_${taskId}_${safeTaskSlug(text)}_seed-${seed || 0}`
}

/** 时间格式化为 YYYYMMDD_HHMMSS_mmm；非法时间回退当前时刻。 */
function formatTaskStartedAt(startedAt) {
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
    pad(validDate.getMilliseconds(), 3)
  ].join('')
}

/**
 * 创建唯一的任务目录（重名时追加 _2、_3… 避让）并返回其路径上下文。
 * @returns {Promise<Object>} 含 paths 映射的任务信息
 */
async function ensureUniqueTaskDir({ outputRoot, mode = 'single', index = 1, text = 'art-text', seed = 0, startedAt = null, summaryDir = null }) {
  const root = path.resolve(outputRoot || getOutputRoot())
  await fs.mkdir(root, { recursive: true })

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

  await fs.mkdir(taskDir, { recursive: true })
  return buildTaskInfo({ outputRoot: root, taskDir, taskName, mode, index, seed, summaryDir })
}

/** 由任务目录推导全部产物的标准路径（与 CLI 端 buildTaskInfo 规则一致）。 */
function buildTaskInfo({ outputRoot, taskDir, taskName, mode, index, seed, summaryDir }) {
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
    summaryDir: summaryRoot
  }
  return { outputRoot, taskDir, taskName, mode, index, seed, paths }
}

/**
 * 把任意形态的产物数据转为 Buffer（空值返回 null，由调用方跳过落盘）。
 * 对象按 JSON 缩进序列化；非文本且是 data URL 时解码其原始字节。
 */
function bufferForArtifact(data, isText = false) {
  if (data === undefined || data === null || data === '') return null
  if (Buffer.isBuffer(data)) return data
  if (typeof data === 'object') return Buffer.from(JSON.stringify(data, null, 2), 'utf8')
  const str = String(data)
  if (!isText && str.startsWith('data:')) return parseDataUrl(str)
  return Buffer.from(str, 'utf8')
}

/** 落盘单个产物文件；自动建父目录，数据为空时返回 null 表示跳过。 */
async function writeArtifactFile(filePath, data, isText = false) {
  const buffer = bufferForArtifact(data, isText)
  if (!buffer) return null
  await fs.mkdir(path.dirname(filePath), { recursive: true })
  await fs.writeFile(filePath, buffer)
  return filePath
}

/** CSV 字段转义：含引号/逗号/换行时用双引号包裹，内部引号翻倍。 */
function csvEscape(value) {
  const text = value === undefined || value === null ? '' : String(value)
  if (/[",\n\r]/.test(text)) return `"${text.replace(/"/g, '""')}"`
  return text
}

/**
 * 从元数据抽取 4 项质量指标；缺失值统一归一为空串，便于写入 CSV 与日志。
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
    svg_fidelity: svgFidelity === undefined || svgFidelity === null ? '' : svgFidelity
  }
}

/**
 * 把质量指标以 key=value 形式 upsert 进 run.log。
 * 采用「存在则替换、不存在则追加」：两阶段写日志时后写的指标能覆盖前次的空值，
 * 而不会产生重复行。
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
    ['svg_fidelity', metrics.svg_fidelity]
  ].reduce((text, [key, value]) => upsertMetric(text, key, value), runLog)
}

/**
 * 向批量汇总 CSV 追加一行；首次写入时先输出表头。
 * 列顺序由 columns 固定，保证多次运行追加到同一文件时列对齐。
 */
async function appendBatchSummaryCsv(summaryPath, row = {}) {
  // 固定的列顺序（也是表头顺序）
  const columns = [
    'task_id', 'task_name', 'mode', 'status', 'text', 'prompt', 'seed', 'resolution', 'task_dir',
    'run_log_path',
    'generation_ms', 'vector_ms', 'png_transparency', 'svg_fidelity', 'error'
  ]
  await fs.mkdir(path.dirname(summaryPath), { recursive: true })
  const exists = fsSync.existsSync(summaryPath)
  const line = columns.map((key) => csvEscape(row[key])).join(',')
  const content = `${exists ? '' : `${columns.join(',')}\n`}${line}\n`
  await fs.appendFile(summaryPath, content, 'utf8')
}

/**
 * 为「按旧协议只传了 key + data」的产物推断文件扩展名。
 *
 * 优先看键名语义（svg / metadata / log / 图片类），键名不足以判断时再看内容
 * 特征（`<svg`、`{`、`data:image/`），最后退回 txt / bin。
 */
function inferLegacyExt(key, data) {
  const lowerKey = String(key).toLowerCase()
  const str = typeof data === 'string' ? data.trim() : ''
  if (lowerKey === 'svg' || lowerKey.includes('svg') || str.startsWith('<svg')) return 'svg'
  if (lowerKey === 'metadata' || lowerKey.includes('meta') || str.startsWith('{') || str.startsWith('[')) return 'json'
  if (lowerKey.includes('log')) return 'log'
  if (str.startsWith('data:image/') || ['original', 'transparent', 'preview', 'png'].some(k => lowerKey.includes(k))) return 'png'
  if (typeof data === 'string') return 'txt'
  return 'bin'
}

/**
 * 构造工作流快照三件套（workflow_api.json / nodes.md / model_dependencies.json）。
 * 调用方已拿到后端真实数据时原样采用，否则退化为基于 metadata 的运行快照，
 * 保证产物结构始终齐全。
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
        captured_at: new Date().toISOString()
      },
      workflowNodes: `# 工作流节点说明\n\n- 任务：${taskName}\n- 模式：${mode}\n- 文生图：生成位图 original.png\n- 矢量化：生成 transparent.png、result.svg、preview.png\n\n> 当前文件为桌面端运行快照；后续可由 txt2img-api 返回完整 ComfyUI 节点清单。\n`,
      modelDependencies: modelDependencies || {
        engine: metadata?.engine || metadata?.generation?.engine || 'unknown',
        workflow: metadata?.workflow || metadata?.generation?.workflow || null,
        style: metadata?.style || metadata?.generation?.style || null,
        note: '运行时模型依赖快照；请结合交付文档中的模型清单核验。'
      }
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
      captured_at: new Date().toISOString()
    },
    workflowNodes: `# 工作流节点说明\n\n- 任务：${taskName}\n- 模式：${mode}\n- 文生图：生成位图 original.png\n- 矢量化：生成 transparent.png、result.svg、preview.png\n\n> 当前文件为桌面端运行快照；后续可由 txt2img-api 返回完整 ComfyUI 节点清单。\n`,
    modelDependencies: {
      engine: metadata?.engine || metadata?.generation?.engine || 'unknown',
      workflow: metadata?.workflow || metadata?.generation?.workflow || null,
      style: metadata?.style || metadata?.generation?.style || null,
      note: '运行时模型依赖快照；请结合交付文档中的模型清单核验。'
    }
  }
}

/**
 * 通过 Electron net 模块向后端发起 POST 请求（走 Chromium 网络栈，不受 Node 网络策略影响）。
 *
 * 超时控制用「外层 setTimeout + request.abort」而非 fetch 的 signal：
 * Electron 的 net.request 没有原生超时，必须手动兜底，否则请求可能永久挂起。
 * `settled` 标志保证 resolve/reject 只生效一次（超时与响应可能同时到达）。
 *
 * @param {string} apiUrl - 完整接口 URL
 * @param {Object} payload - 请求体；其中的 `__timeoutMs` 会被取出作为超时值，不发给后端
 */
async function requestBackend(apiUrl, payload) {
  return new Promise((resolve, reject) => {
    let request = null
    let settled = false
    let timer = null

    /** 幂等的收尾：只允许第一次调用生效，并清理超时定时器。 */
    const finish = (fn, value) => {
      if (settled) return
      settled = true
      if (timer) clearTimeout(timer)
      fn(value)
    }

    try {
      const requestUrl = new URL(apiUrl)
      // 把 __timeoutMs 从请求体中剥离——它只是本函数的控制参数，不是后端字段
      const { __timeoutMs = 300000, ...backendPayload } = payload || {}
      request = net.request({
        method: 'POST',
        protocol: requestUrl.protocol,
        hostname: requestUrl.hostname,
        port: requestUrl.port,
        path: `${requestUrl.pathname}${requestUrl.search}`
      })

      timer = setTimeout(() => {
        try { request?.abort() } catch {}
        finish(reject, new Error(`后端请求超时（${Math.round(Number(__timeoutMs) / 1000)} 秒）`))
      }, Number(__timeoutMs) || 300000)

      request.setHeader('Content-Type', 'application/json')

      let body = ''
      request.on('response', (response) => {
        response.on('data', (chunk) => {
          body += chunk.toString()
        })
        response.on('end', () => {
          if (response.statusCode >= 200 && response.statusCode < 300) {
            try {
              finish(resolve, JSON.parse(body))
            } catch (err) {
              finish(reject, err)
            }
          } else {
            // 非 2xx：尽力从 FastAPI 的 {detail} 字段取出可读错误，取不到就只报状态码
            let detail = ''
            try {
              const parsed = JSON.parse(body)
              if (parsed && typeof parsed.detail === 'string') {
                detail = `: ${parsed.detail}`
              }
            } catch (err) {
              // Non-JSON body is still a valid backend error.
            }
            finish(reject, new Error(`后端服务返回 ${response.statusCode}${detail}`))
          }
        })
      })

      request.on('error', (err) => finish(reject, err))
      request.write(JSON.stringify(backendPayload))
      request.end()
    } catch (err) {
      try { request?.abort() } catch {}
      finish(reject, err)
    }
  })
}

// ── 渲染进程可调用的 IPC 处理器 ──
// 命名统一为 art-text/<动作>，与 preload.cjs 暴露的方法一一对应

/** 返回应用版本号（取自 package.json）。 */
ipcMain.handle('art-text/get-app-version', async () => {
  return app.getVersion()
})

/** 判断是否处于开发模式（未打包，或 NODE_ENV 非 production）。 */
ipcMain.handle('art-text/is-dev', async () => {
  return !app.isPackaged || process.env.NODE_ENV !== 'production'
})

ipcMain.handle('art-text/notify', async (event, options) => {
  const { title = '通知', body = '', silent = false } = options || {}
  if (Notification.isSupported()) {
    new Notification({ title, body, silent }).show()
    return { ok: true }
  }
  throw new Error('系统通知不受支持。')
})

ipcMain.handle('art-text/open-external', async (event, url) => {
  if (!url || typeof url !== 'string') {
    throw new Error('Invalid URL')
  }
  await shell.openExternal(url)
  return { ok: true }
})

ipcMain.handle('art-text/open-path', async (event, targetPath) => {
  if (!targetPath || typeof targetPath !== 'string') {
    throw new Error('Invalid path')
  }
  const errorMessage = await shell.openPath(targetPath)
  if (errorMessage) {
    throw new Error(errorMessage)
  }
  return { ok: true }
})

/**
 * 启动验收测试批处理脚本。
 * 通过 `cmd /c start` 新开一个独立控制台窗口，让用户能看到测试的实时输出；
 * detached + unref 使该窗口不随本进程退出而被回收。
 */
ipcMain.handle('art-text/launch-acceptance-test', async () => {
  if (process.platform !== 'win32') {
    throw new Error('验收测试批处理脚本仅支持 Windows。')
  }
  const scriptPath = resolveAcceptanceScriptPath()
  const outputRoot = getOutputRoot()
  await fs.mkdir(outputRoot, { recursive: true })
  // 传相对文件名而不是 scriptPath 绝对路径：
  // cmd 的 /c 在命令行以引号开头且后面还有引号时，会剥掉「第一个引号 + 最后一个引号」，
  const scriptName = path.basename(scriptPath)
  const child = spawn(
    'cmd.exe',
    ['/d', '/c', 'start', '', scriptName, '-OutputRoot', outputRoot],
    {
      cwd: path.dirname(scriptPath),
      // detached：脱离父进程进程组，父进程退出不影响测试窗口
      detached: true,
      stdio: 'ignore',
      windowsHide: false
    }
  )
  child.unref()
  return { ok: true, path: scriptPath, outputRoot }
})

ipcMain.handle('art-text/prepare-output-task', async (event, options) => {
  const { outputRoot, mode, index, text, seed, startedAt, summaryDir } = options || {}
  return ensureUniqueTaskDir({ outputRoot, mode, index, text, seed, startedAt, summaryDir })
})

ipcMain.handle('art-text/write-task-artifacts', async (event, options) => {
  const {
    outputRoot,
    taskDir,
    taskName,
    paths = {},
    artifacts = {},
    metadata = {},
    runLog = '',
    usesTxt2Img = false,
    workflowArtifacts = null,
    summaryRow = null
  } = options || {}

  if (!taskDir || typeof taskDir !== 'string') {
    throw new Error('taskDir is required')
  }

  const root = path.resolve(outputRoot || path.dirname(taskDir))
  const resolvedTaskDir = path.resolve(taskDir)
  await fs.mkdir(resolvedTaskDir, { recursive: true })

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
    summary: paths.summary || path.join(root, 'batch_summary.csv')
  }

  // 逐个写四类图形产物；缺失的键会被 writeArtifactFile 自动跳过
  const savedFiles = []
  for (const [key, filePath] of [
    ['original', targetPaths.original],
    ['transparent', targetPaths.transparent],
    ['svg', targetPaths.svg],
    ['preview', targetPaths.preview]
  ]) {
    // SVG 是文本，其余按二进制/base64 处理
    const saved = await writeArtifactFile(filePath, artifacts[key], key === 'svg')
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
          log: 'run.log'
        },
        workflow_paths: usesTxt2Img
          ? {
              workflow_api: 'workflows/workflow_api.json',
              nodes: 'workflows/nodes.md',
              model_dependencies: 'workflows/model_dependencies.json'
            }
          : null
      }
    : null

  if (shouldWriteMetadata) {
    const metadataSaved = await writeArtifactFile(targetPaths.metadata, metadataPayload, true)
    if (metadataSaved) savedFiles.push(metadataSaved)
  }

  if (runLog !== undefined && runLog !== null && runLog !== '') {
    const logSaved = await writeArtifactFile(targetPaths.log, appendQualityMetricsToRunLog(runLog, metadataPayload || {}), true)
    if (logSaved) savedFiles.push(logSaved)
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
      modelDependencies: wfDeps
    })
    const workflow = hasRealData ? { ...fallbackWorkflow, ...workflowArtifacts } : fallbackWorkflow
    const wfSaved = await writeArtifactFile(targetPaths.workflowApi, workflow.workflowApi || workflow.workflow_api || {}, true)
    const nodesSaved = await writeArtifactFile(targetPaths.workflowNodes, workflow.workflowNodes || workflow.nodes || '', true)
    const depsSaved = await writeArtifactFile(targetPaths.modelDependencies, workflow.modelDependencies || workflow.model_dependencies || {}, true)
    savedFiles.push(...[wfSaved, nodesSaved, depsSaved].filter(Boolean))
  }

  if (summaryRow) {
    const summaryTarget = summaryRow.summary_path || targetPaths.summary
    if (summaryTarget) {
      await appendBatchSummaryCsv(summaryTarget, {
        ...extractQualityMetrics(metadataPayload || {}),
        ...summaryRow,
        task_name: summaryRow.task_name || taskName || path.basename(resolvedTaskDir),
        task_dir: summaryRow.task_dir || resolvedTaskDir,
        run_log_path: summaryRow.run_log_path || targetPaths.log
      })
    }
  }

  return { canceled: false, outputRoot: root, taskDir: resolvedTaskDir, taskName: taskName || path.basename(resolvedTaskDir), paths: targetPaths, filePaths: savedFiles }
})

/**
 * 删除某个任务目录。
 *
 * 安全校验：目标必须位于 outputs 根目录之内——否则渲染进程一旦被注入，
 * 就能借这个接口删除任意路径。校验用 resolve 后的绝对路径前缀比较。
 */
ipcMain.handle('art-text/delete-output-dir', async (event, targetPath) => {
  if (!targetPath || typeof targetPath !== 'string') {
    throw new Error('Invalid path')
  }
  // 安全校验：只允许删除 outputs 根目录下的任务子目录
  const root = getOutputRoot()
  const resolved = path.resolve(targetPath)
  if (!resolved.startsWith(path.resolve(root))) {
    throw new Error('只允许删除 outputs 目录下的任务子目录')
  }
  if (!fsSync.existsSync(resolved)) {
    return { ok: true, existed: false }
  }
  if (!fsSync.statSync(resolved).isDirectory()) {
    throw new Error('目标不是目录')
  }
  await fs.rm(resolved, { recursive: true, force: true })

  // 同步从 tasks-index.json 中移除已删除的目录记录
  await removeDeletedTaskFromIndex(root, resolved)

  return { ok: true, existed: true }
})

/**
 * 与 CLI batch.mjs 一致的批量输入解析：
 * 支持 | \t , 分隔，支持 CSV 引号转义，支持表头检测。
 */
function splitDelimitedLine(line, delimiter = ',') {
  const values = []
  let current = ''
  let quoted = false
  for (let i = 0; i < line.length; i++) {
    const char = line[i]
    const next = line[i + 1]
    if (char === '"' && quoted && next === '"') { current += '"'; i += 1 }
    else if (char === '"') { quoted = !quoted }
    else if (char === delimiter && !quoted) { values.push(current.trim()); current = '' }
    else { current += char }
  }
  values.push(current.trim())
  return values
}

/**
 * 把一条原始输入归一化为标准批次条目。
 * 接受多种字段别名（text/title/word/content 等），空值统一为 ''。
 */
function normalizeBatchItem(item, index = 0) {
  // 纯字符串输入视为只有文本
  if (typeof item === 'string') {
    return { text: item.trim(), prompt: '', negative: '', seed: '', resolution: '' }
  }
  const raw = item || {}
  return {
    text: String(raw.text ?? raw.title ?? raw.word ?? raw.content ?? '').trim(),
    prompt: String(raw.prompt ?? raw.style ?? raw.description ?? '').trim(),
    negative: String(raw.negative ?? raw.negative_prompt ?? '').trim(),
    seed: raw.seed === undefined || raw.seed === '' ? '' : String(raw.seed),
    resolution: raw.resolution ? String(raw.resolution).trim() : '',
  }
}

/**
 * 解析批量输入文本，自动识别 JSON / 带表头 CSV / 无表头分隔行三种形态。
 * 分隔符在 `|`、制表符、逗号之间按行内容择一，与 CLI batch.mjs 行为一致。
 * @returns {Array<Object>} 归一化后的批次条目列表
 */
function parseBatchInputText(content) {
  const trimmed = (content || '').trim()
  if (!trimmed) return []

  // 以 { 或 [ 开头即认为是 JSON
  if (trimmed.startsWith('[') || trimmed.startsWith('{')) {
    const parsed = JSON.parse(trimmed)
    const rows = Array.isArray(parsed) ? parsed : (Array.isArray(parsed.items) ? parsed.items : [])
    return rows.map(normalizeBatchItem).filter(item => item.text || item.prompt)
  }

  const normalizeLine = (line) => String(line || '')
    .replace(/\uFEFF/g, '')
    .replace(/\uFF5C/g, '|')
    .replace(/\t\|/g, '|')
    .replace(/\|\t/g, '|')
    .trim()

  const lines = trimmed
    .split(/\r?\n/)
    .map(normalizeLine)
    .filter(line => line && !line.startsWith('#'))
  if (lines.length === 0) return []

  const first = lines[0].toLowerCase()
  const hasHeader = /\btext\b/.test(first) && /\bprompt\b/.test(first)
  const rows = hasHeader ? lines.slice(1) : lines
  const headerDelimiter = lines[0].includes('|') ? '|' : (lines[0].includes('\t') ? '\t' : ',')
  const headers = hasHeader ? splitDelimitedLine(lines[0], headerDelimiter).map(n => n.trim().toLowerCase()) : []

  return rows.map((line, index) => {
    const delimiter = line.includes('|') ? '|' : (line.includes('\t') ? '\t' : ',')
    const parts = splitDelimitedLine(line, delimiter)

    if (headers.length > 0) {
      const record = {}
      headers.forEach((key, i) => { record[key] = parts[i] || '' })
      return normalizeBatchItem(record, index)
    }

    return normalizeBatchItem({
      text: parts[0] || '',
      prompt: parts[1] || '',
      negative: parts[2] || '',
      seed: parts[3] || undefined,
      resolution: parts[4] || undefined,
    }, index)
  }).filter(item => item.text || item.prompt)
}

ipcMain.handle('art-text/parse-batch-input', async (event, content) => {
  return parseBatchInputText(content)
})

/**
 * 扫描输出根目录下的 tasks-index.json，返回所有任务索引条目。
 * 同时用于 CLI 产物发现（Desktop 启动时合并到历史列表）。
 */
async function scanHistoryFromFs(outputRoot) {
  const root = path.resolve(outputRoot || getOutputRoot())
  const indexPath = path.join(root, 'tasks-index.json')
  try {
    const content = await fs.readFile(indexPath, 'utf8')
    return normalizeHistoryIndexEntries(JSON.parse(content.replace(/^\uFEFF/, '')), root)
  } catch (err) {
    console.warn(`[history] failed to read ${indexPath}: ${err.message}`)
    // tasks-index.json 不存在 → 回退：扫描目录中的 task_* 目录
  }

  return []
}

/**
 * 规范化历史索引条目：过滤非法项，并补齐缺失的 outputRoot。
 * 兼容早期版本写入的、没有 outputRoot 字段的索引文件。
 */
function normalizeHistoryIndexEntries(index, indexRoot) {
  if (!Array.isArray(index)) return []
  return index
    .filter(entry => entry && typeof entry === 'object')
    .map(entry => ({
      ...entry,
      outputRoot: entry.outputRoot || indexRoot
    }))
}

/**
 * 从 tasks-index.json 中移除已删除目录对应的条目。
 */
async function removeDeletedTaskFromIndex(root, deletedDir) {
  const indexPath = path.join(root, 'tasks-index.json')
  try {
    // 索引文件可能带 BOM（其他工具写入），先剥掉再解析
    const content = await fs.readFile(indexPath, 'utf8')
    let index = JSON.parse(content.replace(/^\uFEFF/, ''))
    const before = index.length
    // 按绝对路径前缀匹配，连同其子目录一并移除
    index = index.filter(e => !e.taskDir || !path.resolve(e.taskDir).startsWith(path.resolve(deletedDir)))
    if (index.length < before) {
      await fs.writeFile(indexPath, JSON.stringify(index, null, 2), 'utf8')
    }
  } catch { /* 索引不存在或不合法，静默忽略 */ }
}

ipcMain.handle('art-text/scan-fs-history', async (event, outputRoot) => {
  return scanHistoryFromFs(outputRoot)
})

/**
 * 读取产物文件内容。
 * 两种编码模式：text 直接返回字符串（用于 SVG / 元数据），
 * dataUrl 返回 data URL（用于 <img src> 直接渲染）。
 */
ipcMain.handle('art-text/read-output-file', async (event, options) => {
  const { filePath, encoding = 'dataUrl', mime = 'application/octet-stream' } = options || {}
  if (!filePath || typeof filePath !== 'string') {
    throw new Error('filePath is required')
  }
  const buffer = await fs.readFile(filePath)
  if (encoding === 'text') {
    return buffer.toString('utf8')
  }
  return `data:${mime};base64,${buffer.toString('base64')}`
})

ipcMain.handle('art-text/vectorize', async (event, payload) => {
  return requestBackend(VECTORIZER_BACKEND_URL, payload)
})

ipcMain.handle('art-text/generate', async (event, payload) => {
  return requestBackend(TXT2IMG_BACKEND_URL, payload)
})

/** 弹出系统保存对话框并写入单个文件（data 可为 data URL 或纯文本）。 */
ipcMain.handle('art-text/save-file', async (event, options) => {
  const win = BrowserWindow.getFocusedWindow()
  const { data, defaultName = 'output', filters = [] } = options || {}
  const { canceled, filePath } = await dialog.showSaveDialog(win, {
    title: '保存文件',
    defaultPath: defaultName,
    filters
  })

  if (canceled || !filePath) {
    return { canceled: true }
  }

  const buffer = parseDataUrl(data)
  await fs.writeFile(filePath, buffer)
  return { canceled: false, filePath }
})

/**
 * 选择目录后批量导出全部产物（SVG / 元数据 / 各类 PNG）。
 * 扩展名按「键名语义 + 内容特征」推断，与 inferLegacyExt 的思路一致。
 */
ipcMain.handle('art-text/save-results', async (event, options) => {
  const win = BrowserWindow.getFocusedWindow()
  const { results = {}, fileBase = 'art-text' } = options || {}
  const { canceled, filePaths } = await dialog.showOpenDialog(win, {
    title: '选择保存目录',
    properties: ['openDirectory', 'createDirectory']
  })

  if (canceled || !filePaths || filePaths.length === 0) {
    return { canceled: true }
  }

  const folder = filePaths[0]
  const savedFiles = []

  // 支持任意键的 results，按命名或内容推断文件类型
  for (const [key, data] of Object.entries(results || {})) {
    if (!data) continue

    let ext = 'bin'
    let buffer = null

    try {
      const str = String(data)
      const trimmed = str.trim()
      if (key === 'svg' || trimmed.startsWith('<svg')) {
        ext = 'svg'
        buffer = Buffer.from(str, 'utf8')
      } else if (key === 'metadata' || key.toLowerCase().includes('meta') || (trimmed.startsWith('{') || trimmed.startsWith('['))) {
        ext = 'json'
        buffer = Buffer.from(typeof data === 'string' ? data : JSON.stringify(data, null, 2), 'utf8')
      } else if (trimmed.startsWith('data:image/') || key.toLowerCase().includes('png') || key.toLowerCase().includes('image') || key.toLowerCase().includes('original') || key.toLowerCase().includes('preview') || key.toLowerCase().includes('transparent')) {
        ext = 'png'
        buffer = parseDataUrl(data)
      } else {
        // fallback: try to parse as data URL else write as utf8
        if (typeof data === 'string' && data.startsWith('data:')) {
          ext = 'bin'
          buffer = parseDataUrl(data)
        } else if (typeof data === 'string') {
          ext = 'txt'
          buffer = Buffer.from(data, 'utf8')
        } else {
          ext = 'bin'
          buffer = Buffer.from(JSON.stringify(data), 'utf8')
        }
      }
    } catch (err) {
      continue
    }

    // 键名做文件名消毒：非字母数字下划线一律换成 "-"，避免非法字符
    const safeKey = String(key).replace(/[^a-zA-Z0-9_-]+/g, '-') || 'artifact'
    const filePath = path.join(folder, `${fileBase}_${safeKey}.${ext}`)
    await fs.writeFile(filePath, buffer)
    savedFiles.push(filePath)
  }

  return { canceled: false, filePaths: savedFiles }
})

ipcMain.handle('art-text/shutdown-backends', async () => {
  shutdownBackends()
  return { ok: true, message: '已向两个后端发送关闭请求' }
})

/** 返回启动阶段的状态快照（返回副本，避免渲染进程改动到主进程内部状态）。 */
ipcMain.handle('art-text/get-startup-status', async () => {
  return { ...startupState }
})

ipcMain.handle('art-text/download-models', async () => {
  const backendDir = getBackendDir()
  if (!backendDir) {
    throw new Error('模型下载仅在生产打包版本中可用')
  }

  const ps1Path = path.join(backendDir, 'download-models.ps1')
  if (!fsSync.existsSync(ps1Path)) {
    throw new Error('未找到模型下载脚本')
  }

  // 向主窗口发送进度
  // 复用 splash:progress 通道——主窗口内的同一套进度组件可直接消费
  const sendProgress = (data) => {
    if (mainWin && !mainWin.isDestroyed()) {
      mainWin.webContents.send('splash:progress', data)
    }
  }

  sendProgress({ step: 2, phase: 'downloading', message: '正在准备下载...', percent: 0, fileIndex: 0 })

  try {
    const result = await downloadModels(backendDir, sendProgress)
    if (result.fail === 0) {
      startupState.modelsReady = true
      startupState.modelsSkipped = false
    }
    sendProgress({ step: 2, phase: 'complete', message: '模型下载完成', percent: 100, result })
    return { ok: true, ...result }
  } catch (err) {
    sendProgress({ step: 2, phase: 'error', message: err.message, percent: 0 })
    throw err
  }
})

/**
 * 关闭两个后端：先发优雅关闭请求，再兜底强杀进程树。
 * 两步都要做——后端可能因卡死而无响应，只发请求会留下僵尸进程占用端口。
 */
function shutdownBackends() {
  // 发送优雅关闭请求
  for (const url of SHUTDOWN_URLS) {
    try {
      const u = new URL(url)
      const req = net.request({
        method: 'POST',
        protocol: u.protocol,
        hostname: u.hostname,
        port: u.port,
        path: u.pathname,
      })
      req.on('error', () => {})
      req.setHeader('Content-Type', 'application/json')
      req.write(JSON.stringify({}))
      req.end()
    } catch {
      // 后端可能本来就已关闭，静默忽略
    }
  }

  // 硬杀整棵进程树（兜底）：后端可能再 spawn ComfyUI / Python 子进程。
  killProcessTree(txt2imgProc)
  killProcessTree(vectorizerProc)
  txt2imgProc = null
  vectorizerProc = null
}

// 全局退出函数 — 杀后端 + 杀所有 PowerShell 子进程 + 退出 app
function forceQuit() {
  shutdownBackends()
  // 预加载阶段可能还在解压引擎或下载模型，必须杀整棵进程树。
  killProcessTree(modelDownloadProc)
  killProcessTree(engineDownloadProc)
  modelDownloadProc = null
  engineDownloadProc = null
  app.exit(0)
}

/** 只清理「启动阶段」遗留的下载进程（引擎 / 模型），不动已就绪的后端。 */
function shutdownPreloadTasks() {
  killProcessTree(modelDownloadProc)
  killProcessTree(engineDownloadProc)
  modelDownloadProc = null
  engineDownloadProc = null
}

app.whenReady().then(async () => {
  if (app.isPackaged) {
    // ── 生产模式：显示启动画面 → 启动后端 → 打开主窗口 ──
    splashWin = createSplashWindow()

    splashWin.on('closed', () => {
      splashWin = null
    })

    // 执行启动序列
    let result = await runStartupSequence(splashWin)

    // 处理重试
    // 循环而非递归：用户在错误界面可反复点「重试」，直到成功或选择退出
    while (result && !result.success && result.error) {
      // 等待用户在错误界面选择重试或退出
      const action = await waitForSplashAction()
      if (action === 'retry') {
        // 清掉上次错误信息，避免重试界面仍显示旧错误
        startupError = null
        result = await runStartupSequence(splashWin)
      } else {
        break
      }
    }

    if (result && result.success) {
      // 短暂延迟让用户看到"完成"状态
      await new Promise(r => setTimeout(r, 600))
      if (splashWin && !splashWin.isDestroyed()) {
        splashWin.close()
      }
      showMainWindow()
    }
    // 如果有未处理的错误且用户没选择重试，splash 上的"退出"按钮会触发 app.quit()
  } else {
    // ── 开发模式：直接打开主窗口（行为不变） ──
    mainWin = createMainWindow()
    mainWin.on('closed', () => {
      mainWin = null
    })
  }

  // macOS 风格：点 Dock 图标且无窗口时重建主窗口
  app.on('activate', () => {
    if (BrowserWindow.getAllWindows().length === 0) {
      if (app.isPackaged) {
        showMainWindow()
      } else {
        mainWin = createMainWindow()
        mainWin.on('closed', () => { mainWin = null })
      }
    }
  })
})

// 所有窗口关闭即退出（macOS 除外，其应用常驻是系统惯例）
app.on('window-all-closed', () => {
  if (process.platform !== 'darwin') {
    app.quit()
  }
})

app.on('before-quit', (event) => {
  // 拦截默认退出：必须先回收后端与下载进程，否则会留下孤儿进程
  event.preventDefault()
  shutdownBackends()
  shutdownPreloadTasks()
  // 延迟退出，确保 TCP 包被 OS 内核发送
  setTimeout(() => { app.exit(0) }, 300)
})

// 捕获所有未预期的错误，确保不留下孤儿进程
process.on('uncaughtException', () => {
  shutdownBackends()
  shutdownPreloadTasks()
  app.exit(1)
})
