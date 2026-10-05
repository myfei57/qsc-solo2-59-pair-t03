"""标定批次台账：只追加的事件溯源账本。

两类事实分开记流水，谁都不能改写历史：

* **读数事件**（``grading_readings`` 流水）：upsert / void。标定数据事后补录或
  修订时再追加一条同 ``(batch_id, cell_id)`` 的 upsert（``revision`` 递增），
  作废追加 void。某一时刻的"当时数据"由回放得到，因此批次结论可以按任意
  历史时刻重算。
* **处置事件**（``grading_dispositions`` 流水）：quarantine / manual_release /
  manual_hold。隔离与人工结论是质量动作，不由数据修订自动撤销。

批次判定口径（:class:`~batterystore.grading.policy.GradingPolicy`）在首次登记
批次时随批冻结，避免以后改了默认阈值导致历史批次"被换口径"。每代结论快照
追加进 ``grading_reports`` 流水，只用于追溯展示。
"""

from __future__ import annotations

import threading
from typing import Any

from ..clock import Clock, SystemClock
from ..errors import StoreError, StorageError
from ..store.jsonfile import JsonStore
from ..store.journal import JsonlJournal
from .policy import CellReading, GradingPolicy

READINGS_TOPIC = "grading_readings"
DISPOSITIONS_TOPIC = "grading_dispositions"
REPORTS_TOPIC = "grading_reports"

UPSERT = "upsert"
VOID = "void"
QUARANTINE = "quarantine"
MANUAL_HOLD = "manual_hold"
MANUAL_RELEASE = "manual_release"

ACTIVE = "active"
QUARANTINED = "quarantined"
HELD = "held"
RELEASED = "released"


class GradingError(StoreError):
    """标定台账动作不满足前置条件。"""


class GradingBook:
    """按流水回放重建的标定批次台账。"""

    def __init__(self, store: JsonStore, clock: Clock | None = None) -> None:
        self._store = store
        self._clock = clock or SystemClock()
        self._readings_journal = JsonlJournal(store.root, READINGS_TOPIC, segment_limit=1024)
        self._dispositions_journal = JsonlJournal(store.root, DISPOSITIONS_TOPIC, segment_limit=1024)
        self._reports_journal = JsonlJournal(store.root, REPORTS_TOPIC, segment_limit=512)
        self._seq = 0
        self._lock = threading.RLock()
        # batch_id -> {cell_id -> {reading, revision, recorded_at}}
        self._readings: dict[str, dict[str, dict]] = {}
        # batch_id -> {cell_id -> {state, at, reason, operator}}
        self._dispositions: dict[str, dict[str, dict]] = {}
        # batch_id -> 冻结策略的映射
        self._policies: dict[str, dict] = {}
        self._restore()

    @property
    def clock(self) -> Clock:
        """台账使用的时钟（复现与测试可用手动时钟推进）。"""

        return self._clock

    # ------------------------------------------------------------------ 回放

    def _restore(self) -> None:
        for event in self._readings_journal.read_all():
            self._apply_reading(event)
        for event in self._dispositions_journal.read_all():
            self._apply_disposition(event)
        policies = self._store.read("grading_policies")
        if isinstance(policies, dict):
            self._policies = policies

    def _next_seq(self) -> int:
        self._seq += 1
        return self._seq

    def _append(self, journal: JsonlJournal, event: dict) -> dict:
        try:
            journal.append(event)
        except StorageError:
            raise
        return event

    def _apply_reading(self, event: dict) -> None:
        batch_id = event["batch_id"]
        cell_id = event["cell_id"]
        self._seq = max(self._seq, int(event["seq"]))
        batch = self._readings.setdefault(batch_id, {})
        if event["op"] == VOID:
            batch.pop(cell_id, None)
            return
        batch[cell_id] = {
            "reading": event["reading"],
            "revision": int(event["revision"]),
            "recorded_at": float(event["recorded_at"]),
        }

    def _apply_disposition(self, event: dict) -> None:
        batch_id = event["batch_id"]
        cell_id = event["cell_id"]
        self._seq = max(self._seq, int(event["seq"]))
        batch = self._dispositions.setdefault(batch_id, {})
        if event["op"] == QUARANTINE:
            batch[cell_id] = {
                "state": QUARANTINED,
                "at": float(event["ts"]),
                "reason": event.get("reason", ""),
                "operator": event.get("operator", "system"),
            }
        elif event["op"] == MANUAL_HOLD:
            batch[cell_id] = {
                "state": HELD,
                "at": float(event["ts"]),
                "reason": event.get("reason", ""),
                "operator": event.get("operator", "operator"),
            }
        elif event["op"] == MANUAL_RELEASE:
            batch[cell_id] = {
                "state": RELEASED,
                "at": float(event["ts"]),
                "reason": event.get("reason", ""),
                "operator": event.get("operator", "operator"),
            }

    # ------------------------------------------------------------------ 写入

    def register_batch(self, batch_id: str, policy: GradingPolicy) -> dict:
        """登记批次并冻结其判定口径；已登记则返回既有口径，不允许偷换。"""

        with self._lock:
            if batch_id in self._policies:
                existing = self._policies[batch_id]
                if existing != _policy_to_dict(policy):
                    raise GradingError(
                        f"批次 {batch_id} 已按另一套口径登记，判定阈值不能中途修改",
                        component="grading",
                    )
                return dict(existing)
            payload = _policy_to_dict(policy)
            self._policies[batch_id] = payload
            self._store.write("grading_policies", self._policies)
            return dict(payload)

    def record_reading(
        self,
        batch_id: str,
        reading: CellReading,
        *,
        recorded_at: float | None = None,
        source: str = "calibration",
    ) -> dict:
        """追加一条单体标定读数；同单体再次写入即修订（revision 递增）。"""

        with self._lock:
            if batch_id not in self._policies:
                raise GradingError(f"批次 {batch_id} 尚未登记，先 register_batch", component="grading")
            at = float(recorded_at if recorded_at is not None else self._clock.now())
            previous = self._readings.get(batch_id, {}).get(reading.cell_id)
            revision = 1 if previous is None else previous["revision"] + 1
            event = {
                "seq": self._next_seq(),
                "ts": self._clock.now(),
                "recorded_at": at,
                "batch_id": batch_id,
                "cell_id": reading.cell_id,
                "op": UPSERT,
                "revision": revision,
                "source": source,
                "reading": {
                    "cell_id": reading.cell_id,
                    "capacity_ah": reading.capacity_ah,
                    "voltage_diff_mv": reading.voltage_diff_mv,
                },
            }
            self._append(self._readings_journal, event)
            self._apply_reading(event)
            return {"batch_id": batch_id, "cell_id": reading.cell_id, "revision": revision, "recorded_at": at}

    def void_reading(self, batch_id: str, cell_id: str, *, reason: str = "") -> dict:
        """追加一条作废事件，把某单体读数从当前视图移除（流水仍保留）。"""

        with self._lock:
            if cell_id not in self._readings.get(batch_id, {}):
                raise GradingError(f"批次 {batch_id} 没有单体 {cell_id} 的有效读数", component="grading")
            event = {
                "seq": self._next_seq(),
                "ts": self._clock.now(),
                "recorded_at": self._clock.now(),
                "batch_id": batch_id,
                "cell_id": cell_id,
                "op": VOID,
                "reason": reason,
            }
            self._append(self._readings_journal, event)
            self._apply_reading(event)
            return {"batch_id": batch_id, "cell_id": cell_id, "op": VOID}

    def manual_disposition(
        self, batch_id: str, cell_id: str, op: str, *, operator: str, reason: str
    ) -> dict:
        """追加人工复核结论：manual_hold 或 manual_release。

        数据重算永远不会自动改变处置状态；隔离单体回到产线只能走人工放行，
        且必须记录操作人与理由。
        """

        if op not in (MANUAL_HOLD, MANUAL_RELEASE):
            raise GradingError(f"未知的人工处置动作: {op}", component="grading")
        if not reason:
            raise GradingError("人工处置必须填写理由", component="grading")
        with self._lock:
            if cell_id not in self._readings.get(batch_id, {}):
                raise GradingError(f"批次 {batch_id} 没有单体 {cell_id} 的读数", component="grading")
            event = {
                "seq": self._next_seq(),
                "ts": self._clock.now(),
                "batch_id": batch_id,
                "cell_id": cell_id,
                "op": op,
                "operator": operator,
                "reason": reason,
            }
            self._append(self._dispositions_journal, event)
            self._apply_disposition(event)
            return dict(event)

    def append_quarantine_events(self, batch_id: str, cell_ids: list[str], reason_template: str) -> list[str]:
        """把本次判定挑出的离群单体落隔离事件；已隔离的不重复记。"""

        with self._lock:
            newly: list[str] = []
            known = self._dispositions.setdefault(batch_id, {})
            for cell_id in sorted(cell_ids):
                if known.get(cell_id, {}).get("state") == QUARANTINED:
                    continue
                event = {
                    "seq": self._next_seq(),
                    "ts": self._clock.now(),
                    "batch_id": batch_id,
                    "cell_id": cell_id,
                    "op": QUARANTINE,
                    "operator": "system",
                    "reason": reason_template,
                }
                self._append(self._dispositions_journal, event)
                self._apply_disposition(event)
                newly.append(cell_id)
            return newly

    def append_report(self, batch_id: str, report: dict, *, as_of: float | None = None) -> dict:
        """追加一代批次结论快照，供事后追溯。"""

        with self._lock:
            event = {
                "seq": self._next_seq(),
                "ts": self._clock.now(),
                "batch_id": batch_id,
                "as_of": as_of,
                "report": report,
            }
            self._append(self._reports_journal, event)
            return {"seq": event["seq"], "batch_id": batch_id, "as_of": as_of}

    # ------------------------------------------------------------------ 查询

    def batch_ids(self) -> list[str]:
        """返回已有读数或已登记口径的全部批次。"""

        return sorted(set(self._readings) | set(self._policies))

    def policy_of(self, batch_id: str) -> GradingPolicy:
        """读取批次冻结的判定口径。"""

        if batch_id not in self._policies:
            raise GradingError(f"批次 {batch_id} 尚未登记", component="grading")
        return _policy_from_dict(self._policies[batch_id])

    def readings(self, batch_id: str, *, as_of: float | None = None) -> list[CellReading]:
        """取当前（或 ``as_of`` 时刻）系统持有的批次有效读数。

        ``as_of`` 按**录入事件的系统时间**（``ts``）回放：复算复现的是"系统在
        那个时刻看到的数据"。事后补录即使把业务标定时间（``recorded_at``）填到
        过去，该读数在录入发生之前对系统也不存在，从而避免时间旅行；修订与作废
        同理，只影响其录入时刻之后的视图。
        """

        if as_of is None:
            return [
                CellReading.from_dict(item["reading"])
                for item in self._readings.get(batch_id, {}).values()
            ]
        active: dict[str, dict] = {}
        for event in self._readings_journal.read_all():
            if event["batch_id"] != batch_id:
                continue
            if float(event["ts"]) > float(as_of):
                continue
            cell_id = event["cell_id"]
            if event["op"] == VOID:
                active.pop(cell_id, None)
            else:
                active[cell_id] = event["reading"]
        return [CellReading.from_dict(item) for item in active.values()]

    def quarantined_cells(self, batch_id: str, *, as_of: float | None = None) -> frozenset[str]:
        """取当前（或 ``as_of`` 时刻）已隔离单体集合。

        重算历史时同样以当时已隔离集合为准；隔离一旦发生，后续修订不会把
        单体自动放回。
        """

        if as_of is None:
            return frozenset(
                cell_id
                for cell_id, item in self._dispositions.get(batch_id, {}).items()
                if item["state"] in (QUARANTINED, HELD)
            )
        states: dict[str, str] = {}
        for event in self._dispositions_journal.read_all():
            if event["batch_id"] != batch_id or float(event["ts"]) > float(as_of):
                continue
            states[event["cell_id"]] = event["op"]
        return frozenset(
            cell_id
            for cell_id, op in states.items()
            if op in (QUARANTINE, MANUAL_HOLD)
        )

    def released_cells(self, batch_id: str, *, as_of: float | None = None) -> frozenset[str]:
        """取已被人工放行、统计重算不得重新隔离的单体集合。"""

        if as_of is None:
            return frozenset(
                cell_id
                for cell_id, item in self._dispositions.get(batch_id, {}).items()
                if item["state"] == RELEASED
            )
        latest: dict[str, str] = {}
        for event in self._dispositions_journal.read_all():
            if event["batch_id"] != batch_id or float(event["ts"]) > float(as_of):
                continue
            latest[event["cell_id"]] = event["op"]
        return frozenset(cell_id for cell_id, op in latest.items() if op == MANUAL_RELEASE)

    def disposition_view(self, batch_id: str) -> list[dict]:
        """返回该批每个单体的最新处置状态。"""

        return [
            {"cell_id": cell_id, **dict(item)}
            for cell_id, item in sorted(self._dispositions.get(batch_id, {}).items())
        ]

    def report_history(self, batch_id: str) -> list[dict]:
        """返回该批历代结论快照（按写入顺序）。"""

        return [
            {"seq": int(event["seq"]), "ts": float(event["ts"]), "as_of": event.get("as_of"),
             "verdict": event["report"]["verdict"]}
            for event in self._reports_journal.read_all()
            if event["batch_id"] == batch_id
        ]


def _policy_to_dict(policy: GradingPolicy) -> dict[str, Any]:
    return {
        "nominal_capacity_ah": policy.nominal_capacity_ah,
        "nominal_voltage_diff_mv": policy.nominal_voltage_diff_mv,
        "capacity_tol_pct": policy.capacity_tol_pct,
        "voltage_diff_tol_mv": policy.voltage_diff_tol_mv,
        "adaptive_min_n": policy.adaptive_min_n,
        "robust_z_cutoff": policy.robust_z_cutoff,
        "cv_cap_pct": policy.cv_cap_pct,
        "voltage_spread_cap_mv": policy.voltage_spread_cap_mv,
        "isolate_fraction_cap": policy.isolate_fraction_cap,
    }


def _policy_from_dict(payload: dict[str, Any]) -> GradingPolicy:
    allowed = {field.name for field in GradingPolicy.__dataclass_fields__.values()}
    return GradingPolicy(**{key: value for key, value in payload.items() if key in allowed})
