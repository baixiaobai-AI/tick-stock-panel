"""Tushare 数据源插件 (https://tushare.pro)。

与 AKShare 同构 (fuyao/akshare 同款插件契约), 但 Tushare 强制需要 Token:
免费注册即送基础积分, 可拉 日K / 复权因子 / 股票列表; 分钟K / 实时等进阶能力
仍需更高积分。本插件接管 daily + adj_factor (其强项), 实时行情建议把
realtime_data_provider 设为 akshare (免费无需 token, 东方财富全市场快照)。

方法签名对齐 fuyao / akshare 插件 (service 层按此签名路由):
  - get_instruments(asset_type)   -> list[dict]   (供 instrument_sync 拉标的维表, 不回退 TickFlow)
  - get_daily(...) / iter_daily(...) -> polars.DataFrame
  - get_adj_factors(...)          -> polars.DataFrame (symbol/trade_date/ex_factor, 单事件比值非累积)
  - get_realtime(...)             -> list[dict]   (best-effort, 仅 symbols 有限集合, 非全市场轮询)

单位口径 (Tushare pro 与本项目契约的关键差异, 见行内注释):
  - daily.vol   成交量(手)            -> 与本项目 daily 契约一致, 直接透传
  - daily.amount 成交额(千元)          -> 本项目 daily 契约为「元」, 此处 *1000 换算 (否则回测金额差 1000 倍!)
  - adj_factor.adj_factor 累积复权因子 -> 转单事件比值 ex_factor[d]=F[d-1]/F[d], 对齐 pipeline._apply_adj_factor
"""
from __future__ import annotations

import logging
import os
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta

import polars as pl

from app import secrets_store
from app.data_providers.normalizer import normalize_adj_factors, normalize_daily

logger = logging.getLogger(__name__)

# 单标的请求间隔(秒): Tushare 积分接口有频率上限, 适当节流避免 429 / 积分耗尽。
_RATE_SLEEP = 0.3
# 单事件比值判定阈值: |ratio-1| 超过此值视为一次除权事件(容忍浮点噪声)。
_EVENT_EPS = 1e-9
# Sina 实时单次上限(代码数): Sina 单请求约 80~100 只, 超出分批。
_SINA_BATCH = 80

SECRETS_FIELD = "tushare_api_key"
API_KEY_ENV = "TUSHARE_TOKEN"


def get_api_key() -> str:
    """secrets.json (tushare_api_key) 优先, 否则环境变量 TUSHARE_TOKEN / TS_TOKEN。"""
    key = secrets_store.get_env_backed_secret(SECRETS_FIELD, API_KEY_ENV)
    if key:
        return key
    return os.environ.get("TS_TOKEN", "").strip()


def availability() -> tuple[bool, str]:
    """loader 启动自检: tushare 已装 + Token 已配置 才注册为可切换数据源。"""
    try:
        import tushare  # noqa: F401
    except ImportError:
        return False, "未安装 tushare (在设置页点击安装, 或执行 uv pip install tushare)"
    if get_api_key():
        return True, "ok"
    return False, f"缺少 Tushare Token (tushare.pro 注册后填入, 或配置环境变量 {API_KEY_ENV}/TS_TOKEN)"


def probe_api_key(api_key: str) -> tuple[bool, str]:
    """用候选 Token 实探一次 stock_basic 接口(先探后存, 对齐 /tickflow-key 语义)。"""
    try:
        import tushare as ts

        pro = ts.pro_api(api_key)
        pro.stock_basic(exchange="", list_status="L", fields="ts_code,symbol,name", limit=1)
        return True, "ok"
    except Exception as e:  # noqa: BLE001
        return False, f"Token 无效或网络失败: {e}"


@dataclass
class _TushareConfig:
    """轻量 config shim, 让 custom loader 的 provider_has_dataset 能识别本 provider。"""

    name: str = "tushare"
    display_name: str = "Tushare"
    datasets: dict = field(
        default_factory=lambda: dict.fromkeys(("daily", "adj_factor"))
    )
    path: None = None
    builtin: bool = True


def _to_date(value) -> date | None:
    """'20240101' / date / datetime -> date; 非法返回 None, 不伪造。"""
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    s = str(value).strip()
    if len(s) >= 8 and s[:8].isdigit():
        try:
            return date(int(s[:4]), int(s[5:7]) if "-" in s else int(s[4:6]),
                        int(s[8:10]) if "-" in s else int(s[6:8]))
        except (ValueError, IndexError):
            return None
    return None


def _f(value) -> float | None:
    if value is None:
        return None
    try:
        v = float(value)
    except (TypeError, ValueError):
        return None
    return v if v == v else None  # 过滤 NaN


def _ts_code(symbol: str) -> str:
    """内部符号 000001.SZ -> Tushare ts_code (已是带后缀格式, 原样返回)。"""
    return symbol


class TushareProvider:
    """Tushare 数据源 (需 Token)。daily + adj_factor 接管, realtime 仅有限 best-effort。"""

    name = "tushare"
    builtin = True

    def __init__(self) -> None:
        self.config = _TushareConfig()
        self._pro = None

    def close(self) -> None:
        """loader.load_all 重建注册表时对每个 provider 调 close (无状态, 释放 client)。"""
        self._pro = None

    def _pro_api(self):
        if self._pro is None:
            key = get_api_key()
            if not key:
                raise RuntimeError("未配置 Tushare Token")
            import tushare as ts

            self._pro = ts.pro_api(key)
        return self._pro

    # ---- instruments ----
    def get_instruments(self, asset_type: str = "stock") -> list[dict]:
        """全市场 A 股标的维表 (代码+名称)。Tushare ts_code 已含交易所后缀 (000001.SZ)。"""
        if asset_type != "stock":
            return []
        try:
            pro = self._pro_api()
            df = pro.stock_basic(
                exchange="", list_status="L", fields="ts_code,symbol,name,exchange"
            )
        except Exception as e:  # noqa: BLE001
            logger.warning("tushare get_instruments 失败: %s", e)
            return []
        if df is None or len(df) == 0:
            return []
        pdf = pl.from_pandas(df)
        out: list[dict] = []
        for row in pdf.iter_rows(named=True):
            ts_code = str(row.get("ts_code") or "").strip()
            if not ts_code:
                continue
            code = str(row.get("symbol") or ts_code.split(".")[0]).strip()
            exch = ts_code.split(".")[-1].upper()
            out.append({
                "symbol": ts_code,
                "name": str(row.get("name") or code).strip(),
                "code": code,
                "exchange": exch,
                "region": "CN",
                "type": "stock",
            })
        logger.info("tushare instruments: %d 只 A 股", len(out))
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
        if not symbols or asset_type != "stock":
            return
        end_dt = end_time or datetime.now()
        start_dt = start_time or (end_dt - timedelta(days=365))
        start_s = start_dt.strftime("%Y%m%d")
        end_s = end_dt.strftime("%Y%m%d")
        total = len(symbols)
        for i, sym in enumerate(symbols):
            try:
                df = self._fetch_daily_one(_ts_code(sym), start_s, end_s)
            except Exception as e:  # noqa: BLE001
                logger.warning("tushare daily %s 失败: %s", sym, e)
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
        chunks = [
            df
            for df in self.iter_daily(
                symbols, start_time=start_time, end_time=end_time,
                asset_type=asset_type, on_chunk_done=on_chunk_done,
            )
            if not df.is_empty()
        ]
        return pl.concat(chunks, how="diagonal_relaxed") if chunks else pl.DataFrame()

    def _fetch_daily_one(self, ts_code: str, start_s: str, end_s: str) -> pl.DataFrame:
        """单标的原始日K -> 内部列 (symbol 由调用方补)。amount 千元→元。"""
        pro = self._pro_api()
        raw = pro.daily(
            ts_code=ts_code, start_date=start_s, end_date=end_s,
            fields="ts_code,trade_date,open,high,low,close,vol,amount,pre_close,change,pct_chg",
        )
        if raw is None or len(raw) == 0:
            return pl.DataFrame()
        d = pl.from_pandas(raw)
        keep = [c for c in ("trade_date", "open", "high", "low", "close", "vol", "amount") if c in d.columns]
        if not keep:
            return pl.DataFrame()
        d = d.select(keep).rename({
            "trade_date": "date",
            "open": "open",
            "high": "high",
            "low": "low",
            "close": "close",
            "vol": "volume",
            "amount": "amount",
        })
        d = d.with_columns(
            pl.col("date").map_elements(_to_date, return_dtype=pl.Date, skip_nulls=False).alias("date"),
            # Tushare amount 单位为「千元」, 本项目 daily 契约为「元」 -> *1000
            (pl.col("amount").cast(pl.Float64, strict=False) * 1000.0).alias("amount"),
        ).filter(pl.col("date").is_not_null())
        return d

    # ---- adj_factor ----
    def get_adj_factors(
        self,
        symbols: list[str],
        start_time: datetime | None,
        end_time: datetime | None,
        asset_type: str = "stock",
        on_chunk_done: Callable[[int, int], None] | None = None,
    ) -> pl.DataFrame:
        """A 股除权因子 -> 内部契约(symbol/trade_date/ex_factor, 单事件比值非累积)。

        Tushare adj_factor 为累积复权因子 F; 单事件比值 ex_factor[d] = F[d-1]/F[d],
        仅在 F 发生跳变(除权日)输出, 对齐 indicators.pipeline._apply_adj_factor 的前向复权契约。
        """
        schema = {"symbol": pl.String, "trade_date": pl.Date, "ex_factor": pl.Float64}
        if not symbols or asset_type != "stock":
            return pl.DataFrame(schema=schema)
        end_dt = end_time or datetime.now()
        start_dt = start_time or (end_dt - timedelta(days=365))
        start_s = start_dt.strftime("%Y%m%d")
        end_s = end_dt.strftime("%Y%m%d")
        total = len(symbols)
        rows: list[dict] = []
        for i, sym in enumerate(symbols):
            try:
                events = self._adj_events_one(_ts_code(sym), start_s, end_s)
            except Exception as e:  # noqa: BLE001
                logger.warning("tushare adj_factor %s 失败: %s", sym, e)
                events = []
            rows.extend({"symbol": sym, "trade_date": d, "ex_factor": f} for d, f in events)
            if on_chunk_done:
                on_chunk_done(i + 1, total)
            if _RATE_SLEEP:
                time.sleep(_RATE_SLEEP)
        if not rows:
            return pl.DataFrame(schema=schema)
        return normalize_adj_factors(rows, source=self.name)

    def _adj_events_one(self, ts_code: str, start_s: str, end_s: str) -> list[tuple[date, float]]:
        """单标的除权事件: (trade_date, ex_factor)。无事件返回空列表。"""
        pro = self._pro_api()
        raw = pro.adj_factor(
            ts_code=ts_code, start_date=start_s, end_date=end_s,
            fields="ts_code,trade_date,adj_factor",
        )
        if raw is None or len(raw) == 0:
            return []
        d = pl.from_pandas(raw).select(
            trade_date=pl.col("trade_date"),
            F=pl.col("adj_factor").cast(pl.Float64, strict=False),
        )
        d = (
            d.sort("trade_date")
            .with_columns(F_prev=pl.col("F").shift(1))
            .with_columns(ratio=pl.col("F_prev") / pl.col("F"))
        )
        ev = d.filter(
            pl.col("ratio").is_not_null()
            & pl.col("ratio").is_finite()
            & ((pl.col("ratio") - 1.0).abs() > _EVENT_EPS)
        )
        out: list[tuple[date, float]] = []
        for dd, r in zip(ev["trade_date"].to_list(), ev["ratio"].to_list(), strict=False):
            dd = _to_date(dd)
            if dd is not None:
                out.append((dd, float(r)))
        return out

    # ---- realtime (best-effort, 仅有限 symbols; 全市场轮询请用 akshare) ----
    def get_realtime(
        self,
        universes: list[str] | None = None,
        symbols: list[str] | None = None,
    ) -> list[dict]:
        """有限集合实时行情 (走 Tushare legacy Sina 接口, 需 Token 且受 Sina 限制)。

        全市场轮询(传空 symbols)直接返回空, 请在设置页把 realtime_data_provider 设为 akshare。
        成交量单位按 Sina 原始口径(手)透传, 成交额为元。
        """
        syms = [s for s in (symbols or [])]
        if not syms:
            return []
        import tushare as ts

        out: list[dict] = []
        for i in range(0, len(syms), _SINA_BATCH):
            batch = syms[i : i + _SINA_BATCH]
            try:
                df = ts.realtime_quote(ts_code=",".join(batch), src="sina")
            except Exception as e:  # noqa: BLE001
                logger.warning("tushare 实时行情批次失败 %s: %s", batch[:3], e)
                continue
            if df is None or len(df) == 0:
                continue
            out.extend(self._map_realtime(df))
            time.sleep(0.2)
        return out

    def _map_realtime(self, df) -> list[dict]:
        """Sina realtime DataFrame -> 内部 realtime records (大小写/列名不敏感映射)。"""
        pdf = pl.from_pandas(df)
        cols = {str(c).upper(): c for c in pdf.columns}
        out: list[dict] = []
        for row in pdf.iter_rows(named=True):
            code = str(row.get(cols.get("TS_CODE") or cols.get("CODE") or "", "") or "").strip()
            if not code:
                continue
            name = str(row.get(cols.get("NAME") or cols.get("NAME_0") or "", "") or code).strip()
            last = _f(row.get(cols.get("PRICE") or cols.get("LAST_PRICE") or ""))
            prev = _f(row.get(cols.get("PRE_CLOSE") or cols.get("PREV_CLOSE") or ""))
            rec = {
                "symbol": code,
                "name": name,
                "last_price": last,
                "prev_close": prev,
                "open": _f(row.get(cols.get("OPEN") or "")),
                "high": _f(row.get(cols.get("HIGH") or "")),
                "low": _f(row.get(cols.get("LOW") or "")),
                "volume": _f(row.get(cols.get("VOLUME") or "")),
                "amount": _f(row.get(cols.get("AMOUNT") or "")),
                "change_pct": None,
                "change_amount": None,
                "amplitude": None,
                "turnover_rate": None,
                "timestamp": int(time.time() * 1000),
                "session": None,
            }
            if last is not None and prev not in (None, 0):
                rec["change_amount"] = last - prev
                rec["change_pct"] = (last - prev) / prev
            out.append(rec)
        return out

    # ---- 测试(设置页试拉) ----
    def test_dataset(self, dataset: str, symbols: list[str] | None = None) -> dict:
        if dataset in ("daily", "adj_factor"):
            syms = [s for s in (symbols or [])][:3] or ["000001.SZ"]
            try:
                if dataset == "daily":
                    df = self.get_daily(syms, datetime.now() - timedelta(days=30), datetime.now())
                else:
                    df = self.get_adj_factors(
                        syms, datetime.now() - timedelta(days=365), datetime.now()
                    )
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
        return {
            "provider": self.name,
            "dataset": dataset,
            "rows": 0,
            "error": "tushare 插件未接入该数据集(自动回退 TickFlow)",
        }
