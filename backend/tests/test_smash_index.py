"""砸盘指数(smash_index)单元测试 — 分档晋级率与 ZPZS。

ZPZS = SUM(各连板梯队晋级率) / divisor * multiplier, 4 档 = 昨日 1板/2板/3板/4板以上;
divisor 默认 4(求均值)、multiplier 默认 10(放大到可读区间), 二者均可在图表左上角调整。
测试用 tmp_path 造出与生产一致的 date=*/*.parquet 分区, 覆盖:
分档正确性、空梯队记 0、理论上限 10、窗口预热(首日仍能拿到昨日连板数)、
start/end 与 limit 两种查询模式的裁剪语义, 以及 divisor/multiplier 可调。
"""
from __future__ import annotations

from datetime import date
from pathlib import Path

import polars as pl
import pytest

from app.services import smash_index

D1 = date(2026, 9, 1)
D2 = date(2026, 9, 2)
D3 = date(2026, 9, 3)
D4 = date(2026, 9, 4)


def _write_enriched(root: Path, rows: list[dict]) -> None:
    """按 date=YYYY-MM-DD/part.parquet 落盘(与生产目录结构一致)。"""
    df = pl.DataFrame(rows, schema_overrides={"date": pl.Date})
    for keys, part in df.group_by(["date"], maintain_order=True):
        d = keys[0]
        p = root / smash_index.ENRICHED_DIR / f"date={d}"
        p.mkdir(parents=True, exist_ok=True)
        part.write_parquet(p / "part.parquet")


@pytest.fixture(autouse=True)
def _no_st_filter(monkeypatch):
    """单测不受真实 ST 名单与用户偏好影响(否则本机配置会静默改变结果)。"""
    monkeypatch.setattr(smash_index, "_load_st_symbols", lambda _dir: [])


@pytest.fixture(autouse=True)
def _clean_cache():
    smash_index.invalidate_cache()
    yield
    smash_index.invalidate_cache()


def _rows(pairs: list[tuple[str, date, int]]) -> list[dict]:
    return [{"symbol": s, "date": d, "consecutive_limit_ups": c} for s, d, c in pairs]


class TestRungRates:
    def test_bucket_promotion_rates(self, tmp_path):
        """1进2/2进3/3进4/4板以上 四档各自按"昨日该档"分池算晋级率。"""
        _write_enriched(tmp_path, _rows([
            # 昨日(D1)                                 今日(D2)
            ("A", D1, 1), ("A", D2, 2),   # 1板 → 2板: 晋级
            ("B", D1, 1), ("B", D2, 0),   # 1板 → 断板: 未晋级
            ("C", D1, 2), ("C", D2, 3),   # 2板 → 3板: 晋级
            ("D", D1, 3), ("D", D2, 0),   # 3板 → 断板: 未晋级
            ("E", D1, 4), ("E", D2, 5),   # 4板 → 5板: 晋级(高度板档)
        ]))
        out = smash_index.compute_smash_series(tmp_path)
        by_date = {r["date"]: r for r in out.to_dicts()}
        # D1 没有"昨日"(窗口首日), 不出现在结果里
        assert D1 not in by_date
        r = by_date[D2]
        assert r["promo_1to2"] == 0.5 and r["promo_1to2_pool"] == 2 and r["promo_1to2_ok"] == 1
        assert r["promo_2to3"] == 1.0 and r["promo_2to3_pool"] == 1
        assert r["promo_3to4"] == 0.0 and r["promo_3to4_pool"] == 1
        assert r["promo_4up"] == 1.0 and r["promo_4up_pool"] == 1
        # ZPZS = (0.5+1+0+1)/4*10
        assert r["zpzs"] == 6.25

    def test_empty_rung_counts_as_zero(self, tmp_path):
        """梯队无样本 → 该档记 0(不是跳过), 与公式"分母固定 4"一致。"""
        _write_enriched(tmp_path, _rows([
            ("A", D1, 1), ("A", D2, 0),
        ]))
        r = smash_index.compute_smash_series(tmp_path).to_dicts()[0]
        assert r["promo_1to2"] == 0.0 and r["promo_1to2_pool"] == 1
        assert r["promo_2to3_pool"] == 0 and r["promo_2to3"] == 0.0
        assert r["promo_3to4_pool"] == 0 and r["promo_4up_pool"] == 0
        assert r["zpzs"] == 0.0

    def test_theoretical_ceiling_is_ten(self, tmp_path):
        """四档全部 100% 晋级 → ZPZS 恰好 10(平均晋级率 1 × 10 为其上限)。"""
        _write_enriched(tmp_path, _rows([
            ("A", D1, 1), ("A", D2, 2),
            ("B", D1, 2), ("B", D2, 3),
            ("C", D1, 3), ("C", D2, 4),
            ("D", D1, 9), ("D", D2, 10),
        ]))
        r = smash_index.compute_smash_series(tmp_path).to_dicts()[0]
        assert r["zpzs"] == 10.0

    def test_high_rung_is_open_ended(self, tmp_path):
        """4 板以上是开口档: 5板→6板、8板→9板 都算这一档的样本。"""
        _write_enriched(tmp_path, _rows([
            ("A", D1, 5), ("A", D2, 6),
            ("B", D1, 8), ("B", D2, 0),
        ]))
        r = smash_index.compute_smash_series(tmp_path).to_dicts()[0]
        assert r["promo_4up_pool"] == 2 and r["promo_4up_ok"] == 1
        assert r["promo_4up"] == 0.5


class TestWindow:
    def test_window_warmup_keeps_first_day_rate(self, tmp_path):
        """窗口首日仍须能拿到"昨日"连板数: 只取 D3 时 1进2 不能因缺预热而变 0。"""
        _write_enriched(tmp_path, _rows([
            ("A", D1, 1), ("A", D2, 1), ("A", D3, 2),   # D3: 1板→2板 晋级
            ("B", D1, 1), ("B", D2, 1), ("B", D3, 0),   # D3: 1板→断板
        ]))
        only_d3 = smash_index.compute_smash_series(tmp_path, start=D3, end=D3)
        assert only_d3.height == 1
        r = only_d3.to_dicts()[0]
        assert r["date"] == D3
        assert r["promo_1to2_pool"] == 2 and r["promo_1to2_ok"] == 1
        # ZPZS = (0.5 + 0 + 0 + 0)/4*10: 只有 1进2 有样本, 其余三档记 0
        assert r["zpzs"] == 1.25

    def test_date_range_is_not_truncated_by_limit(self, tmp_path):
        """传了 start/end 的调用方要完整区间, 不该被 limit 截断(与 /history 同语义)。"""
        _write_enriched(tmp_path, _rows([
            (s, d, 1) for s in ("A", "B") for d in (D1, D2, D3, D4)
        ]))
        rows = smash_index.get_smash_series(tmp_path, start=D2, end=D4, limit=1)
        assert [str(r["date"]) for r in rows] == [str(D2), str(D3), str(D4)]

    def test_limit_mode_takes_latest(self, tmp_path):
        """未传 start/end("最近 N 天"模式)时 limit 生效, 且从最新往回取。"""
        _write_enriched(tmp_path, _rows([
            (s, d, 1) for s in ("A", "B") for d in (D1, D2, D3, D4)
        ]))
        rows = smash_index.get_smash_series(tmp_path, limit=2)
        assert [str(r["date"]) for r in rows] == [str(D3), str(D4)]
        # JSON 安全: date 已是字符串
        assert all(isinstance(r["date"], str) for r in rows)

    def test_missing_data_dir_returns_empty(self, tmp_path):
        """没有 enriched 数据(全新安装)时返回空, 不抛异常。"""
        assert smash_index.compute_smash_series(tmp_path).is_empty()
        assert smash_index.get_smash_series(tmp_path) == []


class TestScale:
    """divisor / multiplier 可在图表左上角调整, 直接影响 ZPZS 量纲。"""

    def test_custom_divisor_multiplier_scales_zpzs(self, tmp_path):
        """同一份晋级率, 改 divisor/multiplier 应线性改变 ZPZS。

        数据: 1进2=0.5, 2进3=1, 3进4=0, 4板以上=1 → Σ=2.5。
        默认(/4*10)=6.25; 改 /2*20 → 2.5/2*20=25.0。
        """
        _write_enriched(tmp_path, _rows([
            ("A", D1, 1), ("A", D2, 2),
            ("B", D1, 1), ("B", D2, 0),
            ("C", D1, 2), ("C", D2, 3),
            ("D", D1, 3), ("D", D2, 0),
            ("E", D1, 4), ("E", D2, 5),
        ]))
        base = smash_index.compute_smash_series(tmp_path).to_dicts()[0]
        scaled = smash_index.compute_smash_series(tmp_path, divisor=2, multiplier=20).to_dicts()[0]
        assert base["zpzs"] == 6.25
        assert scaled["zpzs"] == 25.0
        # 晋级率本身不受缩放影响
        assert scaled["promo_1to2"] == base["promo_1to2"] == 0.5

    def test_get_series_cache_key_includes_scale(self, tmp_path):
        """divisor/multiplier 不同 → 缓存键不同 → 返回不同结果(不被旧缓存串味)。

        数据: A/B 在 D1=1, D2..D4=2 → 仅 D2 的"昨日=1板、今日=2板"算 1进2 晋级,
        1进2=1.0, 其余三档恒 0 → D2 的 Σ=1.0。默认 /4*10=2.5; 改 /2*10=5.0。
        """
        _write_enriched(tmp_path, _rows([
            ("A", D1, 1), ("A", D2, 2), ("A", D3, 2), ("A", D4, 2),
            ("B", D1, 1), ("B", D2, 2), ("B", D3, 2), ("B", D4, 2),
        ]))
        a = smash_index.get_smash_series(tmp_path, divisor=4, multiplier=10)[0]
        b = smash_index.get_smash_series(tmp_path, divisor=2, multiplier=10)[0]
        assert a["zpzs"] == 2.5           # (1.0+0+0+0)/4*10
        assert b["zpzs"] == 5.0           # (1.0+0+0+0)/2*10

