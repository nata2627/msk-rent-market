#!/bin/bash
# Числа панели в README на Mac mini: индекс, подстановка, коммит и пуш.
#
# Зачем. На макбуке это делал daily.sh, а пушили руками. С 09.11.2026 база
# живет на Mac mini, и без этого шага README застыл бы на дне переезда.
# Решено 09.11: сервер пушит сам, как уже пушит витрину.
#
# Что может уйти на GitHub без проверки, и почему это безопасно:
#
# 1. Только список NUM_PATHS, никаких `git add -A`. Текст меняется лишь
#    внутри меток <!--n:...-->, это делает src.readme_numbers.
# 2. Сообщение коммита фиксированное.
# 3. Запускается из box_daily.sh только после ежедневного среза без
#    единого сбоя: неполный день (как 06.11) в README не попадает.
# 4. Перед подстановкой забирается свежий main. Если файлы из списка уже
#    изменены или main не сводится без конфликта, коммита и пуша нет,
#    уходит сообщение в телеграм.
#
# Пуш через ключ развертывания ~/.ssh/rent_site_deploy, тот же, что у
# витрины: доступ только к этому репозиторию.

set -uo pipefail
export TZ=Europe/Moscow
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO" || exit 1
PY="$REPO/.venv/bin/python"
say() { echo "$(date +%H:%M:%S) === $*"; }
notify() { bash "$REPO/scripts/box_notify.sh" "$@" >/dev/null 2>&1; }
export GIT_SSH_COMMAND="ssh -i $HOME/.ssh/rent_site_deploy -o IdentitiesOnly=yes -o BatchMode=yes -o ConnectTimeout=20"
REMOTE="git@github.com:nata2627/msk-rent-market.git"
DAY="$(date +%d.%m.%Y)"

NUM_PATHS=(README.md methods results/panel_index.json results/panel_index_3.json
           figures/panel_index.png figures/panel_index_3.png)

if ! git diff --quiet -- "${NUM_PATHS[@]}" || ! git diff --cached --quiet; then
  say "Числа README: в файлах чужие правки, не трогаю"
  notify numbers-dirty "Числа README не обновлены
$DAY

На Mac mini в рабочей копии правки в README или methods, автоматический коммит смел бы их. Разобраться на машине: git status в ~/rent_msk."
  exit 1
fi

if ! timeout 120 git pull -q --rebase "$REMOTE" main; then
  git rebase --abort 2>/dev/null
  say "Числа README: свежий main не забрался"
  notify numbers-pull "Числа README не обновлены
$DAY

Свежий main с GitHub не забрался или не свелся. Данные панели целы, README покажет вчерашние числа."
  exit 1
fi

say "Индекс"
"$PY" -m src.panel_index >/dev/null 2>&1 || say "Индекс — СБОЙ, подставляю что есть"
say "Числа README"
if ! "$PY" -m src.readme_numbers >/dev/null 2>&1; then
  git checkout -- "${NUM_PATHS[@]}" 2>/dev/null
  say "Числа README — СБОЙ подстановки"
  notify numbers-fail "Числа README не подставились
$DAY

src.readme_numbers упал, README не тронут. Данные панели целы."
  exit 1
fi

if git diff --quiet -- "${NUM_PATHS[@]}"; then
  say "Числа README — без изменений"
  exit 0
fi

git add -u -- "${NUM_PATHS[@]}"
git -c user.name=nata2627 -c user.email=gordeevanatalia04@gmail.com \
  commit -q -m "Числа панели за $DAY"
# Витрина пушит в тот же main из ~/rent_site, поэтому одна повторная
# попытка после свежего pull.
if timeout 120 git push -q "$REMOTE" HEAD:main \
   || { timeout 120 git pull -q --rebase "$REMOTE" main && timeout 120 git push -q "$REMOTE" HEAD:main; }; then
  say "Числа README — запушено"
else
  say "Числа README — пуш не прошел"
  notify numbers-push "Числа README закоммичены, но не ушли на GitHub
$DAY

Коммит лежит на Mac mini в ~/rent_msk, следующий прогон попробует снова."
  exit 1
fi
