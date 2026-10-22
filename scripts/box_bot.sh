#!/bin/bash
# Бот принимает команды с телефона: посмотреть и починить, не доходя до
# консоли.
#
# Зачем. Уведомления научились говорить о беде, но лечение в них было
# написано командой для терминала — а беда случается ровно тогда, когда
# до терминала не добраться: разъезды, чужой город, ночь. Толку от
# сообщения «rm -rf ~/rent_msk/...» с телефона нет никакого. Поэтому
# каждое лечение, которое раньше требовало ssh, теперь есть командой
# бота, и сообщение о беде её называет.
#
# Как это возможно при NAT. Бокс снаружи недоступен, и webhook телеграма
# до него не дотянется. Но наружу бокс ходит свободно, поэтому бот не
# ждёт входящих, а сам раз в минуту спрашивает телеграм, не написали ли
# ему. Ни одного открытого порта это не добавляет.
#
# Ставится в cron рядом с остальным (см. scripts/box_crontab):
#   * * * * * /home/dev2/rent_msk/scripts/box_bot.sh
#
# Отдельного демона намеренно нет: в этом проекте уже был зависший
# процесс, который держал замок девятнадцать часов и чуть не съел день.
# Минутный запуск из cron не зависает в принципе — он живёт секунду.
#
# Проверить команду, не трогая телефон:
#   scripts/box_bot.sh --run /status

set -uo pipefail
export TZ=Europe/Moscow

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO" || exit 1

PG_CONTAINER="rent_pg"
NORM=16500                      # норма сегмента за день, та же, что в box_watch.sh
STATE_DIR="$REPO/data/bot"
LOG_DIR="$REPO/data/logs"
BOT_LOG="$LOG_DIR/box_bot.log"
OFFSET_FILE="$STATE_DIR/offset"
POLL_LOCK="$STATE_DIR/poll.lock"
mkdir -p "$STATE_DIR" "$LOG_DIR"

note() { echo "$(date '+%d.%m %H:%M') $*" >>"$BOT_LOG"; }

from_env() {
  [ -f "$REPO/.env" ] || return 0
  sed -n "s/^[[:space:]]*$1=//p" "$REPO/.env" | tail -n 1 \
    | sed -e 's/[[:space:]]*$//' -e 's/^"\(.*\)"$/\1/' -e "s/^'\(.*\)'\$/\1/"
}

TOKEN="${NOTIFY_TG_TOKEN:-$(from_env NOTIFY_TG_TOKEN)}"
OWNER="${NOTIFY_TG_CHAT:-$(from_env NOTIFY_TG_CHAT)}"

# Ответы уходят через общий отправщик: там уже есть разметка, маскировка
# секретов в журнале и жёсткие сроки. Ключ «bot» с --always, потому что
# ответ на вопрос глушить ограничением частоты нельзя.
reply() { bash "$REPO/scripts/box_notify.sh" --always bot "$1" >/dev/null 2>&1; }

# Запрос к базе под таймаутом: при голоде по памяти демон Docker перестаёт
# отвечать вовсе, и бот не имеет права повиснуть вместе с ним.
q() {
  timeout 30 docker exec -i "$PG_CONTAINER" psql -qtA -F'|' -U rent -d rent -c "$1" 2>/dev/null
}

crawl_running() { [ -d "$REPO/data/box.lock" ]; }

collected_today() {
  q "select count(distinct s.offer_id)
       from raw.offer_snapshot s
       join core.offer o using (source, offer_id)
      where s.snapshot_date = current_date and o.rooms_count in (1, 2);" | tr -d '[:space:]'
}

# ---------------------------------------------------------------------
# Команды
# ---------------------------------------------------------------------

cmd_help() {
  reply "Что я умею
посмотреть

/status — что сейчас с панелью и машиной
/flats — верх подбора под вашими фильтрами
/filter — посмотреть и поменять фильтры подбора
/market — числа рынка за сегодня
/log — хвост журнала прогона
/pulse — прислать итог дня прямо сейчас

починить

/clean — освободить место на диске
/unlock — снять заклинивший замок
/catchup — догнать сегодняшний день
/dbup — поднять базу, если она упала

Команды лечения сами откажутся работать, если идёт обход: второй браузер
в том же профиле сломал бы сессию площадки обоим."
}

cmd_status() {
  local seg pct covered run disk mem fresh lock last
  seg="$(collected_today)"; seg="${seg:-0}"
  pct=$(( seg * 100 / NORM ))
  covered="$(q "select coalesce(string_agg(distinct b.rooms, ', '), 'нет')
                  from raw.collection_run r join raw.bucket b using (run_id)
                 where r.source = 'cian' and r.status = 'ok'
                   and r.snapshot_date = current_date and b.error is null;")"
  run="$(q "select run_id || ' ' || mode || ' ' || status || ', начат ' ||
                   to_char(started_at at time zone 'Europe/Moscow', 'DD.MM HH24:MI')
              from raw.collection_run order by run_id desc limit 1;")"
  fresh="$(q "select verdict from marts.data_freshness;")"
  disk="$(df -Pm "$REPO" | awk 'NR==2 {print $4}')"
  mem="$(free -m | awk 'NR==2 {print $7}')"
  if crawl_running; then
    last="$(ls -t "$LOG_DIR"/box_2*.log 2>/dev/null | head -1)"
    lock="идёт обход, последняя строка журнала: $(tail -1 "$last" 2>/dev/null | cut -c1-80)"
  else
    lock="обход не идёт"
  fi
  reply "Состояние на $(date '+%d.%m, %H:%M')

Сегмент за сегодня: $seg из $NORM ($pct%)
Покрытые комнатности: $covered
Витрины: ${fresh:-не ответили}

$lock
Последний прогон: ${run:-неизвестен}

Диск: $disk МБ свободно
Память: $mem МБ доступно"
}

# ---------------------------------------------------------------------
# Фильтры подбора
# ---------------------------------------------------------------------
# Зачем они боту. Балл за цену в проекте перевёрнут намеренно: дешевизна
# на данных оказалась признаком беды, а не выгоды. Поэтому голый верх по
# оценке — это верх по дороговизне: в первом же ответе бота вышли
# квартиры за 175 тысяч и за три миллиона. На дашборде этого не видно,
# потому что там верх живёт под фильтрами.
#
# Колонки и смысл — те же, что у фильтров дашборда (FILTERS в
# scripts/setup_dashboard.py), а базовые условия отбора общие и лежат в
# marts.offer_pick. Своего определения подбора бот не заводит.
#
# Значения хранятся строками ключ=значение в data/bot/filters и живут
# между запусками: фильтр, который надо задавать заново каждый раз, в
# дороге бесполезен.
FILTER_FILE="$STATE_DIR/filters"

filter_get() {
  [ -f "$FILTER_FILE" ] || return 0
  sed -n "s/^$1=//p" "$FILTER_FILE" | tail -n 1
}

filter_set() {
  local key="$1" val="$2" tmp
  tmp="$(mktemp)"
  grep -v "^$key=" "$FILTER_FILE" 2>/dev/null >"$tmp"
  [ -n "$val" ] && echo "$key=$val" >>"$tmp"
  mv "$tmp" "$FILTER_FILE"
}

# Условие для SQL собирается ТОЛЬКО из проверенных значений: всё, что
# пришло из чата, уже прошло через образец в filter_apply. Подстановки
# сырого текста здесь нет ни одной.
filter_where() {
  local w="" v
  v="$(filter_get price)";  [ -n "$v" ] && w="$w and price <= $(( v * 1000 ))"
  v="$(filter_get area)";   [ -n "$v" ] && w="$w and total_area >= $v"
  v="$(filter_get rooms)";  [ -n "$v" ] && w="$w and rooms_count in ($v)"
  v="$(filter_get metro)";  [ -n "$v" ] && w="$w and metro_real_walk_min <= $v"
  v="$(filter_get entry)";  [ -n "$v" ] && w="$w and entry_cost <= $(( v * 1000 ))"
  v="$(filter_get pets)";   [ -n "$v" ] && w="$w and pets_state = '$v'"
  v="$(filter_get park)";   [ -n "$v" ] && w="$w and park_band in ('до 500 м, пешком', '500 м – 1 км')"
  v="$(filter_get mkad)";   [ -n "$v" ] && w="$w and inside_mkad"
  v="$(filter_get district)"; [ -n "$v" ] && w="$w and district = '$v'"
  # Свежесть: умолчание ноль — «была в последней выдаче». Без него каждое
  # восьмое объявление подбора ведёт на снятую публикацию.
  v="$(filter_get seen)"; w="$w and crawls_missed <= ${v:-0}"
  echo "$w and is_active"
}

filter_human() {
  local out="" v
  v="$(filter_get price)";    [ -n "$v" ] && out="$out, цена до $v тыс"
  v="$(filter_get area)";     [ -n "$v" ] && out="$out, площадь от $v м2"
  v="$(filter_get rooms)";    [ -n "$v" ] && out="$out, комнат $v"
  v="$(filter_get metro)";    [ -n "$v" ] && out="$out, метро до $v мин"
  v="$(filter_get entry)";    [ -n "$v" ] && out="$out, сразу до $v тыс"
  v="$(filter_get pets)";     [ -n "$v" ] && out="$out, животные $v"
  v="$(filter_get park)";     [ -n "$v" ] && out="$out, парк в километре"
  v="$(filter_get mkad)";     [ -n "$v" ] && out="$out, внутри МКАД"
  v="$(filter_get district)"; [ -n "$v" ] && out="$out, район $v"
  v="$(filter_get seen)";     [ -n "$v" ] && out="$out, пропущено обходов до $v"
  echo "${out#, }"
}

cmd_filter() {
  local key="$1" val="$2" human districts
  case "$key" in
    "")
      human="$(filter_human)"
      reply "Фильтры подбора

${human:-не заданы — поэтому наверх лезет дорогое}

Задать: /filter цена 90
Убрать один: /filter цена -
Сбросить все: /filter сброс

Что можно: цена (тыс), площадь (м2), комнаты (1 или 1,2),
метро (мин пешком), сразу (тыс, нужно денег сразу),
животные (можно/договор/нельзя), парк (да — в километре),
мкад (да — внутри), район (название), обходы (0, 1, 2)

Колонки и смысл те же, что у фильтров дашборда." ;;
    сброс|reset)
      rm -f "$FILTER_FILE"
      reply "Фильтры сброшены. Остался только один, несбрасываемый:
показываются объявления, встреченные в последнем обходе — иначе каждое
восьмое ведёт на снятую публикацию." ;;
    цена|price)   filter_num price "$val" "цена в тысячах, от 10 до 999" 10 999 ;;
    площадь|area) filter_num area "$val" "площадь в м2, от 10 до 300" 10 300 ;;
    метро|metro)  filter_num metro "$val" "минут пешком, от 1 до 60" 1 60 ;;
    сразу|entry)  filter_num entry "$val" "сумма в тысячах, от 10 до 9999" 10 9999 ;;
    обходы|seen)  filter_num seen "$val" "пропущено обходов, от 0 до 10" 0 10 ;;
    комнаты|rooms)
      case "$val" in
        -) filter_set rooms ""; reply "Фильтр по комнатам убран." ;;
        [1-5]|[1-5],[1-5]|[1-5],[1-5],[1-5])
           filter_set rooms "$val"; reply "Комнат: $val
Сейчас отбор: $(filter_human)" ;;
        *) reply "Комнатность — числа от 1 до 5 через запятую. Например: /filter комнаты 1,2" ;;
      esac ;;
    животные|pets)
      case "$val" in
        -)        filter_set pets ""; reply "Фильтр по животным убран." ;;
        можно)    filter_set pets allowed;    reply "Животные: можно
Сейчас отбор: $(filter_human)" ;;
        договор)  filter_set pets negotiable; reply "Животные: по договорённости
Сейчас отбор: $(filter_human)" ;;
        нельзя)   filter_set pets forbidden;  reply "Животные: нельзя
Сейчас отбор: $(filter_human)" ;;
        *) reply "Животные: можно, договор или нельзя. Например: /filter животные можно" ;;
      esac ;;
    парк|park)
      case "$val" in
        -|нет) filter_set park ""; reply "Фильтр по парку убран." ;;
        да)    filter_set park 1;  reply "Парк: в пределах километра
Сейчас отбор: $(filter_human)" ;;
        *) reply "Парк: да или нет. Рабочий порог — парк от 10 га в километре." ;;
      esac ;;
    мкад|mkad)
      case "$val" in
        -|нет) filter_set mkad ""; reply "Фильтр по МКАД убран." ;;
        да)    filter_set mkad 1;  reply "Только внутри МКАД
Сейчас отбор: $(filter_human)" ;;
        *) reply "МКАД: да или нет." ;;
      esac ;;
    район|district)
      if [ "$val" = "-" ]; then filter_set district ""; reply "Фильтр по району убран."; return; fi
      # Район не подставляется как пришёл: он сверяется со справочником в
      # базе, и в запрос идёт только то, что в нём нашлось.
      districts="$(q "select district from core.offer where source='cian' and district is not null group by 1 having count(*) >= 3;")"
      if printf '%s\n' "$districts" | grep -qxF "$val"; then
        filter_set district "$val"
        reply "Район: $val
Сейчас отбор: $(filter_human)"
      else
        reply "Района «${val}» не знаю. Названия как на ЦИАНе, например: Хамовники,
Раменки, Южнопортовый. Убрать фильтр: /filter район -"
      fi ;;
    *) reply "Такого фильтра нет. Список — /filter" ;;
  esac
}

# Числовой фильтр: значение проверяется образцом и границами, и только
# потом попадает в файл. В SQL оно идёт уже числом.
filter_num() {
  local key="$1" val="$2" help="$3" lo="$4" hi="$5"
  case "$val" in
    -) filter_set "$key" ""; reply "Фильтр «$key» убран.
Сейчас отбор: $(filter_human)"; return ;;
    ''|*[!0-9]*) reply "Нужно число: $help"; return ;;
  esac
  if [ "$val" -lt "$lo" ] || [ "$val" -gt "$hi" ]; then
    reply "Не похоже на правду: $help"
    return
  fi
  filter_set "$key" "$val"
  reply "Принято: $key = $val
Сейчас отбор: $(filter_human)"
}

cmd_flats() {
  local n="$1" rows where human found
  where="$(filter_where)"
  human="$(filter_human)"
  found="$(q "select count(*) from marts.offer_pick where true $where;" | tr -d '[:space:]')"
  rows="$(q "select round(score)::int || ' | ' || district || ' | ' ||
                    rooms_count || 'к ' || round(total_area)::int || ' м2 | ' ||
                    round(price / 1000.0)::int || ' тыс | метро ' ||
                    coalesce(metro_real_walk_min::text, '?') || ' мин' ||
                    E'\n' || url
               from marts.offer_pick
              where true $where
              order by score desc limit $n;")"
  if [ -z "$rows" ]; then
    reply "Подходящего нет

Отбор: ${human:-без фильтров}
Нашлось: ${found:-0}

Ослабить: /filter цена 120, /filter район -, /filter сброс"
    return
  fi
  # Отдельная оговорка, когда цена не задана. Это не придирка: балл за
  # цену перевёрнут намеренно, и без потолка верх списка — это верх по
  # дороговизне. Первый же ответ бота выдал квартиры за 175 тысяч и за
  # три миллиона, и выглядело это как поломка оценки, а не как
  # отсутствие фильтра.
  local warn=""
  [ -z "$(filter_get price)" ] && warn="

Цена не задана, поэтому наверх лезет дорогое: в оценке дороговизна
читается как признак качества. Задать потолок: /filter цена 90"

  reply "Верх подбора, $n из ${found:-?} подходящих

$rows

Отбор: ${human:-без фильтров}
Поменять: /filter${warn}"
}

cmd_market() {
  local line
  line="$(q "select 'Активных 1-2 комнаты: ' || count(*) filter (where is_active and rooms_count in (1,2)) ||
                    E'\nМедиана, руб/мес: ' ||
                    coalesce(round(percentile_cont(0.5) within group (
                        order by price) filter (where is_active and rooms_count in (1,2)))::text, '?') ||
                    E'\nСнятий вчера: ' ||
                    (select count(*) from core.offer where disappeared_date = current_date - 1) ||
                    E'\nСнятий за неделю: ' ||
                    (select count(*) from core.offer where disappeared_date >= current_date - 7)
               from marts.offer_scored;")"
  reply "Рынок на $(date '+%d.%m, %H:%M')

${line:-база не ответила}

Снятие — это уход объявления с площадки, а не обязательно сдача."
}

cmd_log() {
  local n="$1" last text
  last="$(ls -t "$LOG_DIR"/box_2*.log 2>/dev/null | head -1)"
  [ -z "$last" ] && { reply "Журналов прогона нет вовсе."; return; }
  # Обрезка по длине: у телеграма предел около 4096 знаков, и сообщение
  # сверх него не придёт вообще — то есть хвост журнала молча пропадёт
  # ровно тогда, когда он нужнее всего.
  text="$(tail -n "$n" "$last" | cut -c1-120 | tail -c 3000)"
  reply "Журнал $(basename "$last"), последние $n строк

$text"
}

cmd_pulse() {
  bash "$REPO/scripts/box_report.sh" >/dev/null 2>&1 &
  reply "Считаю итог дня, он придёт отдельным сообщением."
}

cmd_clean() {
  local cache was now freed
  if crawl_running; then
    reply "Сейчас идёт обход — кеш не трогаю.

Браузер держит эти файлы открытыми, и чистка посреди обхода ломает
профиль. Повторите /clean, когда обход кончится, или дождитесь:
прогон сам чистит кеш, когда тот перевалит за 300 МБ."
    return
  fi
  cache="$REPO/data/browser_profile/Default"
  was="$(df -Pm "$REPO" | awk 'NR==2 {print $4}')"
  rm -rf "$cache/Cache" "$cache/Code Cache" 2>/dev/null
  # Чистится только кеш. Файл Cookies лежит отдельно и не трогается — в
  # нём накопленная сессия площадки, без неё выдача даётся заметно хуже.
  now="$(df -Pm "$REPO" | awk 'NR==2 {print $4}')"
  freed=$(( now - was ))
  note "clean: освобождено ${freed} МБ"
  reply "Кеш браузера вычищен

Освободилось: ${freed} МБ
Свободно сейчас: ${now} МБ

Куки не тронуты — без накопленной сессии выдача даётся хуже."
}

cmd_unlock() {
  local age quiet last
  if ! crawl_running; then
    reply "Замка нет, снимать нечего. Обход сейчас не идёт.

Запустить сбор за сегодня: /catchup"
    return
  fi
  age=$(( ( $(date +%s) - $(stat -c %Y "$REPO/data/box.lock") ) / 60 ))
  last="$(ls -t "$LOG_DIR"/box_2*.log 2>/dev/null | head -1)"
  quiet=$(( ( $(date +%s) - $(stat -c %Y "$last" 2>/dev/null || echo 0) ) / 60 ))
  # Живой обход не трогаем даже по прямой команде: молчание журнала —
  # единственный признак клинча, и без него «замок висит» означает
  # «обход работает». Пять минут вместо тридцати, как у сторожа: тут
  # решение принимает человек, и он уже видел /status.
  if [ "$quiet" -lt 5 ]; then
    reply "Обход жив, замок не снимаю

Замок держится $age мин, но журнал писался $quiet мин назад — значит
корзины читаются. Снимать замок у живого обхода нельзя: следующий
запуск войдёт вторым браузером в тот же профиль.

Посмотреть, что он делает: /log"
    return
  fi
  timeout 20 docker kill rent_collect >/dev/null 2>&1
  sleep 5
  pkill -f 'etl\.collect' 2>/dev/null
  pkill -f 'user-data-dir=.*browser_profile' 2>/dev/null
  pkill -f 'Xvfb :99' 2>/dev/null
  sleep 2
  rmdir "$REPO/data/box.lock" 2>/dev/null
  if crawl_running; then
    reply "Замок снять не удалось

Держится $age мин, журнал молчит $quiet мин, но каталог не пуст — а
непустой замок сторож и бот не сносят: это уже не замок.

Тут нужны руки: ls -la ~/rent_msk/data/box.lock"
    return
  fi
  note "unlock: замок снят вручную (возраст $age мин, тишина $quiet мин)"
  reply "Замок снят

Держался $age мин, журнал молчал $quiet мин.
Сторож подхватит сам в ближайшие двадцать минут, как только площадка
отдаст выдачу. Не ждать — /catchup."
}

cmd_catchup() {
  local day="$1" seg
  if crawl_running; then
    reply "Обход уже идёт — второй не запускаю.

Посмотреть, как он идёт: /log
Если он заклинил: /unlock"
    return
  fi
  seg="$(collected_today)"; seg="${seg:-0}"
  setsid nohup "$REPO/scripts/box_daily.sh" narrow "$day" >/dev/null 2>&1 &
  note "catchup: запущен обход за $day"
  reply "Обход за $day запущен

Сейчас по сегменту собрано $seg из $NORM.
Уже собранные корзины пропускаются, так что добор идёт быстрее полного.
Ход: /log, итог придёт сам."
}

cmd_dbup() {
  if timeout 30 docker exec "$PG_CONTAINER" pg_isready -U rent -d rent >/dev/null 2>&1; then
    reply "База и так отвечает, поднимать нечего. /status"
    return
  fi
  ( cd "$HOME/rent_data" && timeout 120 docker compose up -d >/dev/null 2>&1 )
  sleep 10
  if timeout 30 docker exec "$PG_CONTAINER" pg_isready -U rent -d rent >/dev/null 2>&1; then
    note "dbup: база поднята"
    reply "База поднята и отвечает

Сбор продолжится по расписанию. Не ждать — /catchup."
  else
    reply "База не поднялась

docker compose up отработал, но pg_isready по-прежнему молчит. Дальше
только руками: скорее всего кончился диск (/status) или умер демон
Docker."
  fi
}

# ---------------------------------------------------------------------
# Разбор команды
# ---------------------------------------------------------------------
# Текст из телеграма — НЕДОВЕРЕННЫЙ ввод, даже когда приходит из своего
# чата: аккаунт могут увести, да и опечатка не должна превращаться в
# команду оболочки. Поэтому разбор — белым списком, а единственный
# аргумент (дата или число) проверяется образцом, а не подставляется как
# есть.
dispatch() {
  local text="$1" cmd arg
  cmd="$(printf '%s' "$text" | awk '{print tolower($1)}')"
  cmd="${cmd%%@*}"                       # /status@имя_бота — так шлёт телеграм в группах
  arg="$(printf '%s' "$text" | awk '{print $2}')"

  case "$cmd" in
    /start|/help) cmd_help ;;
    /status|/статус) cmd_status ;;
    /flats|/подбор)
        case "$arg" in
          "") cmd_flats 5 ;;
          [1-9]|10) cmd_flats "$arg" ;;
          *) reply "Сколько вариантов показать — число от 1 до 10. Например: /flats 7" ;;
        esac ;;
    /filter|/фильтр|/отбор)
        # Третье слово — значение фильтра, и оно может быть названием
        # района с пробелом, поэтому берётся хвостом, а не awk-полем.
        cmd_filter "$(printf '%s' "$text" | awk '{print tolower($2)}')" \
                   "$(printf '%s' "$text" | cut -s -d' ' -f3-)" ;;
    /market|/рынок) cmd_market ;;
    /log|/журнал)
        case "$arg" in
          "") cmd_log 15 ;;
          [1-9]|[1-4][0-9]|50) cmd_log "$arg" ;;
          *) reply "Сколько строк показать — число от 1 до 50. Например: /log 30" ;;
        esac ;;
    /pulse|/итог) cmd_pulse ;;
    /clean|/чистка) cmd_clean ;;
    /unlock|/замок) cmd_unlock ;;
    /catchup|/догнать)
        case "$arg" in
          "") cmd_catchup "$(date +%F)" ;;
          20[0-9][0-9]-[0-1][0-9]-[0-3][0-9]) cmd_catchup "$arg" ;;
          *) reply "Дата должна выглядеть как 2026-10-22. Без даты беру сегодня." ;;
        esac ;;
    /dbup|/база) cmd_dbup ;;
    /*) reply "Команды «${cmd}» нет. Что умею — /help" ;;
    *) reply "Я понимаю только команды. Список — /help" ;;
  esac
}

# ---------------------------------------------------------------------
# Опрос телеграма
# ---------------------------------------------------------------------

# Ручной прогон команды, чтобы проверить ответ, не доставая телефон.
if [ "${1:-}" = "--run" ]; then
  [ -n "$TOKEN" ] && [ -n "$OWNER" ] || { echo "канал не настроен, см. .env"; exit 1; }
  shift
  dispatch "$*"
  echo "команда «$*» выполнена, ответ ушёл в чат"
  exit 0
fi

[ -n "$TOKEN" ] && [ -n "$OWNER" ] || exit 0

# Один опрос за раз. Замок с собственным сроком давности: зависший опрос
# не имеет права заткнуть бота навсегда — именно так и теряются дни.
if ! mkdir "$POLL_LOCK" 2>/dev/null; then
  LOCK_AGE=$(( ( $(date +%s) - $(stat -c %Y "$POLL_LOCK" 2>/dev/null || echo 0) ) / 60 ))
  [ "$LOCK_AGE" -lt 10 ] && exit 0
  note "замок опроса висит $LOCK_AGE мин — снимаю"
  rmdir "$POLL_LOCK" 2>/dev/null
  mkdir "$POLL_LOCK" 2>/dev/null || exit 0
fi
trap 'rmdir "$POLL_LOCK" 2>/dev/null' EXIT

OFFSET="$(cat "$OFFSET_FILE" 2>/dev/null || echo 0)"
UPD="$(curl -sS --max-time 20 \
        "https://api.telegram.org/bot${TOKEN}/getUpdates?offset=${OFFSET}&timeout=0&allowed_updates=%5B%22message%22%5D" \
        2>/dev/null)" || exit 0

PARSED="$(printf '%s' "$UPD" | python3 -c '
import json, sys
try:
    d = json.loads(sys.stdin.read())
except Exception:
    raise SystemExit
if not d.get("ok"):
    raise SystemExit
for u in d.get("result", []):
    m = u.get("message") or {}
    chat = (m.get("chat") or {}).get("id")
    text = (m.get("text") or "").replace("\t", " ").replace("\n", " ")
    uid = u.get("update_id")
    when = m.get("date", 0)
    print(f"{uid}\t{chat}\t{when}\t{text}")
')"

[ -z "$PARSED" ] && exit 0

FIRST_RUN=0
[ "$OFFSET" = 0 ] && FIRST_RUN=1

while IFS=$'\t' read -r UPD_ID CHAT WHEN TEXT; do
  [ -z "$UPD_ID" ] && continue
  echo $(( UPD_ID + 1 )) >"$OFFSET_FILE"

  # Первый запуск: в телеграме лежит всё, что писали боту до того, как он
  # научился отвечать. Выполнять это сейчас — значит выполнить команды
  # недельной давности. Отметку сдвигаем, сами сообщения пропускаем.
  [ "$FIRST_RUN" = 1 ] && { note "первый запуск, пропускаю старое: $TEXT"; continue; }

  if [ "$CHAT" != "$OWNER" ]; then
    # Чужому не отвечаем вовсе: ответ подтвердил бы, что бот живой и
    # что-то умеет. В журнал — да, чтобы знать, что стучались.
    note "чужой чат $CHAT, пропускаю: $TEXT"
    continue
  fi

  # Протухшие команды не выполняем. Бокс мог молчать сутки, и команда
  # «догнать день», отправленная вчера, сегодня означала бы не то.
  NOW=$(date +%s)
  if [ $(( NOW - WHEN )) -gt 900 ]; then
    note "команда старше пятнадцати минут, пропускаю: $TEXT"
    reply "Команда «${TEXT}» пришла больше пятнадцати минут назад — я её не
выполняю. Если она ещё нужна, пришлите заново."
    continue
  fi

  note "команда: $TEXT"
  dispatch "$TEXT"
done <<EOF
$PARSED
EOF
