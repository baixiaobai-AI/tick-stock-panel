"""Tushare 插件逻辑校验 (沙盒无 Token/无 tushare 网络, 用精确仿真 mock 注入)。

不依赖真实网络: 直接 monkeypatch provider 的 _pro_api / import tushare 行为,
验证字段映射、amount 千元→元、复权因子单事件推导、instruments、插件注册与数据集分流。
"""
from __future__ import annotations

import sys
from datetime import date, datetime, timedelta
from pathlib import Path

import polars as pl

# 让 app.* 可导入
sys.path.insert(0, str(Path(__file__).resolve().parent))

from app.plugins.tushare import provider as tsp
from app.data_providers.custom.loader import provider_has_dataset

FAIL = []


def check(name: str, cond: bool, detail: str = "") -> None:
    status = "PASS" if cond else "FAIL"
    print(f"[{status}] {name}" + (f" :: {detail}" if detail and not cond else ""))
    if not cond:
        FAIL.append(name)


# ── 1. instruments: ts_code 带后缀 -> symbol ──
def fake_stock_basic(**kw):
    import pandas as pd
    return pd.DataFrame([
        {"ts_code": "000001.SZ", "symbol": "000001", "name": "平安银行", "exchange": "SZ"},
        {"ts_code": "600000.SH", "symbol": "600000", "name": "浦发银行", "exchange": "SH"},
        {"ts_code": "830799.BJ", "symbol": "830799", "name": "艾融软件", "exchange": "BJ"},
    ])


class _FakePro:
    def __init__(self):
        self._daily = {
            "000001.SZ": [
                ("20240102", 10.0, 10.5, 9.8, 10.2, 1000.0, 1050.0),  # vol手, amount千元
                ("20240103", 10.2, 10.6, 10.1, 10.4, 1200.0, 1260.0),
            ],
        }
        self._adj = {
            "000001.SZ": [
                ("20240102", 2.0),
                ("20240103", 1.0),
            ],
        }

    def stock_basic(self, **kw):
        return fake_stock_basic(**kw)

    def daily(self, ts_code, start_date, end_date, fields=None):
        import pandas as pd
        rows = self._daily.get(ts_code, [])
        return pd.DataFrame(
            [{"ts_code": ts_code, "trade_date": r[0], "open": r[1], "high": r[2],
              "low": r[3], "close": r[4], "vol": r[5], "amount": r[6]} for r in rows]
        )

    def adj_factor(self, ts_code, start_date, end_date, fields=None):
        import pandas as pd
        rows = self._adj.get(ts_code, [])
        return pd.DataFrame(
            [{"ts_code": ts_code, "trade_date": r[0], "adj_factor": r[1]} for r in rows]
        )


class _FakeTs:
    @staticmethod
    def pro_api(key):
        return _FakePro()


# monkeypatch: provider._pro_api 返回 _FakePro; import tushare 返回 _FakeTs
tsp.get_api_key = lambda: "fake-token"  # type: ignore[assignment]
tsp.import_tushare = _FakeTs  # type: ignore[attr-defined]
_orig_pro_api = tsp.TushareProvider._pro_api
def _patched_pro_api(self):  # noqa: ANN001
    return _FakePro()
tsp.TushareProvider._pro_api = _patched_pro_api  # type: ignore[assignment]

p = tsp.TushareProvider()

# ── 1. instruments ──
inst = p.get_instruments("stock")
check("instruments 数量=3", len(inst) == 3, f"got {len(inst)}")
check("instruments[0].symbol=000001.SZ", inst and inst[0]["symbol"] == "000001.SZ", str(inst[0] if inst else None))
check("instruments[0].exchange=SZ", inst and inst[0]["exchange"] == "SZ")
check("instruments[2].exchange=BJ (北交所)", len(inst) == 3 and inst[2]["exchange"] == "BJ")

# ── 2. daily: amount 千元→元, volume 手 透传, symbol 补上 ──
dd = p.get_daily(["000001.SZ"], datetime(2024, 1, 1), datetime(2024, 1, 4))
check("daily 行数=2", dd.height == 2, f"got {dd.height}")
check("daily 含 symbol 列", "symbol" in dd.columns)
check("daily volume=1000 手(透传)", dd.filter(pl.col("symbol") == "000001.SZ")["volume"][0] == 1000.0)
amt = dd.filter(pl.col("symbol") == "000001.SZ")["amount"][0]
check("daily amount 千元→元 (1050*1000=1.05e6)", abs(amt - 1_050_000.0) < 1.0, f"got {amt}")

# ── 3. adj_factor: 累积 F=[2.0,1.0] -> 单事件 ex_factor=2.0(仅除权日) ──
af = p.get_adj_factors(["000001.SZ"], datetime(2024, 1, 1), datetime(2024, 1, 4))
check("adj_factor 行数=1 (仅除权日)", af.height == 1, f"got {af.height}")
check("adj_factor ex_factor=2.0", af.height == 1 and abs(af["ex_factor"][0] - 2.0) < 1e-9,
      f"got {af['ex_factor'][0] if af.height else None}")
check("adj_factor trade_date=2024-01-03", af.height == 1 and af["trade_date"][0] == date(2024, 1, 3))

# ── 4. get_realtime 空 symbols -> 空 (全市场回退 akshare) ──
rt = p.get_realtime()
check("realtime 空 symbols 返回空 (全市场轮询回退)", rt == [])

# ── 5. 插件注册 / 数据集分流 (loader 视角) ──
# 重新扫描: 用已 mock 的 get_api_key (fake-token) 让 tushare 通过 availability 注册
from app.data_providers.custom.loader import load_all
load_all()
check("provider_has_dataset(tushare, daily)=True", provider_has_dataset("tushare", "daily"))
check("provider_has_dataset(tushare, adj_factor)=True", provider_has_dataset("tushare", "adj_factor"))
check("provider_has_dataset(tushare, realtime)=False (回退)", not provider_has_dataset("tushare", "realtime"))
check("provider_has_dataset(tushare, minute)=False (回退)", not provider_has_dataset("tushare", "minute"))

# ── 6. availability: 无 token 应 False ──
_orig_get_key = tsp.get_api_key
tsp.get_api_key = lambda: ""  # type: ignore[assignment]
ok, why = tsp.availability()
check("availability 无 token=False", ok is False, why)
# 恢复
tsp.get_api_key = _orig_get_key  # type: ignore[assignment]

print("\n" + ("✅ 全部通过" if not FAIL else f"❌ 失败项: {FAIL}"))
sys.exit(1 if FAIL else 0)
