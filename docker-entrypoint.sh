#!/bin/sh
set -eu

until python - <<'PY'
import socket
import sys
sock = socket.socket()
sock.settimeout(2)
try:
    sock.connect(("db", 5432))
except OSError:
    sys.exit(1)
finally:
    sock.close()
PY

do
  sleep 2
done

python -c "from app.etl.pipeline import ingest_directory; ingest_directory('/data')"
exec python -m uvicorn app.api:app --host 0.0.0.0 --port 8000
