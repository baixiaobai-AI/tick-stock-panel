"""砸盘指数(ZPZS) — 按连板梯队分档计算晋级率。

用户口径(2026-09-22 给定):

    ZPZS 砸盘指数 = SUM(每个梯队的晋级率) / 4 * 10

4 个梯队 = 昨日 1 板 / 2 板 / 3 板 / 4 板及以上(高度板), 各自"今日继续封板、
连板数抬高"的比例; "晋级"判据为 今日连板数 > 昨日连板数。
空梯队(昨日该档无样本)记 0 参与求和 —— 没有高度板本身即弱势信号,
分母固定为 4 与公式字面一致。因此本指数的理论区间是 [0, 10]。

数据来源: data/kline_daily_enriched/date=*/*.parquet 的
symbol / date / consecutive_limit_ups 三列(列裁剪 + projection pushdown,
246 个交易日实测 1~2 秒)。刻意不依赖 regime_history —— 这样新增指标无需
重算 regime(重算要扫全市场全指标, 分钟级), 老版本落盘的 regime 数据也能直接用。

口径一致性: 与 regime 的 phase 判定共用同一个 ST 剔除开关
(preferences.sentiment_exclude_st), 避免"情绪周期剔 ST、砸盘指数含 ST"两套宽度。
"""
from __future__ import annotations

import logging
import threading
import time
from datetime import date, timedelta
from pathlib import Path

import polars as pl

logger = logging.getLogger(__name__)

# 梯队口径: (字段前缀, 昨日连板数下界, 上界) —— 上界 None 表示开口(4 板及以上合并为
# "高度板"一档)。改口径只需改这张表, ZPZS 与前端图例都按 4 档推导。
RUNG_DEFS: tuple[tuple[str, int, int | None], ...] = (
    ("promo_1to2", 1, 1),
    ("promo_2to3", 2, 2),
    ("promo_3to4", 3, 3),
    ("promo_4up", 4, None),
)

# 梯队展示名(前端图例/tooltip 用, 与 RUNG_DEFS 一一对应)
RUNG_LABELS = {
    "promo_1to2": "1进2",
    "promo_2to3": "2进3",
    "promo_3to4": "3进4",
    "promo_4up": "4板以上",
}

ENRICHED_DIR = "kline_daily_enriched"
# 回溯天数: 晋级率要拿"昨日"连板数, 窗口首日必须多读一截做预热(取 40 个日历日,
# 覆盖春节长假等连续休市)。聚合后再裁回 [start, end]。
_WARMUP_DAYS = 40
_CACHE_TTL = 300.0

_cache: dict[str, tuple[float, list[dict]]] = {}
_cache_lock = threading.Lock()


def _enriched_glob(data_dir: Path) -> str:
    return str(Path(data_dir) / ENRICHED_DIR / "**" / "*.parquet")


def _load_st_symbols(data_dir: Path) -> list[str]:
    """风险警示(ST)股名单; 与 regime 共用 preferences 开关。"""
    try:
        from app.services import preferences as prefs

        if not prefs.get_sentiment_exclude_st():
            return []
        from app.services.market_mainline import load_risk_warning_symbols

        return sorted(load_risk_warning_symbols(data_dir))
    except Exception as e:  # noqa: BLE001
        logger.warning("smash_index: ST 名单加载失败, 按不剔除处理: %s", e)
        return []


def compute_smash_series(
    data_dir: Path,
    start: date | None = None,
    end: date | None = None,
    divisor: float = 4.0,
    multiplier: float = 10.0,
) -> pl.DataFrame:
    """算 [start, end] 的每日砸盘指数与 4 档晋级率明细。

    ZPZS = Σ(各梯队晋级率) / divisor * multiplier; divisor(默认 4 = 4 档求均值)、
    multiplier(默认 10 = 放大到可读区间)均可在图表左上角调整。

    返回列: date, promo_1to2/2to3/3to4/4up (晋级率 0~1), 同名 *_pool/*_ok
    (分母/分子家数), zpzs。无数据返回空 DataFrame。
    """
    warm_start = (start - timedelta(days=_WARMUP_DAYS)) if start else None
    # 注意: 空目录(全新安装/数据未同步)时 polars 是**在 collect 时**才抛
    # "expanded paths were empty", 只包 scan_parquet 的构建捕捉不到 → 整个 lazy
    # 链连同 collect 一起放 try 里, 让"没数据"退化成空结果而不是 500。
    try:
        lf = pl.scan_parquet(_enriched_glob(data_dir)).select(
            ["symbol", "date", "consecutive_limit_ups"]
        )
        if warm_start is not None:
            lf = lf.filter(pl.col("date") >= warm_start)
        if end is not None:
            lf = lf.filter(pl.col("date") <= end)
        df = lf.collect()
    except Exception as e:  # noqa: BLE001
        logger.info("smash_index: enriched 数据不可用, 返回空: %s", e)
        return pl.DataFrame()

    if df.is_empty():
        return pl.DataFrame()

    st = _load_st_symbols(data_dir)
    if st:
        df = df.filter(~pl.col("symbol").str.to_uppercase().is_in(st))
        if df.is_empty():
            return pl.DataFrame()

    prev = pl.col("_prev")
    consec = pl.col("consecutive_limit_ups")
    advanced = consec > prev
    exprs: list[pl.Expr] = []
    for name, lo, hi in RUNG_DEFS:
        bucket = prev.ge(lo) if hi is None else prev.eq(lo)
        exprs.append(bucket.sum().alias(f"{name}_pool"))
        exprs.append((bucket & advanced).sum().alias(f"{name}_ok"))

    agg = (
        df.sort(["symbol", "date"])
        .with_columns(consec.shift(1).over("symbol").alias("_prev"))
        # 窗口首日没有"昨日"→ _prev 为 null, 不计入分母(否则会把该日所有连板股
        # 误判成"未晋级", 系统性压低首日指数)。
        .filter(pl.col("_prev").is_not_null())
        .group_by("date")
        .agg(exprs)
        .sort("date")
    )

    rows: list[dict] = []
    for r in agg.iter_rows(named=True):
        rates: list[float] = []
        out: dict = {"date": r["date"]}
        for name, _lo, _hi in RUNG_DEFS:
            pool = int(r.get(f"{name}_pool") or 0)
            ok = int(r.get(f"{name}_ok") or 0)
            rate = (ok / pool) if pool > 0 else 0.0
            out[name] = round(rate, 4)
            out[f"{name}_pool"] = pool
            out[f"{name}_ok"] = ok
            rates.append(rate)
        out["zpzs"] = round(sum(rates) / divisor * multiplier, 2)
        rows.append(out)

    result = pl.DataFrame(rows) if rows else pl.DataFrame()
    if not result.is_empty():
        if start is not None:
            result = result.filter(pl.col("date") >= start)
        if end is not None:
            result = result.filter(pl.col("date") <= end)
    return result.sort("date") if not result.is_empty() else result


def get_smash_series(
    data_dir: Path,
    start: date | None = None,
    end: date | None = None,
    limit: int | None = None,
    divisor: float = 4.0,
    multiplier: float = 10.0,
) -> list[dict]:
    """带缓存的砸盘指数时序(JSON 安全: date 转 ISO 字符串)。

    divisor/multiplier 透传进计算(图表左上角可调)。limit 仅在没有 start/end
    ("最近 N 天"模式)时生效, 语义与 /history 一致: 传了日期范围的调用方要的
    是完整区间, 不该被截断。
    """
    key = f"{start}|{end}|{limit}|{divisor}|{multiplier}"
    now = time.time()
    with _cache_lock:
        hit = _cache.get(key)
        if hit and (now - hit[0]) < _CACHE_TTL:
            return hit[1]

    df = compute_smash_series(data_dir, start, end, divisor=divisor, multiplier=multiplier)
    if df.is_empty():
        rows: list[dict] = []
    else:
        if start is None and end is None and limit:
            df = df.sort("date", descending=True).head(limit)
        df = df.sort("date")
        rows = []
        for r in df.to_dicts():
            if r.get("date") is not None:
                r["date"] = str(r["date"])
            rows.append(r)

    with _cache_lock:
        _cache[key] = (now, rows)
    return rows


def invalidate_cache() -> None:
    """数据更新/重算后清缓存(供 pipeline 与重算入口调用)。"""
    with _cache_lock:
        _cache.clear()
