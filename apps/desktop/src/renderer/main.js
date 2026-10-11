/**
 * 渲染进程入口：创建 Vue 应用并挂载到 index.html 的 #app 节点。
 *
 * 全局样式在此引入（先于 App 组件），保证组件内样式可以覆盖默认值。
 */
import { createApp } from 'vue'
import './styles/global.css'
import App from './App.vue'

createApp(App).mount('#app')
