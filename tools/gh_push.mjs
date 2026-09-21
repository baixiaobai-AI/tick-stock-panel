#!/usr/bin/env node
/**
 * gh_push.mjs — 在 `git push` 不可用(github.com:443 被墙)时, 用 GitHub Git Data API
 * 把本地工作区增量同步到远端分支。
 *
 * 为什么不用 tools/gh_update.ps1:
 *   - PowerShell 5.1 把无 BOM 的 UTF-8 脚本按 GBK 读, 中文注释会吞掉花括号; 每次改脚本
 *     都得先补 BOM, 一不小心就是语法错误。
 *   - 本沙盒里 PowerShell 的 stdout 拿不到, 出错只能去翻日志文件; 而 `git` 命令可能不在
 *     PATH 上。Node 两个问题都没有, 且 Node 是打包链的既有依赖。
 *
 * 原理与 gh_update.ps1 一致:
 *   1) 读远端 HEAD 与其完整文件树;
 *   2) 本地按 .gitignore 同口径排除 node_modules/dist/缓存后, 逐个算 git blob sha1
 *      ("blob <len>\0<content>"), 与远端同名文件比对 → 只上传真正变化的 blob;
 *   3) 用本地文件全集(含未变的旧 sha)重建树, 分批带 base_tree —— 单个请求可能超过
 *      体积上限返 422, 分批就不会;
 *   4) 建 commit → 更新 ref。
 *
 * 用法:
 *   node tools/gh_push.mjs --owner <o> --repo <r> --token <pat> --message "..." [--dry-run]
 *   (token 也可用环境变量 GH_TOKEN/GITHUB_TOKEN)
 *
 * 安全阀: 若发现"远端有、本地没有"的文件(会在重建树时被删除), 默认**中止**并列出,
 * 需要显式加 --allow-deletions 才继续 —— 避免本地工作区不完整时把仓库误删。
 */
import { createHash } from 'node:crypto'
import { readFileSync, readdirSync, statSync, existsSync } from 'node:fs'
import { join, relative, extname } from 'node:path'
import { fileURLToPath } from 'node:url'
import { dirname, resolve } from 'node:path'

const API = 'https://api.github.com'
const LEAF_MAX = 250          // 每批 tree 条目数: 一次全量会 422
const CONCURRENCY = 6         // blob 上传并发

/** 与 .gitignore 同口径: 这些目录/后缀既不进仓库, 也不参与 sha 比对 */
const IGNORE = [
  /(^|\/)\.git\//, /(^|\/)node_modules\//, /(^|\/)dist\//, /(^|\/)build\//,
  /(^|\/)__pycache__\//, /(^|\/)\.pytest_cache\//, /(^|\/)\.mypy_cache\//,
  /(^|\/)\.ruff_cache\//, /(^|\/)\.uv\//, /(^|\/)\.venv\//, /(^|\/)venv\//,
  /(^|\/)\.pnpm-store\//, /(^|\/)\.vite\//,
  /\.pyc$/, /\.pyo$/, /\.tsbuildinfo$/,
]

function parseArgs(argv) {
  const out = { branch: 'main', root: process.cwd(), dryRun: false, allowDeletions: false }
  for (let i = 0; i < argv.length; i++) {
    const k = argv[i]
    if (k === '--dry-run') { out.dryRun = true; continue }
    if (k === '--allow-deletions') { out.allowDeletions = true; continue }
    const key = k.replace(/^--/, '').replace(/-([a-z])/g, (_, c) => c.toUpperCase())
    out[key] = argv[++i]
  }
  out.token ||= process.env.GH_TOKEN || process.env.GITHUB_TOKEN
  return out
}

const args = parseArgs(process.argv.slice(2))
for (const req of ['owner', 'repo', 'token', 'message']) {
  if (!args[req]) {
    console.error(`缺少参数 --${req.replace(/[A-Z]/g, c => '-' + c.toLowerCase())}`)
    process.exit(2)
  }
}
const root = resolve(args.root)

async function api(method, path, body) {
  const res = await fetch(`${API}${path}`, {
    method,
    headers: {
      Authorization: `Bearer ${args.token}`,
      Accept: 'application/vnd.github+json',
      'User-Agent': 'tsp-gh-push',
      'Content-Type': 'application/json',
    },
    body: body === undefined ? undefined : JSON.stringify(body),
  })
  const text = await res.text()
  if (!res.ok) {
    throw new Error(`${method} ${path} → ${res.status} ${text.slice(0, 400)}`)
  }
  return text ? JSON.parse(text) : null
}

/** git 的对象寻址: sha1("blob <字节数>\0" + 内容) */
function gitBlobSha(buf) {
  return createHash('sha1').update(`blob ${buf.length}\0`).update(buf).digest('hex')
}

function walk(dir, acc = []) {
  for (const name of readdirSync(dir)) {
    const full = join(dir, name)
    const rel = relative(root, full).replace(/\\/g, '/')
    if (IGNORE.some(re => re.test(rel + (statSync(full).isDirectory() ? '/' : '')))) continue
    const st = statSync(full)
    if (st.isDirectory()) walk(full, acc)
    else if (st.isFile()) acc.push(rel)
  }
  return acc
}

const repoPath = `/repos/${args.owner}/${args.repo}`
console.log(`→ ${args.owner}/${args.repo}@${args.branch}  (root: ${root})`)

const ref = await api('GET', `${repoPath}/git/ref/heads/${args.branch}`)
const parentSha = ref.object.sha
console.log(`远端 HEAD = ${parentSha}`)

const tree = await api('GET', `${repoPath}/git/trees/${args.branch}?recursive=1`)
if (tree.truncated) throw new Error('远端文件树被截断, 无法安全比对')
const remote = new Map(tree.tree.filter(n => n.type === 'blob').map(n => [n.path, n.sha]))
console.log(`远端 blob = ${remote.size}`)

const local = walk(root).sort()
const files = local.map(path => {
  const buf = readFileSync(join(root, path))
  const mode = extname(path).toLowerCase() === '.sh' || path.endsWith('.mjs') ? '100644' : '100644'
  return { path, mode, sha: gitBlobSha(buf), buf }
})
console.log(`本地文件 = ${files.length}`)

const changed = files.filter(f => remote.get(f.path) !== f.sha)
const localSet = new Set(files.map(f => f.path))
const deleted = [...remote.keys()].filter(p => !localSet.has(p)).sort()

console.log(`变化/新增 = ${changed.length}, 远端多余 = ${deleted.length}`)
for (const f of changed.slice(0, 40)) console.log(`  ~ ${f.path}`)
if (changed.length > 40) console.log(`  … 其余 ${changed.length - 40} 个`)
for (const d of deleted.slice(0, 40)) console.log(`  - ${d}`)

if (deleted.length > 0 && !args.allowDeletions) {
  console.error('\n中止: 上面的文件只存在于远端, 重建整棵树会把它们删除。')
  console.error('确认要删除请加 --allow-deletions。')
  process.exit(3)
}
if (changed.length === 0 && deleted.length === 0) {
  console.log('没有变化, 无需推送')
  process.exit(0)
}
if (args.dryRun) {
  console.log('--dry-run: 到此为止, 未上传任何内容')
  process.exit(0)
}

// 只给变化的文件传 blob, 其余复用远端已有 sha
const uploaded = new Map()
for (let i = 0; i < changed.length; i += CONCURRENCY) {
  const batch = changed.slice(i, i + CONCURRENCY)
  const results = await Promise.all(batch.map(f => api('POST', `${repoPath}/git/blobs`, {
    content: f.buf.toString('base64'),
    encoding: 'base64',
  })))
  batch.forEach((f, j) => uploaded.set(f.path, results[j].sha))
  process.stdout.write(`\r  已上传 blob ${Math.min(i + batch.length, changed.length)}/${changed.length}`)
}
if (changed.length) process.stdout.write('\n')

// 用本地全集重建树; 引用远端已有 blob 的 sha, 只对变化的文件用新 sha
const entries = files.map(f => ({
  path: f.path, mode: f.mode, type: 'blob', sha: uploaded.get(f.path) ?? f.sha,
}))
let treeSha = null
for (let i = 0; i < entries.length; i += LEAF_MAX) {
  const body = { tree: entries.slice(i, i + LEAF_MAX) }
  if (treeSha) body.base_tree = treeSha
  treeSha = (await api('POST', `${repoPath}/git/trees`, body)).sha
  console.log(`  建树 ${Math.min(i + LEAF_MAX, entries.length)}/${entries.length}`)
}

const commit = await api('POST', `${repoPath}/git/commits`, {
  message: args.message, tree: treeSha, parents: [parentSha],
})
console.log(`commit = ${commit.sha}`)
await api('PATCH', `${repoPath}/git/refs/heads/${args.branch}`, { sha: commit.sha, force: true })
console.log(`✓ ${args.branch} → ${commit.sha}`)
console.log(`  https://github.com/${args.owner}/${args.repo}/commit/${commit.sha}`)
