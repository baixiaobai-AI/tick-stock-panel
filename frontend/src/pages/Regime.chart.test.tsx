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
  // 参考线阈值写回后端的 spy —— 断言"改 DL/SL 真的发出 PUT /smash-config"
  setSmashThresholds: vi.fn(async (
    dl?: number, sl?: number, divisor?: number, multiplier?: number,
  ) => ({
    dl: dl ?? 8, sl: sl ?? 1.8, divisor: divisor ?? 4, multiplier: multiplier ?? 10,
  })),
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
    // 非空 segments: 让「阶段规律」与「阶段 × 主线」两张卡都真实渲染, 才能断言上下位置
    regimePhases: async () => ({
      segments: [
        { start: '2026-01-01', end: '2026-01-05', days: 5, phase: 'repair', label: '修复',
          avg_height: 3, avg_ge2: 5, avg_promo: 0.2, avg_seal_rate: 0.7, top_mainlines: [] },
        { start: '2026-01-06', end: '2026-01-10', days: 5, phase: 'repair', label: '修复',
          avg_height: 4, avg_ge2: 6, avg_promo: 0.25, avg_seal_rate: 0.72, top_mainlines: [] },
      ],
    }),
    regimeMainline: async () => ({ rows: [], leaders: [] }),
    // 砸盘指数: 给一份合法 config(DL=8 / SL=1.8 / 除数 4 / 倍数 10),
    // 让左上角与右上角的输入框初始值可断言
    regimeSmash: async () => ({
      rows: [],
      total: 0,
      rungs: [
        { key: 'promo_1to2', label: '1进2' },
        { key: 'promo_2to3', label: '2进3' },
        { key: 'promo_3to4', label: '3进4' },
        { key: 'promo_4up', label: '4板以上' },
      ],
      config: { dl: 8, sl: 1.8, divisor: 4, multiplier: 10 },
    }),
    setSmashThresholds: fake.setSmashThresholds,
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

/** 取某张卡(h2 标题定位)里那块 280px 图容器内、当前存活的图表实例。
 * 不能直接用"所有 280px 容器"去筛选 —— 砸盘指数卡片也是 280px, 会和情绪周期
 * 时间轴撞车; 必须按卡定位, 才能断言"具体某张图"的生命周期。 */
type LiveChart = { isDisposed: () => boolean; getDom: () => HTMLElement | null; getZr: () => { trigger: (e: string, p: ZrEvent) => void } }
function liveChartIn(cardTitle: string): LiveChart | null {
  const card = [...host.querySelectorAll('h2')]
    .find(h => h.textContent?.trim() === cardTitle)?.parentElement?.parentElement
  if (!card) return null
  const dom = [...card.querySelectorAll('div')].find(d => d.className.includes('280px')) as HTMLElement | null
  if (!dom) return null
  const live = (fake.instances as LiveChart[]).filter(i => !i.isDisposed() && i.getDom() === dom)
  return live[live.length - 1] ?? null
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

  const first = liveChartIn('情绪周期时间轴')
  expect(first, '首屏应已初始化情绪周期时间轴图').toBeTruthy()
  const firstDom = first!.getDom()
  expect(host.contains(firstDom!)).toBe(true)

  // 切到「2年」: rows 先空(卡片整块卸载) 再回填(React 重建新 div) —— 正是出事的那条路径
  await clickRange('2年')
  expect(caught.errors).toEqual([])
  expect(host.querySelector('[data-crashed]')).toBeNull()

  // 旧实例必须已被销毁(否则它的 zrender 挂在脱离文档的旧节点上)
  expect(first!.isDisposed()).toBe(true)
  const second = liveChartIn('情绪周期时间轴')
  expect(second).toBeTruthy()
  // 旧容器已脱离文档, 新容器必须换人 —— 否则图表永远空白
  expect(host.contains(firstDom!)).toBe(false)
  expect(second!.getDom()).not.toBe(firstDom)
  expect(host.contains(second!.getDom()!)).toBe(true)
})

it('切换时间范围之后, 点击图表仍能回看当日(zr 监听挂在新实例上)', async () => {
  await renderPage()
  await clickRange('2年')
  expect(caught.errors).toEqual([])

  const chart = liveChartIn('情绪周期时间轴')
  expect(chart).toBeTruthy()
  // convertFromPixel 假实现返回索引 2 → 应选中窗口内第 3 个交易日
  await act(async () => chart!.getZr().trigger('click', { offsetX: 1, offsetY: 1 }))
  await settle()
  expect(caught.errors).toEqual([])
  expect(host.textContent).toContain('回到今日')
  expect(host.textContent).toContain('2026-01-03')
})

// ── 砸盘指数卡片落位 + 参考线阈值设置 (2026-09-22 需求) ──
// 需求: 在「阶段规律」及其上方插入这张砸盘指数图, 使之下方内容整体下移; 宽度/风格
// 与「情绪周期时间轴」一致。这里把"位置""等宽""风格"都变成断言, 避免以后调版式
// 时又被挪走; 并补一条"改 DL/SL 真的写回后端"的回归(之前没断言, 后端接口形同虚设)。
it('砸盘指数卡片位于「阶段规律」之上, 与情绪周期时间轴同父容器(等宽), 且渲染参考线输入', async () => {
  await renderPage()
  expect(caught.errors).toEqual([])

  const cardOf = (title: string) =>
    [...host.querySelectorAll('h2')].find(h => h.textContent?.trim() === title)?.parentElement?.parentElement ?? null

  const smashCard = cardOf('砸盘指数')
  const ruleCard = cardOf('阶段规律')
  const axisCard = cardOf('情绪周期时间轴')
  const crossCard = cardOf('阶段 × 主线')
  expect(smashCard, '砸盘指数卡片应已渲染').toBeTruthy()
  expect(ruleCard, '阶段规律卡片应已渲染').toBeTruthy()
  expect(axisCard, '情绪周期时间轴卡片应已渲染').toBeTruthy()
  expect(crossCard, '阶段×主线卡片应已渲染').toBeTruthy()

  // ① 上下位置: 砸盘指数 → 阶段规律 → 时间轴 → 阶段×主线
  const order = [...host.querySelectorAll('h2')].map(h => h.textContent?.trim())
  const at = (t: string) => order.indexOf(t)
  expect(at('砸盘指数')).toBeLessThan(at('阶段规律'))
  expect(at('阶段规律')).toBeLessThan(at('情绪周期时间轴'))
  expect(at('情绪周期时间轴')).toBeLessThan(at('阶段 × 主线'))

  // ② 等宽: 与情绪周期时间轴同为同一列容器的直接子元素 → 宽度必然一致(不靠写死像素)
  const bucket = smashCard!.parentElement
  expect(bucket).toBe(axisCard!.parentElement)
  const kids = [...bucket!.children]
  expect(kids.indexOf(smashCard!)).toBeLessThan(kids.indexOf(ruleCard!))

  // ③ 风格一致: 沿用同一套卡片外框
  expect(smashCard!.className).toContain('rounded-card')
  expect(axisCard!.className).toContain('rounded-card')

  // ④ 参考线输入: 右上角 DL/SL, 值来自后端 config 回填(默认 8 / 1.8)
  const dlInput = host.querySelector('input[aria-label="危险线 DL"]') as HTMLInputElement | null
  const slInput = host.querySelector('input[aria-label="试错线 SL"]') as HTMLInputElement | null
  expect(dlInput, '应渲染 DL 危险线输入').toBeTruthy()
  expect(slInput, '应渲染 SL 试错线输入').toBeTruthy()
  expect(dlInput!.value).toBe('8')
  expect(slInput!.value).toBe('1.8')
})

it('修改 DL 并失焦: 写回后端 setSmashThresholds, 输入框保留新值', async () => {
  await renderPage()
  expect(caught.errors).toEqual([])
  fake.setSmashThresholds.mockClear()

  const dlInput = host.querySelector('input[aria-label="危险线 DL"]') as HTMLInputElement
  expect(dlInput).toBeTruthy()

  // 用 native value setter 触发 React onChange(受控输入必须走这条路径)
  await act(async () => {
    const setter = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, 'value')!.set!
    setter.call(dlInput, '6.5')
    dlInput.dispatchEvent(new Event('input', { bubbles: true }))
  })
  // 失焦触发 commitThresholds → PUT /smash-config; React 的 onBlur 走 focusout 委托
  await act(async () => {
    dlInput.dispatchEvent(new FocusEvent('focusout', { bubbles: true }))
  })
  await settle()
  expect(caught.errors).toEqual([])
  expect(fake.setSmashThresholds).toHaveBeenCalledTimes(1)
  // 只改 DL: 除数/倍数按当前生效值(4/10)一并回传, 后端单接口全量保存
  expect(fake.setSmashThresholds).toHaveBeenCalledWith(6.5, 1.8, 4, 10)
  expect(dlInput.value).toBe('6.5')
})

it('修改公式除数/倍数(左上角): 与 DL/SL 一起写回后端', async () => {
  await renderPage()
  expect(caught.errors).toEqual([])

  // ① 初始值来自后端 config
  const dvInput = host.querySelector('input[aria-label="公式除数"]') as HTMLInputElement
  const mpInput = host.querySelector('input[aria-label="公式倍数"]') as HTMLInputElement
  expect(dvInput, '左上角应有除数输入框').toBeTruthy()
  expect(dvInput.value).toBe('4')
  expect(mpInput.value).toBe('10')

  fake.setSmashThresholds.mockClear()

  const type = async (el: HTMLInputElement, v: string) => {
    await act(async () => {
      const setter = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, 'value')!.set!
      setter.call(el, v)
      el.dispatchEvent(new Event('input', { bubbles: true }))
    })
  }
  await type(dvInput, '2')
  await type(mpInput, '20')
  await act(async () => {
    mpInput.dispatchEvent(new FocusEvent('focusout', { bubbles: true }))
  })
  await settle()
  expect(caught.errors).toEqual([])
  // 改的是 SPA: 缩放变了 → queryKey 变化 → 重新拉 /smash; DL/SL 沿用旧值一起保存
  expect(fake.setSmashThresholds).toHaveBeenCalledWith(8, 1.8, 2, 20)
  expect(dvInput.value).toBe('2')
  expect(mpInput.value).toBe('20')
})

it('输入非法的除数(0/负数): 回滚生效值, 不提交该项 —— DL/SL 照常保存', async () => {
  await renderPage()
  const dvInput = host.querySelector('input[aria-label="公式除数"]') as HTMLInputElement
  fake.setSmashThresholds.mockClear()

  const type = async (el: HTMLInputElement, v: string) => {
    await act(async () => {
      const setter = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, 'value')!.set!
      setter.call(el, v)
      el.dispatchEvent(new Event('input', { bubbles: true }))
    })
  }
  await type(dvInput, '0')
  await act(async () => {
    dvInput.dispatchEvent(new FocusEvent('focusout', { bubbles: true }))
  })
  await settle()
  expect(caught.errors).toEqual([])
  // 除数为 0 会导致除零 → 丢弃缩放变更、回滚到当前生效值 4; DL/SL 仍照常保存
  expect(fake.setSmashThresholds).toHaveBeenCalledWith(8, 1.8, undefined, undefined)
  expect(dvInput.value).toBe('4')
})
