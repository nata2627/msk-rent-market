#!/bin/bash
# Пуш правок, сделанных прямо на Mac mini, в main на GitHub.
#
#   scripts/box_push.sh            # тесты, свежий main, пуш
#   scripts/box_push.sh --no-test  # без тестов, только если они уже прошли
#
# Зачем. Правка на дежурстве делается здесь же, в ~/rent_msk под nata, и
# уходит на GitHub ключом развертывания ~/.ssh/rent_site_deploy (доступ
# только к этому репозиторию), тем же, которым сервер каждый день пушит
# числа README. Отдельного доступа к GitHub дежурному не нужно.
#
# Коммитить под тем же автором, что и сервер:
#   git -c user.name=nata2627 -c user.email=... commit
# Скрипт отказывается пушить чужого автора, чтобы история не разъехалась.
#
# Перед пушем: тесты и свежий main с GitHub через rebase. Сервер сам
# пушит в main (числа README днем, витрина в 19:30), и без rebase пуш
# отказал бы.

set -uo pipefail
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO" || exit 1
export GIT_SSH_COMMAND="ssh -i $HOME/.ssh/rent_site_deploy -o IdentitiesOnly=yes -o BatchMode=yes -o ConnectTimeout=20"
REMOTE="git@github.com:nata2627/msk-rent-market.git"
AUTHOR="nata2627"

[ "$(git rev-parse --abbrev-ref HEAD)" = main ] || { echo "Не на ветке main"; exit 1; }
if ! git diff --quiet || ! git diff --cached --quiet; then
  echo "Есть незакоммиченные правки, сначала коммит:"
  git status --short
  exit 1
fi

git fetch -q "$REMOTE" main || { echo "GitHub не отвечает"; exit 1; }
AHEAD="$(git rev-list --count FETCH_HEAD..HEAD)"
[ "$AHEAD" -gt 0 ] || { echo "Пушить нечего"; exit 0; }

BAD="$(git log --format='%an' FETCH_HEAD..HEAD | grep -vx "$AUTHOR" | sort -u)"
if [ -n "$BAD" ]; then
  echo "Чужой автор в коммитах ($BAD). Переписать: git commit --amend --reset-author с user.name=$AUTHOR"
  exit 1
fi

# Хук формулировок, если он есть в комплекте дежурного: те же правила, что
# у коммитов с макбука.
PAT_FILE="$HOME/rent_msk_handoff/hooks/rent-wording.pat"
if [ -f "$PAT_FILE" ]; then
  HITS="$( { git log --format=%B FETCH_HEAD..HEAD; git diff -U0 FETCH_HEAD...HEAD | grep '^+[^+]'; } \
           | grep -i -E "$(cat "$PAT_FILE")")"
  if [ -n "$HITS" ]; then
    echo "Недопустимые формулировки:"
    echo "$HITS" | head -10
    exit 1
  fi
fi

if [ "${1:-}" != "--no-test" ]; then
  .venv/bin/python -m pytest -q -x 2>&1 | tail -3
  [ "${PIPESTATUS[0]}" = 0 ] || { echo "Тесты упали, пуш отменен"; exit 1; }
fi

git pull -q --rebase "$REMOTE" main || { echo "Rebase не сошелся, разобрать руками: git status"; exit 1; }
git push -q "$REMOTE" HEAD:main && echo "Запушено коммитов: $AHEAD" && git log --oneline -"$AHEAD"
