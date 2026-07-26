FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    BATTERY_DATA_ROOT=/data

WORKDIR /app

# Copied first so the dependency layer is cached independently of the source.
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

# Only the application: the raw data is bind-mounted and the SQLite artifacts are
# not needed in the image.
COPY app ./app

# Neither the ETL nor the API needs to write to the filesystem.
RUN useradd --create-home --uid 1000 battery
USER battery

EXPOSE 8000

CMD ["python", "-m", "uvicorn", "app.api:app", "--host", "0.0.0.0", "--port", "8000"]
