"""筛查台账快照。"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class ScreeningSnapshot:
    """一份筛查台账快照：各批摘要、隔离清单与最近录入流水。"""

    batches: tuple[dict, ...] = field(default_factory=tuple)
    isolated_cells: tuple[str, ...] = field(default_factory=tuple)
    recent_records: tuple[dict, ...] = field(default_factory=tuple)

    def as_dict(self) -> dict:
        return {
            "batches": [dict(item) for item in self.batches],
            "isolated_cells": list(self.isolated_cells),
            "isolated_count": len(self.isolated_cells),
            "recent_records": [dict(item) for item in self.recent_records],
        }
