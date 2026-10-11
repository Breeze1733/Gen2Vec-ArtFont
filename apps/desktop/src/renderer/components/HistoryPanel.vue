<template>
  <!-- 历史任务侧边栏：展示任务列表，点击可恢复产物，右侧按钮可删除记录 -->
  <aside class="sidebar">
    <!-- 固定头部：不随列表滚动 -->
    <div class="sidebar-header">
      <p class="section-kicker">History</p>
      <h2>历史任务</h2>
    </div>

    <div class="sidebar-content">
      <!-- 逐条渲染历史记录；运行中的任务不可点击（产物尚未就绪） -->
      <article
        v-for="item in logs"
        :key="item.id"
        :class="['log-item', { clickable: item.status !== '运行中' }]"
        @click="item.status !== '运行中' && $emit('restore-history', item.id)"
      >
        <div class="log-thumb" aria-hidden="true">
          <!-- 有缩略图则显示图片，否则按模式回退为图标（矢量化 / 文生图） -->
          <img v-if="item.thumb" :src="item.thumb" alt="" />
          <span v-else class="log-thumb-icon">{{ item.mode === '矢量化' ? '📐' : '🎨' }}</span>
        </div>
        <div class="log-main">
          <h3>{{ item.title }}</h3>
          <p class="muted">{{ item.time }} • {{ item.status }} • {{ item.mode }}</p>
        </div>
        <div class="log-actions">
          <!-- @click.stop 阻止冒泡，避免删除时同时触发外层的恢复历史 -->
          <button class="secondary-button tiny" type="button" @click.stop="$emit('delete-history', item.id)">删除</button>
        </div>
      </article>
      <!-- 空状态：列表为空时给出占位提示，避免侧栏一片空白 -->
      <p v-if="logs.length === 0" class="empty-log">暂无运行记录</p>
    </div>
  </aside>
</template>

<script setup>
// 纯展示组件：状态全部由父组件通过 props 传入，交互通过 emit 上报。
// logs 为历史任务列表；currentFiles 保留给父组件扩展，本组件不直接使用。
const props = defineProps({ logs: Array, currentFiles: { type: Array, required: false } })

// 两个事件：删除某条历史、恢复某条历史的结果
const emit = defineEmits(['delete-history', 'restore-history'])
</script>

<style scoped>
/* 侧边栏容器：固定高度、纵向弹性布局，内容区独立滚动 */
.sidebar {
  width: 100%;
  height: 622px;
  background: var(--surface);
  border: 1px solid var(--border);
  border-radius: 8px;
  box-shadow: var(--shadow-soft);
  display: flex;
  flex-direction: column;
  overflow: hidden;
}

/* 头部：不参与滚动（flex-shrink: 0），保证标题始终可见 */
.sidebar-header {
  padding: 16px;
  border-bottom: 1px solid var(--border);
  display: flex;
  flex-direction: column;
  align-items: flex-start;
  gap: 4px;
  flex-shrink: 0;
}

.sidebar-header h2 {
  margin: 0;
  font-size: 18px;
}

/* 列表区：占满剩余高度并纵向滚动 */
.sidebar-content {
  padding: 12px;
  overflow-y: auto;
  flex: 1;
}

/* 单条记录：横向三段布局（缩略图 / 文字 / 操作） */
.log-item {
  display: flex;
  align-items: center;
  gap: 10px;
  padding: 10px;
  border: 1px solid var(--border);
  border-radius: 6px;
  background: rgba(255, 255, 255, 0.68);
  margin-bottom: 8px;
  transition: border-color 0.15s, background 0.15s;
}

/* 仅「已完成」记录可点击；运行中的条目保持默认光标以示不可交互 */
.log-item.clickable {
  cursor: pointer;
}

.log-item.clickable:hover {
  border-color: rgba(15, 118, 110, 0.3);
  background: rgba(15, 118, 110, 0.04);
}

/* 缩略图容器：固定方形，图标与图片共用 */
.log-thumb {
  width: 40px;
  height: 40px;
  border-radius: 4px;
  background: rgba(31, 41, 55, 0.04);
  display: flex;
  align-items: center;
  justify-content: center;
  color: var(--text-muted);
  font-size: 10px;
  flex-shrink: 0;
  overflow: hidden;
}

.log-thumb img {
  width: 100%;
  height: 100%;
  object-fit: cover;
}

.log-thumb-icon {
  font-size: 18px;
}

/* 文字区：min-width: 0 是让下面的省略号生效的必要条件 */
.log-main {
  flex: 1;
  min-width: 0;
}

/* 标题单行显示，超出用省略号截断 */
.log-main h3 {
  margin: 0;
  font-size: 13px;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}

.log-main .muted {
  margin: 2px 0 0;
  color: var(--text-muted);
  font-size: 11px;
}

/* 空状态提示 */
.empty-log {
  color: var(--text-muted);
  font-size: 13px;
  padding: 16px 0;
  text-align: center;
}
</style>
