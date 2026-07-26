# Breathe Batteries — Data Engineer Tech Test

Thank you for taking the time to complete this task. We're excited to be at this stage with you.

---

## Before you start

- Spend **no more than 4 hours** on this. The scope is deliberately ambitious — we expect you to use AI tools to get through it. During the interview we'll ask you to walk through your code, so make sure you can explain every decision.
- Push your solution to a **public GitHub repository** before the interview. Share the link with us ahead of time.
- **Python is preferred**, but you may use any stack you're comfortable with.
- You do not need to spin up any cloud infrastructure. Everything should run locally on your machine.

---

## Context

Breathe's internal platform, **Nexus**, ingests time-series data from battery cyclers across our lab. Each cycler manufacturer exports data in its own proprietary format — different column names, different units, different file structures. A core part of our platform is the pipeline that reads these raw exports, normalises them into a common schema, and stores them for analysis and reporting.

Your task is to build a simplified version of that pipeline.

---

## The data

The `data/` folder contains raw exports from **three different battery cyclers**:

| Folder | Cycler | Format | Files |
|---|---|---|---|
| `data/cycler_a_biologic/` | BioLogic SP-150 | Tab-separated text | 1 file |
| `data/cycler_b_neware/` | Neware BTS4000 | CSV | 10 files |
| `data/cycler_c_novonix/` | Novonix UHPC | CSV | 1 file |

Each file represents a single battery test. The data includes some or all of: time, voltage, current, temperature, capacity, cycle index.

These are **real-world-style exports** — they have not been pre-cleaned. You should expect:

- Different column names for the same physical quantity across cyclers
- Possible unit inconsistencies within or between files
- Missing values
- Rows that cannot be parsed
- Other surprises

Your pipeline should handle these gracefully.

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
