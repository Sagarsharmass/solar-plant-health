# Solar Plant Health Check

A small web app that shows the health of 5 solar plants from 90 days of daily data, flags problems, and explains them in plain language.

- **Backend:** Python, FastAPI, pandas
- **Frontend:** plain HTML/CSS/JS 

## Run it locally

```bash
python -m venv .venv && source .venv/bin/activate      # Windows: .venv\Scripts\activate
pip install -r requirements-dev.txt

# Put the real CSV here (it is picked up automatically):
#   data/solar_plant_data.csv
# The real dataset is already included. If that file is missing, a generated sample
# (data/sample_solar_plant_data.csv) is used instead.

uvicorn backend.main:app --reload
# open http://localhost:8000      API docs: http://localhost:8000/docs
pytest                                                   # 25 tests
```

To point at a different file: `SOLAR_CSV_PATH=/path/to/file.csv uvicorn backend.main:app`.

**Deploy (Render, free):** push to GitHub, create a *Web Service* from the repo. `render.yaml` already has the build and start commands. `data/solar_plant_data.csv` is committed, so the deployed app serves the real data.

## Project layout

```
backend/data_loader.py   cleaning + data-quality report
backend/analysis.py      PR, rolling averages, status, issue rules (all thresholds at the top)
backend/main.py          FastAPI routes, serves the frontend
frontend/                index.html, styles.css, app.js
tests/test_app.py        rules, cleaning and API tests
scripts/generate_sample_data.py   sample data with known problems injected
```

## API

| Endpoint | Purpose |
|---|---|
| `GET /plants` | All plants with current status |
| `GET /plants/{id}?start=&end=` | Daily data + detected issues for one plant. `start`/`end` (YYYY-MM-DD) are optional |
| `GET /data-quality` | What the cleaning step found and fixed |
| `GET /health` | Liveness check |

Errors: `404` unknown plant, `400` start after end, `422` malformed date, `500` data file cannot be loaded. Each has a readable `detail` message that the UI shows.

**`GET /plants`** (trimmed to one plant)
```json
[
{
  "id": "S3",
  "capacity_kw": 75.0,
  "status": "Underperforming",
  "pr_7d": 0.454,
  "as_of": "2026-03-31",
  "issue_counts": {
    "sudden_drop": 1,
    "gradual_decline": 0,
    "outage": 0
  },
  "issue_total": 1,
  "headline": "PR dropped sharply from about 22 Feb: the 7-day average fell from 0.77 to 0.45 (41% lower than the 7 days before). Something changed suddenly at the plant."
}
]
```

**`GET /plants/S4?start=2026-03-07&end=2026-03-09`** (trimmed to one day)
```json
{
  "id": "S4",
  "capacity_kw": 30.0,
  "status": "Healthy",
  "pr_7d": 0.743,
  "as_of": "2026-03-31",
  "thresholds": {
    "healthy_min": 0.7,
    "attention_min": 0.6
  },
  "range": {
    "start": "2026-03-07",
    "end": "2026-03-09",
    "min_date": "2026-01-01",
    "max_date": "2026-03-31"
  },
  "issues": [
    {
      "type": "outage",
      "severity": "high",
      "start_date": "2026-03-08",
      "end_date": "2026-03-08",
      "dates": [
        "2026-03-08"
      ],
      "reason": "Produced 0 kWh for 1 day although irradiance was 6.1 kWh/m2. Likely an outage or inverter trip."
    }
  ],
  "daily": [
    {
      "date": "2026-03-08",
      "generation_kwh": 0.0,
      "irradiance": 6.08,
      "cloud_cover": 15.0,
      "temperature": 29.5,
      "pr": null,
      "pr_7d": 0.778,
      "flags": [
        "outage"
      ],
      "data_quality": [],
      "highlight": "outage"
    }
  ]
}
```
The date range only filters what is returned. Analysis always runs on all 90 days, because a rolling average needs history.

## How issue detection works

`PR = generation_kwh / (capacity_kw x irradiance)`, so plants of different sizes and sunny or cloudy days are compared fairly.

1. **Skip unreliable days.** Days with irradiance below 1.0 are ignored for PR. Outage days are left out of the average and flagged separately. Rows with missing values have no PR.
2. **7-day rolling PR average** over calendar days (a missing day in the CSV does not stretch the window). At least 3 usable days are needed in a window.
3. **Status** from the latest 7-day average: 0.70 or above Healthy, 0.60 to 0.70 Needs attention, below 0.60 Underperforming.
4. **Outage:** 0 kWh on a day with irradiance of 1.0 or more. Consecutive days are grouped into one issue.
5. **Sudden drop:** the 7-day average is more than 20% below the 7-day average from a week earlier. Consecutive flagged days become one issue.
6. **Gradual decline:** average PR of the last 14 days is more than 10% below the first 14 days, and no sudden drop was found for that plant.

Each issue carries a short plain-language `reason` with the actual numbers.

### Refinements I made (and why)

- **Sudden drops are back-dated.** The rolling average only crosses the 20% line a few days after the real drop. The issue starts at the first day where daily PR was clearly below the old level (more than 10% under it), so the highlighted days match what happened. In the sample data the real drop is day 55 and the issue starts on exactly that day.
- **Long outage means Underperforming.** If a plant has 3 or more outage days in the last 7 and too few usable days left, the average would be empty. It is shown as Underperforming instead of "No data".
- **PR above 1.0 is treated as bad data.** It cannot physically happen, so the day is excluded from PR and flagged.

## How bad data is handled

Bad values are never guessed. They become missing, the row is tagged, and `/data-quality` reports counts. The UI shows tagged days in grey and lists the reason in the chart tooltip.

| Problem | What happens |
|---|---|
| Duplicate (site, date) rows | One kept (the one with the most values filled in) |
| Messy `site_id` (spaces, lowercase) | Trimmed and upper-cased |
| Unparseable date or empty site | Row dropped |
| Text in a number column | Treated as missing |
| Missing generation / irradiance | Kept as a gap, no PR for that day, flagged |
| Negative generation | Set to missing, flagged |
| Irradiance below 0 or above 12 | Set to missing, flagged |
| Cloud cover outside 0-100, temperature outside -50 to 70 | Set to missing, flagged |
| Generation above capacity x 24 h | Set to missing, flagged |
| Capacity zero or missing | Filled with that plant's median capacity, flagged |
| PR above 1.0 | Excluded from PR, flagged |

## How I used AI tools

_Edit this to match what you actually did before submitting._ I used Claude (Anthropic) to generate the first version of the backend, frontend, tests and this README. I then read through the code, ran the tests and the app, and can explain and change any part of it (thresholds are all constants at the top of `backend/analysis.py`).

## What I would improve with more time

- Compare each plant to its own normal level so a plant that has been low since day one (for example, dirty panels) is flagged, not just a plant that changed.
- Temperature-correct PR so hot weather does not look like decline.
- Alerts (email or Slack) when a plant changes status.
- Persist results in a database instead of recomputing from the CSV at startup, and support new data arriving daily.
- Add browser tests for the UI, and a table view of the daily rows.

## One case where the detection can be wrong

**Seasonal heat can trigger a false "gradual decline".** Panels lose efficiency when hot. If the first 14 days of the dataset are in a cool spell and the last 14 are in a heatwave, PR can fall more than 10% and the plant is flagged as declining even though nothing is wrong with it. The rule compares two windows and cannot tell a slow fault from a weather-driven change. Temperature-corrected PR (above) would fix this.

A related limit: a plant with a steady PR of 0.55 from day one shows as Underperforming but with no issue listed, because nothing *changed*.

## Results on the provided dataset (Jan to Mar 2026)

| Plant | Status | 7-day PR | Issues found |
|---|---|---|---|
| S1 | Healthy | 0.76 | none |
| S2 | Needs attention | 0.62 | gradual decline (PR 0.77 to 0.62) |
| S3 | Underperforming | 0.45 | sudden drop from 22 Feb |
| S4 | Healthy | 0.74 | 6 single-day outages |
| S5 | Healthy | 0.75 | none (but 4 rows of bad data cleaned) |

The cleaning step removed 2 duplicate rows and neutralised 10 bad or missing readings (missing values, a negative generation, an impossible irradiance, an impossible cloud cover, and a generation above capacity).
