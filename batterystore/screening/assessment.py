"""批次评估。

一次评估把一批的"当前生效记录"算成一份结论：批统计量、逐颗判定、批次决策
与判定依据。评估是纯计算——同样的记录与阈值一定算出同样的结论，因此每一代
评估都可以 append-only 留存，补录修订后重算出的新一代与历史代逐条对照，
"这条结论是按当时哪版数据算的"永远可查。

批次决策三档：

- ``release``：无离群且主体一致性通过，整批放行；
- ``release_partial``：离群已隔离、比例未超限，其余主体一致性通过，其余放行；
- ``hold``：主体一致性超限、离群比例超限或无记录，整批隔离复核，暂不放行。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from ..config import ScreeningLimits
from .criteria import CellVerdict, evaluate_cells
from .records import CalibrationRecord
from .statistics import summarize

RELEASE = "release"
RELEASE_PARTIAL = "release_partial"
HOLD = "hold"


@dataclass(frozen=True)
class BatchAssessment:
    """一代批次评估结论。"""

    batch_id: str
    generation: int
    assessed_at: float
    sample_count: int
    criterion: str
    boundary_note: str
    decision: str
    reasons: tuple[str, ...]
    capacity_stats: dict
    delta_stats: dict
    main_count: int
    main_capacity_spread_ah: float
    main_delta_spread_mv: float
    outlier_ratio: float
    outliers: tuple[str, ...]
    verdicts: tuple[CellVerdict, ...] = field(default_factory=tuple)

    def as_dict(self) -> dict:
        return {
            "batch_id": self.batch_id,
            "generation": self.generation,
            "assessed_at": round(self.assessed_at, 3),
            "sample_count": self.sample_count,
            "criterion": self.criterion,
            "boundary_note": self.boundary_note,
            "decision": self.decision,
            "reasons": list(self.reasons),
            "capacity_stats": dict(self.capacity_stats),
            "delta_stats": dict(self.delta_stats),
            "main_body": {
                "count": self.main_count,
                "capacity_spread_ah": round(self.main_capacity_spread_ah, 4),
                "delta_spread_mv": round(self.main_delta_spread_mv, 2),
            },
            "outliers": list(self.outliers),
            "outlier_ratio": round(self.outlier_ratio, 4),
            "verdicts": [verdict.as_dict() for verdict in self.verdicts],
        }

    def summary(self) -> dict:
        """返回一代评估的摘要，供历史对照。"""

        return {
            "generation": self.generation,
            "assessed_at": round(self.assessed_at, 3),
            "decision": self.decision,
            "sample_count": self.sample_count,
            "outliers": list(self.outliers),
            "outlier_ratio": round(self.outlier_ratio, 4),
        }


def _spread(values: list[float]) -> float:
    if not values:
        return 0.0
    return max(values) - min(values)


def build_assessment(
    batch_id: str,
    generation: int,
    now: float,
    records: list[CalibrationRecord],
    limits: ScreeningLimits,
) -> BatchAssessment:
    """按当前生效记录与阈值算一代批次评估。"""

    verdicts, adaptive_active = evaluate_cells(records, limits)
    count = len(records)
    capacities = [record.capacity_ah for record in records]
    deltas = [abs(record.delta_mv) for record in records]
    outliers = [verdict for verdict in verdicts if verdict.outlier]
    main_body = [verdict for verdict in verdicts if not verdict.outlier]
    main_cap_spread = _spread([verdict.capacity_ah for verdict in main_body])
    main_delta_spread = _spread([abs(verdict.delta_mv) for verdict in main_body])
    outlier_ratio = len(outliers) / count if count else 0.0
    criterion = "fixed+adaptive" if adaptive_active else "fixed"
    if adaptive_active:
        boundary = (
            f"本批 {count} 颗 ≥ 自适应门槛 {limits.adaptive_min_samples}："
            "固定阈值与自适应判据同时生效，任一命中即隔离"
        )
    else:
        boundary = (
            f"本批 {count} 颗 < 自适应门槛 {limits.adaptive_min_samples}："
            "仅按固定工程阈值判定，自适应分数仅供参考，不构成隔离依据"
        )

    reasons: list[str] = []
    if count == 0:
        reasons.append("批内无标定记录，无法评估")
        decision = HOLD
    else:
        if outliers:
            names = "、".join(verdict.cell_id for verdict in outliers)
            reasons.append(f"离群 {len(outliers)}/{count} 颗隔离复核: {names}")
        consistency_ok = True
        if main_body:
            if main_cap_spread > limits.batch_max_capacity_spread_ah:
                consistency_ok = False
                reasons.append(
                    f"主体 {len(main_body)} 颗容量极差 {main_cap_spread:.2f}Ah "
                    f"超过批一致性上限 {limits.batch_max_capacity_spread_ah:.2f}Ah"
                )
            if main_delta_spread > limits.batch_max_delta_spread_mv:
                consistency_ok = False
                reasons.append(
                    f"主体 {len(main_body)} 颗压差极差 {main_delta_spread:.1f}mV "
                    f"超过批一致性上限 {limits.batch_max_delta_spread_mv:.1f}mV"
                )
        ratio_ok = outlier_ratio <= limits.max_outlier_ratio
        if not ratio_ok:
            reasons.append(
                f"离群比例 {outlier_ratio * 100:.1f}%（{len(outliers)}/{count}）"
                f"超过上限 {limits.max_outlier_ratio * 100:.1f}%"
            )
        if not consistency_ok or not ratio_ok:
            decision = HOLD
            reasons.append("整批隔离复核，暂不放行")
        elif outliers:
            decision = RELEASE_PARTIAL
            reasons.append(
                f"其余 {len(main_body)} 颗一致性通过"
                f"（容量极差 {main_cap_spread:.2f}Ah、压差极差 {main_delta_spread:.1f}mV），可放行"
            )
        else:
            decision = RELEASE
            reasons.append(
                f"全批 {count} 颗一致性通过"
                f"（容量极差 {main_cap_spread:.2f}Ah、压差极差 {main_delta_spread:.1f}mV），可放行"
            )

    return BatchAssessment(
        batch_id=batch_id,
        generation=generation,
        assessed_at=float(now),
        sample_count=count,
        criterion=criterion,
        boundary_note=boundary,
        decision=decision,
        reasons=tuple(reasons),
        capacity_stats=summarize(capacities),
        delta_stats=summarize(deltas),
        main_count=len(main_body),
        main_capacity_spread_ah=main_cap_spread,
        main_delta_spread_mv=main_delta_spread,
        outlier_ratio=outlier_ratio,
        outliers=tuple(verdict.cell_id for verdict in outliers),
        verdicts=tuple(verdicts),
    )
