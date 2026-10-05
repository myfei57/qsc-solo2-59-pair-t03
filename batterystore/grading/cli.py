"""标定批次一致性判定的命令行入口。

子命令::

    grading register  BATCH --nominal-ah N [--nominal-vdiff-mv N] [阈值参数]
    grading import    BATCH FILE.csv                # 追加/修订标定读数
    grading void      BATCH CELL --reason ...       # 作废一条读数
    grading analyze   BATCH                         # 按当前数据判定并隔离离群
    grading recompute BATCH --as-of TS              # 按历史时刻只读复算
    grading release   BATCH CELL --operator ... --reason ...
    grading hold      BATCH CELL --operator ... --reason ...
    grading list                                      # 列出全部批次
    grading show      BATCH                          # 批次台账与结论沿革

CSV 表头必须包含 ``cell_id,capacity_ah,voltage_diff_mv``，可选 ``recorded_at``
（Unix 秒，便于把补录数据挂到它实际标定的时刻）与 ``source``。
"""

from __future__ import annotations

import argparse
import csv
import json
import os
from typing import Any

from ..clock import Clock
from ..store.jsonfile import JsonStore
from .ledger import GradingBook
from .policy import CellReading, GradingPolicy
from .service import (
    analyze_batch,
    hold_cell,
    recompute_batch,
    release_cell,
    summary_view,
)

CSV_FIELDS = ("cell_id", "capacity_ah", "voltage_diff_mv")


def _book(args: argparse.Namespace, clock: Clock | None = None) -> GradingBook:
    root = getattr(args, "grading_root", None) or "var/grading"
    return GradingBook(JsonStore(root), clock=clock)


def _dump(payload: object) -> None:
    print(json.dumps(payload, ensure_ascii=False, indent=2, default=str))


def _build_policy(args: argparse.Namespace) -> GradingPolicy:
    return GradingPolicy(
        nominal_capacity_ah=float(args.nominal_ah),
        nominal_voltage_diff_mv=float(getattr(args, "nominal_vdiff_mv", 0.0)),
        capacity_tol_pct=float(args.capacity_tol_pct),
        voltage_diff_tol_mv=float(args.voltage_diff_tol_mv),
        adaptive_min_n=int(args.adaptive_min_n),
        robust_z_cutoff=float(args.robust_z_cutoff),
        cv_cap_pct=float(args.cv_cap_pct),
        voltage_spread_cap_mv=float(args.voltage_spread_cap_mv),
        isolate_fraction_cap=float(args.isolate_fraction_cap),
    )


def _read_csv(path: str) -> list[dict[str, Any]]:
    if not os.path.isfile(path):
        raise FileNotFoundError(f"标定数据文件不存在: {path}")
    with open(path, "r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        if reader.fieldnames is None or not set(CSV_FIELDS).issubset(reader.fieldnames):
            raise ValueError(f"CSV 表头必须包含 {', '.join(CSV_FIELDS)}")
        rows: list[dict[str, Any]] = []
        for line, row in enumerate(reader, start=2):
            if not row.get("cell_id"):
                continue
            try:
                rows.append({
                    "cell_id": row["cell_id"].strip(),
                    "capacity_ah": float(row["capacity_ah"]),
                    "voltage_diff_mv": float(row["voltage_diff_mv"]),
                    "recorded_at": float(row["recorded_at"]) if row.get("recorded_at") else None,
                    "source": (row.get("source") or "csv").strip(),
                })
            except (TypeError, ValueError) as error:
                raise ValueError(f"CSV 第 {line} 行数值无法解析: {error}") from error
    if not rows:
        raise ValueError("CSV 没有任何有效标定行")
    return rows


def _cmd_register(args: argparse.Namespace) -> int:
    book = _book(args)
    payload = book.register_batch(args.batch, _build_policy(args))
    _dump({"batch_id": args.batch, "registered": True, "policy": payload})
    return 0


def _cmd_import(args: argparse.Namespace) -> int:
    book = _book(args)
    rows = _read_csv(args.file)
    results = []
    for row in rows:
        reading = CellReading(row["cell_id"], row["capacity_ah"], row["voltage_diff_mv"])
        results.append(book.record_reading(
            args.batch, reading, recorded_at=row["recorded_at"], source=row["source"]
        ))
    _dump({"batch_id": args.batch, "imported": len(results), "rows": results})
    return 0


def _cmd_void(args: argparse.Namespace) -> int:
    _dump(_book(args).void_reading(args.batch, args.cell, reason=args.reason))
    return 0


def _cmd_analyze(args: argparse.Namespace) -> int:
    outcome = analyze_batch(_book(args), args.batch)
    if args.human:
        print(_render_human(outcome))
    else:
        _dump(outcome)
    return 0


def _cmd_recompute(args: argparse.Namespace) -> int:
    outcome = recompute_batch(_book(args), args.batch, float(args.as_of))
    if args.human:
        print(_render_human(outcome))
    else:
        _dump(outcome)
    return 0


def _cmd_release(args: argparse.Namespace) -> int:
    _dump(release_cell(_book(args), args.batch, args.cell, args.operator, args.reason))
    return 0


def _cmd_hold(args: argparse.Namespace) -> int:
    _dump(hold_cell(_book(args), args.batch, args.cell, args.operator, args.reason))
    return 0


def _cmd_list(args: argparse.Namespace) -> int:
    book = _book(args)
    _dump({"batches": book.batch_ids()})
    return 0


def _cmd_show(args: argparse.Namespace) -> int:
    book = _book(args)
    payload = summary_view(book, args.batch)
    if args.human:
        print(_render_summary(payload))
    else:
        payload["dispositions"] = book.disposition_view(args.batch)
        _dump(payload)
    return 0


def _add_threshold_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--nominal-ah", type=float, required=True, help="批次标称容量 Ah")
    parser.add_argument("--nominal-vdiff-mv", type=float, default=0.0, help="批次标称压差 mV")
    parser.add_argument("--capacity-tol-pct", type=float, default=3.0, help="容量固定阈值 ±%%")
    parser.add_argument("--voltage-diff-tol-mv", type=float, default=10.0, help="压差固定阈值上界增量 mV")
    parser.add_argument("--adaptive-min-n", type=int, default=20, help="自适应口径最小样本数")
    parser.add_argument("--robust-z-cutoff", type=float, default=3.5, help="稳健 z 离群倍数")
    parser.add_argument("--cv-cap-pct", type=float, default=1.5, help="放行：容量 CV 上限 %%")
    parser.add_argument("--voltage-spread-cap-mv", type=float, default=30.0, help="放行：压差极差上限 mV")
    parser.add_argument("--isolate-fraction-cap", type=float, default=0.10, help="放行：离群占比上限")


def build_grading_parser(sub: argparse._SubParsersAction) -> None:
    """把 grading 子命令挂到主解析器上。"""

    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--grading-root", default="var/grading", help="标定台账目录")

    grading = sub.add_parser("grading", help="标定批次一致性判定", parents=[common])
    gsub = grading.add_subparsers(dest="grading_command", required=True)

    register = gsub.add_parser("register", help="登记批次并冻结判定口径", parents=[common])
    register.add_argument("batch")
    _add_threshold_args(register)
    register.set_defaults(grading_func=_cmd_register)

    import_cmd = gsub.add_parser("import", help="从 CSV 追加/修订标定读数", parents=[common])
    import_cmd.add_argument("batch")
    import_cmd.add_argument("file")
    import_cmd.set_defaults(grading_func=_cmd_import)

    void_cmd = gsub.add_parser("void", help="作废一条读数", parents=[common])
    void_cmd.add_argument("batch")
    void_cmd.add_argument("cell")
    void_cmd.add_argument("--reason", default="", help="作废原因")
    void_cmd.set_defaults(grading_func=_cmd_void)

    analyze = gsub.add_parser("analyze", help="按当前数据判定并隔离离群", parents=[common])
    analyze.add_argument("batch")
    analyze.add_argument("--human", action="store_true", help="打印可读结论文本")
    analyze.set_defaults(grading_func=_cmd_analyze)

    recompute = gsub.add_parser("recompute", help="按历史时刻只读复算", parents=[common])
    recompute.add_argument("batch")
    recompute.add_argument("--as-of", required=True, help="复算时刻 Unix 秒")
    recompute.add_argument("--human", action="store_true", help="打印可读结论文本")
    recompute.set_defaults(grading_func=_cmd_recompute)

    release = gsub.add_parser("release", help="人工放行已隔离单体", parents=[common])
    release.add_argument("batch")
    release.add_argument("cell")
    release.add_argument("--operator", required=True, help="操作人")
    release.add_argument("--reason", required=True, help="放行理由")
    release.set_defaults(grading_func=_cmd_release)

    hold = gsub.add_parser("hold", help="人工维持隔离", parents=[common])
    hold.add_argument("batch")
    hold.add_argument("cell")
    hold.add_argument("--operator", required=True, help="操作人")
    hold.add_argument("--reason", required=True, help="维持隔离理由")
    hold.set_defaults(grading_func=_cmd_hold)

    list_cmd = gsub.add_parser("list", help="列出全部批次", parents=[common])
    list_cmd.set_defaults(grading_func=_cmd_list)

    show = gsub.add_parser("show", help="批次台账与结论沿革", parents=[common])
    show.add_argument("batch")
    show.add_argument("--human", action="store_true", help="打印可读结论文本")
    show.set_defaults(grading_func=_cmd_show)


def run_grading(args: argparse.Namespace) -> int:
    """主入口分发到具体 grading 子命令。"""

    return int(args.grading_func(args))


def _render_human(outcome: dict) -> str:
    """把判定结果渲染成带判断依据的可读结论。"""

    report = outcome["report"]
    as_of = outcome.get("as_of")
    header = f"批次 {outcome['batch_id']}" + (f"  按 {as_of} 时刻复算" if as_of else "  当前数据")
    lines = [
        header,
        f"  样本数 n={report['n']}，采用口径：{report['authoritative']}（{report['policy']['boundary_note']}）",
        _render_stats("容量(Ah)", report["stats"]["capacity_ah"]),
        _render_stats("压差(mV)", report["stats"]["voltage_diff_mv"]),
    ]
    if report["kept_stats"]:
        lines.append(_render_stats("放行组容量(Ah)", report["kept_stats"]["capacity_ah"]))
        lines.append(_render_stats("放行组压差(mV)", report["kept_stats"]["voltage_diff_mv"]))
    lines.append("  放行门禁：")
    for gate in report["gates"]:
        mark = "PASS" if gate["passed"] else "FAIL"
        lines.append(f"    [{mark}] {gate['name']}: 实际={gate['actual']} 限值={gate['limit']} —— {gate['detail']}")
    lines.append(f"  新判离群 {len(report['isolated_ids'])} 个（占本批 {report['new_outlier_fraction']:.1%}）：")
    reasons = {cell["cell_id"]: cell["reasons"] for cell in report["cells"]}
    for cell_id in report["isolated_ids"]:
        lines.append(f"    - {cell_id}: {', '.join(reasons[cell_id])}")
    if report["released_ids"]:
        lines.append(f"  随批放行 {len(report['released_ids'])} 个。")
    if report["quarantine_worklist"]:
        lines.append(
            "  隔离区待复核（此前已隔离，数据修订不自动回流，需人工处置；不影响本批放行）："
            + ", ".join(report["quarantine_worklist"])
        )
    verdict_text = {
        "release": "本批其余单体可放行",
        "review": "整批改人工复核（不得自动放行）",
        "isolate": "隔离复核",
    }.get(report["verdict"], report["verdict"])
    lines.append(f"  结论：{report['verdict']} —— {verdict_text}")
    if as_of is None:
        newly = outcome.get("newly_isolated", [])
        lines.append(f"  本次新增隔离事件：{newly if newly else '无'}")
    else:
        lines.append("  复算不产生任何隔离/放行事件。")
    return "\n".join(lines)


def _render_stats(label: str, stats: dict) -> str:
    cv = "—" if stats["cv_pct"] is None else f"{stats['cv_pct']}%"
    return (
        f"  {label}: 均值={stats['mean']} 标准差={stats['sd']} CV={cv} "
        f"极差={stats['spread']} 中位数={stats['median']} MAD={stats['mad']}"
    )


def _render_summary(payload: dict) -> str:
    lines = [
        f"批次 {payload['batch_id']}，有效读数 {payload['n']} 个",
        f"  有效结论：{payload['verdict']}，口径：{payload['authoritative']}",
        f"  处置分布：{payload['disposition_counts']}",
        f"  本批新判离群：{payload['new_outliers']}",
        f"  隔离区待复核：{payload['quarantine_worklist']}",
        "  结论沿革：",
    ]
    for item in payload["history"]:
        tag = f"as_of={item['as_of']}" if item["as_of"] is not None else "current"
        lines.append(f"    seq={item['seq']} ts={item['ts']:.0f} [{tag}] verdict={item['verdict']}")
    return "\n".join(lines)
