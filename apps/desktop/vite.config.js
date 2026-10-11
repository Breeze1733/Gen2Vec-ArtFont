import { defineConfig } from 'vite'
import vue from '@vitejs/plugin-vue'

// https://vite.dev/config/
export default defineConfig({
  // base 用相对路径：打包后由 Electron 以 file:// 加载 dist/index.html，
  // 绝对路径会导致资源 404
  base: './',
  plugins: [vue()],
})
