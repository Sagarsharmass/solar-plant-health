"""Solar plant health API (FastAPI). Also serves the frontend from /."""
from __future__ import annotations

import os
import re

import pandas as pd
from datetime import date
from pathlib import Path

from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from .analysis import ATTENTION_MIN, HEALTHY_MIN, analyse_site, empty_site, summarise
from .data_loader import load_and_clean

ROOT = Path(__file__).resolve().parent.parent
FRONTEND_DIR = ROOT / "frontend"

app = FastAPI(title="Solar Plant Health API", version="1.0.0")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["GET"], allow_headers=["*"])

EXPECTED_SITE_IDS = ["S1", "S2", "S3", "S4", "S5"]
_STATE: dict = {}


def csv_path() -> Path:
    """SOLAR_CSV_PATH env var > data/solar_plant_data.csv > bundled sample."""
    env = os.environ.get("SOLAR_CSV_PATH")
    if env:
        return Path(env)
    real = ROOT / "data" / "solar_plant_data.csv"
    return real if real.exists() else ROOT / "data" / "sample_solar_plant_data.csv"


def get_state(reload: bool = False) -> dict:
    if reload or not _STATE:
        try:
            df, report = load_and_clean(csv_path())
            plants = {sid: analyse_site(sid, g) for sid, g in df.groupby("site_id")}
            # The assignment always monitors S1-S5. Keep a missing plant visible
            # instead of silently omitting it from the dashboard.
            latest_date = df["date"].max() if not df.empty else None
            as_of = latest_date.strftime("%Y-%m-%d") if pd.notna(latest_date) else None
            for sid in EXPECTED_SITE_IDS:
                plants.setdefault(sid, empty_site(sid, as_of))
        except Exception as exc:  # surfaced to the UI as a readable error
            raise HTTPException(status_code=500, detail=f"Could not load data: {exc}") from exc
        report["source_file"] = csv_path().name
        _STATE.update(plants=plants, report=report)
    return _STATE


def _natural(site_id: str):
    return [int(t) if t.isdigit() else t for t in re.split(r"(\d+)", site_id)]


@app.get("/health")
def health():
    return {"status": "ok"}


@app.get("/plants")
def list_plants():
    """All plants with their current health status."""
    plants = get_state()["plants"]
    return [summarise(plants[k]) for k in sorted(plants, key=_natural)]


@app.get("/plants/{plant_id}")
def plant_detail(
    plant_id: str,
    start: date | None = Query(None, description="Only return days on/after this date (YYYY-MM-DD)"),
    end: date | None = Query(None, description="Only return days on/before this date (YYYY-MM-DD)"),
):
    """Daily data and detected issues for one plant, optionally date-filtered.

    Analysis always runs on the full 90 days (rolling averages need history);
    the date range only filters what is returned.
    """
    plants = get_state()["plants"]
    plant = plants.get(plant_id.strip().upper())
    if plant is None:
        raise HTTPException(status_code=404, detail=f"Plant '{plant_id}' not found")
    if start and end and start > end:
        raise HTTPException(status_code=400, detail="'start' must be on or before 'end'")

    all_days = [d["date"] for d in plant["daily"]]
    lo, hi = (start.isoformat() if start else "0000-00-00"), (end.isoformat() if end else "9999-99-99")
    daily = [d for d in plant["daily"] if lo <= d["date"] <= hi]
    issues = [i for i in plant["issues"] if i["end_date"] >= lo and i["start_date"] <= hi]

    return {
        "id": plant["id"],
        "capacity_kw": plant["capacity_kw"],
        "status": plant["status"],
        "pr_7d": plant["pr_7d"],
        "as_of": plant["as_of"],
        "thresholds": {"healthy_min": HEALTHY_MIN, "attention_min": ATTENTION_MIN},
        "range": {
            "start": start.isoformat() if start else None,
            "end": end.isoformat() if end else None,
            "min_date": all_days[0] if all_days else None,
            "max_date": all_days[-1] if all_days else None,
        },
        "issues": issues,
        "daily": daily,
    }


@app.get("/data-quality")
def data_quality():
    """What the cleaning step found and fixed."""
    return get_state()["report"]


# Static frontend last, so API routes win.
if FRONTEND_DIR.exists():
    app.mount("/", StaticFiles(directory=FRONTEND_DIR, html=True), name="frontend")
