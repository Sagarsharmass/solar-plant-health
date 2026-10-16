"""Rule-based health analysis for one solar plant. No ML, just rolling means.

PR (performance ratio) = generation_kwh / (capacity_kw * irradiance)
"""
from __future__ import annotations

import math

import numpy as np
import pandas as pd

# ---- thresholds (all in one place so they are easy to tune) ----------------
HEALTHY_MIN = 0.70
ATTENTION_MIN = 0.60
MIN_IRRADIANCE = 1.0      # below this PR is unreliable, day is skipped
MAX_PR = 1.0              # a PR above this is physically impossible
WINDOW = 7                # days in the rolling average
MIN_VALID_DAYS = 3        # need at least this many usable days in a window
SUDDEN_DROP = 0.20        # 7-day avg fell > 20% vs the 7 days before it
GRADUAL_DECLINE = 0.10    # last 14 days > 10% lower than first 14 days
GRADUAL_WINDOW = 14
MIN_GRADUAL_DAYS = 5      # usable days needed in each 14-day window

HEALTHY, ATTENTION, UNDER, NO_DATA = "Healthy", "Needs attention", "Underperforming", "No data"


def status_for(pr7: float | None) -> str:
    if pr7 is None or math.isnan(pr7):
        return NO_DATA
    if pr7 >= HEALTHY_MIN:
        return HEALTHY
    if pr7 >= ATTENTION_MIN:
        return ATTENTION
    return UNDER


def _num(v, nd=3):
    if v is None or (isinstance(v, float) and math.isnan(v)) or pd.isna(v):
        return None
    return round(float(v), nd)


def _runs(mask: pd.Series) -> list[tuple[pd.Timestamp, pd.Timestamp]]:
    """Group consecutive True days into (start, end) episodes."""
    runs, start, prev = [], None, None
    for day, hit in mask.items():
        if hit:
            start = day if start is None else start
            prev = day
        elif start is not None:
            runs.append((start, prev))
            start = None
    if start is not None:
        runs.append((start, prev))
    return runs


def _d(ts: pd.Timestamp) -> str:
    return ts.strftime("%Y-%m-%d")


def analyse_site(site_id: str, g: pd.DataFrame) -> dict:
    g = g.sort_values("date")
    # Reindex to every calendar day so "7 days" always means 7 calendar days
    # even when a day is missing from the CSV.
    full_index = pd.date_range(g["date"].min(), g["date"].max(), freq="D")
    s = g.set_index("date").reindex(full_index)
    present = full_index.isin(g["date"])

    cap, gen, irr = s["capacity_kw"], s["generation_kwh"], s["irradiance"]

    outage = (gen == 0) & (irr >= MIN_IRRADIANCE)
    usable = (irr >= MIN_IRRADIANCE) & gen.notna() & (cap > 0) & ~outage
    with np.errstate(divide="ignore", invalid="ignore"):
        raw_pr = gen / (cap * irr)
    pr_impossible = usable & ((raw_pr > MAX_PR) | (raw_pr < 0))
    pr = raw_pr.where(usable & ~pr_impossible)  # NaN on skipped days

    pr7 = pr.rolling(WINDOW, min_periods=MIN_VALID_DAYS).mean()
    prev7 = pr7.shift(WINDOW)
    drop = (prev7 - pr7) / prev7
    sudden_mask = drop > SUDDEN_DROP

    issues: list[dict] = []
    flags: dict[pd.Timestamp, set] = {d: set() for d in full_index}

    # ---- outages --------------------------------------------------------
    for start, end in _runs(outage):
        days = pd.date_range(start, end)
        lo, hi = irr[start:end].min(), irr[start:end].max()
        n = len(days)
        sun = f"{lo:.1f}" if abs(hi - lo) < 0.05 else f"{lo:.1f}-{hi:.1f}"
        issues.append({
            "type": "outage",
            "severity": "high",
            "start_date": _d(start),
            "end_date": _d(end),
            "dates": [_d(x) for x in days],
            "reason": (
                f"Produced 0 kWh for {n} day{'s' if n > 1 else ''} although "
                f"irradiance was {sun} kWh/m2. Likely an outage or inverter trip."
            ),
        })
        for x in days:
            flags[x].add("outage")

    # ---- sudden drops ---------------------------------------------------
    for start, end in _runs(sudden_mask):
        peak = drop[start:end].idxmax()
        pct = drop[peak] * 100
        # The rolling average reacts a few days late. Back-date the issue to the
        # first day in the window where daily PR was clearly below the old level.
        floor = prev7[peak] * (1 - SUDDEN_DROP / 2)
        recent = pr[start - pd.Timedelta(days=WINDOW - 1):start]
        below = recent[recent < floor]
        onset = below.index[0] if len(below) else start
        issues.append({
            "type": "sudden_drop",
            "severity": "high",
            "start_date": _d(onset),
            "end_date": _d(end),
            "dates": [],
            "reason": (
                f"PR dropped sharply from about {onset:%d %b}: the 7-day average fell from "
                f"{prev7[peak]:.2f} to {pr7[peak]:.2f} ({pct:.0f}% lower than the 7 days before). "
                "Something changed suddenly at the plant."
            ),
        })
        for x in pd.date_range(onset, end):
            flags[x].add("sudden_drop")

    # ---- gradual decline (only if there was no sudden drop) ---------------
    has_sudden = bool(sudden_mask.any())
    if len(pr) >= 2 * GRADUAL_WINDOW and not has_sudden:
        first, last = pr.iloc[:GRADUAL_WINDOW], pr.iloc[-GRADUAL_WINDOW:]
        if first.count() >= MIN_GRADUAL_DAYS and last.count() >= MIN_GRADUAL_DAYS:
            f, l = first.mean(), last.mean()
            decline = (f - l) / f
            if decline > GRADUAL_DECLINE:
                issues.append({
                    "type": "gradual_decline",
                    "severity": "medium",
                    "start_date": _d(last.index[0]),
                    "end_date": _d(last.index[-1]),
                    "dates": [],
                    "reason": (
                        f"Average PR slipped from {f:.2f} (first 14 days) to {l:.2f} "
                        f"(last 14 days), {decline * 100:.0f}% lower, with no single sharp drop. "
                        "Possible soiling, ageing or a slowly failing component."
                    ),
                })
                for x in last.index:
                    flags[x].add("gradual_decline")

    issues.sort(key=lambda i: i["start_date"])

    # ---- current status -------------------------------------------------
    latest = pr7.iloc[-1]
    status = status_for(_num(latest))
    recent_outages = int(outage.iloc[-WINDOW:].sum())
    if status == NO_DATA and recent_outages >= MIN_VALID_DAYS:
        # Average is empty only because outage days are excluded.
        status = UNDER

    # ---- daily records --------------------------------------------------
    daily = []
    for ts in full_index[present]:
        row = s.loc[ts]
        dq = row["dq"] if isinstance(row["dq"], list) else []
        day_flags = set(flags[ts])
        if irr[ts] < MIN_IRRADIANCE:
            day_flags.add("low_irradiance")
        if pr_impossible[ts]:
            day_flags.add("pr_impossible")
        if dq:
            day_flags.add("bad_data")
        highlight = next(
            (h for h in ("outage", "sudden_drop", "bad_data", "gradual_decline") if h in day_flags),
            None,
        )
        daily.append({
            "date": _d(ts),
            "generation_kwh": _num(gen[ts], 2),
            "irradiance": _num(irr[ts], 2),
            "cloud_cover": _num(row["cloud_cover"], 1),
            "temperature": _num(row["temperature"], 1),
            "pr": _num(pr[ts]),
            "pr_7d": _num(pr7[ts]),
            "flags": sorted(day_flags),
            "data_quality": dq,
            "highlight": highlight,
        })

    capacity = _num(cap.dropna().median(), 1) if cap.notna().any() else None
    return {
        "id": site_id,
        "capacity_kw": capacity,
        "status": status,
        "pr_7d": _num(latest),
        "as_of": _d(full_index[-1]),
        "issues": issues,
        "daily": daily,
    }


def empty_site(site_id: str, as_of: str | None = None) -> dict:
    """Represent an expected plant with no valid rows without inventing readings."""
    return {
        "id": site_id,
        "capacity_kw": None,
        "status": NO_DATA,
        "pr_7d": None,
        "as_of": as_of,
        "issues": [],
        "daily": [],
    }


def summarise(plant: dict) -> dict:
    """The compact version used by GET /plants."""
    counts = {"sudden_drop": 0, "gradual_decline": 0, "outage": 0}
    for i in plant["issues"]:
        counts[i["type"]] += 1
    return {
        "id": plant["id"],
        "capacity_kw": plant["capacity_kw"],
        "status": plant["status"],
        "pr_7d": plant["pr_7d"],
        "as_of": plant["as_of"],
        "issue_counts": counts,
        "issue_total": len(plant["issues"]),
        "headline": plant["issues"][-1]["reason"] if plant["issues"] else None,
    }
