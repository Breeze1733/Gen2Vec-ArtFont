#!/usr/bin/env node

/**
 * CLI 打包脚本：把 ESM 源码手工打成单文件 CJS bundle。
 *
 * 用途是为 Node.js SEA（Single Executable Application）准备输入——SEA 只接受
 * 一个 CommonJS 入口，无法直接消费多文件 ESM 项目。因此这里实现了一个最小
 * 打包器：从 bin/gen2vec.mjs 出发递归收集所有本地依赖，把 `import` 改写为
 * `__require`/`__localRequire` 调用，把 `export` 收集为 `__exports.x = x`，
 * 最后拼成一个自包含的 .cjs 文件。
 *
 * 不依赖 esbuild/rollup 等工具，是为了让构建链保持零第三方依赖。
 */

import { mkdir, readFile, writeFile } from 'node:fs/promises'
import path from 'node:path'
import { fileURLToPath } from 'node:url'

// 路径基线：scriptDir = apps/cli/scripts，cliRoot = apps/cli
const scriptDir = path.dirname(fileURLToPath(import.meta.url))
const cliRoot = path.resolve(scriptDir, '..')
// 入口与产物路径
const entryPath = path.join(cliRoot, 'bin', 'gen2vec.mjs')
const outPath = path.join(cliRoot, 'dist', 'gen2vec_cli.bundle.cjs')

// 匹配 `import ... from '...'`（含多行命名导入），捕获导入子句与模块说明符
const importRe = /import\s+([\s\S]*?)\s+from\s+['"]([^'"]+)['"]\s*;?/g
// id -> { filePath, source }；用 Map 天然去重并保持插入顺序
const modules = new Map()
// 生成临时绑定名的自增计数，避免与源码标识符冲突
let importCounter = 0

/**
 * 把绝对路径转成稳定的模块 id（相对 cliRoot、统一用正斜杠）。
 * 用相对路径而非绝对路径，保证 bundle 产物与构建机器路径无关。
 */
function toId(filePath) {
  return path.relative(cliRoot, filePath).replace(/\\/g, '/')
}

/**
 * 把 import 说明符解析为实际文件路径。
 * 已带扩展名则原样返回，否则补 .mjs（本项目 ESM 源码的固定后缀）。
 */
function resolveLocal(specifier, fromFile) {
  const resolved = path.resolve(path.dirname(fromFile), specifier)
  return path.extname(resolved) ? resolved : `${resolved}.mjs`
}

/**
 * 解析 `{ a, b as c }` 形式的命名导入，返回 `[{ name, alias }]`。
 * 无别名的项 alias 等于 name。
 */
function parseNamedImports(specifier) {
  return specifier
    .replace(/^\{|\}$/g, '')
    .split(',')
    .map((part) => part.trim())
    .filter(Boolean)
    .map((part) => {
      const [name, alias] = part.split(/\s+as\s+/)
      return { name: name.trim(), alias: (alias || name).trim() }
    })
}

/**
 * 把一条 import 语句改写成 CJS 形式的 require + 变量声明。
 *
 * 覆盖三种写法：
 *   - 命名导入 `{ a, b as c }`   → 临时绑定 + 逐个解构
 *   - 默认+命名 `d, { a }`       → 临时绑定 + __importDefault + 逐个解构
 *   - 纯默认导入 `d`             → __importDefault 包装
 *
 * 本地模块走 `__localRequire`（按 id 查表），外部包走 `__nodeRequire`（即 Node 原生 require）。
 */
function buildImportReplacement(imported, specifier, fromFile) {
  const isLocal = specifier.startsWith('.') || specifier.startsWith('/')
  const requireExpr = isLocal
    ? `__localRequire(${JSON.stringify(toId(resolveLocal(specifier, fromFile)))})`
    : `__nodeRequire(${JSON.stringify(specifier)})`
  const cleaned = imported.trim()
  const tempName = () => `__import_${importCounter++}`

  // 命名导入：先拿到模块对象，再逐个取值到局部变量
  if (cleaned.startsWith('{')) {
    const binding = tempName()
    const declarations = parseNamedImports(cleaned)
      .map(({ name, alias }) => (name === alias ? `const ${name} = ${binding}.${name};` : `const ${alias} = ${binding}.${name};`))
      .join('\n')
    return `const ${binding} = ${requireExpr};\n${declarations}\n`
  }

  // 默认 + 命名混合导入
  if (cleaned.includes(',')) {
    const [defaultName, namedPart] = cleaned.split(/,(.+)/).map((part) => part.trim()).filter(Boolean)
    const binding = tempName()
    const declarations = parseNamedImports(namedPart)
      .map(({ name, alias }) => (name === alias ? `const ${name} = ${binding}.${name};` : `const ${alias} = ${binding}.${name};`))
      .join('\n')
    return `const ${binding} = ${requireExpr};\nconst ${defaultName} = __importDefault(${binding});\n${declarations}\n`
  }

  // 纯默认导入
  return `const ${cleaned} = __importDefault(${requireExpr});\n`
}

/**
 * 从入口文件出发，递归收集所有本地依赖模块。
 * 只跟随相对/绝对路径的 import；外部包交给运行时 require，不打包进来。
 */
async function collect(filePath) {
  const absolutePath = path.resolve(filePath)
  const id = toId(absolutePath)
  // 已收集过则直接返回，兼作循环依赖的保护
  if (modules.has(id)) return

  const source = await readFile(absolutePath, 'utf8')
  modules.set(id, { filePath: absolutePath, source })

  for (const match of source.matchAll(importRe)) {
    const specifier = match[2]
    if (specifier.startsWith('.') || specifier.startsWith('/')) {
      await collect(resolveLocal(specifier, absolutePath))
    }
  }
}

/**
 * 转换单个模块：改写 import、剥离 export 关键字、生成导出赋值。
 * 返回可直接放进 `__define` 工厂函数体的代码字符串。
 */
function transformModule({ filePath, source }) {
  const exports = []
  // 去掉 shebang，避免出现在函数体内部导致语法错误
  let code = source.replace(/^#!.*\r?\n/, '')

  code = code.replace(importRe, (match, imported, specifier) => buildImportReplacement(imported, specifier, filePath))

  // 逐个剥离 export 前缀并登记导出名（三种声明形式）
  code = code.replace(/\bexport\s+async\s+function\s+([A-Za-z_$][\w$]*)/g, (_, name) => {
    exports.push(name)
    return `async function ${name}`
  })
  code = code.replace(/\bexport\s+function\s+([A-Za-z_$][\w$]*)/g, (_, name) => {
    exports.push(name)
    return `function ${name}`
  })
  code = code.replace(/\bexport\s+const\s+([A-Za-z_$][\w$]*)\s*=/g, (_, name) => {
    exports.push(name)
    return `const ${name} =`
  })

  // 去重后生成 __exports.x = x，供其他模块通过模块对象访问
  const exportLines = [...new Set(exports)].map((name) => `__exports.${name} = ${name}`).join('\n')
  return `${code}\n${exportLines}\n`
}

// ── 主流程：收集 → 生成运行时 → 拼装 → 落盘 ──
await collect(entryPath)
await mkdir(path.dirname(outPath), { recursive: true })

const chunks = []
// bundle 运行时骨架：模块注册表、缓存与 CommonJS 风格的 require 实现
chunks.push(`#!/usr/bin/env node
'use strict'

const __nodeRequire = require
const __modules = new Map()
const __cache = new Map()

function __define(id, factory) {
  __modules.set(id, factory)
}

function __importDefault(value) {
  return value && value.__esModule && Object.prototype.hasOwnProperty.call(value, 'default') ? value.default : value
}

function __require(id) {
  if (__cache.has(id)) return __cache.get(id).exports
  const factory = __modules.get(id)
  if (!factory) throw new Error('Bundled module not found: ' + id)
  const module = { exports: {} }
  __cache.set(id, module)
  factory(__require, module, module.exports)
  return module.exports
}
`)

// 逐个注册模块：id + 工厂函数（工厂体即转换后的模块代码）
for (const [id, mod] of modules) {
  chunks.push(`\n__define(${JSON.stringify(id)}, function(__localRequire, __module, __exports) {\n${transformModule(mod)}\n})\n`)
}

// 从入口模块开始执行，触发整个依赖图的加载
chunks.push(`\n__require(${JSON.stringify(toId(entryPath))})\n`)

await writeFile(outPath, chunks.join(''), 'utf8')
console.log(`Bundled CLI: ${outPath}`)
