<template>
  <!-- 输入面板：顶部选模式，下方按模式渲染不同的参数表单 -->
  <section class="panel main-panel">
    <div class="panel-header">
      <div class="header-top">
        <div>
          <p class="section-kicker">Input</p>
          <h2>模式选择</h2>
        </div>
        <!-- 运行中禁用重置，避免清掉正在跑的任务参数 -->
        <button class="reset-btn secondary-button small" type="button" :disabled="running" @click="$emit('reset')">重置</button>
      </div>
      <!-- 三个模式按钮；需要 GPU 的模式在无可用 GPU 时置灰 -->
      <div class="mode-switch">
        <button
          v-for="item in modes"
          :key="item.value"
          type="button"
          :class="['mode-button', { active: mode === item.value, disabled: item.requiresGpu && !hasUsableGpu }]"
          :disabled="item.requiresGpu && !hasUsableGpu"
          @click="handleModeChange(item.value)"
          :title="item.requiresGpu && !hasUsableGpu ? '未检测到可用的 GPU' : (item.requiresGpu && gpuTier === 'integrated' ? '使用集成显卡，生成速度可能较慢' : '')"
        >
          {{ item.label }}
        </button>
      </div>
    </div>

    <div class="panel-body">
      <!-- 状态条：错误优先于「正在生成」显示 -->
      <div v-if="error" class="status-banner error">{{ error }}</div>
      <div v-else-if="running" class="status-banner info">正在生成，请稍候…</div>

      <!-- 生成参数面板 -->

      <!-- 单条模式：文字 + 风格 + 负面 + 分辨率 + 种子 -->
      <!-- 单条模式：文字 + 风格 + 负面 + 分辨率 + 种子 -->
      <div v-if="mode === 'single'" class="gen-single">
        <!-- 要渲染的艺术字内容；后端上限 60 字符 -->
        <label class="field">
          <span>文字内容</span>
          <input v-model="payload.text" type="text" placeholder="支持 2-8 个字符，示例：霓虹之风" />
        </label>
        <!-- 风格描述，可自由书写（含版式要求，后端不拆字段） -->
        <label class="field">
          <span>风格提示词</span>
          <textarea v-model="payload.prompt" rows="4" placeholder="例如：霓虹渐变、金属质感"></textarea>
        </label>
        <!-- 负面提示词：描述不希望出现的缺陷，后端会与默认负面词合并 -->
        <label class="field">
          <span>负面提示词</span>
          <textarea v-model="payload.negative" rows="2" placeholder="缺字，错字，笔画断裂，杂乱背景，低清晰度"></textarea>
        </label>
        <!-- 分辨率：预设下拉 + 选「自定义」时补一个输入框 -->
        <label class="field two-col">
          <div>
            <span>分辨率</span>
            <select v-model="payload.resolution">
              <option v-for="r in FLUX_RESOLUTIONS" :key="r.value" :value="r.value">
                {{ r.display }}
              </option>
              <option value="custom">自定义</option>
            </select>
          </div>
          <div v-if="payload.resolution === 'custom'">
            <input v-model="customRes" placeholder="例如 1400x800" />
          </div>
        </label>
        <!-- 输出格式已移除，默认由后台或全局配置决定 -->
        <label class="field seed-field">
          <span>随机种子</span>
          <input v-model.number="payload.seed" type="number" />
        </label>
      </div>

      <!-- 批量模式：多行文本或文件上传，另有全局的负面词/分辨率/种子兜底 -->
      <div v-else-if="mode === 'batch'" class="gen-batch">
        <label class="field">
          <span>批量提示词（或上传文件 CSV/TXT/JSON）</span>
          <textarea v-model="payload.batch" rows="5" placeholder="每行一条：文本 | 风格 | 负面提示词(可选) | 种子(可选)&#10;例如：霓虹之风 | cyberpunk neon glow | 模糊,低质量"></textarea>
          <div class="batch-upload">
            <input type="file" accept=".txt,.csv,.json" @change="onBatchFileChange" />
          </div>
        </label>
        <label class="field">
          <span>全局负面提示词</span>
          <textarea v-model="payload.negative" rows="2" placeholder="应用于所有批量任务"></textarea>
        </label>
        <label class="field two-col">
          <div>
            <span>全局分辨率</span>
            <select v-model="payload.resolution">
              <option v-for="r in FLUX_RESOLUTIONS" :key="r.value" :value="r.value">
                {{ r.display }}
              </option>
              <option value="custom">自定义</option>
            </select>
          </div>
          <div v-if="payload.resolution === 'custom'">
            <input v-model="customRes" placeholder="例如 1400x800" />
          </div>
        </label>
        <!-- 全局输出格式已移除 -->
        <label class="field seed-field">
          <span>全局随机种子</span>
          <input v-model.number="payload.seed" type="number" />
        </label>
      </div>

      <!-- 矢量化模式：只需选一张本地图片，并给出缩略图预览 -->
      <div v-else class="gen-image">
        <label class="field">
          <span>选择图片（PNG/JPG）</span>
          <input type="file" accept="image/png,image/jpeg" @change="onImageFileChange" />
          <div v-if="previewThumb" class="thumb"><img :src="previewThumb" alt="thumb" /></div>
          <small>支持拖拽上传</small>
        </label>
      </div>
      
    </div>
  </section>
</template>

<script setup>
// 受控组件：模式、载荷、运行态与 GPU 档位全部由父组件传入
const props = defineProps({
  mode: String,
  payload: Object,
  running: Boolean,
  error: String,
  hasUsableGpu: {
    type: Boolean,
    default: false
  },
  gpuTier: {
    type: String,
    default: 'unknown'
  }
})

// requiresGpu 标记该模式是否依赖文生图能力——无 GPU 时会被禁用
const modes = [
  { label: '单条提示词', value: 'single', requiresGpu: true },
  { label: '批量提示词', value: 'batch', requiresGpu: true },
  { label: '图片矢量化', value: 'vectorize', requiresGpu: false }
]

const emit = defineEmits(['file-change', 'update:mode', 'batch-file', 'reset'])

// Flux 官方支持的 7 种分辨率（所有边长均为 64 的整数倍）
const FLUX_RESOLUTIONS = [
  { value: '1024 x 1024', display: '1024 × 1024 (1:1)' },
  { value: '1344 x 768',  display: '1344 × 768 (16:9)' },
  { value: '768 x 1344',  display: '768 × 1344 (9:16)' },
  { value: '1152 x 896',  display: '1152 × 896 (4:3)' },
  { value: '896 x 1152',  display: '896 × 1152 (3:4)' },
  { value: '1216 x 832',  display: '1216 × 832 (3:2)' },
  { value: '832 x 1216',  display: '832 × 1216 (2:3)' },
]

import { ref, computed, watch } from 'vue'

/**
 * 切换模式。
 * 无可用 GPU 时只允许切到矢量化模式——其余模式依赖文生图，切过去也跑不通。
 */
const handleModeChange = (newMode) => {
  // 仅在完全没有 GPU 时限制为矢量化模式
  if (!props.hasUsableGpu && newMode !== 'vectorize') {
    return
  }
  emit('update:mode', newMode)
}

// 自定义分辨率的原始输入；非空时同步写回 payload
const customRes = ref('')

watch(customRes, (val) => {
  if (val && val.trim()) {
    props.payload.resolution = val.trim()
  }
})

// 矢量化模式下所选图片的缩略图 data URL
const previewThumb = ref('')

// 随机种子功能已移除

/**
 * 批量输入文件选择：读成文本写入 payload.batch，并通知父组件文件名。
 * 不做解析——具体格式（TXT/CSV/JSON）由主进程的 parseBatchInput 统一处理。
 */
const onBatchFileChange = async (event) => {
  const file = event.target.files?.[0]
  if (!file) return
  const text = await new Promise((res, rej) => {
    const r = new FileReader()
    r.onload = () => res(String(r.result || ''))
    r.onerror = () => rej(new Error('文件读取失败'))
    r.readAsText(file)
  })
  // 简单解析：每行一条，分隔符可为 | 或 ,
  props.payload.batch = text
  emit('batch-file', { name: file.name, content: text })
}

/** 矢量化图片选择：写入 payload、上报父组件，并生成本地缩略图预览。 */
const onImageFileChange = async (event) => {
  const file = event.target.files?.[0]
  if (!file) return
  props.payload.imageFile = file
  emit('file-change', file)
  const url = await fileToDataUrl(file)
  previewThumb.value = url
}

/** 把 File 读成 data URL（仅用于本地预览，不参与上传）。 */
const fileToDataUrl = (file) => new Promise((resolve, reject) => {
  const r = new FileReader()
  r.onload = () => resolve(String(r.result || ''))
  r.onerror = () => reject(new Error('读取失败'))
  r.readAsDataURL(file)
})

</script>

<style scoped>
.main-panel {
  display: flex;
  flex-direction: column;
  height: 622px;
}

.panel-header {
  flex-shrink: 0;
  flex-direction: column;
  align-items: stretch;
  margin-bottom: -2px;
}

.header-top {
  display: flex;
  justify-content: space-between;
  align-items: flex-start;
  margin-bottom: 10px;
}

.mode-switch {
  display: flex;
  gap: 6px;
}

/* 不可用的模式按钮：降透明度 + 去色，明确传达「点不了」 */
.mode-button.disabled {
  opacity: 0.5;
  cursor: not-allowed;
  filter: grayscale(100%);
}

.panel-body {
  overflow-y: auto;
  flex: 1;
  min-height: 0;
}

.panel-body textarea {
  min-height: auto;
}

/* 两列布局里的输入控件统一高度，避免与下拉框错位 */
.two-col select,
.two-col input {
  height: 42px;
  min-height: 42px;
  max-height: 42px;
  box-sizing: border-box;
}

.reset-btn {
  background: #1f2937;
  color: #fff;
  border: none;
}

.reset-btn:hover {
  background: #374151;
}
</style>
