"""单体标定记录台账。

同一颗单体的标定数据可能事后补录或修订：每次录入生成一个新 revision，旧版本
退出生效集但留在流水里。批次评估只按"当前生效"的记录算；流水用于追溯某一代
结论是按哪一版数据算出来的，补录前后可以逐条对照。
"""

from __future__ import annotations

import threading
from dataclasses import dataclass


@dataclass(frozen=True)
class CalibrationRecord:
    """一条生效中的单体标定记录。"""

    batch_id: str
    cell_id: str
    capacity_ah: float
    delta_mv: float
    revision: int
    source: str
    recorded_at: float

    def as_dict(self) -> dict:
        return {
            "batch_id": self.batch_id,
            "cell_id": self.cell_id,
            "capacity_ah": round(self.capacity_ah, 4),
            "delta_mv": round(self.delta_mv, 2),
            "revision": self.revision,
            "source": self.source,
            "recorded_at": round(self.recorded_at, 3),
        }


class CalibrationBook:
    """按批归集的标定记录台账（并发安全）。"""

    def __init__(self) -> None:
        self._effective: dict[str, dict[str, CalibrationRecord]] = {}
        self._revisions: dict[tuple[str, str], int] = {}
        self._history: list[dict] = []
        self._lock = threading.RLock()

    def record(
        self,
        batch_id: str,
        cell_id: str,
        capacity_ah: float,
        delta_mv: float,
        now: float,
        source: str,
    ) -> dict:
        """登记一条标定记录；同单体重复录入视为修订，revision 递增。"""

        with self._lock:
            key = (batch_id, cell_id)
            revision = self._revisions.get(key, 0) + 1
            self._revisions[key] = revision
            record = CalibrationRecord(
                batch_id=batch_id,
                cell_id=cell_id,
                capacity_ah=float(capacity_ah),
                delta_mv=float(delta_mv),
                revision=revision,
                source=source,
                recorded_at=float(now),
            )
            self._effective.setdefault(batch_id, {})[cell_id] = record
            entry = record.as_dict()
            self._history.append(entry)
            return dict(entry)

    def effective(self, batch_id: str) -> list[CalibrationRecord]:
        """返回一批当前生效的标定记录。"""

        with self._lock:
            return list(self._effective.get(batch_id, {}).values())

    def batches(self) -> list[str]:
        """返回已有记录的批次清单。"""

        with self._lock:
            return sorted(self._effective)

    def history(self, limit: int = 50) -> list[dict]:
        """返回最近的录入流水（含被修订覆盖的旧版本）。"""

        with self._lock:
            return [dict(item) for item in self._history[-limit:]]
