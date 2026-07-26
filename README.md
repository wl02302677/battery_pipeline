# Battery Pipeline Prototype

This repository contains a local ETL prototype for battery cycler data.

## What it does

- Ingests raw exports from the three provided cycler folders
- Normalizes each row into a shared schema
- Persists the data in a local SQLite database
- Exposes the data through a lightweight FastAPI service
- Skips malformed rows without failing the whole run and reports them in the ingestion summary

## Run locally

```bash
python -m pip install -r requirements.txt
python -c "from app.etl.pipeline import ingest_directory; ingest_directory('data', db_path='battery.sqlite3')"
uvicorn app.api:app --reload
```

Then open:
- http://localhost:8000/tests
- http://localhost:8000/tests/biologic_cell_001/timeseries
- http://localhost:8000/tests/novonix_cell_001/cycles

## Run with Docker Compose

```bash
docker compose up --build
```

The container will ingest the data from the local data folder into /data/battery.sqlite3 and expose the API on port 8000.

## Assumptions and handling rules

- Test IDs are derived from the file path, such as biologic_cell_001 or neware_cell_003.
- Units are normalized where practical: mA is converted to A and hours are converted to seconds.
- Missing or malformed values are skipped rather than failing the entire run.
- The ETL uses SQLite for a simple local-first setup, which is sufficient for the prototype and interview scenario.
- Rows without a usable timestamp, voltage, or current are omitted from the database and counted as skipped.

## Verification

Run the test suite with:

```bash
pytest -q
```

The repository also includes a GitHub Actions workflow that runs the tests on push and pull requests.

---

## Part 1 — Build the pipeline

### 1. ETL pipeline

Write a pipeline that reads all files across all three cycler folders and loads them into a database.

Note that `cell_001` appears in more than one cycler folder. Use a **test ID** derived from the file path to uniquely identify each test — for example `biologic_cell_001` or `neware_cell_003`. This becomes the primary key for the API.

The pipeline should map each file to a common schema containing at least:

| Field | Type | Notes |
|---|---|---|
| `test_id` | string | Derived from file path, e.g. `biologic_cell_001` |
| `cycler` | string | `biologic`, `neware`, or `novonix` |
| `timestamp_s` | float | Seconds since test start |
| `voltage_v` | float | Volts |
| `current_a` | float | Amps |
| `temperature_c` | float | Celsius — include if present in source |
| `cycle_index` | int | Include if present in source |

Document any assumptions you make about units or how you handle missing or malformed data.

### 2. Database

Store the normalised data in a database of your choice (PostgreSQL recommended).

### 3. REST API

Build a REST API on top of the database with at least these endpoints:

| Endpoint | Description |
|---|---|
| `GET /tests` | List all available test IDs and their cycler source |
| `GET /tests/{test_id}/timeseries` | Return time, voltage, current, and temperature for a test |
| `GET /tests/{test_id}/cycles` | Return per-cycle summary statistics (e.g. capacity, min/max voltage) |

### 4. Frontend (optional)

Build a simple dashboard that visualises the data from one or more tests. Focus on the pipeline and API first — this is a bonus.

---

## Part 2 — Interview discussion

During the interview you will have **15 minutes to walk through your code**, followed by questions. We're interested in:

- The decisions you made and why
- How your pipeline handles errors and edge cases
- What you would do next with more time

Come prepared to discuss your schema design, how you'd scale the pipeline to handle many more files and cycler types, and how you'd detect and surface data quality issues automatically.

### Stretch goal — CI/CD (optional)

Add a GitHub Actions workflow that runs your tests on every push. We use GitLab CI internally — be ready to discuss how you'd adapt it.

---

## Submission checklist

Before the interview, make sure your repo includes:

- [ ] Working ETL pipeline
- [ ] Working API (with the `/tests` endpoints above)
- [ ] A `docker-compose.yml` that starts everything with `docker-compose up` — no further setup or configuration should be required to run your solution
- [ ] A README in your repo explaining any non-obvious decisions or assumptions

---

## Evaluation criteria

| Criterion | What we look for |
|---|---|
| **Schema design** | Does the common schema make sense for time-series battery data? Could it scale? |
| **Pipeline robustness** | Does it handle missing values, bad rows, and unit inconsistencies gracefully? |
| **API design** | Sensible endpoints, correct HTTP semantics, some thought about filtering large datasets |
| **Code quality** | Readable, structured, evidence of testability |
| **Prioritisation** | What did you choose to finish vs. leave rough — and did you document why? |
| **Ownership** | Can you explain every part of what you submitted, including AI-assisted code? |

Good luck — and please reach out if you have any questions.
