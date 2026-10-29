#!/bin/bash
# Хвост дня на макбуке: витрина на GitHub, облачная копия, итог дня в
# телеграм. Временно, пока у Mac mini нет нового диска.
#
# Зачем. 29.10.2026 SSD Mac mini начал терять секторы, и сбор переехал на
# макбук до замены диска. scripts/daily.sh здесь собирает срез и витрины,
# а то, что на боксе делали отдельные задачи cron (box_site_export.sh,
# box_backup.sh, box_report.sh), делает этот скрипт после среза.
#
# Отличия от бокса: витрина считается локальным .venv, а не образом
# rent-collector, а клон сайта ~/rent_site пушит обычным ключом
# макбука, а не деплой-ключом бокса. GNU timeout и stat скриптам бокса
# дает coreutils из Homebrew, путь задан в plist.
#
# Ставится launchd на 14:00, через три часа после среза в 11:00. Если
# срез еще идет (data/daily.lock), ждет до часа.

set -uo pipefail
export TZ=Europe/Moscow
export PATH="/opt/homebrew/opt/coreutils/libexec/gnubin:/opt/homebrew/bin:$PATH"

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SITE="$HOME/rent_site"
cd "$REPO" || exit 1
LOG_DIR="$REPO/data/logs"; mkdir -p "$LOG_DIR"
exec >>"$LOG_DIR/mac_tail_$(date +%Y%m).log" 2>&1
say() { echo "$(date '+%d.%m %H:%M:%S') === $*"; }
notify() { bash "$REPO/scripts/box_notify.sh" "$@" >/dev/null 2>&1; }

for _ in $(seq 1 60); do
  [ -d "$REPO/data/daily.lock" ] || break
  sleep 60
done
[ -d "$REPO/data/daily.lock" ] && { say "Срез идет больше часа после 14:00, хвост пропускаю"; exit 0; }

say "Витрина"
if git -C "$SITE" pull -q --rebase --autostash \
  && RESULTS_DIR="$SITE/results" SITE_OUT="$SITE/site/data/site.json" "$REPO/.venv/bin/python" -m src.site_data; then
  git -C "$SITE" add site/data/site.json results/price_model.json results/price_runs.csv 2>/dev/null
  if git -C "$SITE" diff --cached --quiet; then
    say "Нечего выгружать"
  elif git -C "$SITE" commit -q -m "Данные витрины за $(date +%d.%m.%Y)" && git -C "$SITE" push -q origin HEAD:main; then
    say "Выгружено"
  else
    notify site-push "Витрина не выгрузилась на GitHub с макбука, сайт показывает прошлую дату."
  fi
else
  notify site-fail "Витрина не пересчиталась на макбуке, сайт показывает прошлую дату."
fi

say "Облачная копия"
export RCLONE_CONFIG="$HOME/.config/rent_backup/rclone.conf"
bash "$REPO/scripts/box_backup.sh" || say "копия упала, подробности в backup_*.log"

say "Итог дня"
bash "$REPO/scripts/box_report.sh" >/dev/null 2>&1 || say "итог дня не ушел"
# Отметка «жив» для сторожа на GitHub, раз в сутки после хвоста: порог
# сторожа на время макбука 30 часов (переменная BEAT_LATE_H).
C=$(git -C "$SITE" commit-tree "$(git -C "$SITE" mktree </dev/null)" -m "пульс $(date '+%d.%m.%Y %H:%M') МСК, макбук")
git -C "$SITE" push -q -f origin "$C:refs/heads/heartbeat" || say "отметка на GitHub не ушла"
say "Конец"
