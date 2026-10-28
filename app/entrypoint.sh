#!/bin/sh
set -e

DB_HOST="${POSTGRES_HOST:-db}"
DB_PORT="${POSTGRES_PORT:-5432}"

echo "==> Жду PostgreSQL на ${DB_HOST}:${DB_PORT}"
python - "$DB_HOST" "$DB_PORT" <<'PY'
import socket
import sys
import time

host, port = sys.argv[1], int(sys.argv[2])
deadline = time.time() + 90
while time.time() < deadline:
    try:
        with socket.create_connection((host, port), timeout=3):
            print("    PostgreSQL отвечает")
            sys.exit(0)
    except OSError:
        time.sleep(1)
print("    PostgreSQL не поднялся за 90 секунд", file=sys.stderr)
sys.exit(1)
PY

echo "==> Применяю миграции"
python manage.py migrate --noinput

echo "==> Суперпользователь"
python manage.py bootstrap_admin

echo "==> Снимаю подвисшие запуски"
python manage.py reset_stuck_runs

echo "==> Собираю статику"
python manage.py collectstatic --noinput --clear >/dev/null

echo "==> Старт: $*"
exec "$@"
