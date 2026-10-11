<template>
  <!-- 任务类型切换器：纯受控组件，选中项由父组件的 modelValue 决定。
       aria-label 便于读屏软件识别该区域用途。 -->
  <section class="mode-switch" aria-label="任务类型">
    <button
      v-for="item in modes"
      :key="item.value"
      type="button"
      :class="['mode-button', { active: modelValue === item.value }]"
      @click="$emit('update:modelValue', item.value)"
    >
      {{ item.label }}
    </button>
  </section>
</template>

<script setup>
// 无内部状态：只接收 modes 列表与当前值，点击时向父组件回传新值。
// modes 每项形如 { value, label }；选中态由 modelValue 决定。
const props = defineProps({
  modes: Array,
  modelValue: String
})

// 标准 v-model 事件名，配合 v-model:modelValue 使用
const emit = defineEmits(['update:modelValue'])
</script>
