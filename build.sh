#!/usr/bin/env bash
# Собирает и запускает LinkedIn Jobs: админка Django + PostgreSQL.
# Один скрипт для чистой машины: ждёт движок Docker, создаёт .env, дотягивает
# базовые образы с повторами и поднимает оба контейнера.
#   ./build.sh            обычный запуск
#   ./build.sh --rebuild  пересборка без кеша
#   ./build.sh --reset    снести базу и начать с нуля
#   ./build.sh --down     остановить
#   ./build.sh --logs     смотреть логи
set -euo pipefail
cd "$(dirname "$0")"

REBUILD=0; RESET=0; DOWN=0; LOGS=0
for arg in "$@"; do
  case "$arg" in
    --rebuild) REBUILD=1 ;;
    --reset)   RESET=1 ;;
    --down)    DOWN=1 ;;
    --logs)    LOGS=1 ;;
    *) echo "Неизвестный аргумент: $arg" >&2; exit 2 ;;
  esac
done

step() { printf '\n==> %s\n' "$1"; }
ok()   { printf '    %s\n' "$1"; }
warn() { printf '    %s\n' "$1" >&2; }

# --- Docker ------------------------------------------------------------
step 'Проверяю Docker'
command -v docker >/dev/null 2>&1 || {
  echo 'Docker не найден. Установите Docker: https://docs.docker.com/get-docker/' >&2
  exit 1
}

engine_version() { docker version --format '{{.Server.Version}}' 2>/dev/null; }

SERVER="$(engine_version || true)"
if [ -z "$SERVER" ]; then
  warn 'Движок Docker не отвечает, жду до 4 минут (запустите Docker, если он не запущен)'
  for _ in $(seq 1 80); do
    SERVER="$(engine_version || true)"
    [ -n "$SERVER" ] && break
    sleep 3
  done
fi
[ -n "$SERVER" ] || { echo 'Движок Docker так и не поднялся. Запустите Docker и повторите.' >&2; exit 1; }
ok "docker $SERVER"

if docker compose version >/dev/null 2>&1; then
  dc() { docker compose "$@"; }
elif command -v docker-compose >/dev/null 2>&1; then
  dc() { docker-compose "$@"; }
else
  echo 'Не найден ни "docker compose", ни "docker-compose".' >&2
  exit 1
fi

# --- Сервисные режимы --------------------------------------------------
if [ "$DOWN" = 1 ]; then
  step 'Останавливаю контейнеры'
  dc down
  ok 'Остановлено. База сохранена (том pgdata цел).'
  exit 0
fi
if [ "$LOGS" = 1 ]; then
  dc logs -f --tail 200
  exit 0
fi

# --- .env --------------------------------------------------------------
step 'Проверяю .env'
if [ ! -f .env ]; then
  cp .env.example .env
  ok '.env создан из .env.example (логин admin / пароль admin)'
else
  ok '.env уже есть - не трогаю'
fi

if [ "$RESET" = 1 ]; then
  step 'Сношу контейнеры и базу'
  dc down -v || true
  ok 'Том с данными удалён.'
fi

# --- Базовые образы ----------------------------------------------------
# Тянем отдельно и с повторами: у Docker Desktop внутренний прокси после
# старта какое-то время таймаутит Docker Hub, и сборка падает на шаге
# "load metadata for ...".
base_images() {
  {
    [ -f Dockerfile ] && sed -nE 's/^[[:space:]]*FROM[[:space:]]+([^[:space:]]+).*/\1/p' Dockerfile
    [ -f docker-compose.yml ] && sed -nE 's/^[[:space:]]*image:[[:space:]]*([^[:space:]#]+).*/\1/p' docker-compose.yml \
      | grep -v 'linkedin-jobs'
  } | awk 'NF' | sort -u
}

pull_image() {
  local image="$1" attempts=6 i wait
  if [ "$REBUILD" != 1 ] && docker image inspect "$image" >/dev/null 2>&1; then
    ok "$image - уже есть"
    return 0
  fi
  for i in $(seq 1 "$attempts"); do
    if docker pull "$image" >/dev/null 2>&1; then
      ok "$image - скачан"
      return 0
    fi
    if [ "$i" -lt "$attempts" ]; then
      wait=$(( i * 10 )); [ "$wait" -gt 40 ] && wait=40
      warn "$image - попытка $i из $attempts не удалась, жду ${wait} с (прокси Docker прогревается)"
      sleep "$wait"
    fi
  done
  return 1
}

step 'Проверяю базовые образы'
MISSING=""
IMAGES="$(base_images)"
[ -n "$IMAGES" ] || IMAGES=$'python:3.12-slim\npostgres:16-alpine'
while IFS= read -r image; do
  [ -n "$image" ] || continue
  pull_image "$image" || MISSING="$MISSING $image"
done <<< "$IMAGES"
if [ -n "$MISSING" ]; then
  echo "Не удалось скачать:$MISSING. Проверьте интернет и что Docker полностью запустился, затем повторите." >&2
  exit 1
fi

# --- Сборка ------------------------------------------------------------
step 'Собираю образ'
BUILT=0
for i in 1 2 3; do
  if [ "$REBUILD" = 1 ]; then
    dc build --no-cache --pull && { BUILT=1; break; }
  else
    dc build && { BUILT=1; break; }
  fi
  [ "$i" -lt 3 ] && { warn "Сборка не удалась (попытка $i из 3), жду 15 с"; sleep 15; }
done
[ "$BUILT" = 1 ] || { echo 'Сборка образа не удалась. Смотрите вывод выше.' >&2; exit 1; }

# --- Запуск ------------------------------------------------------------
step 'Поднимаю контейнеры (web + db)'
UP=0
for i in 1 2 3; do
  dc up -d && { UP=1; break; }
  [ "$i" -lt 3 ] && { warn "Запуск не удался (попытка $i из 3), жду 15 с"; sleep 15; }
done
[ "$UP" = 1 ] || { echo 'Не удалось поднять контейнеры. Смотрите вывод выше.' >&2; exit 1; }

# --- Ожидание админки --------------------------------------------------
env_value() {
  local name="$1" fallback="$2" value
  value="$(grep -E "^[[:space:]]*${name}[[:space:]]*=" .env | tail -1 | cut -d= -f2- | tr -d '[:space:]')"
  printf '%s' "${value:-$fallback}"
}

PORT="$(env_value WEB_PORT 8000)"
URL="http://localhost:${PORT}"

step "Жду админку на ${URL}"
READY=0
for _ in $(seq 1 90); do
  if curl -fsS --max-time 3 "${URL}/healthz" >/dev/null 2>&1; then READY=1; break; fi
  sleep 2
done

if [ "$READY" != 1 ]; then
  warn 'Админка не ответила за 3 минуты. Логи:'
  dc logs --tail 60 web >&2
  exit 1
fi

cat <<EOF

  Готово.

  Админка:  ${URL}/admin/
  Логин:    $(env_value DJANGO_SUPERUSER_USERNAME admin)
  Пароль:   $(env_value DJANGO_SUPERUSER_PASSWORD admin)

  Логи:     ./build.sh --logs
  Стоп:     ./build.sh --down
  С нуля:   ./build.sh --reset

EOF
