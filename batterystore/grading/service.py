"""标定批次判定编排：统计判定与处置台账联动。

``analyze`` 跑"当前数据"：按冻结口径出结论，把新挑出的离群单体追加隔离事件，
并落一代结论快照。离群单体隔离是自动的；**批次整批回到产线只能人工放行**。

``recompute`` 按某历史时刻（``as_of``）回放当时数据与当时已隔离集合，复现
"当时会得出的结论"，只落带 ``as_of`` 标记的快照，不产生任何新的隔离或放行
事件。补录/修订之后再跑 ``analyze`` 时，已经隔离过的单体即使统计转好也保持
隔离（sticky），结论里单独标出，必须走人工复核。
"""

from __future__ import annotations

from .ledger import GradingBook, HELD, MANUAL_HOLD, MANUAL_RELEASE, QUARANTINED
from .policy import grade_batch


def analyze_batch(book: GradingBook, batch_id: str) -> dict:
    """按当前数据重算批次结论，并把新离群单体落隔离。"""

    readings = book.readings(batch_id)
    if not readings:
        raise ValueError(f"批次 {batch_id} 没有有效标定读数")
    policy = book.policy_of(batch_id)
    pre_quarantined = book.quarantined_cells(batch_id)
    released_override = book.released_cells(batch_id)
    report = grade_batch(
        readings,
        policy,
        pre_quarantined=pre_quarantined,
        released_override=released_override,
    )
    newly_isolated = book.append_quarantine_events(
        batch_id,
        [
            cell_id
            for cell_id in report["isolated_ids"]
            if cell_id not in pre_quarantined and cell_id not in released_override
        ],
        reason_template=_reason_text(report),
    )
    snapshot = book.append_report(batch_id, report)
    return {
        "batch_id": batch_id,
        "as_of": None,
        "newly_isolated": newly_isolated,
        "already_quarantined": sorted(pre_quarantined),
        "manual_released": sorted(released_override),
        "snapshot": snapshot,
        "report": report,
    }


def recompute_batch(book: GradingBook, batch_id: str, as_of: float) -> dict:
    """按历史时刻回放数据重算结论；只读式复算，不改任何处置状态。"""

    readings = book.readings(batch_id, as_of=as_of)
    if not readings:
        raise ValueError(f"批次 {batch_id} 在 {as_of} 时刻没有有效标定读数")
    policy = book.policy_of(batch_id)
    pre_quarantined = book.quarantined_cells(batch_id, as_of=as_of)
    released_override = book.released_cells(batch_id, as_of=as_of)
    report = grade_batch(
        readings,
        policy,
        pre_quarantined=pre_quarantined,
        released_override=released_override,
    )
    snapshot = book.append_report(batch_id, report, as_of=as_of)
    return {
        "batch_id": batch_id,
        "as_of": as_of,
        "newly_isolated": [],
        "already_quarantined": sorted(pre_quarantined),
        "manual_released": sorted(released_override),
        "snapshot": snapshot,
        "report": report,
    }


def release_cell(book: GradingBook, batch_id: str, cell_id: str, operator: str, reason: str) -> dict:
    """人工放行一个已隔离单体（复核合格后唯一的回流产线路径）。"""

    return book.manual_disposition(batch_id, cell_id, MANUAL_RELEASE, operator=operator, reason=reason)


def hold_cell(book: GradingBook, batch_id: str, cell_id: str, operator: str, reason: str) -> dict:
    """人工维持隔离（复核确认不合格，需要退料/复测）。"""

    return book.manual_disposition(batch_id, cell_id, MANUAL_HOLD, operator=operator, reason=reason)


def summary_view(book: GradingBook, batch_id: str) -> dict:
    """汇总批次当前统计结论、处置分布与历代重算记录。"""

    policy = book.policy_of(batch_id)
    readings = book.readings(batch_id)
    quarantined = book.quarantined_cells(batch_id)
    released_override = book.released_cells(batch_id)
    report = (
        grade_batch(
            readings,
            policy,
            pre_quarantined=quarantined,
            released_override=released_override,
        )
        if readings
        else None
    )
    dispositions = book.disposition_view(batch_id)
    counts = {QUARANTINED: 0, HELD: 0, MANUAL_RELEASE: 0}
    for item in dispositions:
        counts[item["state"]] = counts.get(item["state"], 0) + 1
    return {
        "batch_id": batch_id,
        "n": len(readings),
        "verdict": None if report is None else report["verdict"],
        "authoritative": None if report is None else report["authoritative"],
        "disposition_counts": counts,
        "new_outliers": [] if report is None else report["isolated_ids"],
        "quarantine_worklist": [] if report is None else report["quarantine_worklist"],
        "released_ids": [] if report is None else report["released_ids"],
        "history": book.report_history(batch_id),
    }


def _reason_text(report: dict) -> str:
    """给自动隔离事件生成可追溯的理由摘要。"""

    basis = "adaptive" if report["authoritative"] == "adaptive" else "fixed"
    return f"批次一致性判定命中离群(authoritative={basis})"
