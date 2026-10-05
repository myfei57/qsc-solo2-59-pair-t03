"""标定批次一致性判据。

一批单体标定完后按批统计容量与压差离散度，把离群单体挑出隔离复核，其余
单体给出能否放行的结论。离群有两种口径，二者在小批量上可能给出相反结论：

* **固定工程阈值（fixed）**：相对该批标称值的绝对容差。口径与批次规模无关，
  是工程规格线；样本再小也能判，因此是小批量唯一可信的口径。
* **自适应（adaptive）**：用该批自己的分布（中位数与 MAD 的稳健 z 分数）判离群。
  分布紧时能抓出固定阈值放过去的"相对异类"，分布散时不会把全批都判离群；
  但样本太少时 MAD 退化（例如 6 个单体里 4 个同值，MAD 直接为 0），统计口径
  不成立，所以样本不足时自适应结果只作展示、不作结论。

默认选择规则（边界写死、可配置）：

``n < adaptive_min_n`` → 固定阈值为准；``n >= adaptive_min_n`` → 自适应为准，
但固定规格线始终作为**绝对硬门禁**：统计上不离群但超出工程容差的单体，照样
隔离。两种口径对每个单体都同时计算并写进报告，分歧时以哪条为准写在
``authoritative`` 字段里，结论可逐条复核。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from statistics import median

RELEASE = "release"
ISOLATE = "isolate"
REVIEW = "review"

FIXED = "fixed"
ADAPTIVE = "adaptive"

CAPACITY = "capacity_ah"
VOLTAGE_DIFF = "voltage_diff_mv"
METRICS = (CAPACITY, VOLTAGE_DIFF)

# 小批量边界：低于该样本数不启用自适应口径。
DEFAULT_ADAPTIVE_MIN_N = 20
# 稳健 z 分数 = 0.6745*(x-中位数)/MAD，超过该倍数判离群（约 3.5σ）。
DEFAULT_ROBUST_Z_CUTOFF = 3.5
# 容量相对标称值的工程容差（±3%）。
DEFAULT_CAPACITY_TOL_PCT = 3.0
# 压差相对标称值的工程容差（±10mV，压差本身非负，实际只看上界）。
DEFAULT_VOLTAGE_DIFF_TOL_MV = 10.0
# 放行门禁：剔除离群后，容量变异系数与压差极差上限、离群占比上限。
DEFAULT_CV_CAP_PCT = 1.5
DEFAULT_VOLTAGE_SPREAD_CAP_MV = 30.0
DEFAULT_ISOLATE_FRACTION_CAP = 0.10


@dataclass(frozen=True)
class CellReading:
    """一条单体标定记录。"""

    cell_id: str
    capacity_ah: float
    voltage_diff_mv: float

    @classmethod
    def from_dict(cls, payload: dict) -> "CellReading":
        return cls(
            cell_id=str(payload["cell_id"]),
            capacity_ah=float(payload["capacity_ah"]),
            voltage_diff_mv=float(payload["voltage_diff_mv"]),
        )

    def value_of(self, metric: str) -> float:
        if metric == CAPACITY:
            return self.capacity_ah
        if metric == VOLTAGE_DIFF:
            return self.voltage_diff_mv
        raise KeyError(f"未知的标定指标: {metric}")


@dataclass(frozen=True)
class GradingPolicy:
    """批次判定的全部可调参数（默认值即工程默认口径）。"""

    nominal_capacity_ah: float
    nominal_voltage_diff_mv: float = 0.0
    capacity_tol_pct: float = DEFAULT_CAPACITY_TOL_PCT
    voltage_diff_tol_mv: float = DEFAULT_VOLTAGE_DIFF_TOL_MV
    adaptive_min_n: int = DEFAULT_ADAPTIVE_MIN_N
    robust_z_cutoff: float = DEFAULT_ROBUST_Z_CUTOFF
    cv_cap_pct: float = DEFAULT_CV_CAP_PCT
    voltage_spread_cap_mv: float = DEFAULT_VOLTAGE_SPREAD_CAP_MV
    isolate_fraction_cap: float = DEFAULT_ISOLATE_FRACTION_CAP

    def fixed_band(self, metric: str) -> tuple[float, float]:
        """返回某指标固定工程阈值的上下界。"""

        if metric == CAPACITY:
            margin = self.nominal_capacity_ah * self.capacity_tol_pct / 100.0
            return self.nominal_capacity_ah - margin, self.nominal_capacity_ah + margin
        if metric == VOLTAGE_DIFF:
            # 压差非负，下界夹到 0；标称 0 时阈值即 ±tol 的正半轴。
            return 0.0, self.nominal_voltage_diff_mv + self.voltage_diff_tol_mv
        raise KeyError(f"未知的标定指标: {metric}")


@dataclass(frozen=True)
class MetricStats:
    """单指标的批统计量。"""

    metric: str
    n: int
    mean: float
    sd: float
    minimum: float
    maximum: float
    spread: float
    median_value: float
    mad: float
    cv_pct: float | None

    def as_dict(self) -> dict:
        return {
            "metric": self.metric,
            "n": self.n,
            "mean": round(self.mean, 6),
            "sd": round(self.sd, 6),
            "min": round(self.minimum, 6),
            "max": round(self.maximum, 6),
            "spread": round(self.spread, 6),
            "median": round(self.median_value, 6),
            "mad": round(self.mad, 6),
            "cv_pct": None if self.cv_pct is None else round(self.cv_pct, 4),
        }


@dataclass(frozen=True)
class MetricJudgement:
    """单体在一个指标上的两种口径判定。"""

    metric: str
    value: float
    fixed_outlier: bool
    fixed_band: tuple[float, float]
    adaptive_outlier: bool
    adaptive_applicable: bool
    robust_z: float | None

    def as_dict(self) -> dict:
        low, high = self.fixed_band
        return {
            "metric": self.metric,
            "value": round(self.value, 6),
            "fixed_outlier": self.fixed_outlier,
            "fixed_band": [round(low, 6), round(high, 6)],
            "adaptive_outlier": self.adaptive_outlier,
            "adaptive_applicable": self.adaptive_applicable,
            "robust_z": None if self.robust_z is None else round(self.robust_z, 3),
        }


@dataclass(frozen=True)
class CellVerdict:
    """单体结论：任一指标命中即隔离，reasons 记录命中的指标与口径。"""

    cell_id: str
    disposition: str
    reasons: list[str] = field(default_factory=list)
    metrics: dict[str, MetricJudgement] = field(default_factory=dict)

    def as_dict(self) -> dict:
        return {
            "cell_id": self.cell_id,
            "disposition": self.disposition,
            "reasons": list(self.reasons),
            "metrics": {key: item.as_dict() for key, item in self.metrics.items()},
        }


@dataclass(frozen=True)
class GateCheck:
    """一条放行门禁的核对结果。"""

    name: str
    passed: bool
    actual: float
    limit: float
    detail: str

    def as_dict(self) -> dict:
        return {
            "name": self.name,
            "passed": self.passed,
            "actual": round(self.actual, 6),
            "limit": round(self.limit, 6),
            "detail": self.detail,
        }


def metric_stats(values: list[float], metric: str) -> MetricStats:
    """计算一个指标的批统计量（含稳健口径需要的中位数/MAD）。"""

    if not values:
        raise ValueError("没有单体可以计算批统计")
    n = len(values)
    mean_value = sum(values) / n
    variance = sum((item - mean_value) ** 2 for item in values) / n
    sd = variance ** 0.5
    med = median(values)
    mad = median([abs(item - med) for item in values])
    spread = max(values) - min(values)
    cv_pct = (sd / mean_value * 100.0) if mean_value else None
    return MetricStats(
        metric=metric,
        n=n,
        mean=mean_value,
        sd=sd,
        minimum=min(values),
        maximum=max(values),
        spread=spread,
        median_value=med,
        mad=mad,
        cv_pct=cv_pct,
    )


def _judge_metric(
    value: float,
    metric: str,
    stats: MetricStats,
    policy: GradingPolicy,
    adaptive_ok: bool,
) -> MetricJudgement:
    low, high = policy.fixed_band(metric)
    fixed_outlier = not (low <= value <= high)
    adaptive_outlier = False
    robust_z: float | None = None
    if adaptive_ok and stats.mad > 0.0:
        robust_z = 0.6745 * (value - stats.median_value) / stats.mad
        adaptive_outlier = abs(robust_z) > policy.robust_z_cutoff
    return MetricJudgement(
        metric=metric,
        value=value,
        fixed_outlier=fixed_outlier,
        fixed_band=(low, high),
        adaptive_outlier=adaptive_outlier,
        adaptive_applicable=adaptive_ok and stats.mad > 0.0,
        robust_z=robust_z,
    )


def _is_outlier(judgement: MetricJudgement, use_adaptive: bool) -> tuple[bool, str]:
    """返回该指标是否命中隔离及命中口径说明。"""

    if judgement.fixed_outlier:
        return True, f"{judgement.metric}:fixed"
    if use_adaptive and judgement.adaptive_applicable and judgement.adaptive_outlier:
        return True, f"{judgement.metric}:adaptive"
    return False, ""


def grade_batch(
    readings: list[CellReading],
    policy: GradingPolicy,
    *,
    pre_quarantined: frozenset[str] = frozenset(),
    released_override: frozenset[str] = frozenset(),
) -> dict:
    """按批统计并给出每个单体与整批结论。

    ``pre_quarantined`` 是此前已经隔离、重算时不得自动放回产线的单体集合；
    它们即使在新数据下统计合格，处置也保持隔离并打上 ``sticky`` 标记。
    ``released_override`` 是已经人工复核放行的单体：统计上仍离群也不再自动
    重新隔离，处置记为放行并打上 ``manual_release`` 标记（统计事实不抹掉）。
    """

    if not readings:
        raise ValueError("空批次无法判定一致性")
    n = len(readings)
    use_adaptive = n >= policy.adaptive_min_n
    authoritative = ADAPTIVE if use_adaptive else FIXED

    stats_by_metric = {
        metric: metric_stats([item.value_of(metric) for item in readings], metric) for metric in METRICS
    }

    cells: list[CellVerdict] = []
    for reading in readings:
        judgements = {
            metric: _judge_metric(
                reading.value_of(metric), metric, stats_by_metric[metric], policy, use_adaptive
            )
            for metric in METRICS
        }
        statistical_hits = [
            reason for metric in METRICS for hit, reason in (
                _is_outlier(judgements[metric], use_adaptive),
            ) if hit
        ]
        reasons = list(statistical_hits)
        sticky = reading.cell_id in pre_quarantined
        manually_released = reading.cell_id in released_override
        if sticky:
            # 隔离区单体：维持隔离，数据修订不自动回流；不参与本批放行统计。
            reasons.append("sticky:previously_quarantined")
            disposition = ISOLATE
        elif statistical_hits and not manually_released:
            disposition = ISOLATE
        else:
            disposition = RELEASE
        if manually_released and not sticky:
            # 统计事实仍展示在 metrics 里，但处置以人工放行为准、不再隔离。
            reasons.append("manual_release:review_passed")
        cells.append(CellVerdict(reading.cell_id, disposition, reasons, judgements))

    sticky_ids = {cell.cell_id for cell in cells if cell.cell_id in pre_quarantined}
    isolated = [
        cell for cell in cells
        if cell.disposition == ISOLATE and cell.cell_id not in sticky_ids
    ]
    held = [cell for cell in cells if cell.cell_id in sticky_ids]
    kept = [cell for cell in cells if cell.disposition == RELEASE]
    # 门禁的离群占比只统计本批新判出的离群单体；老粘性单体已在隔离区，
    # 既不算进这批的"散"，也不能把合格的同批单体连坐成复核。
    new_outlier_fraction = len(isolated) / n
    kept_ids = {cell.cell_id for cell in kept}
    kept_stats = (
        {
            metric: metric_stats(
                [reading.value_of(metric) for reading in readings if reading.cell_id in kept_ids],
                metric,
            )
            for metric in METRICS
        }
        if kept
        else None
    )

    gates = _release_gates(kept_stats, new_outlier_fraction, policy)
    verdict = RELEASE if all(gate.passed for gate in gates) else REVIEW

    return {
        "n": n,
        "authoritative": authoritative,
        "policy": _policy_basis(policy, use_adaptive),
        "stats": {metric: stats_by_metric[metric].as_dict() for metric in METRICS},
        "kept_stats": None if kept_stats is None else {
            metric: kept_stats[metric].as_dict() for metric in METRICS
        },
        "cells": [cell.as_dict() for cell in sorted(cells, key=lambda item: item.cell_id)],
        "isolated_ids": sorted(cell.cell_id for cell in isolated),
        "held_ids": sorted(cell.cell_id for cell in held),
        "released_ids": sorted(cell.cell_id for cell in kept),
        "new_outlier_fraction": round(new_outlier_fraction, 6),
        "gates": [gate.as_dict() for gate in gates],
        "sticky_holds": sorted(sticky_ids),
        "quarantine_worklist": sorted(sticky_ids),
        "manual_released": sorted(
            cell.cell_id for cell in cells if cell.cell_id in released_override
        ),
        "verdict": verdict,
    }


def _release_gates(
    kept_stats: dict[str, MetricStats] | None,
    new_outlier_fraction: float,
    policy: GradingPolicy,
) -> list[GateCheck]:
    """放行门禁：离群占比 + 剩余单体容量 CV + 剩余单体压差极差。"""

    gates = [
        GateCheck(
            name="new_outlier_fraction",
            passed=new_outlier_fraction <= policy.isolate_fraction_cap,
            actual=new_outlier_fraction,
            limit=policy.isolate_fraction_cap,
            detail="本批新判离群占比超上限，工艺不稳定，整批转人工复核",
        ),
    ]
    if kept_stats is None:
        gates.append(GateCheck(
            name="kept_nonempty",
            passed=False,
            actual=0.0,
            limit=1.0,
            detail="剔除离群后没有可放行单体",
        ))
        return gates
    capacity = kept_stats[CAPACITY]
    voltage = kept_stats[VOLTAGE_DIFF]
    cv_pct = capacity.cv_pct if capacity.cv_pct is not None else 0.0
    gates.append(GateCheck(
        name="capacity_cv",
        passed=cv_pct <= policy.cv_cap_pct,
        actual=cv_pct,
        limit=policy.cv_cap_pct,
        detail="剩余单体容量变异系数超限，配组后木桶效应明显",
    ))
    gates.append(GateCheck(
        name="voltage_diff_spread",
        passed=voltage.spread <= policy.voltage_spread_cap_mv,
        actual=voltage.spread,
        limit=policy.voltage_spread_cap_mv,
        detail="剩余单体压差极差超限，无法配成一致簇",
    ))
    return gates


def _policy_basis(policy: GradingPolicy, use_adaptive: bool) -> dict:
    """把两种口径与选择边界写进报告，结论可逐条追溯。"""

    cap_low, cap_high = policy.fixed_band(CAPACITY)
    _, vd_high = policy.fixed_band(VOLTAGE_DIFF)
    return {
        "adaptive_min_n": policy.adaptive_min_n,
        "robust_z_cutoff": policy.robust_z_cutoff,
        "fixed_capacity_band_ah": [round(cap_low, 6), round(cap_high, 6)],
        "fixed_voltage_diff_max_mv": round(vd_high, 6),
        "cv_cap_pct": policy.cv_cap_pct,
        "voltage_spread_cap_mv": policy.voltage_spread_cap_mv,
        "isolate_fraction_cap": policy.isolate_fraction_cap,
        "adaptive_active": use_adaptive,
        "boundary_note": (
            f"n>={policy.adaptive_min_n} 以自适应(稳健z)为准，固定阈值始终是硬门禁"
            if use_adaptive
            else f"n<{policy.adaptive_min_n} 自适应样本不足，只以固定工程阈值为准"
        ),
    }
