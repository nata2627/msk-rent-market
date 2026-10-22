#!/bin/bash
# Подключение бота телеграма как канала доклада — одной командой.
#
# Зачем отдельный скрипт. Бота нельзя создать программно: @BotFather
# разговаривает только с живым аккаунтом. Но всё остальное делать руками
# незачем, а id чата руками узнают неприятно — через getUpdates и чтение
# JSON. Скрипт берёт на себя всё, кроме самой переписки с BotFather.
#
#   make notify-tg            # спросит токен, не показывая его на экране
#   make notify-tg TOKEN=...  # если токен уже под рукой
#
# Токен по умолчанию не передаётся аргументом намеренно: аргумент
# остаётся в истории оболочки и виден в списке процессов. Ввод через
# read -s не остаётся нигде.
#
# Что делает:
#   1. проверяет токен (getMe) и называет имя бота;
#   2. находит id чата по последнему сообщению боту (getUpdates);
#   3. вписывает NOTIFY_TG_TOKEN и NOTIFY_TG_CHAT в .env — здесь и на
#      боксе сбора;
#   4. отправляет проверочное сообщение с обеих машин.
#
# Токен нигде не печатается: в выводе только имя бота и маска.

set -uo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO" || exit 1

BOX_REPO="~/rent_msk"

say() { echo "$*"; }
die() { echo "!!! $*" >&2; exit 1; }

# Адрес машины сбора берётся из .env, а не из кода: репозиторий публичный,
# и сведения о домашней сети в нём не нужны никому, кроме нас. Образец
# переменных — в .env.example.
box_env() {
    sed -n "s/^[[:space:]]*$1=//p" "$REPO/.env" 2>/dev/null | tail -n 1 \
      | sed -e 's/[[:space:]]*$//' -e 's/^"\(.*\)"$/\1/' -e "s/^'\(.*\)'\$/\1/"
}
BOX_HOST="${BOX_SSH_HOST:-$(box_env BOX_SSH_HOST)}"
BOX_PORT="${BOX_SSH_PORT:-$(box_env BOX_SSH_PORT)}"
BOX_USER="${BOX_SSH_USER:-$(box_env BOX_SSH_USER)}"
if [ -z "$BOX_HOST" ] || [ -z "$BOX_USER" ]; then
    die "не заданы BOX_SSH_HOST и BOX_SSH_USER — ни в окружении, ни в .env (образец в .env.example)"
fi
BOX="ssh -o ConnectTimeout=20 -o BatchMode=yes -p ${BOX_PORT:-22} ${BOX_USER}@${BOX_HOST}"

TOKEN="${TOKEN:-${1:-}}"
if [ -z "$TOKEN" ]; then
    printf 'Токен бота от @BotFather (не отобразится): '
    read -rs TOKEN
    echo
fi
[ -n "$TOKEN" ] || die "токен не введён"

# Пробел и перевод строки в токене — самая частая беда копирования.
TOKEN="$(printf '%s' "$TOKEN" | tr -d '[:space:]')"

say "Проверяю токен"
ME="$(curl -sS --max-time 10 "https://api.telegram.org/bot${TOKEN}/getMe")" \
    || die "телеграм не ответил"
case "$ME" in
    *'"ok":true'*) ;;
    *) die "токен не принят. Ответ: $(printf '%s' "$ME" | tr -d '\n' | cut -c1-200)" ;;
esac
BOT="$(printf '%s' "$ME" | sed -n 's/.*"username":"\([^"]*\)".*/\1/p')"
say "Бот: @${BOT}"

# id чата берётся из последнего сообщения боту. Иначе его негде взять:
# бот не может написать первым, пока с ним не поздоровались.
#
# Разбор JSON — питоном, а не регулярным выражением. Выражением по тексту
# ответа id чата не находился вовсе: в ответе несколько объектов с полем
# "id" (отправитель, чат, сообщение), порядок полей телеграм не обещает, и
# поиск молча возвращал пустоту при живых сообщениях (22.10.2026). Питон в
# проекте есть всегда — на нём написан весь сбор.
chat_from_updates() {
    python3 -c '
import json, sys
try:
    d = json.loads(sys.stdin.read())
except Exception:
    print("РАЗБОР|ответ не разобран|", end=""); raise SystemExit
if not d.get("ok"):
    print("РАЗБОР|телеграм отказал|", end=""); raise SystemExit
res = d.get("result", [])
# Берём последнее сообщение: если боту писали несколько раз, свежее вернее.
for u in reversed(res):
    for key in ("message", "edited_message", "channel_post", "my_chat_member"):
        m = u.get(key) or {}
        cid = (m.get("chat") or {}).get("id")
        if cid is not None:
            print(f"РАЗБОР|обновлений {len(res)}|{cid}", end="")
            raise SystemExit
print(f"РАЗБОР|обновлений {len(res)}, чата среди них нет|", end="")
'
}

# Webhook. Если он у бота стоит, getUpdates ВСЕГДА пуст, сколько ни пиши
# боту. Проверка стоит одного запроса и снимает самую непонятную из
# возможных причин «боту никто не писал».
WH="$(curl -sS --max-time 10 "https://api.telegram.org/bot${TOKEN}/getWebhookInfo" 2>/dev/null)"
case "$WH" in
    *'"url":""'*) ;;
    *'"url":"http'*)
        say "У бота стоит webhook — из-за него getUpdates всегда пуст. Снимаю."
        curl -sS --max-time 10 "https://api.telegram.org/bot${TOKEN}/deleteWebhook" >/dev/null 2>&1
        ;;
esac

say "Ищу чат"
CHAT=""
TOLD=0
for attempt in $(seq 1 12); do
    UPD="$(curl -sS --max-time 10 "https://api.telegram.org/bot${TOKEN}/getUpdates")" || true
    PARSED="$(printf '%s' "$UPD" | chat_from_updates)"
    WHAT="$(printf '%s' "$PARSED" | cut -d'|' -f2)"
    CHAT="$(printf '%s' "$PARSED" | cut -d'|' -f3)"
    [ -n "$CHAT" ] && break
    if [ "$TOLD" = 0 ]; then
        say "  пока нечего взять ($WHAT)"
        say "  Откройте https://t.me/${BOT} и отправьте боту любое сообщение."
        say "  Жду до минуты."
        TOLD=1
    fi
    sleep 5
done
[ -n "$CHAT" ] || die "сообщений боту так и нет ($WHAT), id чата взять негде. Напишите боту и повторите"
say "Чат: $CHAT"

# Запись в .env идемпотентна: прежние строки убираются, новые дописываются.
# Файл не под git (.gitignore), но на всякий случай проверяем — репозиторий
# публичный, и токен в нём это токен, выложенный в интернет.
git check-ignore -q .env || die ".env не в .gitignore — не пишу туда секрет"

write_env() {
    local file="$1"
    local tmp
    tmp="$(mktemp)"
    grep -v -E '^NOTIFY_TG_(TOKEN|CHAT)=' "$file" >"$tmp" 2>/dev/null
    {
        echo "NOTIFY_TG_TOKEN=$TOKEN"
        echo "NOTIFY_TG_CHAT=$CHAT"
    } >>"$tmp"
    cat "$tmp" >"$file"
    rm -f "$tmp"
}

say "Пишу в .env на макбуке"
write_env "$REPO/.env"

say "Пишу в .env на боксе"
if $BOX "cd $BOX_REPO && grep -v -E '^NOTIFY_TG_(TOKEN|CHAT)=' .env > .env.new && printf 'NOTIFY_TG_TOKEN=%s\nNOTIFY_TG_CHAT=%s\n' '$TOKEN' '$CHAT' >> .env.new && mv .env.new .env && grep -c '^NOTIFY_TG_' .env" >/dev/null 2>&1; then
    say "  готово"
else
    say "  НЕ ВЫШЛО: бокс не ответил. Повторите скрипт позже, здесь всё уже записано"
fi

# Проверка. Отправщик шлёт во ВСЕ настроенные каналы разом, поэтому
# обычной проверки достаточно: в её выводе будет строка на каждый канал.
say ""
say "Проверяю с макбука"
bash "$REPO/scripts/box_notify.sh" --test
say "Проверяю с бокса"
$BOX "cd $BOX_REPO && bash scripts/box_notify.sh --test" 2>&1 \
    || say "  с бокса не вышло, посмотрите вручную"

say ""
say "Если сообщения пришли в телеграм — канал подключён."
# Число каналов называется честно: раньше здесь всегда писалось «уходит
# в оба», хотя ntfy убран и канал остался один. Мелочь, но ровно такие
# мелочи и превращают вывод в то, что перестают читать.
if [ -n "$(sed -n 's/^NOTIFY_NTFY_URL=//p' "$REPO/.env" 2>/dev/null)" ]; then
    say "Беда уходит сразу в оба канала: дубль дешевле пропажи."
else
    say "Канал один, телеграм. Беда и вечерний пульс придут сюда же."
fi
