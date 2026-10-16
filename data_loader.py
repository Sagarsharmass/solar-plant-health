"""Load the solar CSV and clean it.

Nothing is silently invented: bad values become missing (NaN) and the row is
tagged with a data-quality flag so the API and UI can show what happened.
Every decision is counted in the returned report (served at /data-quality).
"""
from __future__ import annotations

from pathlib import Path

import numpy as np
import pandas as pd

REQUIRED_COLUMNS = [
    "date",
    "site_id",
    "capacity_kw",
    "generation_kwh",
    "irradiance",
    "cloud_cover",
    "temperature",
]
NUMERIC_COLUMNS = REQUIRED_COLUMNS[2:]

# Physical limits used to reject impossible readings.
MAX_IRRADIANCE = 12.0      # kWh/m2/day. Even a perfect desert day is ~8-9.
CLOUD_RANGE = (0.0, 100.0)  # percent
TEMP_RANGE = (-50.0, 70.0)  # degrees C
MAX_SUN_HOURS = 24         # a plant cannot make more than capacity x 24 h


def _flag(df: pd.DataFrame, mask: pd.Series, text: str) -> None:
    for i in df.index[mask]:
        df.at[i, "dq"] = df.at[i, "dq"] + [text]


def load_and_clean(path: str | Path) -> tuple[pd.DataFrame, dict]:
    raw = pd.read_csv(path, skipinitialspace=True)
    raw.columns = [str(c).strip().lower() for c in raw.columns]
    missing_cols = [c for c in REQUIRED_COLUMNS if c not in raw.columns]
    if missing_cols:
        raise ValueError(f"CSV is missing required columns: {missing_cols}")

    df = raw[REQUIRED_COLUMNS].copy()
    report: dict = {"rows_read": int(len(df))}

    # 1. Keys: site_id and date must be usable, otherwise the row is unusable.
    df["site_id"] = df["site_id"].astype(object).where(df["site_id"].notna(), "")
    df["site_id"] = df["site_id"].astype(str).str.strip().str.upper()
    df["date"] = pd.to_datetime(df["date"], errors="coerce").dt.normalize()
    bad_key = (df["site_id"] == "") | df["date"].isna()
    report["rows_dropped_bad_site_or_date"] = int(bad_key.sum())
    df = df[~bad_key].copy()

    # 2. Numbers: text like "n/a" or "error" becomes missing.
    unparseable = 0
    missing_counts = {}
    for col in NUMERIC_COLUMNS:
        before = df[col].notna()
        df[col] = pd.to_numeric(df[col], errors="coerce")
        unparseable += int((before & df[col].isna()).sum())
        missing_counts[col] = int(df[col].isna().sum())
    report["unparseable_values_set_to_missing"] = unparseable
    report["missing_values"] = missing_counts

    # 3. Duplicates: one row per (site, date). Keep the row with most values.
    df["_nulls"] = df[NUMERIC_COLUMNS].isna().sum(axis=1)
    df = df.sort_values(["site_id", "date", "_nulls"], kind="stable")
    dup = df.duplicated(["site_id", "date"], keep="first")
    report["duplicate_rows_removed"] = int(dup.sum())
    df = df[~dup].drop(columns="_nulls").reset_index(drop=True)

    df["dq"] = [[] for _ in range(len(df))]

    # 4. Capacity: must be positive. Fill from the plant's own median.
    bad_cap = ~(df["capacity_kw"] > 0)
    median_cap = df.loc[~bad_cap].groupby("site_id")["capacity_kw"].median()
    fill = df["site_id"].map(median_cap)
    fixable = bad_cap & fill.notna()
    df.loc[fixable, "capacity_kw"] = fill[fixable]
    df.loc[bad_cap & ~fixable, "capacity_kw"] = np.nan
    _flag(df, fixable, "capacity_filled_from_median")
    _flag(df, bad_cap & ~fixable, "capacity_missing")
    report["capacity_values_filled"] = int(fixable.sum())

    # 5. Missing readings are kept as gaps and flagged.
    _flag(df, df["generation_kwh"].isna(), "missing_generation")
    _flag(df, df["irradiance"].isna(), "missing_irradiance")

    # 6. Impossible readings become missing (the raw value is not trusted).
    impossible = {
        "negative_generation": df["generation_kwh"] < 0,
        "impossible_irradiance": (df["irradiance"] < 0) | (df["irradiance"] > MAX_IRRADIANCE),
        "impossible_cloud_cover": (df["cloud_cover"] < CLOUD_RANGE[0]) | (df["cloud_cover"] > CLOUD_RANGE[1]),
        "impossible_temperature": (df["temperature"] < TEMP_RANGE[0]) | (df["temperature"] > TEMP_RANGE[1]),
        "generation_exceeds_capacity": df["generation_kwh"] > df["capacity_kw"] * MAX_SUN_HOURS,
    }
    column_for = {
        "negative_generation": "generation_kwh",
        "impossible_irradiance": "irradiance",
        "impossible_cloud_cover": "cloud_cover",
        "impossible_temperature": "temperature",
        "generation_exceeds_capacity": "generation_kwh",
    }
    counts = {}
    for name, mask in impossible.items():
        counts[name] = int(mask.sum())
        _flag(df, mask, name)
        df.loc[mask, column_for[name]] = np.nan
    report["impossible_readings_set_to_missing"] = counts

    # 7. Physically impossible performance ratio (>1.0): flag, keep raw values.
    with np.errstate(divide="ignore", invalid="ignore"):
        pr = df["generation_kwh"] / (df["capacity_kw"] * df["irradiance"])
    pr_bad = (df["irradiance"] >= 1.0) & (pr > 1.0)
    _flag(df, pr_bad, "pr_above_1")
    report["pr_above_1_flagged"] = int(pr_bad.sum())

    df = df.sort_values(["site_id", "date"]).reset_index(drop=True)
    report["rows_clean"] = int(len(df))
    report["rows_with_quality_flags"] = int(df["dq"].map(bool).sum())
    report["sites"] = sorted(df["site_id"].unique().tolist())
    return df, report
