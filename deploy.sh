#!/usr/bin/env bash
# Деплой на VPS: забрать код, пересобрать, поднять, убедиться, что бот живой.
#
#   ./deploy.sh            обычное обновление
#   ./deploy.sh --ingest   плюс перезагрузка базы знаний после правки knowledge/
#
# Миграции сюда не входят: строку роли postgres на сервер не кладём, миграции
# применяются с ноутбука (решение этапа 2).
set -euo pipefail

# Тело в функции нарочно: git pull ниже переписывает этот же файл, а bash
# дочитывает скрипт по ходу выполнения. Функция разбирается целиком заранее.
main() {
  cd "$(dirname "$0")"

  if [ ! -f .env ]; then
    echo "нет .env: скопируйте .env.example и заполните" >&2
    exit 2
  fi

  # docker compose подставляет переменные в значения .env, поэтому секрет с
  # долларом доедет до контейнера обрезанным. Ловим это до деплоя.
  if grep -qE '^[A-Z_]+=[^#]*\$' .env; then
    echo "в .env есть значение со знаком доллара:" >&2
    grep -nE '^[A-Z_]+=[^#]*\$' .env | cut -d= -f1 >&2
    echo "docker compose испортит его при подстановке. Перевыпустите секрет или удвойте доллар: \$\$" >&2
    exit 2
  fi

  echo "== забираю код"
  git pull --ff-only

  echo "== собираю образы"
  docker compose build

  echo "== поднимаю"
  docker compose up -d
  # Caddyfile примонтирован отдельным файлом, а git pull заменяет его целиком:
  # у файла появляется новый inode, и контейнер продолжает видеть старую версию.
  # Поэтому Caddy пересоздаём всегда. Сертификаты лежат в томе и переживают это.
  docker compose up -d --force-recreate caddy

  echo "== жду, пока бот станет здоровым"
  for i in $(seq 1 60); do
    status=$(docker inspect -f '{{.State.Health.Status}}' "$(docker compose ps -q bot)" 2>/dev/null || echo starting)
    [ "$status" = healthy ] && break
    sleep 2
  done
  if [ "${status:-}" != healthy ]; then
    echo "бот не стал здоровым, логи:" >&2
    docker compose logs --tail 40 bot >&2
    exit 1
  fi

  if [ "${1:-}" = "--ingest" ]; then
    echo "== загружаю базу знаний"
    docker compose exec -T bot python -m app.ingest
  fi

  echo "== готово"
  docker compose ps
}

main "$@"
