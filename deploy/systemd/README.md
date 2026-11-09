# Расписание на Mac mini: systemd user-юниты

С 09.11.2026 вместо `scripts/box_crontab`. На боксе sudo не было, поэтому
был cron, а часы в нем шли по UTC. Сейчас сбор идет прямо на машине под
пользователем `nata`, пояс системы Europe/Moscow, и время в `OnCalendar`
московское.

| юнит | когда | что |
|---|---|---|
| rent-daily | каждый день 11:00 | срез 1-2 комнат, `box_daily.sh narrow` |
| rent-weekly | воскресенье 13:00 | добор 3+, ждет замок среза до трех часов |
| rent-price | воскресенье 16, 18, 20 | переобучение модели цены, первая удачная попытка, остальные выходят |
| rent-site | 19:30 | витрина на GitHub через ключ развертывания |
| rent-backup | 20:45, 21:45, 22:45 | облачная копия, первая удачная попытка |
| rent-report | 23:50 | итог дня в телеграм |
| rent-pulse | раз в 5 минут | память, диск, отметка heartbeat на GitHub |
| rent-bot | всегда | бот, перезапуск при любом выходе |

`Persistent=true` у таймеров: пропущенный из-за простоя запуск делается
сразу после включения машины. Это заменяет догон, который на макбуке
делал `mac_boot_catchup.sh`.

Сторожа `box_watch.sh` здесь нет намеренно: он запускал полный обход, как
только за сутки собрано меньше нормы, то есть каждую ночь после
полуночи. Нужен был, когда площадка после аварии 11.10.2026 отвечала
рывками; с 29.10 по 09.11 на макбуке хватало одного среза в день.

Установка (таймеры работают без входа в систему, нужен
`sudo loginctl enable-linger nata`, сделан 09.11):

```sh
mkdir -p ~/.config/systemd/user
cp ~/rent_msk/deploy/systemd/rent-*.{service,timer} ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now rent-{daily,weekly,price,site,backup,report,pulse}.timer
systemctl --user enable --now rent-bot.service
systemctl --user list-timers 'rent-*'
```

Бот и бот на макбуке на одном токене не уживаются (ошибка 409), поэтому
`rent-bot` включается только после выгрузки задачи бота на макбуке.
