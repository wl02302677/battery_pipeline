Plan: Battery data ETL and API
The repository currently contains only the dataset and the task README, so the implementation will start from scratch. The work should focus on building a robust local pipeline that ingests all three cycler formats, normalizes them into a common schema, stores the data, and exposes it through a small API.

1. Set up PR merge protection with GitHub Actions
Create a workflow that runs on pull requests and pushes to main so tests must pass before merge.
The workflow should install dependencies, run the test suite, and fail fast on regressions.
2. Confirm the data contract and edge cases
Review the raw formats in the README and sample files.
Expect each cycler to use different column names and units, plus malformed or missing values.
Define normalization rules:
Use a derived test ID such as biologic_cell_001 or neware_cell_003 from the file path.
Map all rows to a shared schema with fields for test_id, cycler, timestamp_s, voltage_v, current_a, temperature_c, and cycle_index.
Convert units where practical, such as mA to A and mAh to Ah if present, while documenting assumptions.
Skip rows that are clearly unusable and log the reason instead of failing the entire run.
3. Create the implementation skeleton
Use Python as the primary implementation language.
Add separate modules for ingestion/parsing, normalization, database persistence, API endpoints, and tests.
Keep the first version simple and local-first so it runs quickly without cloud services.
4. Build the ETL pipeline
Walk the data directories recursively and discover all files.
Infer the cycler from the folder name and derive the test ID from the relative path.
Parse each file according to its own format:
BioLogic: tab-separated text with unusual column names
Neware: CSV with a straightforward time/voltage/current/capacity layout
Novonix: CSV with a different column set and time fields
Normalize each row into the common schema and insert it into the database.
Handle malformed rows gracefully by dropping them and recording a summary of skipped records.
5. Define the database model
Create a relational schema with at least a tests table and a timeseries table.
Make test_id the stable identifier used throughout the API.
Switch to PostgreSQL for the main runtime so the service matches a production-style deployment, while keeping SQLite as a lightweight fallback for local tests.
Use environment-based configuration via DATABASE_URL and a container-friendly connection layer.
6. Implement the API
Build a lightweight REST API with:
GET /tests
GET /tests/{test_id}/timeseries
GET /tests/{test_id}/cycles
Keep responses compact and practical for inspection and later frontend use.
7. Add tests before the full implementation
Write tests for parsing and normalization logic first.
Cover at least one file from each cycler and one malformed-row case.
Add API tests for the required endpoints.
8. Add containerization and documentation
Create a Docker Compose setup that starts the database and API with a single command.
Document the assumptions around units, missing values, and skipped rows in the repository README.
9. Verification plan
Run the ETL locally and confirm that all files are ingested.
Verify that the database contains rows for every discovered test.
Exercise each API endpoint and confirm the response structure.
Run the test suite and ensure it passes before handing off the work.


TODO: 
0. the postgresql should be created first, and then in the docker, run the pipeline to ingest files, transform, and then save into postgreSQL
Then last step is run the api to query from postgreSQL
1. catch error message, like web error code
<!-- 2. simple front-end
3. document generated automically -->
