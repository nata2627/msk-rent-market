# Образ сбора: Python 3.11, настоящий Google Chrome и Xvfb.
#
#   docker build -t rent-collector -f docker/collector/Dockerfile .
#
# Рецепт восстановлен 09.11.2026: прежний образ собирался прямо на боксе
# Mac mini и умер вместе с его SSD, в репозитории было только описание.
#
# Chrome настоящий, а не Chromium Playwright: площадка отвечает 403 на
# HeadlessChrome и пускает обычный Chrome в окне. Окно дает Xvfb внутри
# контейнера, запуск в scripts/box_daily.sh (Xvfb отдельной командой, не
# xvfb-run, --shm-size=1g, CHROME_NO_SANDBOX=1). Браузеры Playwright не
# качаются намеренно. Код проекта в образ не кладется, он монтируется в
# /work при запуске.
FROM python:3.11-slim-bookworm

ENV DEBIAN_FRONTEND=noninteractive \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    TZ=Europe/Moscow

RUN apt-get update \
 && apt-get install -y --no-install-recommends \
      ca-certificates curl gnupg tzdata xvfb \
      fonts-liberation fonts-noto-core fonts-noto-color-emoji \
 && curl -fsSL https://dl.google.com/linux/linux_signing_key.pub \
      | gpg --dearmor -o /usr/share/keyrings/google-chrome.gpg \
 && echo "deb [arch=amd64 signed-by=/usr/share/keyrings/google-chrome.gpg] https://dl.google.com/linux/chrome/deb/ stable main" \
      > /etc/apt/sources.list.d/google-chrome.list \
 && apt-get update \
 && apt-get install -y --no-install-recommends google-chrome-stable \
 && rm -rf /var/lib/apt/lists/*

COPY requirements.txt /tmp/requirements.txt
RUN pip install -r /tmp/requirements.txt && rm /tmp/requirements.txt

WORKDIR /work
