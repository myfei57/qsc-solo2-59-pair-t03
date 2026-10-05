"""批次一致性筛查：离群口径边界、代际重算与 sticky 处置。"""

from __future__ import annotations

import pytest

from batterystore.clock import ManualClock
from batterystore.config import ScreeningLimits, default_config
from batterystore.errors import ConfigError, InterlockError
from batterystore.screening import ScreeningRegistry

BATCH = "B-001"


def make_registry() -> ScreeningRegistry:
    return ScreeningRegistry(default_config().screening, ManualClock())


def record_batch(registry: ScreeningRegistry, capacities: list[float], delta_mv: float = 10.0, batch: str = BATCH) -> None:
    for index, capacity in enumerate(capacities, start=1):
        registry.record(batch, f"{batch}-c{index}", capacity, delta_mv, source="test")


def cell_state(registry: ScreeningRegistry, batch: str, cell_id: str) -> str:
    report = registry.batch_report(batch)
    states = {cell["cell_id"]: cell["state"] for cell in report["cells"]}
    return states[cell_id]


def test_fixed_threshold_isolates_outlier_and_releases_rest():
    registry = make_registry()
    record_batch(registry, [280.0] * 11 + [260.0])
    report = registry.assess(BATCH)
    assert report["decision"] == "release_partial"
    assert report["criterion"] == "fixed+adaptive"
    assert report["outliers"] == [f"{BATCH}-c12"]
    verdict = {item["cell_id"]: item for item in report["verdicts"]}[f"{BATCH}-c12"]
    assert verdict["fixed_hits"] == ["capacity_below_min"]
    assert any("固定下限" in reason for reason in verdict["reasons"])
    assert report["batch_disposition"] == "open"
    assert cell_state(registry, BATCH, f"{BATCH}-c12") == "isolated"
    assert cell_state(registry, BATCH, f"{BATCH}-c1") == "pending"


def test_adaptive_catches_in_spec_drifter():
    registry = make_registry()
    capacities = [279.8, 280.0, 280.2, 279.9, 280.1, 280.0, 279.8, 280.2, 280.1, 279.9, 280.0, 290.0]
    record_batch(registry, capacities)
    report = registry.assess(BATCH)
    assert report["decision"] == "release_partial"
    verdict = {item["cell_id"]: item for item in report["verdicts"]}[f"{BATCH}-c12"]
    assert verdict["fixed_hits"] == []
    assert verdict["adaptive_hits"] == ["capacity_robust_z"]
    assert any("偏离本批分布" in reason for reason in verdict["reasons"])


def test_small_batch_boundary_fixed_criterion_wins():
    # 4 颗里 1 颗规格内但明显偏离本批：自适应会抓、固定阈值会放——
    # 样本量不足时以固定阈值为准，自适应只进 advisory。
    registry = make_registry()
    record_batch(registry, [280.0, 280.2, 279.8, 283.0])
    report = registry.assess(BATCH)
    assert report["sample_count"] == 4
    assert report["criterion"] == "fixed"
    assert "仅供参考" in report["boundary_note"]
    assert report["decision"] == "release"
    verdict = {item["cell_id"]: item for item in report["verdicts"]}[f"{BATCH}-c4"]
    assert verdict["outlier"] is False
    assert verdict["advisory_hits"] == ["capacity_robust_z"]
    assert cell_state(registry, BATCH, f"{BATCH}-c4") == "pending"


def test_small_batch_fixed_violation_still_isolated():
    registry = make_registry()
    record_batch(registry, [280.0, 280.0, 280.0, 260.0])
    report = registry.assess(BATCH)
    assert report["criterion"] == "fixed"
    assert report["decision"] == "release_partial"
    assert report["outliers"] == [f"{BATCH}-c4"]
    assert cell_state(registry, BATCH, f"{BATCH}-c4") == "isolated"


def test_scattered_batch_holds_without_outliers():
    # 没有单颗离群，但整批离散超过一致性上限：整批隔离，不放行。
    registry = make_registry()
    capacities = [272.0, 273.5, 274.9, 276.4, 277.8, 279.3, 280.7, 282.2, 283.6, 285.1, 286.5, 288.0]
    record_batch(registry, capacities)
    report = registry.assess(BATCH)
    assert report["outliers"] == []
    assert report["decision"] == "hold"
    assert any("容量极差" in reason for reason in report["reasons"])
    assert report["batch_disposition"] == "held"


def test_outlier_ratio_beyond_limit_holds_batch():
    registry = make_registry()
    record_batch(registry, [280.0] * 7 + [260.0] * 3)
    report = registry.assess(BATCH)
    assert len(report["outliers"]) == 3
    assert report["decision"] == "hold"
    assert any("离群比例" in reason for reason in report["reasons"])
    assert report["batch_disposition"] == "held"


def test_backfill_and_revision_reassessed_as_of_then():
    registry = make_registry()
    record_batch(registry, [280.0] * 8)
    first = registry.assess(BATCH)
    assert first["generation"] == 1
    assert first["decision"] == "release"
    assert first["sample_count"] == 8

    # 补录一颗不足容的：新一代必须按含补录的数据重算。
    registry.record(BATCH, f"{BATCH}-c9", 260.0, 10.0, source="backfill")
    second = registry.assess(BATCH)
    assert second["generation"] == 2
    assert second["sample_count"] == 9
    assert second["decision"] == "release_partial"
    assert second["outliers"] == [f"{BATCH}-c9"]

    # 修订补录值：再重算一代，结论按修订后的数据。
    registry.record(BATCH, f"{BATCH}-c9", 280.0, 10.0, source="revision")
    third = registry.assess(BATCH)
    assert third["generation"] == 3
    assert third["decision"] == "release"

    history = registry.batch_report(BATCH)["history"]
    assert [item["generation"] for item in history] == [1, 2, 3]
    assert [item["decision"] for item in history] == ["release", "release_partial", "release"]
    assert history[1]["outliers"] == [f"{BATCH}-c9"]


def test_reassessment_never_releases_isolated_cell():
    registry = make_registry()
    record_batch(registry, [260.0] + [280.0] * 7)
    registry.assess(BATCH)
    assert cell_state(registry, BATCH, f"{BATCH}-c1") == "isolated"

    # 数据修订变好，重算结论转为放行，但已隔离的单体不会被自动放回。
    registry.record(BATCH, f"{BATCH}-c1", 280.0, 10.0, source="revision")
    report = registry.assess(BATCH)
    assert report["decision"] == "release"
    assert cell_state(registry, BATCH, f"{BATCH}-c1") == "isolated"


def test_reassessment_never_releases_held_batch():
    registry = make_registry()
    capacities = [272.0, 273.5, 274.9, 276.4, 277.8, 279.3, 280.7, 282.2, 283.6, 285.1, 286.5, 288.0]
    record_batch(registry, capacities)
    assert registry.assess(BATCH)["decision"] == "hold"
    assert registry.batch_report(BATCH)["disposition"] == "held"

    # 整批数据修订到一致，重算结论放行，但批次仍处隔离，需人工复核。
    for index in range(1, 13):
        registry.record(BATCH, f"{BATCH}-c{index}", 280.0, 10.0, source="revision")
    report = registry.assess(BATCH)
    assert report["decision"] == "release"
    assert report["batch_disposition"] == "held"
    assert any("需人工复核" in reason for reason in report["reasons"])

    reviewed = registry.review(BATCH, "release", reviewer="qc-li", note="复测一致")
    assert reviewed["state"] == "released"
    assert registry.batch_report(BATCH)["disposition"] == "released"


def test_review_release_then_reassessment_re_isolates():
    registry = make_registry()
    record_batch(registry, [260.0] + [280.0] * 7)
    registry.assess(BATCH)
    reviewed = registry.review(BATCH, "release", cell_id=f"{BATCH}-c1", reviewer="qc-li")
    assert reviewed["state"] == "released"

    # 人工放行后数据仍离群，重算把它打回隔离（只朝保守方向走）。
    report = registry.assess(BATCH)
    assert report["re_isolated"] == [f"{BATCH}-c1"]
    assert cell_state(registry, BATCH, f"{BATCH}-c1") == "isolated"


def test_review_requires_isolated_state():
    registry = make_registry()
    record_batch(registry, [280.0] * 8)
    registry.assess(BATCH)
    with pytest.raises(InterlockError):
        registry.review(BATCH, "release", cell_id=f"{BATCH}-c1")
    with pytest.raises(InterlockError):
        registry.review(BATCH, "release")
    with pytest.raises(InterlockError):
        registry.review(BATCH, "maybe", cell_id=f"{BATCH}-c1")


def test_empty_batch_holds():
    registry = make_registry()
    report = registry.assess("B-EMPTY")
    assert report["decision"] == "hold"
    assert report["sample_count"] == 0
    assert any("无标定记录" in reason for reason in report["reasons"])
    assert report["batch_disposition"] == "held"


def test_config_rejects_inconsistent_screening_limits():
    raw = {
        "capacity_min_ah": 294.0,
        "capacity_max_ah": 266.0,
        "delta_max_mv": 60.0,
        "adaptive_min_samples": 8,
        "adaptive_z": 3.5,
        "batch_max_capacity_spread_ah": 8.0,
        "batch_max_delta_spread_mv": 40.0,
        "max_outlier_ratio": 0.25,
    }
    with pytest.raises(ConfigError):
        ScreeningLimits.from_mapping(raw)
    raw["capacity_min_ah"], raw["capacity_max_ah"] = 266.0, 294.0
    raw["max_outlier_ratio"] = 1.5
    with pytest.raises(ConfigError):
        ScreeningLimits.from_mapping(raw)


def test_platform_screening_flow(platform):
    for index in range(1, 9):
        cell_id = f"LOT1-c{index}"
        capacity = 260.0 if index == 1 else 280.0
        entry = platform.record_cell_calibration("LOT1", cell_id, capacity, 10.0, source="test")
        assert entry["revision"] == 1

    report = platform.assess_batch("LOT1")
    assert report["decision"] == "release_partial"
    assert report["outliers"] == ["LOT1-c1"]

    snapshot = platform.screening_snapshot()
    assert snapshot["isolated_cells"] == ["LOT1/LOT1-c1"]
    assert snapshot["batches"][0]["batch_id"] == "LOT1"

    detail = platform.batch_screening("LOT1")
    assert detail["generations"] == 1
    assert detail["latest"]["criterion"] == "fixed+adaptive"

    with pytest.raises(InterlockError):
        platform.review_screening("LOT1", "release", cell_id="LOT1-c2")
    released = platform.review_screening("LOT1", "release", cell_id="LOT1-c1", reviewer="qc-li")
    assert released["state"] == "released"

    assert "screening" in platform.status()
