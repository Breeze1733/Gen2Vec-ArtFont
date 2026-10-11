<template>
  <!-- 矢量化参数面板：上半选预设，下半微调 6 个底层参数 -->
  <section class="panel main-panel">
    <div class="panel-header">
      <div>
        <p class="section-kicker">Vectorization</p>
        <h2>矢量化参数</h2>
      </div>
    </div>

    <div class="panel-body">
      <!-- 预设选择：预设会给 6 个参数一组默认值 -->
      <!-- 预设选择：一键把 6 个参数设为一组经验值 -->
      <div class="panel-section">
        <h3>预设选择</h3>
        <div class="presets">
          <label v-for="(p, key) in vectorPresets" :key="key" class="preset">
            <!-- presetDirty 为真表示参数已被手动改动，此时取消预设高亮 -->
            <input
              type="radio"
              name="preset"
              :value="key"
              :checked="!presetDirty && vector.preset === key"
              @change="$emit('preset-change', key)"
            />
            <span>{{ presetLabels[key] }}</span>
          </label>
        </div>
      </div>

      <!-- 参数调整：每个输入框的 min/max 与后端 Pydantic 的约束保持一致 -->
      <div class="panel-section">
        <h3>参数调整</h3>
        <!-- 6 个底层参数：调色板层次 / 碎块过滤 / 硬角判定 / 短段合并 / 色层容差 / 上采样 -->
        <div class="vector-grid">
          <!-- 颜色精度：保留的色彩层次数，越大越接近原图但路径更多 -->
          <label>
            <span>颜色精度 (1-8)</span>
            <input
              :value="vector.color_precision"
              @input="$emit('update:vector', { ...vector, color_precision: Number($event.target.value) })"
              type="number"
              min="1"
              max="8"
            />
          </label>
          <!-- 斑点过滤：忽略小于该面积的色块，用于清除孤立噪点 -->
          <label>
            <span>斑点过滤 (0-64)</span>
            <input
              :value="vector.filter_speckle"
              @input="$emit('update:vector', { ...vector, filter_speckle: Number($event.target.value) })"
              type="number"
              min="0"
              max="64"
            />
          </label>
          <!-- 拐角阈值：判定硬角的切线夹角（度），越小越易保留锐利转折 -->
          <label>
            <span>拐角阈值 (1-180)</span>
            <input
              :value="vector.corner_threshold"
              @input="$emit('update:vector', { ...vector, corner_threshold: Number($event.target.value) })"
              type="number"
              min="1"
              max="180"
            />
          </label>
          <!-- 长度阈值：短于此长度的路径段会被平滑合并，值越大轮廓越简洁 -->
          <label>
            <span>长度阈值 (1-64)</span>
            <input
              :value="vector.length_threshold"
              @input="$emit('update:vector', { ...vector, length_threshold: Number($event.target.value) })"
              type="number"
              min="1"
              max="64"
            />
          </label>
          <!-- 图层差异：相邻颜色层的合并容差，值越大色阶越少 -->
          <label>
            <span>图层差异 (1-64)</span>
            <input
              :value="vector.layer_difference"
              @input="$emit('update:vector', { ...vector, layer_difference: Number($event.target.value) })"
              type="number"
              min="1"
              max="64"
            />
          </label>
          <!-- 放大倍数：追踪前上采样倍率，用于缓解边缘锯齿（大图会被后端自动下调） -->
          <label>
            <span>放大倍数 (1-4)</span>
            <input
              :value="vector.scale"
              @input="$emit('update:vector', { ...vector, scale: Number($event.target.value) })"
              type="number"
              min="1"
              max="4"
            />
          </label>
        </div>
      </div>

      <div class="actions">
        <!-- 提交入口：运行中禁用，避免重复触发任务 -->
        <button class="primary-button submit-btn" type="button" :disabled="running" @click="$emit('submit')">开始生成</button>
      </div>
    </div>
  </section>
</template>

<script setup>
import { computed } from 'vue'

// 受控组件：vector / vectorPresets / running 全部由父组件提供
const props = defineProps({
  vector: {
    type: Object,
    required: true
  },
  vectorPresets: {
    type: Object,
    required: true
  },
  running: {
    type: Boolean,
    default: false
  }
})

// 预设 key → 中文显示名（clean 路径最少、ultra 细节最多）
const presetLabels = {
  clean: '清爽',
  balanced: '平衡',
  detailed: '精细',
  ultra: '超清'
}

// 三个事件：改参数、切预设、提交任务
defineEmits(['update:vector', 'preset-change', 'submit'])

// 参与「是否偏离预设」判定的 6 个参数键，顺序与后端预设表一致
const PARAM_KEYS = [
  'color_precision',
  'filter_speckle',
  'corner_threshold',
  'length_threshold',
  'layer_difference',
  'scale'
]

// 参数偏离当前预设时取消预设按钮高亮，重新点击预设后恢复
const presetDirty = computed(() => {
  const preset = props.vectorPresets[props.vector.preset]
  // 预设 key 无效时视为「已偏离」，避免误高亮
  if (!preset) return true
  // 数值比较前先 Number()，规避输入框返回字符串导致的误判
  return PARAM_KEYS.some((key) => Number(props.vector[key]) !== Number(preset[key]))
})
</script>

<style scoped>
/* 面板容器：与左侧输入面板等高，内部纵向布局 */
.panel {
  height: 622px;
  display: flex;
  flex-direction: column;
}

/* 内容区独立滚动，min-height: 0 是 flex 子项可收缩的必要条件 */
.panel-body {
  overflow-y: auto;
  flex: 1;
  min-height: 0;
}

/* 6 个参数排成两列 */
.vector-grid {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 10px;
}

.vector-grid label {
  display: flex;
  flex-direction: column;
  gap: 4px;
}

.vector-grid input[type="number"] {
  width: 80px;
  min-height: 32px;
}

/* 提交按钮：主操作，字号与高度都放大以突出 */
.submit-btn {
  font-size: 24px;
  padding: 0 24px;
  min-height: 46px;
}
</style>
