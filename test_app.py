import os
from pathlib import Path

import numpy as np
import pandas as pd
import pytest
from fastapi.testclient import TestClient

from backend import main
from backend.analysis import analyse_site, empty_site, status_for
from backend.data_loader import load_and_clean


def make_site(prs, cap=100.0, irr=5.0, start="2026-01-01", gen_override=None, irr_override=None):
    """Build one plant's frame from a list of daily PR values."""
    n = len(prs)
    dates = pd.date_range(start, periods=n)
    irradiance = np.full(n, irr, dtype=float)
    for i, v in (irr_override or {}).items():
        irradiance[i] = v
    gen = np.array(prs, dtype=float) * cap * irradiance
    for i, v in (gen_override or {}).items():
        gen[i] = v
    return pd.DataFrame({
        "date": dates, "site_id": "T1", "capacity_kw": cap, "generation_kwh": gen,
        "irradiance": irradiance, "cloud_cover": 20.0, "temperature": 30.0,
        "dq": [[] for _ in range(n)],
    })


def kinds(result):
    return [i["type"] for i in result["issues"]]


# ---- PR and status ----------------------------------------------------------
def test_pr_example_from_brief():
    df = make_site([0.78] * 10, cap=50, irr=5.0)
    assert df["generation_kwh"].iloc[0] == pytest.approx(195)
    assert analyse_site("T1", df)["daily"][0]["pr"] == pytest.approx(0.78)


@pytest.mark.parametrize("pr,expected", [
    (0.78, "Healthy"), (0.70, "Healthy"), (0.65, "Needs attention"),
    (0.60, "Needs attention"), (0.59, "Underperforming"),
])
def test_status_thresholds(pr, expected):
    assert status_for(pr) == expected
    assert analyse_site("T1", make_site([pr] * 20))["status"] == expected


# ---- exclusions ---------------------------------------------------------------
def test_low_irradiance_day_is_skipped():
    df = make_site([0.78] * 10, irr_override={4: 0.5}, gen_override={4: 1.0})
    day = analyse_site("T1", df)["daily"][4]
    assert day["pr"] is None and "low_irradiance" in day["flags"]


def test_outage_flagged_and_excluded_from_average():
    df = make_site([0.78] * 20, gen_override={19: 0.0})
    res = analyse_site("T1", df)
    assert kinds(res) == ["outage"]
    assert res["issues"][0]["dates"] == ["2026-01-20"]
    assert res["pr_7d"] == pytest.approx(0.78)  # the zero did not drag it down
    assert res["status"] == "Healthy"


def test_zero_on_dark_day_is_not_an_outage():
    df = make_site([0.78] * 10, irr_override={3: 0.4}, gen_override={3: 0.0})
    assert kinds(analyse_site("T1", df)) == []


def test_consecutive_outage_days_are_one_issue():
    df = make_site([0.78] * 20, gen_override={10: 0, 11: 0, 12: 0})
    res = analyse_site("T1", df)
    assert len(res["issues"]) == 1 and len(res["issues"][0]["dates"]) == 3


# ---- sudden drop / gradual decline ---------------------------------------------
def test_sudden_drop_detected():
    res = analyse_site("T1", make_site([0.78] * 40 + [0.52] * 20))
    assert "sudden_drop" in kinds(res)
    issue = next(i for i in res["issues"] if i["type"] == "sudden_drop")
    assert issue["start_date"] == "2026-02-10"  # back-dated to the real onset (day 40)
    assert res["status"] == "Underperforming"


def test_small_dip_is_not_a_sudden_drop():
    assert kinds(analyse_site("T1", make_site([0.78] * 30 + [0.74] * 20))) == []


def test_gradual_decline_detected_without_sudden_drop():
    res = analyse_site("T1", make_site(list(np.linspace(0.78, 0.62, 90))))
    assert kinds(res) == ["gradual_decline"]


def test_gradual_not_reported_when_sudden_drop_exists():
    res = analyse_site("T1", make_site([0.78] * 40 + [0.52] * 50))
    assert "gradual_decline" not in kinds(res)


def test_healthy_noisy_plant_has_no_issues():
    rng = np.random.default_rng(0)
    res = analyse_site("T1", make_site(list(0.775 + rng.normal(0, 0.012, 90))))
    assert res["issues"] == [] and res["status"] == "Healthy"


def test_missing_day_in_csv_does_not_break_windows():
    df = make_site([0.78] * 30).drop(index=[10, 11]).reset_index(drop=True)
    assert analyse_site("T1", df)["status"] == "Healthy"


# ---- cleaning ---------------------------------------------------------------------
def test_cleaning(tmp_path):
    csv = tmp_path / "d.csv"
    csv.write_text(
        "date,site_id,capacity_kw,generation_kwh,irradiance,cloud_cover,temperature\n"
        "2026-01-01,S1,50,195,5,10,30\n"
        "2026-01-01,S1,50,195,5,10,30\n"          # exact duplicate
        "2026-01-02, s1 ,50,-5,5,10,30\n"          # messy id + negative generation
        "2026-01-03,S1,50,error,5,10,30\n"         # text value
        "2026-01-04,S1,50,200,25,10,30\n"          # impossible irradiance
        "2026-01-05,S1,50,200,5,150,30\n"          # impossible cloud cover
        "2026-01-06,S1,0,200,5,10,30\n"            # bad capacity -> median
        "not-a-date,S1,50,200,5,10,30\n"           # unusable row
    )
    df, rep = load_and_clean(csv)
    assert rep["duplicate_rows_removed"] == 1
    assert rep["rows_dropped_bad_site_or_date"] == 1
    assert rep["impossible_readings_set_to_missing"]["negative_generation"] == 1
    assert rep["capacity_values_filled"] == 1
    assert set(df["site_id"]) == {"S1"}
    assert df.loc[df.date == "2026-01-02", "generation_kwh"].isna().all()
    assert df.loc[df.date == "2026-01-04", "irradiance"].isna().all()
    assert df.loc[df.date == "2026-01-06", "capacity_kw"].iloc[0] == 50


def test_missing_columns_raise(tmp_path):
    csv = tmp_path / "bad.csv"
    csv.write_text("date,site_id\n2026-01-01,S1\n")
    with pytest.raises(ValueError):
        load_and_clean(csv)


# ---- API -------------------------------------------------------------------------------
@pytest.fixture(scope="module")
def client():
    # Always test against the bundled sample so results don't depend on the real CSV.
    os.environ["SOLAR_CSV_PATH"] = str(Path(__file__).resolve().parent.parent / "data" / "sample_solar_plant_data.csv")
    main.get_state(reload=True)
    return TestClient(main.app)


def test_plants_endpoint(client):
    r = client.get("/plants")
    assert r.status_code == 200
    data = r.json()
    assert [p["id"] for p in data] == ["S1", "S2", "S3", "S4", "S5"]
    assert {p["status"] for p in data} <= {"Healthy", "Needs attention", "Underperforming", "No data"}


def test_plant_detail_and_case_insensitive_id(client):
    r = client.get("/plants/s2")
    assert r.status_code == 200
    body = r.json()
    assert body["id"] == "S2" and body["issues"] and body["daily"]


def test_unknown_plant_404(client):
    assert client.get("/plants/S99").status_code == 404


def test_date_range_filter(client):
    r = client.get("/plants/S1?start=2026-07-10&end=2026-07-19").json()
    assert len(r["daily"]) == 10
    assert r["daily"][0]["date"] == "2026-07-10"


def test_bad_range_400_and_bad_date_422(client):
    assert client.get("/plants/S1?start=2026-08-01&end=2026-07-01").status_code == 400
    assert client.get("/plants/S1?start=banana").status_code == 422


def test_data_quality(client):
    assert client.get("/data-quality").json()["duplicate_rows_removed"] >= 0


def test_real_csv_loads_if_present():
    real = Path(__file__).resolve().parent.parent / "data" / "solar_plant_data.csv"
    if not real.exists():
        pytest.skip("real CSV not present")
    df, rep = load_and_clean(real)
    assert rep["rows_clean"] > 0 and len(rep["sites"]) == 5


def test_no_usable_pr_days_returns_no_data():
    df = make_site([0.78] * 10, irr_override={i: 0.5 for i in range(10)})
    res = analyse_site("T1", df)
    assert res["status"] == "No data"
    assert res["pr_7d"] is None
    assert res["issues"] == []


def test_expected_site_without_rows_has_explicit_no_data_status():
    plant = empty_site("S5", "2026-03-31")
    assert plant["id"] == "S5"
    assert plant["status"] == "No data"
    assert plant["daily"] == [] and plant["pr_7d"] is None


def test_duplicate_tie_is_deterministic_and_reported(tmp_path):
    csv = tmp_path / "duplicates.csv"
    csv.write_text(
        "date,site_id,capacity_kw,generation_kwh,irradiance,cloud_cover,temperature\n"
        "2026-01-01,S1,50,190,5,10,30\n"
        "2026-01-01,S1,50,200,5,10,30\n"
    )
    first, report = load_and_clean(csv)
    second, _ = load_and_clean(csv)
    assert report["duplicate_rows_removed"] == 1
    assert first["generation_kwh"].tolist() == second["generation_kwh"].tolist()
    # Equal completeness is resolved by stable input order and documented behavior.
    assert first["generation_kwh"].iloc[0] == 190


def test_missing_expected_plant_is_kept_in_overview(tmp_path, monkeypatch):
    csv = tmp_path / "one-plant.csv"
    csv.write_text(
        "date,site_id,capacity_kw,generation_kwh,irradiance,cloud_cover,temperature\n"
        "2026-01-01,S1,50,195,5,10,30\n"
        "2026-01-02,S1,50,195,5,10,30\n"
        "2026-01-03,S1,50,195,5,10,30\n"
    )
    monkeypatch.setenv("SOLAR_CSV_PATH", str(csv))
    main.get_state(reload=True)
    try:
        data = TestClient(main.app).get("/plants").json()
        assert [p["id"] for p in data] == ["S1", "S2", "S3", "S4", "S5"]
        assert next(p for p in data if p["id"] == "S5")["status"] == "No data"
    finally:
        monkeypatch.setenv("SOLAR_CSV_PATH", str(Path(__file__).resolve().parent.parent / "data" / "sample_solar_plant_data.csv"))
        main.get_state(reload=True)
