"""AKShare 免费数据源插件 (https://akshare.akfamily.xyz)。

完全脱离 TickFlow: 日K / 复权因子 / 全市场快照均来自东方财富公开接口,
无需任何 API Key。分钟K(freq=1)免费源稳定性差, 本插件不接入, 自动回退 TickFlow。

方法签名对齐 fuyao 插件 (service 层按此签名路由):
  - get_instruments(asset_type) -> list[dict]        (供 instrument_sync 拉标的维表, 不回退 TickFlow)
  - get_daily(...) / iter_daily(...) -> polars.DataFrame (内部契约: symbol/date/open/high/low/close/volume/手/amount/元)
  - get_adj_factors(...) -> polars.DataFrame          (symbol/trade_date/ex_factor, 单事件比值非累积)
  - get_realtime() -> list[dict]                      (全市场快照, 近实时, 供 quote_service 轮询)

复权因子推导: 取 adjust="" (原始) 与 adjust="qfq" (前复权) 两份日K,
F[d] = 原始收盘[d] / 前复权收盘[d] (累积前向因子), 单事件比值 ex_factor[d] = F[d-1] / F[d],
仅在除权日(F 发生跳变)输出。与 indicators.pipeline._apply_adj_factor 的前向复权契约一致。

单位口径: AKShare 成交量单位为手, 成交额单位为元, 与本项目 daily / realtime 契约一致。
"""
from __future__ import annotations

import logging
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta

import polars as pl

from app.data_providers.normalizer import normalize_adj_factors, normalize_daily

logger = logging.getLogger(__name__)

# 单标的请求间隔(秒): 免费源需克制, 避免 IP 被东方财富限频。可按需调大。
_RATE_SLEEP = 0.0
# 单事件比值判定阈值: |ratio-1| 超过此值视为一次除权事件(容忍浮点噪声)。
_EVENT_EPS = 1e-6


def _f(value) -> float | None:
    """任意值 → float, 空/非法返回 None。"""
    if value is None:
        return None
    try:
        f = float(value)
    except (TypeError, ValueError):
        return None
    return f if f == f else None  # 过滤 NaN


def _exchange_suffix(code: str) -> str:
    """6 位代码 → 交易所后缀 (SH/SZ/BJ)。"""
    if code.startswith(("60", "68", "90")):
        return "SH"  # 沪市主板 / 科创板(688,689) / B 股
    if code.startswith(("8", "4", "92")):
        return "BJ"  # 北交所
    return "SZ"  # 深市主板(00/001/002/003) / 创业板(30)


def _ak_code(symbol: str) -> str:
    """内部符号 000001.SZ -> AKShare 6 位代码 000001。"""
    return symbol.split(".")[0]


def _availability() -> tuple[bool, str]:
    """loader 启动自检: akshare 已安装才注册为可切换数据源。"""
    try:
        import akshare  # noqa: F401
        return True, "ok"
    except ImportError:
        return False, "未安装 akshare (在设置页点击安装, 或执行 uv pip install akshare)"


class AKShareProvider:
    """AKShare 免费数据源。realtime = 全市场快照(quote_service 轮询调用)。"""

    name = "akshare"
    builtin = True

    def __init__(self) -> None:
        self.config = _AKShareConfig()

    def close(self) -> None:
        """loader.load_all 重建注册表时对每个 provider 调 close (无状态, 空实现)。"""

    # ---- instruments ----
    def get_instruments(self, asset_type: str = "stock") -> list[dict]:
        """全市场 A 股标的维表 (代码+名称)。返回 list[dict] 供 instrument_sync 扁平化。

        instrument_sync 仅在 daily_data_provider != tickflow 且 provider 含 get_instruments
        时调用, 因此本方法即替代 TickFlow 的 exchanges.get_instruments, 不再回退。
        """
        if asset_type not in ("stock", "etf", "index"):
            # 仅 stock 由 AKShare 提供; index/etf 维表仍走免费 TickFlow exchanges(无需 Key)
            return []
        try:
            import akshare as ak

            df = ak.stock_info_a_code_name()
        except Exception as e:  # noqa: BLE001
            logger.warning("akshare get_instruments 失败: %s", e)
            return []
        if df is None or len(df) == 0:
            return []
        pdf = pl.from_pandas(df)
        code_col = "code" if "code" in pdf.columns else pdf.columns[0]
        name_col = "name" if "name" in pdf.columns else (pdf.columns[1] if len(pdf.columns) > 1 else code_col)
        out: list[dict] = []
        for row in pdf.select(code_col, name_col).iter_rows(named=True):
            code = str(row.get(code_col) or "").strip()
            if not code:
                continue
            name = str(row.get(name_col) or code).strip()
            exch = _exchange_suffix(code)
            out.append({
                "symbol": f"{code}.{exch}",
                "name": name,
                "code": code,
                "exchange": exch,
                "region": "CN",
                "type": "stock",
            })
        logger.info("akshare instruments: %d 只 A 股", len(out))
        return out

    # ---- daily ----
    def iter_daily(
        self,
        symbols: list[str],
        start_time: datetime | None,
        end_time: datetime | None,
        asset_type: str = "stock",
        on_chunk_done: Callable[[int, int], None] | None = None,
    ) -> Iterator[pl.DataFrame]:
        """逐标的产出日K, 供历史同步逐批落盘, 避免全市场结果累积在内存。"""
        if not symbols or asset_type != "stock":
            return
        end_dt = end_time or datetime.now()
        start_dt = start_time or (end_dt - _year())
        start_s = start_dt.strftime("%Y%m%d")
        end_s = end_dt.strftime("%Y%m%d")
        total = len(symbols)
        for i, sym in enumerate(symbols):
            try:
                df = self._fetch_daily_one(_ak_code(sym), start_s, end_s)
            except Exception as e:  # noqa: BLE001
                logger.warning("akshare daily %s 失败: %s", sym, e)
                df = pl.DataFrame()
            if not df.is_empty():
                df = df.with_columns(pl.lit(sym).alias("symbol"))
                df = normalize_daily(df, source=self.name)
            if on_chunk_done:
                on_chunk_done(i + 1, total)
            if not df.is_empty():
                yield df
            if _RATE_SLEEP:
                time.sleep(_RATE_SLEEP)

    def get_daily(
        self,
        symbols: list[str],
        start_time: datetime | None,
        end_time: datetime | None,
        asset_type: str = "stock",
        on_chunk_done: Callable[[int, int], None] | None = None,
    ) -> pl.DataFrame:
        chunks = [df for df in self.iter_daily(
            symbols, start_time=start_time, end_time=end_time,
            asset_type=asset_type, on_chunk_done=on_chunk_done,
        ) if not df.is_empty()]
        return pl.concat(chunks, how="diagonal_relaxed") if chunks else pl.DataFrame()

    def _fetch_daily_one(self, code: str, start_s: str, end_s: str) -> pl.DataFrame:
        """单标的原始日K (adjust="") -> 内部列(symbol 由调用方补)。

        列: 日期(date), 开盘, 收盘, 最高, 最低, 成交量(手), 成交额(元)。
        """
        import akshare as ak

        raw = ak.stock_zh_a_hist(
            symbol=code, period="daily", start_date=start_s, end_date=end_s, adjust="",
        )
        if raw is None or len(raw) == 0:
            return pl.DataFrame()
        df = pl.from_pandas(raw)
        keep = {c: c for c in ("日期", "开盘", "收盘", "最高", "最低", "成交量", "成交额") if c in df.columns}
        if not keep:
            return pl.DataFrame()
        df = df.select(list(keep)).rename({
            "日期": "date",
            "开盘": "open",
            "收盘": "close",
            "最高": "high",
            "最低": "low",
            "成交量": "volume",
            "成交额": "amount",
        })
        if df.schema.get("date") is not None and df.schema["date"] != pl.Date:
            df = df.with_columns(pl.col("date").cast(pl.Date, strict=False))
        return df

    # ---- adj_factor ----
    def get_adj_factors(
        self,
        symbols: list[str],
        start_time: datetime | None,
        end_time: datetime | None,
        asset_type: str = "stock",
        on_chunk_done: Callable[[int, int], None] | None = None,
    ) -> pl.DataFrame:
        """A 股除权因子 -> 内部契约(symbol/trade_date/ex_factor, 单事件比值非累积)。"""
        schema = {"symbol": pl.String, "trade_date": pl.Date, "ex_factor": pl.Float64}
        if not symbols or asset_type != "stock":
            return pl.DataFrame(schema=schema)
        end_dt = end_time or datetime.now()
        start_dt = start_time or (end_dt - _year())
        start_s = start_dt.strftime("%Y%m%d")
        end_s = end_dt.strftime("%Y%m%d")
        total = len(symbols)
        rows: list[dict] = []
        for i, sym in enumerate(symbols):
            try:
                events = self._adj_events_one(_ak_code(sym), start_s, end_s)
            except Exception as e:  # noqa: BLE001
                logger.warning("akshare adj_factor %s 失败: %s", sym, e)
                events = []
            rows.extend({"symbol": sym, "trade_date": d, "ex_factor": f} for d, f in events)
            if on_chunk_done:
                on_chunk_done(i + 1, total)
            if _RATE_SLEEP:
                time.sleep(_RATE_SLEEP)
        if not rows:
            return pl.DataFrame(schema=schema)
        return normalize_adj_factors(rows, source=self.name)

    def _adj_events_one(self, code: str, start_s: str, end_s: str) -> list[tuple[date, float]]:
        """单标的除权事件: (trade_date, ex_factor)。无事件返回空列表。"""
        import akshare as ak

        raw = ak.stock_zh_a_hist(symbol=code, period="daily", start_date=start_s, end_date=end_s, adjust="")
        qfq = ak.stock_zh_a_hist(symbol=code, period="daily", start_date=start_s, end_date=end_s, adjust="qfq")
        if raw is None or qfq is None or len(raw) == 0 or len(qfq) == 0:
            return []
        r = pl.from_pandas(raw).select(d=pl.col("日期"), raw_close=pl.col("收盘").cast(pl.Float64, strict=False))
        q = pl.from_pandas(qfq).select(d=pl.col("日期"), qfq_close=pl.col("收盘").cast(pl.Float64, strict=False))
        m = r.join(q, on="d", how="inner")
        if m.is_empty():
            return []
        m = (
            m.with_columns(F=(pl.col("raw_close") / pl.col("qfq_close")).fill_nan(None).fill_null(1.0))
            .sort("d")
            .with_columns(F_prev=pl.col("F").shift(1))
            .with_columns(ratio=pl.col("F_prev") / pl.col("F"))
        )
        ev = m.filter(
            pl.col("ratio").is_not_null()
            & pl.col("ratio").is_finite()
            & (pl.col("ratio") != 1.0)
            & ((pl.col("ratio") - 1.0).abs() > _EVENT_EPS)
        )
        out: list[tuple[date, float]] = []
        for d, r in zip(ev["d"].to_list(), ev["ratio"].to_list(), strict=False):
            dd = d.date() if isinstance(d, datetime) else d
            out.append((dd, float(r)))
        return out

    # ---- realtime ----
    def get_realtime(self) -> list[dict]:
        """全市场实时快照 -> 内部 realtime records (近实时, 非逐笔推送)。

        东方财富快照涨跌幅/振幅/换手率为百分数(3.66=3.66%), 本项目契约为小数制, 此处 /100。
        """
        try:
            import akshare as ak

            df = ak.stock_zh_a_spot_em()
        except Exception as e:  # noqa: BLE001
            logger.warning("akshare 实时行情拉取失败: %s", e)
            return []
        if df is None or len(df) == 0:
            return []
        pdf = pl.from_pandas(df)
        code_col = "代码" if "代码" in pdf.columns else pdf.columns[0]
        out: list[dict] = []
        for row in pdf.iter_rows(named=True):
            code = str(row.get(code_col) or "").strip()
            if not code:
                continue
            exch = _exchange_suffix(code)
            sym = f"{code}.{exch}"
            last = _f(row.get("最新价"))
            prev = _f(row.get("昨收"))
            pct = _f(row.get("涨跌幅"))
            amp = _f(row.get("振幅"))
            tor = _f(row.get("换手率"))
            rec = {
                "symbol": sym,
                "name": str(row.get("名称") or code).strip(),
                "last_price": last,
                "prev_close": prev,
                "open": _f(row.get("今开")),
                "high": _f(row.get("最高")),
                "low": _f(row.get("最低")),
                "volume": _f(row.get("成交量")),
                "amount": _f(row.get("成交额")),
                "change_pct": pct / 100.0 if pct is not None else None,
                "change_amount": _f(row.get("涨跌额")),
                "amplitude": amp / 100.0 if amp is not None else None,
                "turnover_rate": tor / 100.0 if tor is not None else None,
                "timestamp": int(time.time() * 1000),
                "session": None,
            }
            out.append(rec)
        logger.info("akshare 实时行情: %d 条", len(out))
        return out

    # ---- 测试(设置页试拉) ----
    def test_dataset(self, dataset: str, symbols: list[str] | None = None) -> dict:
        if dataset in ("daily", "adj_factor"):
            syms = [s for s in (symbols or [])][:3] or ["000001.SZ"]
            try:
                if dataset == "daily":
                    df = self.get_daily(syms, datetime.now() - _year(), datetime.now())
                else:
                    df = self.get_adj_factors(syms, datetime.now() - _year(), datetime.now())
            except Exception as e:  # noqa: BLE001
                return {"provider": self.name, "dataset": dataset, "rows": 0, "error": str(e)}
            head = df.head(5).to_dicts()
            for row in head:
                for k, v in list(row.items()):
                    if isinstance(v, (date, datetime)):
                        row[k] = v.isoformat()
            return {
                "provider": self.name,
                "dataset": dataset,
                "rows": df.height,
                "columns": df.columns,
                "preview": head,
            }
        if dataset == "realtime":
            try:
                rows = self.get_realtime()
            except Exception as e:  # noqa: BLE001
                return {"provider": self.name, "dataset": "realtime", "rows": 0, "error": str(e)}
            head = rows[:5]
            return {
                "provider": self.name,
                "dataset": "realtime",
                "rows": len(rows),
                "columns": list(head[0].keys()) if head else [],
                "preview": head,
            }
        return {
            "provider": self.name,
            "dataset": dataset,
            "rows": 0,
            "error": f"akshare 插件未接入 {dataset} 数据集(自动回退 TickFlow)",
        }


@dataclass
class _AKShareConfig:
    """轻量 config shim, 让 custom loader 的 provider_has_dataset 能识别本 provider。"""

    name: str = "akshare"
    display_name: str = "AKShare"
    datasets: dict = field(default_factory=lambda: {"daily": {}, "adj_factor": {}, "realtime": {}})
    path: None = None
    builtin: bool = True


def _year() -> timedelta:
    """返回约 1 年的 timedelta, 供默认窗口使用。"""
    return timedelta(days=365)
