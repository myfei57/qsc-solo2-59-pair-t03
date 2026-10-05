"""批内统计量。

批次一致性用"这批自己的分布"来描述：均值与总体标准差看整体离散，中位数与
MAD（绝对中位差）看稳健中心。离群点本身会污染均值和标准差，自适应判据必须
建立在稳健量上，才不会出现"离群点把自己洗进正常范围"。
"""

from __future__ import annotations

import math
import statistics


def summarize(values: list[float]) -> dict:
    """返回一组测量值的描述统计；空列表返回全零占位，调用方不必先判空。"""

    if not values:
        return {
            "n": 0,
            "mean": 0.0,
            "pstdev": 0.0,
            "min": 0.0,
            "max": 0.0,
            "spread": 0.0,
            "median": 0.0,
            "mad": 0.0,
        }
    ordered = sorted(float(value) for value in values)
    median = statistics.median(ordered)
    mad = statistics.median([abs(value - median) for value in ordered])
    return {
        "n": len(ordered),
        "mean": round(statistics.fmean(ordered), 4),
        "pstdev": round(statistics.pstdev(ordered), 4),
        "min": round(ordered[0], 4),
        "max": round(ordered[-1], 4),
        "spread": round(ordered[-1] - ordered[0], 4),
        "median": round(median, 4),
        "mad": round(mad, 4),
    }


def robust_zscores(values: list[float]) -> list[float]:
    """返回每个值的稳健 z 分数（相对中位数，按 MAD 缩放，带符号）。

    MAD 为 0 表示批内过半数的值完全相同：与中位数一致的值得 0，任何偏离
    都记为无穷大——完全一致的一批里混进一个异值，必须被抓出来。
    """

    if not values:
        return []
    median = statistics.median(values)
    mad = statistics.median([abs(value - median) for value in values])
    scores: list[float] = []
    for value in values:
        if mad == 0.0:
            scores.append(0.0 if float(value) == median else math.inf)
        else:
            scores.append(0.6745 * (float(value) - median) / mad)
    return scores
