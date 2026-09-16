"""AKShare 插件逻辑验证 (mock 模式, 无需联网)。

构造与真实 AKShare 完全同构的返回(列名/单位一致), 验证:
  1) 股票列表映射 (代码 -> 交易所后缀)
  2) 日K 字段映射到内部契约 (symbol/date/open/high/low/close/volume手/amount元)
  3) 复权因子单事件比值推导 (50% 除权 -> ex_factor=2.0)
  4) 实时快照字段 + 百分数->小数制
  5) 插件被 custom loader 自动发现并注册为可选 daily provider
"""
from __future__ import annotations

import sys
import types
import datetime as dt

import pandas as pd

# ---- 构造 mock akshare ----
fake = types.ModuleType("akshare")


def stock_info_a_code_name():
    return pd.DataFrame({
        "code": ["000001", "600000", "300750", "688001", "830799"],
        "name": ["平安银行", "浦发银行", "宁德时代", "华兴源创", "北交所测试"],
    })


def _raw_df():
    return pd.DataFrame({
        "日期": [dt.date(2024, 1, 2), dt.date(2024, 1, 3)],
        "开盘": [10.0, 5.0],
        "收盘": [10.0, 5.0],
        "最高": [10.5, 5.2],
        "最低": [9.5, 4.8],
        "成交量": [1000, 1000],   # 手
        "成交额": [10000.0, 5000.0],  # 元
        "振幅": [10.0, 8.0],
        "涨跌幅": [0.0, -50.0],
        "涨跌额": [0.0, -5.0],
        "换手率": [1.0, 1.0],
        "股票代码": ["000001", "000001"],
    })


def _qfq_df():
    # 前复权: 最新价不变, 历史按 0.5 系数下修 -> 两日收盘均为 5
    return pd.DataFrame({
        "日期": [dt.date(2024, 1, 2), dt.date(2024, 1, 3)],
        "开盘": [5.0, 5.0],
        "收盘": [5.0, 5.0],
        "最高": [5.25, 5.2],
        "最低": [4.75, 4.8],
        "成交量": [1000, 1000],
        "成交额": [5000.0, 5000.0],
        "振幅": [10.0, 8.0],
        "涨跌幅": [-50.0, 0.0],
        "涨跌额": [-5.0, 0.0],
        "换手率": [1.0, 1.0],
        "股票代码": ["000001", "000001"],
    })


def stock_zh_a_hist(symbol, period="daily", start_date="", end_date="", adjust="", timeout=None):
    return _qfq_df() if adjust == "qfq" else _raw_df()


def stock_zh_a_spot_em():
    return pd.DataFrame({
        "代码": ["000001"],
        "名称": ["平安银行"],
        "最新价": [10.5],
        "涨跌幅": [3.66],
        "涨跌额": [0.37],
        "成交量": [123456],
        "成交额": [1296288.0],
        "振幅": [4.21],
        "换手率": [1.23],
        "今开": [10.2],
        "最高": [10.8],
        "最低": [10.1],
        "昨收": [10.13],
    })


fake.stock_info_a_code_name = stock_info_a_code_name
fake.stock_zh_a_hist = stock_zh_a_hist
fake.stock_zh_a_spot_em = stock_zh_a_spot_em
fake.__version__ = "mock"
sys.modules["akshare"] = fake

# ---- 开始验证 ----
import polars as pl
from app.plugins.akshare.provider import AKShareProvider, _exchange_suffix
from app.data_providers import custom as custom_sources
from app.services import preferences

p = AKShareProvider()
ok = True


def check(label, cond, extra=""):
    global ok
    print(f"[{'PASS' if cond else 'FAIL'}] {label} {extra}")
    if not cond:
        ok = False


# 1) 交易所后缀
check("suffix 600000->SH", _exchange_suffix("600000") == "SH")
check("suffix 000001->SZ", _exchange_suffix("000001") == "SZ")
check("suffix 688001->SH", _exchange_suffix("688001") == "SH")
check("suffix 830799->BJ", _exchange_suffix("830799") == "BJ")

# 2) instruments
inst = p.get_instruments("stock")
syms = {d["symbol"] for d in inst}
check("instruments 数量=5", len(inst) == 5, f"got {len(inst)}")
check("instruments 含 000001.SZ", "000001.SZ" in syms, str(sorted(syms)))
check("instruments 含 688001.SH", "688001.SH" in syms)
check("instruments 含 830799.BJ", "830799.BJ" in syms)

# 3) daily
d = p.get_daily(["000001.SZ"], dt.datetime(2024, 1, 1), dt.datetime(2024, 1, 5))
need = {"symbol", "date", "open", "high", "low", "close", "volume", "amount"}
check("daily 列完整", need.issubset(set(d.columns)), str(d.columns))
check("daily 行数=2", d.height == 2, f"got {d.height}")
check("daily 收盘价正确", d.filter(pl.col("symbol") == "000001.SZ")["close"].to_list() == [10.0, 5.0])
check("daily volume 单位=手", d["volume"].to_list() == [1000, 1000])

# 4) adj_factor
af = p.get_adj_factors(["000001.SZ"], dt.datetime(2024, 1, 1), dt.datetime(2024, 1, 5))
check("adj_factor 列完整", {"symbol", "trade_date", "ex_factor"}.issubset(set(af.columns)), str(af.columns))
check("adj_factor 单事件 ex_factor=2.0", abs(af["ex_factor"][0] - 2.0) < 1e-6, str(af.to_dicts()))

# 5) realtime
rt = p.get_realtime()
check("realtime 非空", len(rt) == 1, f"got {len(rt)}")
r0 = rt[0]
check("realtime symbol=000001.SZ", r0["symbol"] == "000001.SZ")
check("realtime change_pct 小数制", abs(r0["change_pct"] - 0.0366) < 1e-6, f"got {r0['change_pct']}")
check("realtime amplitude 小数制", abs(r0["amplitude"] - 0.0421) < 1e-6, f"got {r0['amplitude']}")
check("realtime turnover 小数制", abs(r0["turnover_rate"] - 0.0123) < 1e-6, f"got {r0['turnover_rate']}")

# 6) 插件注册
custom_sources.load_all()
check("插件被注册为 akshare", "akshare" in custom_sources.names(), str(sorted(custom_sources.names())))
check("provider_has_dataset(daily)=True", custom_sources.provider_has_dataset("akshare", "daily"))
check("provider_has_dataset(adj_factor)=True", custom_sources.provider_has_dataset("akshare", "adj_factor"))
check("provider_has_dataset(realtime)=True", custom_sources.provider_has_dataset("akshare", "realtime"))
check("provider_has_dataset(minute)=False(回退)", not custom_sources.provider_has_dataset("akshare", "minute"))
allowed = preferences._allowed_data_providers()
check("akshare 进入可选 provider 列表", "akshare" in allowed, str(sorted(allowed)))

print("\n==== 结果:", "全部通过 ✅" if ok else "存在失败 ❌", "====")
sys.exit(0 if ok else 1)
