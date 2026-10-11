/**
 * 预加载脚本（preload）：在渲染进程与主进程之间架设受控的通信桥。
 *
 * 运行在 contextIsolation 开启的隔离上下文中，通过 contextBridge 只向渲染进程
 * 暴露白名单内的 API，渲染进程无法直接触碰 Node/Electron 的原生能力。
 *
 * 两类通信模式：
 *   - invoke 模式：`ipcRenderer.invoke` 返回 Promise，用于请求-响应式调用
 *     （生成、保存文件、读写产物等）；
 *   - send/on 模式：主进程主动推送事件（如启动页进度），渲染进程用回调订阅。
 */

const { contextBridge, ipcRenderer } = require('electron')

// Exposed Electron API for renderer.
// API 版本号：渲染进程可据此判断主进程提供的接口是否兼容
const API_VERSION = '1.1.0'
// 暴露的方法清单：仅作自省用途（listMethods），便于调试时确认可用能力
const API_METHODS = [
  'getPlatform',
  'getAppVersion',
  'isDev',
  'vectorize',
  'generate',
  'saveFile',
  'saveResults',
  'prepareOutputTask',
  'writeTaskArtifacts',
  'readOutputFile',
  'deleteOutputDir',
  'openPath',
  'notify',
  'openExternal',
  'getStartupStatus',
  'downloadModels',
  'launchAcceptanceTest',
  'shutdownBackends',
  'scanFsHistory',
  'parseBatchInput'
]

/**
 * Expose a minimal, safe API to the renderer process.
 * Methods return Promises where appropriate and proxy to ipcRenderer.invoke.
 *
 * 通道名统一加 `art-text/` 前缀，避免与 Electron 内置通道或其它插件冲突。
 */
contextBridge.exposeInMainWorld('artTextApp', {
  // API metadata
  apiVersion: API_VERSION,
  platform: process.platform,
  listMethods: () => API_METHODS.slice(),

  // Utilities
  getPlatform: () => process.platform,
  getAppVersion: () => ipcRenderer.invoke('art-text/get-app-version'),
  isDev: () => ipcRenderer.invoke('art-text/is-dev'),

  // Core actions
  vectorize: (payload) => ipcRenderer.invoke('art-text/vectorize', payload),
  generate: (payload) => ipcRenderer.invoke('art-text/generate', payload),

  // File helpers
  saveFile: (options) => ipcRenderer.invoke('art-text/save-file', options),
  saveResults: (options) => ipcRenderer.invoke('art-text/save-results', options),
  prepareOutputTask: (options) => ipcRenderer.invoke('art-text/prepare-output-task', options),
  writeTaskArtifacts: (options) => ipcRenderer.invoke('art-text/write-task-artifacts', options),
  readOutputFile: (options) => ipcRenderer.invoke('art-text/read-output-file', options),
  deleteOutputDir: (targetPath) => ipcRenderer.invoke('art-text/delete-output-dir', targetPath),
  openPath: (targetPath) => ipcRenderer.invoke('art-text/open-path', targetPath),

  // Misc
  notify: (options) => ipcRenderer.invoke('art-text/notify', options),
  openExternal: (url) => ipcRenderer.invoke('art-text/open-external', url),
  shutdownBackends: () => ipcRenderer.invoke('art-text/shutdown-backends'),

  // 文件系统历史扫描（发现 CLI 产生的产物）
  scanFsHistory: (outputRoot) => ipcRenderer.invoke('art-text/scan-fs-history', outputRoot),

  // 批量输入解析（与 CLI 一致的分隔符和引号处理）
  parseBatchInput: (content) => ipcRenderer.invoke('art-text/parse-batch-input', content),

  // Startup & model management (生产打包模式)
  getStartupStatus: () => ipcRenderer.invoke('art-text/get-startup-status'),
  downloadModels: () => ipcRenderer.invoke('art-text/download-models'),
  launchAcceptanceTest: () => ipcRenderer.invoke('art-text/launch-acceptance-test'),

  // Splash / progress events (send/on 模式，非 invoke)
  // 启动页进度推送：主进程 emit，渲染进程注册回调消费
  onSplashProgress: (callback) => {
    ipcRenderer.on('splash:progress', (_event, data) => callback(data))
  },
  // 移除全部 splash:progress 监听，避免组件销毁后回调仍被触发
  removeSplashProgressListener: () => {
    ipcRenderer.removeAllListeners('splash:progress')
  },
  // 渲染进程向主进程发送启动页操作指令（如「跳过下载」）
  sendSplashAction: (action) => {
    ipcRenderer.send('splash:action', action)
  }
})
