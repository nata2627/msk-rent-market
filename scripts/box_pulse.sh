#!/bin/bash
# Пульс бокса: раз в пять минут строка о памяти и диске, ранняя тревога и
# отметка «жив» на GitHub.
#
# Зачем. 29.10.2026 около 08:15 Mac mini завис целиком: перестали
# пускать ssh хоста и обоих боксов, бот молчал, и тревоги не было ни
# одной. Все проверки живут на самой машине, а зависшая машина ничего не
# отправит. О беде узнали через шесть часов, спросив сами. После
# принудительной перезагрузки корень диска хоста не прошел проверку, и
# загрузка встала до сети. Что съело машину, понять не удалось: журналы
# хоста из бокса не видны, а счетчики бокса обнулились перезагрузкой.
#
# Отсюда три дела, все дешевые:
#
# 1. Строка в data/logs/box_health.log каждые пять минут. Давление на
#    память и диск в /proc/pressure показывает весь хост, а не только
#    бокс, поэтому в следующий раз будет видно, что росло и с какого
#    часа, даже если сама беда случится у соседа.
# 2. Ранняя тревога в телеграм, пока машина еще способна ее отправить:
#    память бокса у предела, хост ждет память или диск.
# 3. Отметка на GitHub, ветка heartbeat, раз в пятнадцать минут. Ее
#    читает .github/workflows/watchdog.yml на серверах GitHub и пишет в
#    телеграм, если отметки нет больше часа. Это единственная проверка,
#    которая переживает смерть машины.
#
# Ветка heartbeat всегда из одного коммита без родителя и с пустым
# деревом: пушится с --force, история main не растет, сборки на нее не
# реагируют (tests и pages слушают только main).
#
# Не падает и не держит замок обхода: пульс обязан идти и во время
# обхода, именно тогда память и кончается.

set -uo pipefail
export TZ=Europe/Moscow

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
LOG_DIR="$REPO/data/logs"
mkdir -p "$LOG_DIR"
HEALTH="$LOG_DIR/box_health.log"
SITE="${SITE_CLONE:-$HOME/rent_site}"
notify() { bash "$REPO/scripts/box_notify.sh" "$@" >/dev/null 2>&1; }

# Пороги. Память бокса: лимит 3072 МБ, memory.high 2765 МБ, выше него
# ядро тормозит процессы бокса, и обход ползет. Давление хоста: доля
# времени за минуту, когда все задачи стояли в ожидании памяти или
# диска. Пять процентов на памяти уже чувствуется, на диске при обходе
# бывает до трех, сорок значит, что машина почти стоит.
MEM_WARN_MB="${PULSE_MEM_WARN_MB:-2600}"
PSI_MEM_WARN="${PULSE_PSI_MEM_WARN:-5}"
PSI_IO_WARN="${PULSE_PSI_IO_WARN:-40}"

psi() { awk -v k="$2" '$1 == k { split($3, a, "="); print a[2] }' "/proc/pressure/$1" 2>/dev/null; }
# Занятая память без неактивного кэша файлов: memory.current считает и
# кэш, который ядро отдаст по первому требованию. 29.10.2026 копия базы
# читала 2,7 ГБ файлов, memory.current показал 2763 МБ и поднял ложную
# тревогу, а занято процессами было около 940 МБ.
INACTIVE=$(awk '$1 == "inactive_file" { print $2 }' /sys/fs/cgroup/memory.stat 2>/dev/null)
MEM_MB=$(( ( $(cat /sys/fs/cgroup/memory.current 2>/dev/null || echo 0) - ${INACTIVE:-0} ) / 1048576 ))
OOM=$(awk '$1 == "oom_kill" { print $2 }' /sys/fs/cgroup/memory.events 2>/dev/null)
CHROME_MB=$(ps -eo rss=,comm= 2>/dev/null | awk '$2 ~ /chrom/ { s += $1 } END { print int(s / 1024) }')
MEM_SOME=$(psi memory some); MEM_FULL=$(psi memory full); IO_FULL=$(psi io full)
FREE_MB=$(df -Pm "$REPO" | awk 'NR == 2 { print $4 }')
LOAD=$(cut -d' ' -f1-3 /proc/loadavg)

echo "$(date '+%d.%m %H:%M') бокс ${MEM_MB} МБ, chrome ${CHROME_MB} МБ, oom ${OOM:-?}; хост память ${MEM_SOME:-?}/${MEM_FULL:-?}%, диск ${IO_FULL:-?}%; нагрузка ${LOAD}; свободно ${FREE_MB} МБ" >>"$HEALTH"

# Журнал пульса живет две недели: 288 строк в сутки.
if [ "$(wc -l <"$HEALTH")" -gt 4100 ]; then
  tail -n 4032 "$HEALTH" >"$HEALTH.tmp" && mv "$HEALTH.tmp" "$HEALTH"
fi

gt() { awk -v a="${1:-0}" -v b="$2" 'BEGIN { exit !(a > b) }'; }
if [ "$MEM_MB" -gt "$MEM_WARN_MB" ]; then
  notify mem-box "Память бокса ${MEM_MB} из 3072 МБ, Chrome ${CHROME_MB} МБ
Еще немного, и ядро начнет убивать процессы. /status покажет, что идет. /unlock не нажимать, пока не ясно, что замок держит зависший обход: он же держит копию базы и выгрузку."
fi
if gt "$MEM_FULL" "$PSI_MEM_WARN"; then
  notify mem-host "Хост Mac mini ждет память: ${MEM_FULL}% времени за минуту
Это вся машина, не только бокс. Если сообщения прекратятся, машина зависла."
fi
if gt "$IO_FULL" "$PSI_IO_WARN"; then
  notify io-host "Хост Mac mini стоит на диске: ${IO_FULL}% времени за минуту
Если сообщения прекратятся, машина зависла."
fi

# Отметка на GitHub раз в пятнадцать минут, а не каждые пять: этого
# хватает сторожу, который ходит раз в полчаса, и не дергает GitHub зря.
if [ $(( 10#$(date +%M) % 15 )) -lt 5 ] && [ -d "$SITE/.git" ]; then
  export GIT_SSH_COMMAND="ssh -i $HOME/.ssh/rent_site_deploy -o IdentitiesOnly=yes -o BatchMode=yes -o ConnectTimeout=20"
  EMPTY=$(git -C "$SITE" mktree </dev/null)
  C=$(git -C "$SITE" -c user.name=box -c user.email=box@localhost \
      commit-tree "$EMPTY" -m "пульс $(date '+%d.%m.%Y %H:%M') МСК, бокс ${MEM_MB} МБ, свободно ${FREE_MB} МБ")
  timeout 60 git -C "$SITE" push -q -f origin "$C:refs/heads/heartbeat" >/dev/null 2>&1 \
    || echo "$(date '+%d.%m %H:%M') отметка на GitHub не ушла" >>"$HEALTH"
fi
exit 0
