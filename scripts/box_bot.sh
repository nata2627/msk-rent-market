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

# Гости. Чат владелицы один, но посмотреть панель может быть позволено и
# кому-то ещё: несколько id через запятую в NOTIFY_TG_GUESTS.
#
# Гостю доступно только смотреть. Всё остальное — чистка диска, снятие
# замка, запуск обхода — делает что-то на чужой машине, и такое не дают
# «посмотреть вместе». Фильтры подбора тоже не его: они одни на бота и
# принадлежат владелице, поэтому гость видит ровно тот список, который
# увидела бы она.
GUESTS="${NOTIFY_TG_GUESTS:-$(from_env NOTIFY_TG_GUESTS)}"
# Что доступно гостю. Журнала здесь намеренно нет: в нём пути, ошибки и
# устройство машины — внутренняя кухня, которая гостю ничего не даёт, а
# показывает лишнее.
GUEST_COMMANDS="/help /start /status /flats /saved /market /filter /price /статус /подбор /рынок /фильтр /отбор /цена /отложенные"

# Кто спрашивает. Чужой — это не ошибка и не враг: бота находят поиском,
# и чужому по-прежнему не уходит ничего.
who() {
  case "$1" in
    "$OWNER") echo owner; return ;;
  esac
  case ",$(printf '%s' "$GUESTS" | tr -d '[:space:]')," in
    *",$1,"*) echo guest; return ;;
  esac
  echo stranger
}

guest_allowed() {
  local cmd
  cmd="$(printf '%s' "$1" | awk '{print tolower($1)}')"
  cmd="${cmd%%@*}"
  case " $GUEST_COMMANDS " in *" $cmd "*) return 0 ;; esac
  return 1
}

# Кому уходит ответ. По умолчанию владелице — так отвечают проверки
# вроде --run; в опросе значение меняется на чат спросившего.
TARGET_CHAT="$OWNER"
ROLE="owner"

# Ответ отправляется напрямую, а не через box_notify.sh: тот всегда шлёт
# владелице, а ответ обязан прийти тому, кто спросил. Разметка одна и та
# же, она живёт в box_tg.py.
reply() {
  python3 "$REPO/scripts/box_tg.py" send "$1" "" "$TARGET_CHAT" >/dev/null 2>&1
  note "ответ в чат $TARGET_CHAT: $(printf '%s' "$1" | head -1)"
}

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
  if [ "$ROLE" != "owner" ]; then
    reply "Что я умею

/flats — подбор карточками, листается кнопками
/saved — отложенные варианты
/filter — ваши фильтры подбора, с кнопками
/market — числа рынка за сегодня
/price — оценка цены: ссылка на объявление или «2к 45 м2 Хамовники»
/status — собрано ли сегодня и насколько свежи данные

Фильтры у вас свои: что ни выставите, у владелицы ничего не изменится.
Команды, которые что-то делают на машине сбора, мне недоступны."
    return
  fi
  reply "Что я умею
посмотреть

/status — что сейчас с панелью и машиной
/flats — подбор карточками, листается кнопками
/saved — отложенные варианты
/filter — посмотреть и поменять фильтры подбора
/market — числа рынка за сегодня
/price — оценка цены: ссылка на объявление или «2к 45 м2 Хамовники»
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
    if [ "$ROLE" = "owner" ]; then
      lock="идёт обход, последняя строка журнала: $(tail -1 "$last" 2>/dev/null | cut -c1-80)"
    else
      lock="сбор сейчас идёт"
    fi
  else
    lock="обход не идёт"
  fi
  # Гостю — про данные, а не про машину. Диск, память и номера прогонов
  # ему ничего не говорят, а показывают устройство чужой машины.
  if [ "$ROLE" != "owner" ]; then
    reply "Состояние на $(date '+%d.%m, %H:%M')

Собрано за сегодня: $seg объявлений 1-2 комнаты ($pct% от обычного дня)
Покрытые комнатности: $covered
Свежесть витрин: ${fresh:-неизвестна}

$lock"
    return
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
# Фильтры — СВОИ У КАЖДОГО ЧАТА. Сначала файл был один на бота, и из-за
# этого гостю запрещалось их трогать: он менял бы список владелицы. Но
# фильтр ничего не делает на машине, это настройка показа — и запрет
# выглядел дикостью, справедливо. Свой файл на чат снимает причину
# запрета, а не прикрывает её.
filter_file() { echo "$STATE_DIR/filters.${TARGET_CHAT:-$OWNER}"; }

filter_get() {
  local f; f="$(filter_file)"
  [ -f "$f" ] || return 0
  sed -n "s/^$1=//p" "$f" | tail -n 1
}

filter_set() {
  local key="$1" val="$2" tmp f
  f="$(filter_file)"
  tmp="$(mktemp)"
  grep -v "^$key=" "$f" 2>/dev/null >"$tmp"
  [ -n "$val" ] && echo "$key=$val" >>"$tmp"
  mv "$tmp" "$f"
}

# Фильтры, заданные до разделения по чатам, достаются владелице: они её
# и были.
if [ -f "$STATE_DIR/filters" ] && [ ! -f "$STATE_DIR/filters.$OWNER" ]; then
  mv "$STATE_DIR/filters" "$STATE_DIR/filters.$OWNER" 2>/dev/null
fi

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

# Состояние животных по-русски. В базе оно лежит словами платформы
# (allowed, negotiable, forbidden), и показывать их человеку нельзя:
# «животные allowed» — это не ответ, а утечка внутреннего словаря.
pets_human() {
  case "$1" in
    allowed)    echo "можно" ;;
    negotiable) echo "по договорённости" ;;
    forbidden)  echo "нельзя" ;;
    unknown)    echo "не указано" ;;
    *)          echo "$1" ;;
  esac
}

filter_human() {
  local out="" v
  v="$(filter_get price)";    [ -n "$v" ] && out="$out, цена до $v тыс"
  v="$(filter_get area)";     [ -n "$v" ] && out="$out, площадь от $v м2"
  v="$(filter_get rooms)";    [ -n "$v" ] && out="$out, комнаты $v"
  v="$(filter_get metro)";    [ -n "$v" ] && out="$out, метро до $v мин"
  v="$(filter_get entry)";    [ -n "$v" ] && out="$out, сразу до $v тыс"
  v="$(filter_get pets)";     [ -n "$v" ] && out="$out, животные $(pets_human "$v")"
  v="$(filter_get park)";     [ -n "$v" ] && out="$out, парк в километре"
  v="$(filter_get mkad)";     [ -n "$v" ] && out="$out, внутри МКАД"
  v="$(filter_get district)"; [ -n "$v" ] && out="$out, район $v"
  v="$(filter_get seen)";     [ -n "$v" ] && out="$out, пропущено обходов до $v"
  echo "${out#, }"
}

# То же самое списком — для экрана фильтров. Строка через запятую годится
# как приписка под вариантами, но как главное содержимое экрана читается
# плохо: глазу не за что зацепиться.
filter_lines() {
  local v
  v="$(filter_get price)";    [ -n "$v" ] && echo "Цена: до $v тыс"
  v="$(filter_get area)";     [ -n "$v" ] && echo "Площадь: от $v м2"
  v="$(filter_get rooms)";    [ -n "$v" ] && echo "Комнаты: $v"
  v="$(filter_get metro)";    [ -n "$v" ] && echo "Метро: до $v мин пешком"
  v="$(filter_get entry)";    [ -n "$v" ] && echo "Нужно сразу: до $v тыс"
  v="$(filter_get pets)";     [ -n "$v" ] && echo "Животные: $(pets_human "$v")"
  v="$(filter_get park)";     [ -n "$v" ] && echo "Парк: в пределах километра"
  v="$(filter_get mkad)";     [ -n "$v" ] && echo "МКАД: только внутри"
  v="$(filter_get district)"; [ -n "$v" ] && echo "Район: $v"
  v="$(filter_get seen)";     [ -n "$v" ] && echo "Пропущено обходов: до $v"
  return 0
}

# Клавиатура экрана фильтров. Собирается питоном, а не склейкой строк в
# bash: это JSON, и кавычки в нём живут по своим правилам.
#
# В callback_data телеграм пускает не больше 64 байт, поэтому метки
# короткие: p-10 это «цена минус десять тысяч». Пришедшее значение всё
# равно сверяется белым списком — кнопка снаружи выглядит как любой
# другой ввод из интернета.
filter_keyboard() {
  python3 -c '
import json, sys
price, rooms, area, metro, pets, park, mkad = sys.argv[1:8]

def mark(flag, label):
    # Точка перед надписью показывает, что выбрано: у телеграма нет
    # состояния «нажато», кнопка всегда выглядит одинаково.
    return "\u25cf " + label if flag else label

price_label = "цена: " + (price + " тыс" if price else "любая")
area_label = "от " + (area + " м2" if area else "любой")
pets_label = {"allowed": "можно", "negotiable": "по договорённости",
              "forbidden": "нельзя"}.get(pets, "не важно")

rows = [
    [("Цена -10", "p-10"), (price_label, "noop"), ("+10", "p+10")],
    [("Площадь -5", "a-5"), (area_label, "noop"), ("+5", "a+5")],
    [(mark(rooms == "1", "1к"), "r1"), (mark(rooms == "2", "2к"), "r2"),
     (mark(rooms == "1,2", "1-2к"), "r12"), (mark(not rooms, "любая"), "r0")],
    [(mark(metro == "10", "метро 10"), "m10"), (mark(metro == "15", "15"), "m15"),
     (mark(metro == "20", "20"), "m20"), (mark(not metro, "любое"), "m0")],
    # Животные — одна кнопка по кругу, а не три подряд. Тремя кнопками
    # надпись приходилось резать до «договор», а это читается как
    # «договор аренды», хотя речь о состоянии «по договорённости».
    [("Животные: " + pets_label, "e")],
    [(mark(bool(park), "парк в 1 км"), "k"), (mark(bool(mkad), "внутри МКАД"), "d")],
    [("Сбросить всё", "x"), ("Показать варианты", "s")],
]
print(json.dumps({"inline_keyboard": [
    [{"text": t, "callback_data": d} for t, d in row] for row in rows]},
    ensure_ascii=False))
' "$(filter_get price)" "$(filter_get rooms)" "$(filter_get area)" \
  "$(filter_get metro)" "$(filter_get pets)" "$(filter_get park)" "$(filter_get mkad)"
}

filter_screen_text() {
  local lines found
  lines="$(filter_lines)"
  found="$(q "select count(*) from marts.offer_pick where true $(filter_where);" | tr -d '[:space:]')"
  printf '%s' "Фильтры подбора

${lines:-Пока ничего не задано, поэтому наверх лезет дорогое: в оценке
дороговизна читается как признак качества.}

Подходит: ${found:-?} вариантов

Меняйте кнопками под этим сообщением — оно обновится на месте.
Точные числа и район удобнее текстом, например:
\$ /filter цена 90
Весь список текстовых команд: /filter текстом"
}

# Экран фильтров: отправить новый или поправить на месте после нажатия.
filter_screen() {
  local msg_id="${1:-}" kb
  # Начальный отбор ставится и здесь: экран фильтров открывают раньше,
  # чем подбор, и увидеть на нём «ничего не задано» — то же самое, что
  # получить список квартир за три миллиона.
  filter_defaults >/dev/null || true
  kb="$(filter_keyboard)"
  if [ -n "$msg_id" ]; then
    python3 "$REPO/scripts/box_tg.py" edit "$msg_id" "$(filter_screen_text)" "$kb" "$TARGET_CHAT"
  else
    python3 "$REPO/scripts/box_tg.py" send "$(filter_screen_text)" "$kb" "$TARGET_CHAT"
  fi
}

# Нажатие кнопки. Белый список: ничего, кроме перечисленного здесь, в
# фильтры не попадает, даже если в callback_data пришло что угодно.
filter_button() {
  local data="$1" msg_id="$2" cb_id="$3" cur toast="готово"
  card_button "$data" "$msg_id" "$cb_id" && return
  case "$data" in
    p-10|p+10)
      cur="$(filter_get price)"; cur="${cur:-100}"
      [ "$data" = "p-10" ] && cur=$(( cur - 10 )) || cur=$(( cur + 10 ))
      [ "$cur" -lt 10 ] && cur=10
      [ "$cur" -gt 999 ] && cur=999
      filter_set price "$cur"; toast="цена до $cur тыс" ;;
    a-5|a+5)
      cur="$(filter_get area)"; cur="${cur:-40}"
      [ "$data" = "a-5" ] && cur=$(( cur - 5 )) || cur=$(( cur + 5 ))
      [ "$cur" -lt 10 ] && cur=10
      [ "$cur" -gt 300 ] && cur=300
      filter_set area "$cur"; toast="площадь от $cur м2" ;;
    r1)  filter_set rooms 1;   toast="однокомнатные" ;;
    r2)  filter_set rooms 2;   toast="двухкомнатные" ;;
    r12) filter_set rooms 1,2; toast="одно- и двухкомнатные" ;;
    r0)  filter_set rooms "";  toast="комнатность любая" ;;
    m10) filter_set metro 10;  toast="метро до 10 минут" ;;
    m15) filter_set metro 15;  toast="метро до 15 минут" ;;
    m20) filter_set metro 20;  toast="метро до 20 минут" ;;
    m0)  filter_set metro "";  toast="метро не важно" ;;
    e)
      # По кругу: не важно -> можно -> по договорённости -> не важно.
      # «Нельзя» в круг не входит намеренно: отбирать квартиры, где
      # животных запрещают, незачем. Текстом такое всё ещё можно
      # задать — для проверок рынка, не для подбора.
      case "$(filter_get pets)" in
        "")           filter_set pets allowed;    toast="животные: можно" ;;
        allowed)      filter_set pets negotiable; toast="животные: по договорённости" ;;
        *)            filter_set pets "";         toast="животные: не важно" ;;
      esac ;;
    k)   [ -n "$(filter_get park)" ] && { filter_set park ""; toast="парк не важен"; } \
                                     || { filter_set park 1;  toast="парк в пределах километра"; } ;;
    d)   [ -n "$(filter_get mkad)" ] && { filter_set mkad ""; toast="МКАД не важен"; } \
                                     || { filter_set mkad 1;  toast="только внутри МКАД"; } ;;
    x)   rm -f "$(filter_file)"; toast="фильтры сброшены" ;;
    s|first|next)
         # next и first живут на сообщениях до карусели: нажатие на
         # старой кнопке открывает карусель, а не молчит.
         python3 "$REPO/scripts/box_tg.py" answer "$cb_id" "ищу"
         if [ "$(card_get mode)" = saved ] && [ -n "$msg_id" ] && [ "$data" = s ]; then
           card_show pick 0 "$msg_id" >/dev/null
         else
           cmd_flats
         fi
         return ;;
    filters)
         python3 "$REPO/scripts/box_tg.py" answer "$cb_id" "фильтры"
         filter_screen
         return ;;
    noop) python3 "$REPO/scripts/box_tg.py" answer "$cb_id" "это просто надпись"
         return ;;
    *)   note "неизвестная кнопка: $data"
         python3 "$REPO/scripts/box_tg.py" answer "$cb_id" "не знаю такой кнопки"
         return ;;
  esac
  python3 "$REPO/scripts/box_tg.py" answer "$cb_id" "$toast"
  filter_screen "$msg_id"
}

# Начальный отбор для чата, у которого фильтров ещё нет.
#
# Пустой отбор — это не «показать всё», а «показать самое дорогое»: балл
# за цену в проекте перевёрнут намеренно, дешевизна на данных оказалась
# признаком беды. Владелица увидела это первой же командой (квартиры за
# 175 тысяч и за три миллиона), а потом ровно то же получил гость, когда
# у него завёлся собственный пустой набор.
#
# Потолок берётся из данных, а не выдумывается: медиана цены по
# однушкам и двушкам, которые сейчас в выдаче. Это не предпочтение, а
# точка отсчёта, и она честно называется в ответе.
filter_defaults() {
  local med
  [ -f "$(filter_file)" ] && return 1
  med="$(q "select round(percentile_cont(0.5) within group (order by price) / 1000.0)::int
              from marts.offer_pick
             where is_active and crawls_missed <= 0 and rooms_count in (1, 2);" | tr -d '[:space:]')"
  [ -z "$med" ] && med=100
  filter_set rooms "1,2"
  filter_set price "$med"
  echo "$med"
  return 0
}

cmd_filter() {
  local key="$1" val="$2" human districts
  case "$key" in
    "")
      # Экран с кнопками. Текстовый путь никуда не делся и остаётся
      # единственным для района и для точных чисел: кнопками удобно
      # крутить, текстом — задавать.
      filter_screen ;;
    текстом|help|справка)
      reply "Фильтры текстом

/filter цена 90          потолок в тысячах
/filter площадь 35       от скольких м2
/filter комнаты 1,2      комнатность
/filter метро 15         минут пешком
/filter сразу 250        сколько нужно денег сразу
/filter животные можно   можно / договор / нельзя
/filter парк да          парк от 10 га в километре
/filter мкад да          только внутри
/filter район Хамовники  сверяется со справочником базы
/filter обходы 2         насколько старую выдачу пускать
/filter цена -           убрать один
/filter сброс            убрать все

Кнопками: /filter
Колонки и смысл те же, что у фильтров дашборда." ;;
    сброс|reset)
      rm -f "$(filter_file)"
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
        договор|договорённость|по\ договорённости)
                  filter_set pets negotiable; reply "Животные: по договорённости
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

# ---------------------------------------------------------------------
# Подбор карусельной карточкой
# ---------------------------------------------------------------------
# Одно сообщение с фото, которое листается кнопками на месте. Раньше
# подбор приходил пачкой по пять сообщений, и после двух листаний чат
# превращался в ленту, где уже не найти, что понравилось. Теперь вариант
# один, понравившийся откладывается звездой и живет в /saved.
#
# Состояние карусели свое у каждого чата: режим (подбор или отложенные),
# номер варианта, id сообщения карточки и id показанного объявления.
card_file()  { echo "$STATE_DIR/card.${TARGET_CHAT:-$OWNER}"; }
saved_file() { echo "$STATE_DIR/saved.${TARGET_CHAT:-$OWNER}"; }
card_get() {
  local i
  case "$1" in mode) i=1 ;; idx) i=2 ;; msg) i=3 ;; offer) i=4 ;; esac
  cut -d'|' -f"$i" "$(card_file)" 2>/dev/null
}
card_set() { echo "$1|$2|$3|$4" >"$(card_file)"; }
is_saved() { grep -qxF "$1" "$(saved_file)" 2>/dev/null; }
# Через cat: у grep -c на пустом файле код возврата 1, и запасное «|| echo 0»
# дописывало второй ноль, а сравнение «0 0» с числом падало.
saved_count() { cat "$(saved_file)" 2>/dev/null | grep -c . || true; }

# Отложить или вернуть. Id объявления берется из состояния карточки, а
# не из кнопки, и все равно проверяется образцом: в файл идут только цифры.
saved_toggle() {
  local id="$1" f tmp
  case "$id" in ''|*[!0-9]*) return 1 ;; esac
  f="$(saved_file)"
  if is_saved "$id"; then
    tmp="$(mktemp)"; grep -vxF "$id" "$f" >"$tmp"; mv "$tmp" "$f"
    echo "убрано из отложенных"
  else
    # Новое сверху: последнее отложенное обычно и нужно первым.
    tmp="$(mktemp)"; { echo "$id"; cat "$f" 2>/dev/null; } >"$tmp"; mv "$tmp" "$f"
    echo "отложено, список /saved"
  fi
}

CARD_COLS="o.offer_id, round(o.score)::int, o.district, o.rooms_count, round(o.total_area)::int,
           -- Цена точная, в рублях: округление до тысяч прятало, что 89 500
           -- это не 90 000. Рядом сумма при заселении (месяц, залог,
           -- комиссия), со звездочкой, если залог не указан и предположен.
           replace(to_char(o.price, 'FM999,999,999'), ',', ' '),
           coalesce(replace(to_char(o.entry_cost, 'FM999,999,999'), ',', ' ')
                    || case when o.deposit_assumed then '*' else '' end, ''),
           coalesce(o.metro_real_walk_min::text, '?'), o.pets_state,
           o.is_active,
           -- Адрес без хвоста: в нем метки показа, вдвое длиннее самой
           -- ссылки и ничего не значащие для человека.
           split_part(o.url, '?', 1),
           coalesce((select p.url from core.offer_photo p
                      where p.source = o.source and p.offer_id = o.offer_id
                      order by p.photo_pos limit 1), '')"

card_keyboard() {
  python3 -c '
import json, sys
mode, saved, price, rooms = sys.argv[1:5]
def mark(flag, label):
    return "● " + label if flag else label
if mode == "pick":
    rows = [
        [("◀", "c<"), ("★ убрать" if saved == "1" else "★ отложить", "cs"), ("▶", "c>")],
        [("цена -10", "cp-"), ("до " + price + " тыс" if price else "цена любая", "noop"), ("+10", "cp+")],
        [(mark(rooms == "1", "1к"), "cr1"), (mark(rooms == "2", "2к"), "cr2"),
         (mark(rooms == "1,2", "1-2к"), "cr12")],
        [("еще фильтры", "filters"), ("отложенные", "sv")],
    ]
else:
    rows = [
        [("◀", "v<"), ("★ убрать" if saved == "1" else "★ вернуть", "vs"), ("▶", "v>")],
        [("к подбору", "s")],
    ]
print(json.dumps({"inline_keyboard": [
    [{"text": t, "callback_data": d} for t, d in row] for row in rows]}, ensure_ascii=False))
' "$1" "$2" "$(filter_get price)" "$(filter_get rooms)"
}

# Показать вариант номер idx в режиме mode. msg_id пустой означает новое
# сообщение. Печатает «end», если варианта с таким номером нет.
card_show() {
  local mode="$1" idx="$2" msg_id="${3:-}" total row id score district rooms area price entry metro pets active url photo
  local head tail="" saved=0 caption new_id row=""
  if [ "$mode" = "pick" ]; then
    total="$(q "select count(*) from marts.offer_pick o where true $(filter_where);" | tr -d '[:space:]')"
    row="$(q "select $CARD_COLS from marts.offer_pick o where true $(filter_where)
              order by o.score desc limit 1 offset $idx;")"
    head="Вариант $(( idx + 1 )) из ${total:-?}"
    tail="

Отбор: $(filter_human)"
  else
    total="$(saved_count)"
    id="$(sed -n "$(( idx + 1 ))p" "$(saved_file)" 2>/dev/null)"
    # Отложенное могло уйти из подбора: снято, подорожало, выпало из
    # фильтров. Поэтому ищется без фильтров, по одному id.
    [ -n "$id" ] && row="$(q "select $CARD_COLS from marts.offer_pick o
                              where o.source = 'cian' and o.offer_id = '$id' limit 1;")"
    head="Отложенное $(( idx + 1 )) из $total"
  fi
  [ -z "$row" ] && { echo end; return 0; }
  IFS='|' read -r id score district rooms area price entry metro pets active url photo <<<"$row"
  is_saved "$id" && saved=1
  [ "$active" = "t" ] || tail="$tail

Снято с публикации, по ссылке может не открыться."
  caption="$head
$district · $score баллов
$rooms комн., $area м2
$price руб/мес$([ -n "$entry" ] && printf ', при заселении %s руб' "${entry%\*}")$([ "${entry: -1}" = "*" ] && printf ', залог не указан, взят за месяц')
метро $metro мин пешком, животные $(pets_human "$pets")$([ "$saved" = 1 ] && printf '\n★ отложено')$tail

$url"
  new_id="$(python3 "$REPO/scripts/box_tg.py" card "$TARGET_CHAT" "${msg_id:--}" "${photo:--}" \
             "$caption" "$(card_keyboard "$mode" "$saved")" 2>/dev/null)"
  card_set "$mode" "$idx" "${new_id:-$msg_id}" "$id"
  note "карточка $mode $idx: $district, $price руб"
  # Фото следующей карточки в кеш, пока человек смотрит эту. Фоном и с
  # закрытым выводом: иначе подстановка $(card_show ...) ждала бы его.
  if [ "$mode" = pick ]; then
    ( u="$(q "select coalesce((select p.url from core.offer_photo p
                                where p.source = o.source and p.offer_id = o.offer_id
                                order by p.photo_pos limit 1), '')
                from marts.offer_pick o where true $(filter_where)
               order by o.score desc limit 1 offset $(( idx + 1 ));")"
      [ -n "$u" ] && python3 "$REPO/scripts/box_tg.py" prefetch "$u" ) >/dev/null 2>&1 &
  fi
}

cmd_flats() {
  local started
  started="$(filter_defaults)" || started=""
  if [ -n "$started" ]; then
    reply "Фильтров у вас ещё не было, поставил начальные: 1-2 комнаты и цена до
$started тыс. Это медиана по таким квартирам в сегодняшней выдаче, а не
чьё-то мнение. Меняйте кнопками под карточкой."
  fi
  if [ "$(card_show pick 0)" = end ]; then
    reply "Подходящего нет

Отбор: $(filter_human)

Ослабить: /filter"
  fi
}

cmd_saved() {
  if [ "$(saved_count)" -eq 0 ]; then
    reply "Отложенных пока нет

Откройте подбор (/flats) и нажмите «★ отложить» под понравившимся."
    return
  fi
  card_show saved 0 >/dev/null
}

# Нажатие на карточке. Возвращает 1, если кнопка не карусельная.
card_button() {
  local data="$1" msg_id="$2" cb_id="$3" mode idx toast cur
  mode="$(card_get mode)"; idx="$(card_get idx)"; idx="${idx:-0}"
  case "$data" in
    c\<|v\<)
      [ "$idx" -le 0 ] && { python3 "$REPO/scripts/box_tg.py" answer "$cb_id" "это первый"; return 0; }
      python3 "$REPO/scripts/box_tg.py" answer "$cb_id"
      card_show "${mode:-pick}" "$(( idx - 1 ))" "$msg_id" >/dev/null ;;
    c\>|v\>)
      if [ "$(card_show "${mode:-pick}" "$(( idx + 1 ))" "$msg_id")" = end ]; then
        python3 "$REPO/scripts/box_tg.py" answer "$cb_id" "дальше ничего нет"
      else
        python3 "$REPO/scripts/box_tg.py" answer "$cb_id"
      fi ;;
    cs|vs)
      toast="$(saved_toggle "$(card_get offer)")" || toast="не вышло"
      python3 "$REPO/scripts/box_tg.py" answer "$cb_id" "$toast"
      # В отложенных убранный вариант пропадает из списка, поэтому номер
      # остается, а на его место встает следующий. Убран последний, тогда
      # шаг назад, а если список опустел, карточка остается как была.
      if [ "$mode" != saved ]; then
        card_show pick "$idx" "$msg_id" >/dev/null
      elif [ "$(card_show saved "$idx" "$msg_id")" = end ] && [ "$idx" -gt 0 ]; then
        card_show saved "$(( idx - 1 ))" "$msg_id" >/dev/null
      fi ;;
    cp-|cp+)
      cur="$(filter_get price)"; cur="${cur:-100}"
      [ "$data" = "cp-" ] && cur=$(( cur - 10 )) || cur=$(( cur + 10 ))
      [ "$cur" -lt 10 ] && cur=10
      [ "$cur" -gt 999 ] && cur=999
      filter_set price "$cur"
      python3 "$REPO/scripts/box_tg.py" answer "$cb_id" "цена до $cur тыс"
      card_show pick 0 "$msg_id" >/dev/null ;;
    cr1|cr2|cr12)
      case "$data" in cr1) cur=1 ;; cr2) cur=2 ;; *) cur=1,2 ;; esac
      filter_set rooms "$cur"
      python3 "$REPO/scripts/box_tg.py" answer "$cb_id" "комнат: $cur"
      card_show pick 0 "$msg_id" >/dev/null ;;
    sv)
      python3 "$REPO/scripts/box_tg.py" answer "$cb_id" "отложенные"
      if [ "$(saved_count)" -eq 0 ]; then
        reply "Отложенных пока нет. Нажмите «★ отложить» под понравившимся."
      else
        card_show saved 0 "$msg_id" >/dev/null
      fi ;;
    *) return 1 ;;
  esac
  return 0
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
    /flats|/подбор) cmd_flats ;;
    /saved|/отложенные) cmd_saved ;;
    /filter|/фильтр|/отбор)
        # Третье слово — значение фильтра, и оно может быть названием
        # района с пробелом, поэтому берётся хвостом, а не awk-полем.
        cmd_filter "$(printf '%s' "$text" | awk '{print tolower($2)}')" \
                   "$(printf '%s' "$text" | cut -s -d' ' -f3-)" ;;
    /market|/рынок) cmd_market ;;
    # Оценка читается из готовых таблиц ml, модели и площадки в пути ответа
    # нет. Текст идет хвостом целиком: в нем ссылка или «2к 45 м2 район».
    /price|/цена)
        reply "$(PG_CONTAINER="$PG_CONTAINER" python3 "$REPO/scripts/box_price.py" \
                 "$(printf '%s' "$text" | cut -s -d' ' -f2-)")" ;;
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

# Нажатие кнопки руками: проверить обработчик, не доставая телефон.
#   box_bot.sh --button r12 <id сообщения> <id нажатия>
if [ "${1:-}" = "--button" ]; then
  [ -n "$TOKEN" ] && [ -n "$OWNER" ] || { echo "канал не настроен, см. .env"; exit 1; }
  # Пятым доводом — чат: без него проверка нажатия всегда шла бы от
  # имени владелицы, и ошибка с чужим чатом снова осталась бы незамеченной.
  TARGET_CHAT="${5:-$OWNER}"
  filter_button "${2:-}" "${3:-}" "${4:-проверка}"
  echo "кнопка «${2:-}» обработана"
  exit 0
fi

# Ручной прогон команды, чтобы проверить ответ, не доставая телефон.
if [ "${1:-}" = "--run" ]; then
  [ -n "$TOKEN" ] && [ -n "$OWNER" ] || { echo "канал не настроен, см. .env"; exit 1; }
  shift
  dispatch "$*"
  echo "команда «$*» выполнена, ответ ушёл в чат"
  exit 0
fi

[ -n "$TOKEN" ] && [ -n "$OWNER" ] || exit 0

# Ошибки оболочки в журнал. cron вывод выбрасывает, и заход, упавший на
# опечатке, выглядел бы для человека как бот, который минуту не отвечает,
# без единого следа.
exec 2>>"$LOG_DIR/box_bot.err"

# Один опрос за раз. Замок с собственным сроком давности: зависший опрос
# не имеет права заткнуть бота навсегда — именно так и теряются дни.
# Срок давности — три минуты. Честный опрос живёт не дольше семидесяти
# секунд (пятьдесят ждёт телеграм плюс запас), поэтому всё, что старше
# трёх минут, — след убитого процесса.
#
# Раньше стояло десять минут, и этого хватило, чтобы обжечься: опрос,
# прибитый при проверке, оставил замок, и бот молчал бы десять минут. С
# кнопками это особенно неприятно — человек жмёт, а ответа нет.
#
# Занятый замок ждется, а не бросается. Заход, который обрабатывал
# нажатия, заканчивает позже своей минуты, и следующий из cron, увидев
# замок, раньше сразу выходил. До очередной минуты бота не слушал никто,
# и 24.10.2026 это выглядело как бот, который лег посреди листания.
for _ in $(seq 1 50); do
  [ -d "$POLL_LOCK" ] || break
  sleep 1
done
if ! mkdir "$POLL_LOCK" 2>/dev/null; then
  LOCK_AGE=$(( ( $(date +%s) - $(stat -c %Y "$POLL_LOCK" 2>/dev/null || echo 0) ) / 60 ))
  [ "$LOCK_AGE" -lt 3 ] && exit 0
  note "замок опроса висит $LOCK_AGE мин — снимаю"
  rmdir "$POLL_LOCK" 2>/dev/null
  mkdir "$POLL_LOCK" 2>/dev/null || exit 0
fi
# Сигналы ловятся наравне с обычным выходом: замок, оставленный после
# kill, останавливает бота целиком, а снимается он только по сроку.
trap 'rmdir "$POLL_LOCK" 2>/dev/null' EXIT INT TERM HUP

OFFSET="$(cat "$OFFSET_FILE" 2>/dev/null || echo 0)"
# Меню команд телеграма обновляется раз в сутки: оно меняется редко, а
# лишний запрос на каждом заходе — это 1440 запросов в день впустую.
MENU_STAMP="$STATE_DIR/menu"
if [ ! -f "$MENU_STAMP" ] || [ -n "$(find "$MENU_STAMP" -mtime +1 2>/dev/null)" ]; then
  python3 "$REPO/scripts/box_tg.py" commands >/dev/null 2>&1 && touch "$MENU_STAMP"
fi

FIRST_RUN=0
[ "$(cat "$OFFSET_FILE" 2>/dev/null || echo 0)" = 0 ] && FIRST_RUN=1

# Заход живёт 55 секунд и опрашивает телеграм по кругу, а не один раз.
#
# Сначала было по одному опросу на заход: пачка событий обработана —
# процесс вышел, следующий поднимается только по cron, через минуту.
# Первое сообщение отвечалось мгновенно, а следующее ждало до минуты, и
# со стороны это выглядело как «бот тормозит». Теперь между заходами
# остаётся щель в несколько секунд, а не в минуту.
#
# Демона всё равно нет: 55 секунд — потолок, дальше процесс выходит сам
# и его сменяет следующий из cron. Зависнуть он не может.
DEADLINE=$(( $(date +%s) + 55 ))

while [ "$(date +%s)" -lt "$DEADLINE" ]; do
  OFFSET="$(cat "$OFFSET_FILE" 2>/dev/null || echo 0)"
  LEFT=$(( DEADLINE - $(date +%s) ))
  PARSED="$(python3 "$REPO/scripts/box_tg.py" poll "$OFFSET" "$LEFT" 2>/dev/null)"

  if [ -z "$PARSED" ]; then
    # Пусто бывает по двум причинам: за время ожидания никто не написал
    # (это норма) или телеграм недоступен и ответ пришёл сразу. Во
    # втором случае без паузы получился бы цикл в тысячу запросов в
    # минуту.
    sleep 3
    continue
  fi

  # Разделитель \x1f, а не табуляция: таб для bash пробельный, и пустое
  # поле между двумя табами схлопывается вместе с ними. Подробности — в
  # scripts/box_tg.py, там же, где это печатается.
  while IFS=$'\x1f' read -r UPD_ID KIND CHAT WHEN EXTRA TEXT; do
    [ -z "$UPD_ID" ] && continue
    echo $(( UPD_ID + 1 )) >"$OFFSET_FILE"

    # Первый запуск: в телеграме лежит всё, что писали боту до того, как
    # он научился отвечать. Выполнять это сейчас — значит выполнить
    # команды недельной давности. Отметку сдвигаем, сообщения пропускаем.
    [ "$FIRST_RUN" = 1 ] && { note "первый запуск, пропускаю старое: $TEXT"; continue; }

    ROLE="$(who "$CHAT")"
    if [ "$ROLE" = stranger ]; then
      # Чужому не отвечаем вовсе: ответ подтвердил бы, что бот живой и
      # что-то умеет. В журнал — да, чтобы знать, что стучались.
      note "чужой чат $CHAT, пропускаю: $TEXT"
      continue
    fi
    TARGET_CHAT="$CHAT"

    if [ "$KIND" = "cb" ]; then
      # Нажатие кнопки. В EXTRA лежит id нажатия и id сообщения через
      # двоеточие: первое нужно, чтобы погасить индикатор, второе —
      # чтобы поправить экран на месте.
      note "кнопка: $TEXT"
      filter_button "$TEXT" "${EXTRA#*:}" "${EXTRA%%:*}"
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

    if [ "$ROLE" = guest ] && ! guest_allowed "$TEXT"; then
      note "гость $CHAT просил «$TEXT», отказано"
      reply "Это команда владелицы

Показывать можно всё, а трогать машину сбора — нет. Доступно:
/status, /flats, /market, /log, /filter со своими фильтрами"
      continue
    fi

    note "команда от ${ROLE}: $TEXT"
    dispatch "$TEXT"
  done <<EOF
$PARSED
EOF

  # Старое пропускается ровно один раз, на самом первом заходе.
  FIRST_RUN=0
done
