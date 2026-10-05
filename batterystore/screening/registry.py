"""批次筛查台账：记录、评估代际与 sticky 处置。

评估结论与处置状态分开存放：评估随数据重算一代代更新，处置状态只朝保守方向
流动——

- 新评估判离群的单体进入（或回到）``isolated``；判 ``hold`` 的批次进入
  （或回到）``held``；
- 人工复核放行（``released``）的单体/批次在产线上，重算发现问题会打回隔离；
- 人工判退（``rejected``）的单体/批次已离场，是终态，重算不再改动；
- **重算永远不解除隔离**：无论隔离是评估判的还是人工维持的，只有人工复核
  动作能把隔离中的单体/批次放行或判退。补录修订把数据改得再好看，也不会把
  已经隔离的批次自动放回产线。
"""

from __future__ import annotations

import threading

from ..config import ScreeningLimits
from ..errors import InterlockError
from .assessment import HOLD, BatchAssessment, build_assessment
from .records import CalibrationBook
from .snapshot import ScreeningSnapshot

CELL_PENDING = "pending"
CELL_ISOLATED = "isolated"
CELL_RELEASED = "released"
CELL_REJECTED = "rejected"

BATCH_OPEN = "open"
BATCH_HELD = "held"
BATCH_RELEASED = "released"
BATCH_REJECTED = "rejected"

REVIEW_ACTIONS = ("release", "reject")


class ScreeningRegistry:
    """批次一致性筛查台账（并发安全）。"""

    def __init__(self, limits: ScreeningLimits, clock) -> None:
        self._limits = limits
        self._clock = clock
        self._book = CalibrationBook()
        self._assessments: dict[str, list[BatchAssessment]] = {}
        self._cells: dict[tuple[str, str], dict] = {}
        self._batches: dict[str, str] = {}
        self._lock = threading.RLock()

    @property
    def limits(self) -> ScreeningLimits:
        return self._limits

    def record(
        self,
        batch_id: str,
        cell_id: str,
        capacity_ah: float,
        delta_mv: float,
        source: str = "manual",
    ) -> dict:
        """登记一颗单体的标定记录；补录与修订共用此入口。"""

        entry = self._book.record(batch_id, cell_id, capacity_ah, delta_mv, self._clock.now(), source)
        with self._lock:
            self._cells.setdefault(
                (batch_id, cell_id),
                {"state": CELL_PENDING, "updated_at": float(self._clock.now()), "note": ""},
            )
            self._batches.setdefault(batch_id, BATCH_OPEN)
        return entry

    def assess(self, batch_id: str) -> dict:
        """按当前生效数据重算一批，生成新一代评估并应用 sticky 处置。"""

        with self._lock:
            records = self._book.effective(batch_id)
            generation = len(self._assessments.get(batch_id, [])) + 1
            assessment = build_assessment(batch_id, generation, self._clock.now(), records, self._limits)
            self._assessments.setdefault(batch_id, []).append(assessment)
            self._batches.setdefault(batch_id, BATCH_OPEN)
            re_isolated = self._apply(assessment)
            report = assessment.as_dict()
            report["re_isolated"] = re_isolated
            report["batch_disposition"] = self._batches[batch_id]
            if self._batches[batch_id] == BATCH_HELD and assessment.decision != HOLD:
                report["reasons"].append("批次仍处于隔离状态，重算不自动解除，需人工复核放行")
            return report

    def _apply(self, assessment: BatchAssessment) -> list[str]:
        """把评估结论落到处置状态：只朝保守方向走，返回被打回隔离的单体。"""

        re_isolated: list[str] = []
        for verdict in assessment.verdicts:
            if not verdict.outlier:
                continue
            key = (assessment.batch_id, verdict.cell_id)
            cell = self._cells.setdefault(
                key,
                {"state": CELL_PENDING, "updated_at": assessment.assessed_at, "note": ""},
            )
            if cell["state"] in (CELL_ISOLATED, CELL_REJECTED):
                continue
            if cell["state"] == CELL_RELEASED:
                re_isolated.append(verdict.cell_id)
            cell["state"] = CELL_ISOLATED
            cell["updated_at"] = assessment.assessed_at
            cell["note"] = f"第 {assessment.generation} 代评估判离群"
        if assessment.decision == HOLD and self._batches[assessment.batch_id] != BATCH_REJECTED:
            self._batches[assessment.batch_id] = BATCH_HELD
        return re_isolated

    def review(
        self,
        batch_id: str,
        action: str,
        cell_id: str | None = None,
        reviewer: str = "operator",
        note: str = "",
    ) -> dict:
        """人工复核：解除隔离放行（release）或判退（reject），只接受隔离中的对象。"""

        if action not in REVIEW_ACTIONS:
            raise InterlockError(f"未知的复核动作: {action}", component="screening")
        with self._lock:
            now = float(self._clock.now())
            target_state = CELL_RELEASED if action == "release" else CELL_REJECTED
            if cell_id is None:
                state = self._batches.get(batch_id, BATCH_OPEN)
                if state != BATCH_HELD:
                    raise InterlockError(
                        f"批次 {batch_id} 未处于隔离状态（当前 {state}），无需复核",
                        component="screening",
                    )
                self._batches[batch_id] = BATCH_RELEASED if action == "release" else BATCH_REJECTED
                return {
                    "batch_id": batch_id,
                    "cell_id": None,
                    "action": action,
                    "state": self._batches[batch_id],
                    "reviewer": reviewer,
                    "note": note,
                    "reviewed_at": round(now, 3),
                }
            key = (batch_id, cell_id)
            cell = self._cells.get(key)
            if cell is None or cell["state"] != CELL_ISOLATED:
                current = cell["state"] if cell else "unknown"
                raise InterlockError(
                    f"单体 {cell_id} 未处于隔离状态（当前 {current}），无法复核",
                    component="screening",
                )
            cell["state"] = target_state
            cell["updated_at"] = now
            cell["note"] = note or f"{reviewer} 复核{'放行' if action == 'release' else '判退'}"
            return {
                "batch_id": batch_id,
                "cell_id": cell_id,
                "action": action,
                "state": cell["state"],
                "reviewer": reviewer,
                "note": cell["note"],
                "reviewed_at": round(now, 3),
            }

    def batch_report(self, batch_id: str) -> dict:
        """返回一批的台账：最新结论、历代评估摘要与逐颗处置。"""

        with self._lock:
            assessments = self._assessments.get(batch_id, [])
            records = {record.cell_id: record for record in self._book.effective(batch_id)}
            cells: list[dict] = []
            for (batch, cell_id), info in sorted(self._cells.items()):
                if batch != batch_id:
                    continue
                record = records.get(cell_id)
                cells.append(
                    {
                        "cell_id": cell_id,
                        "state": info["state"],
                        "note": info["note"],
                        "updated_at": round(info["updated_at"], 3),
                        "revision": record.revision if record else None,
                        "capacity_ah": round(record.capacity_ah, 4) if record else None,
                        "delta_mv": round(record.delta_mv, 2) if record else None,
                    }
                )
            return {
                "batch_id": batch_id,
                "disposition": self._batches.get(batch_id, BATCH_OPEN),
                "generations": len(assessments),
                "latest": assessments[-1].as_dict() if assessments else None,
                "history": [assessment.summary() for assessment in assessments],
                "cells": cells,
                "records": [record.as_dict() for record in records.values()],
            }

    def snapshot(self) -> ScreeningSnapshot:
        """返回筛查台账快照。"""

        with self._lock:
            batches: list[dict] = []
            for batch_id in self._book.batches():
                assessments = self._assessments.get(batch_id, [])
                isolated = sum(
                    1
                    for (batch, _cell), info in self._cells.items()
                    if batch == batch_id and info["state"] == CELL_ISOLATED
                )
                batches.append(
                    {
                        "batch_id": batch_id,
                        "disposition": self._batches.get(batch_id, BATCH_OPEN),
                        "sample_count": len(self._book.effective(batch_id)),
                        "generations": len(assessments),
                        "decision": assessments[-1].decision if assessments else "unevaluated",
                        "isolated": isolated,
                    }
                )
            isolated_cells = tuple(
                f"{batch}/{cell_id}"
                for (batch, cell_id), info in sorted(self._cells.items())
                if info["state"] == CELL_ISOLATED
            )
            return ScreeningSnapshot(
                batches=tuple(batches),
                isolated_cells=isolated_cells,
                recent_records=tuple(self._book.history(20)),
            )
