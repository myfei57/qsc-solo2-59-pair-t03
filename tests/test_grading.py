"""标定批次一致性判定。

覆盖四件事：
1. 固定阈值与自适应两种口径在小批量/大批量上的分歧及口径选择边界；
2. 批统计、离群挑选与放行门禁（CV、压差极差、离群占比）；
3. 补录/修订后的按时刻重算必须复现当时结论；
4. 已隔离单体粘性保持：重算不自动回流产线，只有人工放行能解除。
"""

from __future__ import annotations

import pytest

from batterystore.grading import (
    CellReading,
    GradingBook,
    GradingPolicy,
    analyze_batch,
    grade_batch,
    hold_cell,
    recompute_batch,
    release_cell,
    summary_view,
)
from batterystore.grading.cli import _read_csv
from batterystore.clock import ManualClock
from batterystore.store.jsonfile import JsonStore


@pytest.fixture()
def clock():
    return ManualClock(start=1000.0)


@pytest.fixture()
def book(tmp_path, clock):
    return GradingBook(JsonStore(str(tmp_path / "grading")), clock=clock)


def _policy(**overrides) -> GradingPolicy:
    base = dict(
        nominal_capacity_ah=100.0,
        nominal_voltage_diff_mv=0.0,
        capacity_tol_pct=3.0,
        voltage_diff_tol_mv=10.0,
        adaptive_min_n=20,
        robust_z_cutoff=3.5,
        cv_cap_pct=1.5,
        voltage_spread_cap_mv=30.0,
        isolate_fraction_cap=0.10,
    )
    base.update(overrides)
    return GradingPolicy(**base)


def _batch(n, *, outlier=False, tight=False):
    """造一批默认合格的单体；outlier 时最后一个在固定与自适应上都离群。

    tight=True 时分布极紧（±0.05Ah 抖动，MAD>0），相对异类会被自适应抓住但
    仍在固定 ±3% 带内；普通批次用 ±0.8Ah 抖动。
    """

    readings = []
    for index in range(n - (1 if outlier else 0)):
        if tight:
            jitter = 0.05 if index % 2 == 0 else -0.05
            vdiff = 2.5 if index % 2 == 0 else 3.0
        else:
            jitter = 0.8 if index % 2 == 0 else -0.8
            vdiff = 2.0 if index % 2 == 0 else 4.0
        readings.append(CellReading(f"c{index:03d}", 100.0 + jitter, vdiff))
    if outlier:
        readings.append(CellReading(f"c{n - 1:03d}", 96.0, 14.0))
    return readings


# ---------------------------------------------------------------- 口径与统计

def test_small_batch_uses_fixed_threshold_only():
    report = grade_batch(_batch(8), _policy())
    assert report["n"] == 8
    assert report["authoritative"] == "fixed"
    assert report["policy"]["adaptive_active"] is False
    # 小批量下自适应结果只作展示，任何单体都不能以 adaptive 为由被隔离。
    assert all(
        not reason.endswith(":adaptive")
        for cell in report["cells"]
        for reason in cell["reasons"]
    )
    assert report["verdict"] == "release"


def test_small_batch_fixed_outlier_isolated():
    report = grade_batch(_batch(20, outlier=True), _policy(adaptive_min_n=30))
    outlier = next(cell for cell in report["cells"] if cell["cell_id"] == "c019")
    assert outlier["disposition"] == "isolate"
    assert "capacity_ah:fixed" in outlier["reasons"]
    assert "voltage_diff_mv:fixed" in outlier["reasons"]
    assert report["isolated_ids"] == ["c019"]
    # 1/21 离群且其余一致性合格：好单体不连坐，随批放行。
    assert report["verdict"] == "release"


def test_adaptive_catches_relative_outlier_that_fixed_tolerates():
    # 大样本、分布极紧：离群单体仍在 ±3% 固定带内，但相对本批明显异类。
    readings = _batch(24, tight=True)
    readings.append(CellReading("c024", 98.2, 8.0))
    report = grade_batch(readings, _policy())
    assert report["authoritative"] == "adaptive"
    cell = next(item for item in report["cells"] if item["cell_id"] == "c024")
    assert cell["metrics"]["capacity_ah"]["fixed_outlier"] is False
    assert cell["metrics"]["capacity_ah"]["adaptive_outlier"] is True
    assert cell["disposition"] == "isolate"
    assert "capacity_ah:adaptive" in cell["reasons"]


def test_fixed_threshold_remains_hard_veto_under_adaptive():
    # 96Ah 对 100Ah 标称恰好 -4%，超出固定 ±3%；即便本批很散、稳健 z 不够大，
    # 固定规格线仍是硬门禁。
    readings = [CellReading(f"c{i:03d}", 100.0 + (i % 5) * 1.2 - 2.4, 3.0 + (i % 4))
                for i in range(23)]
    readings.append(CellReading("c023", 96.0, 5.0))
    report = grade_batch(readings, _policy())
    cell = next(item for item in report["cells"] if item["cell_id"] == "c023")
    assert report["authoritative"] == "adaptive"
    assert cell["metrics"]["capacity_ah"]["fixed_outlier"] is True
    assert cell["disposition"] == "isolate"


def test_two_criteria_disagree_on_small_batch_boundary():
    # 紧分布 + 相对异类：自适应想隔离，但 n=20 低于边界 21，固定阈值合格 → 放行。
    readings = _batch(19, tight=True)
    readings.append(CellReading("c019", 98.3, 7.0))
    small = grade_batch(readings, _policy(adaptive_min_n=21))
    assert small["n"] == 20
    assert small["authoritative"] == "fixed"
    assert small["cells"][-1]["disposition"] == "release"
    # 同样的分布形态补到 n=21，口径翻转为自适应，该单体被隔离。
    big = grade_batch(readings + [CellReading("c020", 100.0, 3.0)], _policy(adaptive_min_n=21))
    assert big["n"] == 21
    assert big["authoritative"] == "adaptive"
    target = next(item for item in big["cells"] if item["cell_id"] == "c019")
    assert target["disposition"] == "isolate"


def test_mad_zero_does_not_flag_everyone():
    # 半数以上同值导致 MAD=0：自适应不可用，不能误判全批离群。
    readings = [CellReading(f"c{i:03d}", 100.0, 2.0) for i in range(22)]
    readings[5] = CellReading("c005", 100.5, 2.0)
    report = grade_batch(readings, _policy())
    assert report["authoritative"] == "adaptive"
    assert all(cell["metrics"]["capacity_ah"]["adaptive_applicable"] is False
               for cell in report["cells"])
    assert report["verdict"] == "release"


def test_stats_fields():
    stats = grade_batch(_batch(8, outlier=True), _policy())["stats"]["capacity_ah"]
    assert stats["n"] == 8
    assert stats["min"] < stats["max"]
    assert stats["spread"] == pytest.approx(4.8)
    assert stats["cv_pct"] > 0


# ---------------------------------------------------------------- 放行门禁

def test_excessive_outlier_fraction_holds_batch():
    # 6/20 离群（30%）超 10% 上限：不是好单体跟着走，而是整批改人工复核。
    readings = [CellReading(f"c{i:03d}", 100.0, 3.0) for i in range(14)]
    readings += [CellReading(f"b{i:03d}", 90.0, 20.0) for i in range(6)]
    report = grade_batch(readings, _policy())
    assert report["verdict"] == "review"
    gate = next(item for item in report["gates"] if item["name"] == "new_outlier_fraction")
    assert gate["passed"] is False
    assert gate["actual"] == pytest.approx(0.3)


def test_capacity_cv_gate():
    readings = [CellReading(f"c{i:03d}", 97.0 + i * 0.3, 3.0) for i in range(20)]
    report = grade_batch(readings, _policy())
    gate = next(item for item in report["gates"] if item["name"] == "capacity_cv")
    assert gate["passed"] is False
    assert report["verdict"] == "review"


def test_voltage_spread_gate():
    # 放宽固定带使单体不触发规格离群，用极差门禁单独检验：36mV > 30mV 上限。
    readings = [
        CellReading(f"c{i:03d}", 100.0, 2.0 + i * (36.0 / 19))
        for i in range(20)
    ]
    report = grade_batch(readings, _policy(voltage_diff_tol_mv=40.0, robust_z_cutoff=99.0))
    gate = next(item for item in report["gates"] if item["name"] == "voltage_diff_spread")
    assert gate["passed"] is False
    assert report["verdict"] == "review"


def test_sticky_held_cells_do_not_poison_fresh_batch_gate():
    # 隔离区里已有 5 个老粘性单体，本批 20 个新单体全部合格：
    # 老单体不进本批离群占比，本批结论仍可放行。
    readings = [CellReading(f"c{i:03d}", 100.0, 3.0) for i in range(20)]
    report = grade_batch(readings, _policy(), pre_quarantined=frozenset({f"old{i}" for i in range(5)}))
    gate = next(item for item in report["gates"] if item["name"] == "new_outlier_fraction")
    assert gate["actual"] == 0.0
    assert report["verdict"] == "release"
    assert len(report["quarantine_worklist"]) == 0  # 老单体不在本批读数里，不出现


# ---------------------------------------------------------------- 台账与粘性

def _populate(book, batch, readings, *, start=1000.0):
    book.register_batch(batch, _policy())
    clock = book.clock
    clock._now = start
    for reading in readings:
        book.record_reading(batch, reading, recorded_at=start, source="test")
        clock.advance(1.0)


def test_analyze_isolates_and_second_run_is_idempotent(book):
    _populate(book, "B1", _batch(24, tight=True) + [CellReading("c024", 98.2, 8.0)])
    first = analyze_batch(book, "B1")
    assert first["newly_isolated"] == ["c024"]
    assert first["report"]["verdict"] == "release"
    second = analyze_batch(book, "B1")
    assert second["newly_isolated"] == []
    # c024 已在隔离区：列为待复核，但不连坐其余 24 个合格单体。
    assert "c024" in second["report"]["quarantine_worklist"]
    assert second["report"]["isolated_ids"] == []
    assert second["report"]["verdict"] == "release"
    assert len(second["report"]["released_ids"]) == 24


def test_revision_does_not_return_quarantined_cell_to_line(book):
    readings = _batch(24, tight=True)
    _populate(book, "B1", readings + [CellReading("c024", 98.2, 8.0)])
    analyze_batch(book, "B1")
    # 事后修订：把离群单体改得完全合格。
    book.record_reading("B1", CellReading("c024", 100.0, 3.0), recorded_at=2000.0)
    outcome = analyze_batch(book, "B1")
    report = outcome["report"]
    cell = next(item for item in report["cells"] if item["cell_id"] == "c024")
    # 数据虽转好，但处置仍隔离、列入待复核，绝不自动回流产线。
    assert cell["disposition"] == "isolate"
    assert "sticky:previously_quarantined" in cell["reasons"]
    assert "c024" in report["quarantine_worklist"]
    assert outcome["newly_isolated"] == []
    # 其余 24 个合格单体不受连累，本批仍可放行。
    assert report["verdict"] == "release"
    assert len(report["released_ids"]) == 24


def test_manual_release_is_only_path_back(book):
    readings = _batch(24, tight=True)
    _populate(book, "B1", readings + [CellReading("c024", 98.2, 8.0)])
    analyze_batch(book, "B1")
    with pytest.raises(Exception):
        release_cell(book, "B1", "c024", operator="q", reason="")
    release_cell(book, "B1", "c024", operator="qa-li", reason="复测容量99.8压差4mV")
    outcome = analyze_batch(book, "B1")
    # 人工放行后该单体离开隔离区待办，且不会被统计重算再次自动隔离。
    assert "c024" not in outcome["report"]["quarantine_worklist"]
    assert "c024" in outcome["manual_released"]
    assert "c024" in outcome["report"]["released_ids"]
    assert outcome["newly_isolated"] == []
    assert outcome["report"]["verdict"] == "release"
    view = {item["cell_id"]: item["state"] for item in book.disposition_view("B1")}
    assert view["c024"] == "released"


def test_manual_hold_keeps_cell_in_quarantine_worklist(book):
    _populate(book, "B1", _batch(20))
    book.record_reading("B1", CellReading("c019", 100.0, 3.0))
    hold_cell(book, "B1", "c019", operator="qa-li", reason="外观异常待检")
    outcome = analyze_batch(book, "B1")
    assert "c019" in outcome["report"]["quarantine_worklist"]
    # c019 是 21 个读数里的一个；它隔离，其余 20 个合格仍可放行。
    assert outcome["report"]["verdict"] == "release"
    assert "c019" not in outcome["report"]["released_ids"]


def test_recompute_reproduces_then_state_without_side_effects(book, clock):
    readings = _batch(24, tight=True)
    _populate(book, "B1", readings, start=1000.0)
    # 24 条录入落在系统时间 1000..1023。
    analyze_batch(book, "B1")
    first_analyze_ts = clock.now()
    # 之后（系统时间）补录一个相对异类；业务标定时间填过去也不改变系统视图。
    clock.advance(100.0)
    book.record_reading("B1", CellReading("c024", 98.2, 8.0), recorded_at=1000.0)
    analyze_batch(book, "B1")

    # 按补录发生之前的系统时刻复算：系统当时只有 24 个、结论放行。
    at = recompute_batch(book, "B1", as_of=first_analyze_ts + 1.0)
    assert at["report"]["n"] == 24
    assert at["report"]["verdict"] == "release"
    assert "c024" not in {cell["cell_id"] for cell in at["report"]["cells"]}
    assert at["newly_isolated"] == []
    # 复算不落隔离事件：台账里 c024 的隔离来自补录后的那次 analyze（仅 1 条）。
    events = [item for item in book.disposition_view("B1") if item["cell_id"] == "c024"]
    assert len(events) == 1 and events[0]["state"] == "quarantined"
    # 当前结论里 c024 仍在隔离区（held），不再作为新离群。
    current = analyze_batch(book, "B1")
    assert "c024" in current["report"]["held_ids"]
    assert current["newly_isolated"] == []


def test_recompute_respects_quarantine_at_that_time(book):
    readings = _batch(20)
    _populate(book, "B1", readings, start=1000.0)
    analyze_batch(book, "B1")
    # 人工隔离一个单体后，晚于该时刻的复算必须把它算进隔离待办。
    hold_cell(book, "B1", "c019", operator="qa", reason="抽检不合格")
    at = recompute_batch(book, "B1", as_of=10_000_000_000.0)
    assert "c019" in at["report"]["quarantine_worklist"]
    cell = next(item for item in at["report"]["cells"] if item["cell_id"] == "c019")
    assert cell["disposition"] == "isolate"


def test_void_reading_removes_from_current_but_kept_in_history(book, clock):
    _populate(book, "B1", _batch(20), start=1000.0)
    # 20 条录入的系统时间为 1000..1019；作废发生在更晚，按 1019 复算读数仍在。
    book.void_reading("B1", "c019", reason="录入串号")
    assert len(book.readings("B1")) == 19
    assert len(book.readings("B1", as_of=1019.0)) == 20


def test_policy_frozen_per_batch(book):
    _populate(book, "B1", _batch(8))
    with pytest.raises(Exception):
        book.register_batch("B1", _policy(capacity_tol_pct=5.0))


def test_persistence_across_book_instances(tmp_path):
    root = str(tmp_path / "grading")
    first = GradingBook(JsonStore(root), clock=ManualClock(start=1000.0))
    _populate(first, "B1", _batch(24, tight=True) + [CellReading("c024", 98.2, 8.0)])
    analyze_batch(first, "B1")

    reopened = GradingBook(JsonStore(root), clock=ManualClock(start=9999.0))
    assert "B1" in reopened.batch_ids()
    assert "c024" in reopened.quarantined_cells("B1")
    outcome = analyze_batch(reopened, "B1")
    # 重启后隔离状态不丢：c024 在隔离待办里；其余 24 个合格单体仍可放行。
    assert "c024" in outcome["report"]["quarantine_worklist"]
    assert outcome["report"]["verdict"] == "release"
    assert reopened.policy_of("B1").capacity_tol_pct == 3.0


def test_summary_view_history(book):
    _populate(book, "B1", _batch(20))
    analyze_batch(book, "B1")
    recompute_batch(book, "B1", as_of=10_000_000_000.0)
    summary = summary_view(book, "B1")
    assert summary["n"] == 20
    tags = [item["as_of"] is None for item in summary["history"]]
    assert tags == [True, False]


# ---------------------------------------------------------------- CSV

def test_csv_import_roundtrip(tmp_path):
    path = tmp_path / "cells.csv"
    path.write_text(
        "cell_id,capacity_ah,voltage_diff_mv,recorded_at,source\n"
        "c1,100.1,3.0,1000.0,bench-1\n"
        "c2,99.8,4.2,,bench-1\n",
        encoding="utf-8",
    )
    rows = _read_csv(str(path))
    assert rows[0]["recorded_at"] == 1000.0
    assert rows[1]["recorded_at"] is None
    assert rows[1]["voltage_diff_mv"] == 4.2


def test_csv_missing_header_rejected(tmp_path):
    path = tmp_path / "bad.csv"
    path.write_text("cell_id,capacity\nc1,100\n", encoding="utf-8")
    with pytest.raises(ValueError):
        _read_csv(str(path))
