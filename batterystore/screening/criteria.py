"""离群判定口径。

两种口径与一条边界：

- **固定工程阈值**：容量落在规格区间、压差绝对值不超过上限。这是规格与安全
  底线，任何批量都生效，越线必隔离。
- **自适应**：按本批分布算稳健 z 分数（中位数 ± k·MAD），把"规格内但明显
  偏离本批主体"的单体挑出来。样本量不足时中位数与 MAD 都不可信——三四颗里
  有一颗偏离，可能是离群，也可能只是正常波动被小样本放大。

边界规则（两种口径结论相反时以谁为准）：

- 批量 < ``adaptive_min_samples``：只按固定阈值判，自适应分数照算但仅作参考
  （advisory），不构成隔离依据——小批量冲突，固定阈值赢；
- 批量 ≥ ``adaptive_min_samples``：两种口径同时生效，任一命中即隔离。隔离是
  保守方向，并集不会放走固定阈值认定的离群单体。

边界写进每份评估报告的 ``boundary_note``，结论拿出去复核时不必翻代码。
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

from ..config import ScreeningLimits
from .records import CalibrationRecord
from .statistics import robust_zscores


@dataclass(frozen=True)
class CellVerdict:
    """一颗单体的离群判定。"""

    cell_id: str
    revision: int
    capacity_ah: float
    delta_mv: float
    fixed_hits: tuple[str, ...] = ()
    adaptive_hits: tuple[str, ...] = ()
    advisory_hits: tuple[str, ...] = ()
    capacity_z: float = 0.0
    delta_z: float = 0.0
    outlier: bool = False
    reasons: tuple[str, ...] = field(default_factory=tuple)

    def as_dict(self) -> dict:
        return {
            "cell_id": self.cell_id,
            "revision": self.revision,
            "capacity_ah": round(self.capacity_ah, 4),
            "delta_mv": round(self.delta_mv, 2),
            "fixed_hits": list(self.fixed_hits),
            "adaptive_hits": list(self.adaptive_hits),
            "advisory_hits": list(self.advisory_hits),
            "capacity_z": _z_display(self.capacity_z),
            "delta_z": _z_display(self.delta_z),
            "outlier": self.outlier,
            "reasons": list(self.reasons),
        }


def _z_display(score: float) -> float | str:
    """z 分数展示：无穷大与 NaN 落成字符串，避免非法 JSON。"""

    if not math.isfinite(score):
        return "inf" if score > 0 else ("-inf" if score < 0 else "nan")
    return round(score, 2)


def evaluate_cells(
    records: list[CalibrationRecord], limits: ScreeningLimits
) -> tuple[list[CellVerdict], bool]:
    """逐颗判定离群，返回判定清单与自适应判据是否生效。"""

    capacities = [record.capacity_ah for record in records]
    deltas = [abs(record.delta_mv) for record in records]
    capacity_z = robust_zscores(capacities)
    delta_z = robust_zscores(deltas)
    adaptive_active = len(records) >= limits.adaptive_min_samples
    verdicts: list[CellVerdict] = []
    for record, cap_z, del_z in zip(records, capacity_z, delta_z):
        fixed: list[str] = []
        reasons: list[str] = []
        if record.capacity_ah < limits.capacity_min_ah:
            fixed.append("capacity_below_min")
            reasons.append(f"容量 {record.capacity_ah:.2f}Ah 低于固定下限 {limits.capacity_min_ah:.2f}Ah")
        if record.capacity_ah > limits.capacity_max_ah:
            fixed.append("capacity_above_max")
            reasons.append(f"容量 {record.capacity_ah:.2f}Ah 高于固定上限 {limits.capacity_max_ah:.2f}Ah")
        if abs(record.delta_mv) > limits.delta_max_mv:
            fixed.append("delta_above_max")
            reasons.append(f"压差绝对值 {abs(record.delta_mv):.1f}mV 超过固定上限 {limits.delta_max_mv:.1f}mV")
        adaptive: list[str] = []
        if abs(cap_z) > limits.adaptive_z:
            adaptive.append("capacity_robust_z")
        if abs(del_z) > limits.adaptive_z:
            adaptive.append("delta_robust_z")
        if adaptive and adaptive_active:
            for hit in adaptive:
                metric = "容量" if hit.startswith("capacity") else "压差"
                score = cap_z if hit.startswith("capacity") else del_z
                reasons.append(f"{metric}稳健z={score:.2f} 超过自适应阈值 {limits.adaptive_z:.2f}（明显偏离本批分布）")
        verdicts.append(
            CellVerdict(
                cell_id=record.cell_id,
                revision=record.revision,
                capacity_ah=record.capacity_ah,
                delta_mv=record.delta_mv,
                fixed_hits=tuple(fixed),
                adaptive_hits=tuple(adaptive) if adaptive_active else (),
                advisory_hits=tuple(adaptive) if not adaptive_active else (),
                capacity_z=cap_z,
                delta_z=del_z,
                outlier=bool(fixed) or (adaptive_active and bool(adaptive)),
                reasons=tuple(reasons),
            )
        )
    return verdicts, adaptive_active
