# SigmaSteamBot — стадия 2

Автозапуск и удалённое управление игрой **Sigma World Online** на Windows-VM
через Telegram-бота. Актуальное описание всего проекта — в корневом `README.md`.

## Что делает

- **При входе в систему** (автологон выбранного пользователя Windows) задача планировщика
  `SigmaSteamBot` запускает `supervisor.py`.
- `supervisor.py` держит один процесс с потоками:
  - **watchdog** — следит, что Steam и игра запущены; поднимает упавшее,
    шлёт события в Telegram, каждые 30 c пишет снапшот в `state.json`;
  - **bot** — long-polling Telegram Bot API **через SOCKS5-прокси**
    (`telegram.proxy`; если с VM `api.telegram.org` напрямую недоступен);
  - **webui** — HTTP-панель на `0.0.0.0:8080` (то же, что бот, + правка ролей и
    логи; вход по логину/паролю). Отключается `webui.enabled=false` в конфиге.
- Игра запускается через `steam://rungameid/1690980`.

## Файлы

| Файл | Назначение |
|---|---|
| `config.json` | токен, роли (админы/модераторы/главный админ), язык, прокси, пути, параметры watchdog, `steam_api_dll` / `steam_web_api_key` / `monitor` |
| `common.py` | конфиг, логи, `State`, клиент Telegram (обёртка над `curl.exe`) |
| `i18n.py` | двуязычные строки бота (ru/en) + `t()` |
| `sysinfo.py` | сбор ресурсов / состояния Steam / игры / сессий |
| `gamectl.py` | запуск-останов Steam и игры, перезагрузка VM |
| `watchdog.py` | цикл поддержания игры |
| `bot.py` | команды и кнопки Telegram, фоновый монитор сервера |
| `webui.py` | веб-панель: HTTP-поток в супервизоре, аутентификация (`webui_auth.json`), API, встроенный SPA, правка ролей на лету |
| `players.py` | вкладка «Игроки» веб-панели: список/онлайн игроков локального сервера из `analytics.txt` + `Data\users\*` + `Logs\game_state.txt` (пароли `Code` не отдаются) |
| `serverlist.py` | `fetch(cfg)` — список серверов: Steam-лобби, при ошибке Web API |
| `serverlist_steam.py` | перечисление Steam-лобби через `steam_api64.dll` игры (`ctypes`), отдельный процесс |
| `supervisor.py` | точка входа, single-instance lock |
| `install.ps1` | автологон + энергосбережение + регистрация задачи |
| `start.cmd` / `stop.cmd` | ручной запуск/останов |

## Установка (на VM)

```powershell
powershell -ExecutionPolicy Bypass -File <BASE>\install.ps1
Start-ScheduledTask -TaskName SigmaSteamBot
```

После этого бот пришлёт в Telegram «🟢 SigmaSteamBot запущен» с текущим статусом.

## Роли

- **admin** — ID из `telegram.allowed_user_ids`: полный доступ.
- **moderator** — ID из `telegram.moderator_user_ids`: только `/status`, `/shot`,
  `/restartgame`, `/login`, `/lang`. Watchdog/Steam/VM/`/stopbot` недоступны, кнопки
  этих действий не показываются. ID из обоих списков = админ.
- **главный админ** — `telegram.super_admin_id` (по умолчанию первый из
  `allowed_user_ids`): получает строку `👤 Имя (роль) → /команда` на каждое
  действие другого админа/модератора, **меняющее состояние** (start/stop/restart
  игры и Steam, `/login`, `/watchdog on|off`, подтверждённый ребут VM, `/stopbot`).
  `/status`, `/shot`, `/lang` не логируются.

Язык интерфейса — на пользователя (`/lang` или кнопка «🌐 Язык», значения `ru`/`en`),
хранится в `state.json`; по умолчанию `telegram.default_lang`.

## Команды бота

| Команда | Роль | Действие |
|---|---|---|
| `/status` | админ, модератор | CPU/RAM/диск, аптайм, Steam (работает + вошёл ли в аккаунт), игра (PID, время, RAM, окно, «не отвечает»/свёрнуто), сессия/RDP, счётчики перезапусков |
| `/shot` | админ, модератор | скриншот экрана VM |
| `/servers` | админ | список публичных серверов (Steam-лобби): имя, игроки, карта, версия; ваш сервер (`monitor.server_name`) — 👑 и наверху |
| `/restartgame` | админ, модератор | перезапуск игры **+ сразу авто-вход в мир**, в конце скриншот |
| `/login` | админ, модератор | пройти вход в игру вручную |
| `/lang ru\|en` | админ, модератор | язык интерфейса |
| `/startgame` `/stopgame` | админ | запуск / останов игры без входа |
| `/restartsteam` | админ | перезапуск Steam |
| `/restartvm` | админ | перезагрузка VM (с кнопкой подтверждения) |
| `/watchdog on\|off` | админ | вкл/выкл авто-поддержание |
| `/stopbot` | админ | остановить скрипт: `schtasks /Change /TN SigmaSteamBot /DISABLE` + выход процесса (с подтверждением). Игра/Steam не трогаются. Обратно — только с VM: `Enable-ScheduledTask -TaskName SigmaSteamBot; Start-ScheduledTask -TaskName SigmaSteamBot` |

Те же действия продублированы инлайн-кнопками под каждым ответом.
Доступ — только Telegram-ID из `allowed_user_ids` / `moderator_user_ids`; остальным
на `/start` бот отвечает их ID и больше ничего не делает.

**Поднять после `/stopbot`** (задача отключена, Telegram недоступен):
двойной клик по ярлыку «Запустить SigmaSteamBot» на рабочем столе VM
(скрипт `start-bot.cmd`), либо по SSH с Linux — `../deploy/start-bot.sh`,
либо вручную `Enable-ScheduledTask -TaskName SigmaSteamBot; Start-ScheduledTask -TaskName SigmaSteamBot`.

## Список серверов и монитор

Sigma World Online не регистрирует game-серверы в мастер-листе Valve — публичный
хост создаёт **Steam-лобби**. `/servers` и монитор берут список так:

- `serverlist_steam.py` отдельным процессом грузит `steam_api64.dll` **игры**
  (`config.json` → `steam_api_dll`) через `ctypes`, вызывает `SteamAPI_InitFlat`
  → `RequestLobbyList` → `GetLobbyData` (`server_name`, `players`, `max_players`,
  `build`). Нужна **консольная сессия** (там Steam) — супервизор в ней и работает.
- Запасной слой — Steam Web API `GetServerList` (`config.json` → `steam_web_api_key`,
  https://steamcommunity.com/dev/apikey). Пусто, пока игра не публикует game-серверы.

**Фоновый монитор** (поток `srvmonitor`, `config.json` → `monitor`): первая
проверка через ~90 c, дальше каждые `interval_seconds` (300). Нет `server_name`
(имя вашего сервера) `misses_before_alert` (2) проверок подряд → рассылка **всем
админам И модераторам** «⚠️ … пропал из списка серверов Steam»; повтор каждые
`repeat_alert_seconds` (3600, `0` = один раз); вернулся → «✅ … снова в списке»
один раз. Ошибка запроса списка — тик пропускается. Состояние монитора
(в `state.json`) переживает рестарт бота.

## Веб-панель

`webui.py` — HTTP-поток внутри супервизора, слушает `webui.host:webui.port`
(`0.0.0.0:8080`). `http://<ip-vm>:8080/`. Правило фаервола ставит `install.ps1`.
Обычный HTTP — **только для локальной сети**.

- **Вход** — `webui_auth.json` в `base_dir` (в `.gitignore`). Первый запуск →
  **`admin` / `admin`** с `must_change`: до смены пароля доступен только экран
  смены (≥6 символов, не `admin`). PBKDF2-HMAC-SHA256; cookie-сессия в памяти
  (12 ч); CSRF на POST; лок-аут по IP (5 неудач → 60 c).
- **Дашборд** — VM / Steam / игра / watchdog / внутренности бота / монитор + живой
  скрин. Автообновление 5 c при активной вкладке; статус из `last_snapshot`
  watchdog'а, живой `sysinfo.collect()` — по кнопке.
- **Действия** — `startgame` / `stopgame` / `restartgame` (перезапуск + вход) /
  `restartsteam` / `login` / `watchdog on|off` / `restartvm` / `stopbot` (с
  подтверждением) + `restarttask` (перезапуск задачи через одноразовую задачу
  планировщика — переживает `schtasks /End` самого себя) и `testalert`. Длинные
  операции — заданием с опросом результата. Перед `stopgame`/`restartgame`/
  `restartsteam`/`restartvm` (если игра запущена) — `exit1.txt` в корень мира +
  3 минуты на штатную остановку, и 5-минутный лок на эти 4 действия между
  админами/модераторами (см. `gamectl.request_world_exit` / `common.try_action_lock`).
- **Серверы** — тот же `serverlist.fetch` (кэш 45 c).
- **Роли** — `allowed_user_ids` / `moderator_user_ids` / `super_admin_id` /
  `default_lang` / `alerts_enabled`. Сохранение → `common.save_config` (UTF-8 без
  BOM) + `Bot.apply_roles` на лету, без перезапуска. `login_flow` из веба не
  правится.
- **Логи** — хвост `supervisor.log` (фильтр по уровню, автообновление, скачивание),
  аудит панели `webui_audit.log` (кто/когда/что), галерея `logs/nav/*.png`.
- **Игроки** — `players.py`: список и онлайн-статус игроков локального сервера из
  файлов игры (`...\LocalServer\<мир>\analytics.txt` — журнал
  `register|enter|exit`, час бывает однозначным; `Data\users\user_list.json` —
  id→имя; `Data\users\user<N>.json` — часы/уровень/роль/бан; `Logs\game_state.txt`
  — онлайн по картам). Каталог мира — `config.json → players`
  (`localserver_root` / `world` / `world_dir`, пусто = автоопределение по свежести
  `analytics.txt`). Кэш 15 c. **Пароли игроков (`Code`) в выдачу списка/карточки не попадают.**
  Клик по нику → **карточка игрока**: профиль, клан (`clans.json`) + друзья
  (`friends.json`), исследования/миссии (время до конца — по `serverTime` из
  `Data\game\settings.json`), позиция/территории, аватар (статы, навыки,
  способности (русские названия из клиентской локализации, `resources.assets`)
  и инвентарь с именами из `Data\{items,ability}.json`), сессии +
  гистограмма по часам суток, история (роли/смерти/земли/награды из `Logs\`),
  публичный чат игрока (`chat_0..3.txt`). Под **своим паролем панели** (повторная
  проверка + throttle + аудит): кнопки «Показать пароль» игрока (`code`) и
  «Приваты и IP» (`chat_privat.txt` + `log_net_ip.txt`).
  **Правка инвентаря** — только для **оффлайн**-игрока (последнее событие `exit`,
  повторная проверка перед записью), тоже под паролем панели: выдать на склад
  (`life = items.life×90000` или как у существующей записи; разбивка по `stack`) /
  изъять со склада или из инвентаря при себе (`unit<id>.json`). Бэкап файла в
  `logs\game_edits\<ts>\`, правится только `Inventory.items`, атомарная запись +
  guard по `mtime`. Аудит: «ИНВЕНТАРЬ игрока #N: give/take …».

Двуязычно ru/en (тумблер в шапке), тёмная/светлая тема.

## Логи и состояние

- `logs/supervisor.log` (ротация 5 МБ × 3)
- `state.json` — счётчики + последний снапшот + `user_lang` + состояние монитора
- `webui_auth.json` — логин/хэш пароля панели; `webui_audit.log` — действия из панели
- `supervisor.lock` — PID работающего экземпляра

## Отключить

```powershell
Disable-ScheduledTask -TaskName SigmaSteamBot   # не стартовать при входе
Stop-ScheduledTask   -TaskName SigmaSteamBot    # остановить сейчас
```

Автологон снять: в `HKLM\SOFTWARE\Microsoft\Windows NT\CurrentVersion\Winlogon`
установить `AutoAdminLogon=0` и удалить `DefaultPassword`.

## Известные ограничения

- Игра рендерится в той сессии, куда вошёл автологон (консоль). Если подключиться
  по RDP и **отключиться** (не выйти), сессия уезжает на RDP-станцию и Unity-игра
  может свернуться/перестать рендерить. Обходной путь (не входит в стадию 2):
  задача на событие отключения RDP, выполняющая `tscon <id> /dest:console`.
- `max_restarts_per_hour` (по умолчанию 8) защищает от «шторма» перезапусков —
  при достижении лимита watchdog приостанавливает авто-действия и пишет об этом.
- Прокси из `telegram.proxy` должен быть доступен; при его недоступности бот
  молча ретраит, а watchdog продолжает держать игру локально.
