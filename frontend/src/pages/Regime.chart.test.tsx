// @vitest-environment jsdom
/**
 * 回归测试: 市场环境(Regime)页「切换时间范围」时的图表实例生命周期。
 *
 * 这里覆盖两个真实崩过的坑, 都发生在同一个 useEffect 序列里:
 *
 *  A. rows 空窗期整块情绪周期卡片被卸载 → 数据回来后 React 重建的是一个**全新 div**;
 *     实例若仍绑在已脱离文档的旧节点上, 之后 setOption 只画进那块看不见的旧画布,
 *     新容器永远空白(表现: 图表区连坐标轴都没有)。
 *  B. 销毁旧实例走的是 echarts.dispose(), 它会把内部 `_zr` 置为 null
 *     (见 echarts lib/core/echarts.js 的 "Set properties to null") → 此后任何
 *     `getZr().on(...)` 都抛 `Cannot read properties of null (reading 'on')`,
 *     而它抛在 useEffect 里 → 整页被 React 错误边界接管
 *     (用户看到的就是 "Unexpected Application Error!")。
 *
 * 下面的 echarts 假实现**如实复刻** dispose 后 getZr() === null 这一行为, 所以一旦
 * 重新出现"销毁之后再挂 zr 事件"的写法, 这个测试会立刻红。
 */
import { Component, type ReactNode } from 'react'
import { act } from 'react'
import { createRoot, type Root } from 'react-dom/client'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import { Regime } from './Regime'

type ZrEvent = { offsetX: number; offsetY: number }

/** 让测试能观察到实例的创建/销毁, 并触发 zr 事件 */
const fake = vi.hoisted(() => ({
  inits: 0,
  disposals: 0,
  instances: [] as unknown[],
}))

vi.mock('echarts', () => {
  class FakeZr {
    private handlers = new Map<string, ((e: ZrEvent) => void)[]>()
    on(evt: string, fn: (e: ZrEvent) => void) {
      const list = this.handlers.get(evt) ?? []
      list.push(fn)
      this.handlers.set(evt, list)
    }
    off(evt: string, fn: (e: ZrEvent) => void) {
      this.handlers.set(evt, (this.handlers.get(evt) ?? []).filter(f => f !== fn))
    }
    /** 测试用: 手动派发, 等价于点了一下图表 */
    trigger(evt: string, e: ZrEvent) {
      for (const fn of this.handlers.get(evt) ?? []) fn(e)
    }
  }
  class FakeChart {
    private zr: FakeZr | null = new FakeZr()
    private dom: HTMLElement | null
    constructor(dom: HTMLElement) {
      this.dom = dom
      fake.inits++
      fake.instances.push(this)
    }
    // ── 与真实 echarts 一致的关键行为: dispose 之后 _zr / _dom 全为 null ──
    getZr() { return this.zr }
    getDom() { return this.dom }
    isDisposed() { return this.zr === null }
    dispose() {
      if (this.zr === null) return
      this.zr = null
      this.dom = null
      fake.disposals++
    }
    setOption() { /* noop */ }
    resize() { /* noop */ }
    clear() { /* noop */ }
    containPixel() { return true }
    convertFromPixel() { return [2] }
  }
  return { init: (dom: HTMLElement) => new FakeChart(dom) }
})

vi.mock('@/lib/api', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@/lib/api')>()
  // limit 决定返回窗口: 1年(250)/2年(500) 给出不同日期区间, 以便断言"窗口真的换了"
  const makeRows = (limit?: number) => {
    const n = limit === 500 ? 45 : limit === 250 ? 30 : Math.min(limit ?? 30, 60)
    return Array.from({ length: n }, (_, i) => {
      const date = new Date(Date.UTC(2026, 0, 1 + i)).toISOString().slice(0, 10)
      return {
        date, state: 'range', score: 50, phase: 'repair',
        max_consecutive: 3, first_board: 20, ge2_count: 5, limit_up: 25,
        promo_rate: 0.2, seal_rate: 0.7, ladder_completeness: 0.8,
      }
    })
  }
  const api = {
    regimeCoverage: async () => ({ earliest_date: '2026-01-01', latest_date: '2026-03-01', rows: 400 }),
    regimeHistory: async (_start?: string, _end?: string, limit?: number) => ({ rows: makeRows(limit) }),
    regimeStates: async () => ({ distribution: [] }),
    regimePhases: async () => ({ segments: [] }),
    regimeMainline: async () => ({ rows: [], leaders: [] }),
  }
  return { ...actual, api: api as unknown as typeof actual.api }
})

/** 复刻生产环境的失败形态: 图表崩溃 → 错误边界接管整页 */
const caught = vi.hoisted(() => ({ errors: [] as unknown[] }))

class Boundary extends Component<{ children: ReactNode }, { failed: boolean }> {
  state = { failed: false }
  static getDerivedStateFromError() { return { failed: true } }
  componentDidCatch(error: unknown) { caught.errors.push(error) }
  render() { return this.state.failed ? <div data-crashed /> : this.props.children }
}

/** 情绪周期时间轴容器的 class(h-[280px]); 环境趋势/分布图是 h-[320px], 借此区分 */
const isPhaseDom = (el: HTMLElement | null) => !!el && el.className.includes('280px')

let host: HTMLDivElement
let root: Root
let client: QueryClient

beforeEach(() => {
  Object.assign(globalThis, { IS_REACT_ACT_ENVIRONMENT: true })
  fake.inits = 0
  fake.disposals = 0
  fake.instances.length = 0
  caught.errors.length = 0
  host = document.createElement('div')
  document.body.append(host)
  root = createRoot(host)
  client = new QueryClient({ defaultOptions: { queries: { retry: false, staleTime: Infinity, gcTime: Infinity } } })
})

afterEach(async () => {
  await act(async () => root.unmount())
  client.clear()
  host.remove()
})

async function settle(rounds = 8) {
  for (let i = 0; i < rounds; i++) {
    await act(async () => { await new Promise(resolve => setTimeout(resolve, 0)) })
  }
}

async function renderPage() {
  await act(async () => root.render(
    <Boundary><QueryClientProvider client={client}><Regime /></QueryClientProvider></Boundary>,
  ))
  await settle()
}

/** 当前存活、且绑在当前文档里那个 280px 容器上的实例 */
function livePhaseChart() {
  const live = (fake.instances as { isDisposed: () => boolean; getDom: () => HTMLElement | null; getZr: () => { trigger: (e: string, p: ZrEvent) => void } }[])
    .filter(i => !i.isDisposed() && isPhaseDom(i.getDom()))
  return live[live.length - 1]
}

async function clickRange(label: string) {
  const btn = [...host.querySelectorAll('button')].find(b => b.textContent?.trim() === label)
  expect(btn, `找不到时间范围按钮「${label}」`).toBeTruthy()
  await act(async () => btn!.click())
  await settle()
}

it('切换时间范围: 不崩到错误边界, 旧实例被销毁, 新实例重建在当前容器上', async () => {
  await renderPage()
  expect(caught.errors).toEqual([])

  const first = livePhaseChart()
  expect(first, '首屏应已初始化情绪周期图').toBeTruthy()
  const firstDom = first.getDom()
  expect(host.contains(firstDom!)).toBe(true)

  // 切到「2年」: rows 先空(卡片整块卸载) 再回填(React 重建新 div) —— 正是出事的那条路径
  await clickRange('2年')
  expect(caught.errors).toEqual([])
  expect(host.querySelector('[data-crashed]')).toBeNull()

  // 旧实例必须已被销毁(否则它的 zrender 挂在脱离文档的旧节点上)
  expect(fake.disposals).toBe(1)
  const second = livePhaseChart()
  expect(second).toBeTruthy()
  // 旧容器已脱离文档, 新容器必须换人 —— 否则图表永远空白
  expect(host.contains(firstDom!)).toBe(false)
  expect(second.getDom()).not.toBe(firstDom)
  expect(host.contains(second.getDom()!)).toBe(true)
})

it('切换时间范围之后, 点击图表仍能回看当日(zr 监听挂在新实例上)', async () => {
  await renderPage()
  await clickRange('2年')
  expect(caught.errors).toEqual([])

  const chart = livePhaseChart()
  expect(chart).toBeTruthy()
  // convertFromPixel 假实现返回索引 2 → 应选中窗口内第 3 个交易日
  await act(async () => chart.getZr().trigger('click', { offsetX: 1, offsetY: 1 }))
  await settle()
  expect(caught.errors).toEqual([])
  expect(host.textContent).toContain('回到今日')
  expect(host.textContent).toContain('2026-01-03')
})
