<!-- SPDX note: private project — TaidremRU/sigmabot_win -->

# SigmaSteamBot

**RU:** Автозапуск, авто-вход и Telegram-управление игрой **Sigma World Online** (Steam AppID `1690980`) на выделенной Windows-VM. Держит Steam и игру запущенными, сам проходит вход в мир, отдаёт статус/скриншоты и список публичных серверов в Telegram, следит за присутствием нужного сервера в списке Steam-лобби.

**EN:** Auto-start, auto-login and Telegram control for the game **Sigma World Online** (Steam AppID `1690980`) on a dedicated Windows VM. Keeps Steam and the game running, walks the in-game login itself, serves status / screenshots / the public server list over Telegram, and watches whether a specific server stays present in the Steam lobby list.

**Языки / Languages:** [Русский](#русский) · [English](#english)

---

## Русский

### Что это

Один процесс (`supervisor.py`), запускаемый задачей планировщика `SigmaSteamBot` при входе в систему (автологон `alex`). Внутри — два потока:

| Поток | Задача |
|---|---|
| **watchdog** | следит, что Steam и игра запущены; поднимает упавшее; после старта игры сам проходит вход в мир; каждые ~30 c пишет снапшот в `state.json` |
| **bot** | Telegram long-polling **через SOCKS5-прокси** (с VM `api.telegram.org` напрямую недоступен); команды, кнопки, алерты |
| **webui** | HTTP-панель на `0.0.0.0:8080` — то же, что бот, + правка ролей и просмотр логов; вход по логину/паролю |
| *(+ поток `srvmonitor`)* | раз в 5 минут проверяет, есть ли сервер `AstralSigma` в списке публичных Steam-лобби |

Вход в игру (`Game → Local → ✔ Show server in the public Steam list → Play → окно Login → Ok`) прогоняется отдельной задачей `SigmaNav` — фоновому потоку не даёт фокус окна `SetForegroundWindow`. Игра рендерится и принимает ввод только в **активной консольной сессии**; задача `SigmaConsoleGuard` возвращает сессию на консоль при отключении RDP.

### Архитектура

```
Задача SigmaSteamBot (AtLogon, Interactive/Highest, авто-рестарт каждые 2 мин)
  └─ venv\pythonw.exe supervisor.py                     (single-instance по supervisor.lock)
       ├─ watchdog   держит Steam+игру → авто-вход → state.json
       ├─ bot        Telegram long-poll через SOCKS5 (обёртка над curl.exe)
       │              ├─ поток-отправитель (сеть не блокирует опрос)
       │              └─ srvmonitor: раз в 5 мин serverlist.fetch → есть ли AstralSigma
       ├─ webui      http.server на 0.0.0.0:8080 — тот же функционал + роли + логи
       │              (статус из last_snapshot watchdog'а, живой collect() по кнопке)
       └─ вход в игру → runner.run_nav("seq login")
              └─ Start-ScheduledTask SigmaNav → nav.py (detect состояния экрана → клики)

Задача SigmaConsoleGuard (SYSTEM, по событию отключения RDP)
  └─ console_guard.ps1 → tscon <сессия> /dest:console

Кнопка «Серверы» / монитор:
  serverlist.fetch → serverlist_steam.py (отдельный процесс)
       └─ ctypes + steam_api64.dll игры → RequestLobbyList → список Steam-лобби
       └─ запасной слой: Steam Web API GetServerList (нужен ключ)
```

### Компоненты

| Файл | Назначение |
|---|---|
| `supervisor.py` | точка входа, lock, запуск watchdog + bot |
| `watchdog.py` | цикл поддержания Steam/игры, авто-вход, алерты |
| `bot.py` | команды и inline-кнопки Telegram, роли, аудит, фоновый монитор сервера |
| `webui.py` | веб-панель (HTTP-поток в супервизоре): аутентификация, API, встроенный SPA; правка ролей на лету через `bot.apply_roles` |
| `players.py` | вкладка «Игроки» веб-панели: список и онлайн-статус игроков локального сервера из файлов игры (`analytics.txt`, `Data\users\*`, `Logs\game_state.txt`); пароли (`Code`) не отдаются |
| `common.py` | конфиг (`load_config` / `save_config` без BOM), логи, `State` (в `state.json`), клиент Telegram поверх `curl.exe` + SOCKS5 |
| `i18n.py` | двуязычные строки (`ru`/`en`) + `t()`; паритет ключей проверяет `selftest.py` |
| `gamectl.py` | старт/стоп/рестарт Steam и игры, перезагрузка VM, отключение задачи бота |
| `sysinfo.py` | сбор ресурсов, состояния Steam/игры/сессий |
| `nav.py` / `detect.py` / `runner.py` | последовательность входа в мир (по пикселям экрана) |
| `serverlist.py` | `fetch(cfg) → (ok, servers, source)`: сначала Steam-лобби, при ошибке — Web API |
| `serverlist_steam.py` | перечисление Steam-лобби через `steam_api64.dll` игры (`ctypes`), отдельный процесс |
| `screenshot.py` | снимок окна игры через `PrintWindow` (работает в простаивающей сессии) |
| `console_guard.ps1` | возврат сессии на консоль при отключении RDP |
| `install.ps1` | автологон + отключение сна + регистрация 3 задач + ярлык на рабочем столе |
| `selftest.py` | быстрая проверка модулей/конфига/i18n/Telegram без входа в бесконечный цикл |

### Роли

- **admin** — ID из `telegram.allowed_user_ids`: полный доступ.
- **moderator** — ID из `telegram.moderator_user_ids`: только `/status`, `/shot`, `/restartgame`, `/login`, `/lang`. Кнопки админ-действий не показываются; `/servers`, watchdog, Steam, VM, `/stopbot` недоступны. ID из обоих списков считается админом.
- **главный админ** — `telegram.super_admin_id` (по умолчанию первый из `allowed_user_ids`): получает строку `👤 Имя (роль) → действие` на каждое действие другого админа/модератора, **меняющее состояние** (start/stop/restart игры и Steam, `/login`, `/watchdog on|off`, подтверждённый ребут VM, `/stopbot`). `/status`, `/shot`, `/lang`, `/servers` не логируются.

Язык — на пользователя (`/lang ru|en` или кнопка «🌐 Язык»), хранится в `state.json`; по умолчанию `telegram.default_lang`.

### Команды бота

| Команда / кнопка | Роль | Действие |
|---|---|---|
| `/status` | админ, модератор | CPU/RAM/диск, аптайм; Steam (+вход в аккаунт); игра (PID, RAM, окно, «в меню / вход выполняется / в игре»); сессия/RDP; счётчики перезапусков |
| `/shot` | админ, модератор | скриншот окна игры |
| `/servers` | **админ** | список публичных серверов (Steam-лобби): имя, игроки, карта, версия. **AstralSigma** подсвечивается 👑 и поднимается наверх |
| `/restartgame` | админ, модератор | перезапуск игры **+ сразу авто-вход в мир**, в конце скриншот |
| `/login` | админ, модератор | пройти вход в игру вручную |
| `/lang ru\|en` | админ, модератор | язык интерфейса |
| `/startgame` `/stopgame` | админ | запуск / останов игры без входа |
| `/restartsteam` | админ | перезапуск Steam |
| `/restartvm` | админ | перезагрузка VM (с подтверждением) |
| `/watchdog on\|off` | админ | авто-поддержание игры |
| `/stopbot` | админ | отключить задачу `SigmaSteamBot` и завершить процесс (с подтверждением). Игра и Steam не трогаются. Обратно — только с VM |

Те же действия продублированы inline-кнопками. Неизвестным ID на `/start` бот отвечает их numeric ID и больше ничего.

### Список серверов

Sigma World Online **не регистрирует game-серверы** в мастер-листе Valve — каждый публичный хост создаёт **Steam-лобби** (`ISteamMatchmaking::CreateLobby`). Поэтому:

- **Основной путь** — `serverlist_steam.py` отдельным коротким процессом грузит `steam_api64.dll` **самой игры** через `ctypes`, вызывает `SteamAPI_InitFlat` → `RequestLobbyList` → читает `GetLobbyData` (`server_name`, `world_name`, `players`, `max_players`, `build`). Требует **консольную сессию** (там Steam) — супервизор в ней и работает, поэтому обычного `subprocess` достаточно.
- **Запасной слой** — Steam Web API `IGameServersService/GetServerList` (нужен ключ `steam_web_api_key`, https://steamcommunity.com/dev/apikey). Пусто, пока игра не публикует настоящие серверы — оставлено на будущее.

### Фоновый монитор сервера

Поток `srvmonitor` в супервизоре. Первая проверка через ~90 c после старта, дальше каждые `monitor.interval_seconds` (по умолчанию 300):

- нет `monitor.server_name` в списке **`misses_before_alert`** проверок подряд (2 ≈ 10 мин) → рассылка **всем админам И модераторам** `⚠️ <name> пропал из публичного списка серверов Steam`;
- пока отсутствует — тишина, но раз в `repeat_alert_seconds` (3600, `0` = один раз) — напоминание;
- вернулся → `✅ <name> снова в публичном списке` (один раз), счётчик сбрасывается;
- запрос списка не удался → тик пропускается (не считается «пропажей»), только запись в лог.

Состояние (`monitor_astral` в `state.json`) переживает перезапуск бота — повторных ложных тревог после рестарта нет.

### Веб-панель

HTTP-поток внутри супервизора (`webui.py`), слушает `webui.host:webui.port` (по умолчанию `0.0.0.0:8080`) — `http://<ip-vm>:8080/`. Правило фаервола на порт ставит `install.ps1`. Протокол обычный HTTP — панель **только для локальной сети**.

- **Вход** — логин/пароль из `webui_auth.json` (в `base_dir`, в `.gitignore`). При первом запуске создаётся **`admin` / `admin`** с флагом `must_change`: до смены пароля доступен только экран смены (минимум 6 символов, не `admin`). Хэш — PBKDF2-HMAC-SHA256; сессия — cookie `sid` в памяти процесса (TTL 12 ч); POST защищены CSRF-токеном; неудачные входы — лок-аут по IP (5 попыток → пауза 60 c).
- **Дашборд** — карты VM / Steam / игра / watchdog / внутренности бота (аптайм процесса, потоки, очередь отправки, возраст снапшота) / монитор сервера, плюс живой скриншот экрана VM. Автообновление раз в 5 c только при активной вкладке; статус берётся из `last_snapshot`, который пишет watchdog, — нагрузки на супервизор почти нет. Кнопка «Обновить (живой опрос)» дёргает `sysinfo.collect()` по требованию.
- **Действия** — то же, что у бота: `startgame` / `stopgame` / `restartgame` (перезапуск + вход) / `restartsteam` / `login` / `watchdog on|off` / `restartvm` / `stopbot` (с подтверждением) + `restarttask` (чистый перезапуск задачи `SigmaSteamBot` отдельным процессом) и `testalert` (тестовое сообщение админам через `bot.push_alert`). Длинные операции идут заданием, панель опрашивает результат.
- **Серверы** — тот же список Steam-лобби (`serverlist.fetch`, кэш 45 c), AstralSigma наверху.
- **Роли** — правка `telegram.allowed_user_ids` / `moderator_user_ids` / `super_admin_id` / `default_lang` / `alerts_enabled`. Сохранение пишет `config.json` в чистом UTF-8 (`common.save_config`, без BOM) и **применяет роли на лету** (`Bot.apply_roles`) — перезапуск не нужен. Координаты `login_flow` из веба не редактируются намеренно.
- **Логи** — хвост `supervisor.log` (фильтр по уровню, автообновление, скачивание), аудит панели (`webui_audit.log` — кто/когда/что нажал, отдельно от Telegram-аудита) и галерея скринов последовательности входа (`logs/nav/*.png`).
- **Игроки** — список и онлайн-статус игроков локального сервера (см. ниже).

Интерфейс двуязычный (ru/en, тумблер в шапке, выбор в `localStorage` браузера), тёмная/светлая тема. Отключить панель целиком — `webui.enabled = false` в `config.json`.

### Чат сервера и события

Вкладка **«Чат»** (суб-вкладки):

- **Чат сервера** — публичный чат `chat_0..3.txt` (все каналы; фильтр по каналу и поиск; автообновление 5 c), ники — ссылки на карточки.
- **События** — сводная лента `join`/`leave`/`register` (`analytics.txt`), `death` (`dead_user.txt`), `land` (`delete_land*.txt`) + история выдач ролей (`user_role.txt`).
- **Приваты** — все приватные сообщения сервера (`chat_privat.txt`) под паролем панели + аудит.

### Статы

Вкладка **«Статы»** — сводная аналитика:

- **Экспорт / бэкап** — список игроков в **CSV** (без паролей); **бэкап каталога мира** в zip под паролем панели: `state` (`analytics` + `Data\{users,units,game}` + `Logs`, без бинарных `map*.dt`, ~2 МБ) или `full` (всё). Последние 5 хранятся в `logs\backups\`.
- **Онлайн (7 дней)** — реконструкция числа онлайн по времени из `enter`/`exit`, пик за 7 д, сейчас.
- **Рост** — регистрации и DAU по дням, retention D1 / D7.
- **Топы** — по уровню, по часам; **кланы** (`clans.json` по рейтингу); **бан-лист**; **стафф** + история ролей; **помесячный топ** (`reward_order.txt`); распределения по уровням и странам.
- **Мир · карты** — онлайн / аватары / территории по картам; **территории** — все `userTerritories` 286 игроков с владельцами (фильтр по карте). Существа/животные — в бинарных `map*.dt`, в JSON недоступны.
- **Здоровье сервера** — последний `Server ready` (`world_performance.txt`: `startupMs`, кластеры, `managedMb`) + медленные фазы старта; **лаг-события** (медленные тики из `time_shedule*.txt` — всего, по дням, топ функций); ошибки коннекта (`error_game*.txt`). `memory_log.txt` игрой не заполняется.

### Твинки

Вкладка **«Твинки»** — детект аккаунтов, заходивших с одного IP, из `Logs\log_net_ip.txt` (`ник = IP = InterNetwork = порт`). Открывается после ввода **своего пароля от панели** (IP + связывание аккаунтов — чувствительно; попытка пишется в аудит). Показывает группы: IP → список аккаунтов (ID, ник со ссылкой на карточку, число подключений, первый/последний вход, другие IP этого аккаунта), отсортировано по размеру группы. Порог «мин. аккаунтов на IP» настраивается (по умолчанию 2). `config.json → players.twink_ignore_ips` — какие IP не учитывать (напр. локальный релей `127.0.0.2`, через который на текущей VM идут все клиенты — там реальных IP в логе нет).

### Игроки локального сервера

Вкладка «Игроки» веб-панели (`players.py`). Читает файлы, которые пишет сам локальный сервер игры, из каталога активного мира под `%USERPROFILE%\AppData\LocalLow\Crematorium of Time\SigmaWorld\SigmaWorld\LocalServer\<мир>\`:

| Файл | Что берётся |
|---|---|
| `analytics.txt` | журнал `ДД.ММ.ГГГГ Ч:ММ:СС: register\|enter\|exit <id> [<сек>]` (час бывает однозначным). Онлайн = последнее событие игрока `enter`; `exit` несёт длину сессии в секундах; отсюда же лента последних событий |
| `Data\users\user_list.json` | `id → имя` (поле `Code` — **пароль игрока, наружу не отдаётся**) |
| `Data\users\user<N>.json` | профиль: всего часов (`timeGame`), уровень (`unitLevel`), роль (`role`: 0 игрок, 1 модератор, 2 админ, 3 GM), бан (`isBlock`/`timeBan`), карта, клан, страна |
| `Logs\game_state.txt` | авторитетные счётчики онлайна по картам (без имён) — показываются рядом с оценкой по `analytics.txt` |

Каталог мира — `config.json → players`: `localserver_root` (пусто = путь по умолчанию выше), `world` / `world_dir` (пусто = мир с самым свежим `analytics.txt`). Данные кэшируются в панели на 15 c. Пароли (`code` / `Code`) вырезаются на сервере и в выдачу карточки/списка не попадают. Отключить — `players.enabled = false`.

**Карточка игрока** (клик по нику в таблице или ленте) — модальное окно:

- **Профиль:** уровень, рейтинг, страна, видеокарта, разрешение экрана, всего часов, «последняя сессия N ч назад», бан + когда снят
- **Клан** (из `Data\game\clans.json`): имя, рейтинг, состав с именами (👑 лидер) и переходами на карточки · **Друзья** (из `friends.json`)
- **Исследования:** текущее + ~время до конца (по `serverTime` из `Data\game\settings.json`), изучено техов, бустер · **Миссии**
- **Позиция:** карта / координаты / точка респавна / список территорий
- **Аватар:** статы (`paramList`) полосками, навыки, способности с именами (из `Data\ability.json`), склад и «при себе» с названиями предметов (из `Data\items.json`)
- **Сессии:** всего / часов онлайн / средняя / макс, гистограмма активности по часам суток, последние 15 сессий
- **История:** смены ролей (`user_role.txt`), смерти/сбросы (`dead_user.txt`), снос земель (`delete_land*.txt`), месячные награды (`reward_order.txt`)
- **Чат игрока:** его публичные сообщения из `chat_0..3.txt` (канал + время)
- **Под своим паролем панели** (повторная проверка `auth.verify` + throttle + аудит): кнопка **«Показать пароль»** игрока (`code`) и кнопка **«Приваты и IP»** — приватные сообщения игрока (`chat_privat.txt`) и история IP (`log_net_ip.txt`). Каждый показ пишется в `webui_audit.log`.
- **Правка инвентаря — только для оффлайн-игрока** (последнее событие в `analytics.txt` = `exit`; повторная проверка прямо перед записью; при входе игрока запись отменяется). Тоже под своим паролем панели. **Выдать на склад** (`user<N>.json → Inventory.items`): предмет по имени/id из `Data\items.json` + количество, с разбивкой по `stack`, `life` = `items.life × 90000` (или копируется у уже имеющейся записи того же типа), инструментам ставится полная `durability`. **Изъять** со склада или из инвентаря при себе (`unit<id>.json → Inventory.items`): уменьшает `count`, не больше, чем есть. Перед каждой записью — бэкап файла в `logs\game_edits\<ts>\`; правится только `Inventory.items`; атомарная запись; guard по `mtime` (если файл изменился между чтением и записью — отмена). Всё пишется в `webui_audit.log` («ИНВЕНТАРЬ игрока #N: give/take …»).

### Установка

Подробный гайд — [`deploy/README.md`](deploy/README.md). Кратко: скопировать `deploy\` на VM → `setup.bat [BASE] [/autologon USER PASS]` → заполнить `%BASE%\config.json` → `selftest.py` → `Start-ScheduledTask -TaskName SigmaSteamBot`. Шаблон конфига — [`deploy/app/config.example.json`](deploy/app/config.example.json).

### Ключевые поля `config.json`

| Поле | Назначение |
|---|---|
| `game_appid` | `1690980` |
| `base_dir` | папка установки (с `\\`) |
| `steam_api_dll` | путь к `steam_api64.dll` **игры** (для `/servers` и монитора) |
| `steam_web_api_key` | ключ Steam Web API — запасной путь списка серверов |
| `python_exe` | python для дочерних скриптов; пусто → авто (`sys.executable`, `pythonw`→`python`) |
| `monitor` | `{ enabled, server_name, interval_seconds, misses_before_alert, repeat_alert_seconds }` |
| `webui` | `{ enabled, host, port }` — веб-панель; `0.0.0.0:8080` по умолчанию. Креды — в `webui_auth.json` (не в конфиге) |
| `players` | `{ enabled, localserver_root, world, world_dir }` — вкладка «Игроки»; всё пусто = автоопределение каталога мира |
| `telegram.token` | токен @BotFather |
| `telegram.allowed_user_ids` / `moderator_user_ids` / `super_admin_id` | роли (правятся и из веб-панели) |
| `telegram.default_lang` | `ru` \| `en` |
| `telegram.proxy` | `socks5h://HOST:PORT` — клиент всегда идёт через SOCKS5 |
| `game_window_size` / `login_flow` | координаты кликов входа (жёстко под 1024×768) |
| `watchdog.*` | авто-поддержание, лимит перезапусков, тайминги авто-входа |

`config.json` и `key.txt` — в `.gitignore` (секреты). Шаблон — `deploy/app/config.example.json`.

### Эксплуатация

```powershell
Start-ScheduledTask   -TaskName SigmaSteamBot   # запустить
Stop-ScheduledTask    -TaskName SigmaSteamBot   # остановить сейчас
Disable-ScheduledTask -TaskName SigmaSteamBot   # не стартовать при входе
Get-Content <BASE>\logs\supervisor.log -Tail 40 -Wait
<BASE>\venv\Scripts\python.exe <BASE>\selftest.py
```

После `/stopbot` (задача отключена, Telegram не поможет): ярлык **«Запустить SigmaSteamBot»** на рабочем столе VM, либо по SSH — `deploy/start-bot.sh`, либо вручную `Enable-ScheduledTask` + `Start-ScheduledTask`.

### Ограничения

- Координаты `login_flow` рассчитаны под окно **1024×768**; другое разрешение → пересобрать координаты (`deploy/README.md`, §5).
- Всё UI-взаимодействие требует **активной консольной сессии**; при заходе по RDP и отключении сессию возвращает `SigmaConsoleGuard`.
- SOCKS5-прокси должен быть доступен; при недоступности бот молча ретраит, watchdog продолжает держать игру локально.
- В публичном списке лобби обычно 1 запись (нишевая игра) — мультисписок в `/servers` протестирован на синтетике.
- Язык игры иногда самопроизвольно переключается на Polski (клик по стрелке языка) — на координаты не влияет.

---

## English

### What it is

A single process (`supervisor.py`) launched by the `SigmaSteamBot` scheduled task at logon (auto-logon as `alex`). It runs two threads:

| Thread | Job |
|---|---|
| **watchdog** | keeps Steam and the game running; relaunches whatever died; after the game starts, walks the in-game login itself; writes a `state.json` snapshot every ~30 s |
| **bot** | Telegram long-polling **through a SOCKS5 proxy** (`api.telegram.org` is unreachable directly from the VM); commands, buttons, alerts |
| **webui** | HTTP panel on `0.0.0.0:8080` — same as the bot, plus role editing and log viewing; login/password auth |
| *(+ `srvmonitor` thread)* | every 5 minutes checks whether the `AstralSigma` server is present in the public Steam lobby list |

The in-game login (`Game → Local → ✔ Show server in the public Steam list → Play → Login window → Ok`) runs in a dedicated `SigmaNav` task — a background thread can't take window focus (`SetForegroundWindow`). The game renders and accepts input only in an **active console session**; the `SigmaConsoleGuard` task redirects the session back to the console when RDP disconnects.

### Architecture

```
SigmaSteamBot task (AtLogon, Interactive/Highest, auto-restart every 2 min)
  └─ venv\pythonw.exe supervisor.py                     (single instance via supervisor.lock)
       ├─ watchdog    keeps Steam+game → auto-login → state.json
       ├─ bot         Telegram long-poll via SOCKS5 (wrapper over curl.exe)
       │               ├─ sender thread (network never blocks polling)
       │               └─ srvmonitor: every 5 min serverlist.fetch → is AstralSigma listed
       ├─ webui       http.server on 0.0.0.0:8080 — same features + roles + logs
       │               (status from watchdog's last_snapshot, live collect() on demand)
       └─ game login → runner.run_nav("seq login")
              └─ Start-ScheduledTask SigmaNav → nav.py (screen-state detect → clicks)

SigmaConsoleGuard task (SYSTEM, on RDP-disconnect event)
  └─ console_guard.ps1 → tscon <session> /dest:console

"Servers" button / monitor:
  serverlist.fetch → serverlist_steam.py (separate process)
       └─ ctypes + the game's steam_api64.dll → RequestLobbyList → Steam lobby list
       └─ fallback layer: Steam Web API GetServerList (needs a key)
```

### Components

| File | Purpose |
|---|---|
| `supervisor.py` | entry point, lock, starts watchdog + bot |
| `watchdog.py` | Steam/game keep-alive loop, auto-login, alerts |
| `bot.py` | Telegram commands and inline buttons, roles, audit, background server monitor |
| `webui.py` | web panel (HTTP thread in the supervisor): authentication, API, embedded SPA; live role editing via `bot.apply_roles` |
| `players.py` | web panel "Players" tab: local-server player list and online status from the game's own files (`analytics.txt`, `Data\users\*`, `Logs\game_state.txt`); passwords (`Code`) are never exposed |
| `common.py` | config (`load_config` / `save_config` without BOM), logging, `State` (in `state.json`), Telegram client over `curl.exe` + SOCKS5 |
| `i18n.py` | bilingual strings (`ru`/`en`) + `t()`; key parity checked by `selftest.py` |
| `gamectl.py` | start/stop/restart Steam and game, reboot VM, disable the bot task |
| `sysinfo.py` | collects resources, Steam/game/session state |
| `nav.py` / `detect.py` / `runner.py` | in-game login sequence (driven by screen pixels) |
| `serverlist.py` | `fetch(cfg) → (ok, servers, source)`: Steam lobbies first, Web API on failure |
| `serverlist_steam.py` | enumerates Steam lobbies via the game's `steam_api64.dll` (`ctypes`), separate process |
| `screenshot.py` | game-window capture via `PrintWindow` (works in an idle session) |
| `console_guard.ps1` | redirects the session to the console on RDP disconnect |
| `install.ps1` | auto-logon + sleep off + registers 3 tasks + desktop shortcut |
| `selftest.py` | quick check of modules/config/i18n/Telegram without entering the main loop |

### Roles

- **admin** — IDs in `telegram.allowed_user_ids`: full access.
- **moderator** — IDs in `telegram.moderator_user_ids`: only `/status`, `/shot`, `/restartgame`, `/login`, `/lang`. Admin-action buttons are hidden; `/servers`, watchdog, Steam, VM, `/stopbot` are unavailable. An ID in both lists counts as an admin.
- **super-admin** — `telegram.super_admin_id` (defaults to the first of `allowed_user_ids`): receives a `👤 Name (role) → action` line for every **state-changing** action by another admin/moderator (start/stop/restart of game and Steam, `/login`, `/watchdog on|off`, confirmed VM reboot, `/stopbot`). `/status`, `/shot`, `/lang`, `/servers` are not logged.

Language is per-user (`/lang ru|en` or the “🌐 Language” button), stored in `state.json`; default is `telegram.default_lang`.

### Bot commands

| Command / button | Role | Action |
|---|---|---|
| `/status` | admin, moderator | CPU/RAM/disk, uptime; Steam (+ signed in); game (PID, RAM, window, “at menu / logging in / in game”); session/RDP; restart counters |
| `/shot` | admin, moderator | game-window screenshot |
| `/servers` | **admin** | public server list (Steam lobbies): name, players, map, version. **AstralSigma** is highlighted 👑 and pinned to the top |
| `/restartgame` | admin, moderator | restart the game **and immediately auto-login to the world**, screenshot at the end |
| `/login` | admin, moderator | run the in-game login manually |
| `/lang ru\|en` | admin, moderator | interface language |
| `/startgame` `/stopgame` | admin | start / stop the game without login |
| `/restartsteam` | admin | restart Steam |
| `/restartvm` | admin | reboot the VM (with confirmation) |
| `/watchdog on\|off` | admin | game keep-alive |
| `/stopbot` | admin | disable the `SigmaSteamBot` task and exit the process (with confirmation). The game and Steam are left running. Restart only from the VM |

The same actions are mirrored as inline buttons. To an unknown ID, `/start` replies with its numeric ID and nothing else.

### Server list

Sigma World Online **does not register game servers** with Valve’s master list — each public host creates a **Steam lobby** (`ISteamMatchmaking::CreateLobby`). Therefore:

- **Primary path** — `serverlist_steam.py`, a short-lived separate process, loads **the game’s own** `steam_api64.dll` via `ctypes`, calls `SteamAPI_InitFlat` → `RequestLobbyList` → reads `GetLobbyData` (`server_name`, `world_name`, `players`, `max_players`, `build`). Requires a **console session** (that’s where Steam lives) — the supervisor already runs there, so a plain `subprocess` is enough.
- **Fallback layer** — Steam Web API `IGameServersService/GetServerList` (needs a `steam_web_api_key`, https://steamcommunity.com/dev/apikey). Empty until the game publishes real servers — kept for the future.

### Background server monitor

The `srvmonitor` thread in the supervisor. First check ~90 s after start, then every `monitor.interval_seconds` (default 300):

- `monitor.server_name` missing for **`misses_before_alert`** consecutive checks (2 ≈ 10 min) → broadcast to **all admins AND moderators**: `⚠️ <name> disappeared from the public Steam server list`;
- while still missing — silent, except a reminder every `repeat_alert_seconds` (3600, `0` = once);
- back → `✅ <name> is back in the public list` (once), counter reset;
- if the list request itself fails → the tick is skipped (not counted as “missing”), logged only.

State (`monitor_astral` in `state.json`) survives a bot restart — no repeated false alarms after a restart.

### Web UI

An HTTP thread inside the supervisor (`webui.py`), listening on `webui.host:webui.port` (default `0.0.0.0:8080`) — `http://<vm-ip>:8080/`. `install.ps1` adds the firewall rule for the port. Plain HTTP — the panel is **LAN-only**.

- **Login** — username/password from `webui_auth.json` (in `base_dir`, gitignored). On first start it is created as **`admin` / `admin`** with a `must_change` flag: until the password is changed only the change-password screen is available (min 6 chars, not `admin`). Hash — PBKDF2-HMAC-SHA256; session — an in-memory `sid` cookie (12 h TTL); POSTs are CSRF-token protected; failed logins are rate-limited per IP (5 tries → 60 s lock-out).
- **Dashboard** — VM / Steam / game / watchdog / bot-internals (process uptime, threads, send queue, snapshot age) / server-monitor cards, plus a live VM screenshot. Auto-refresh every 5 s only while the tab is visible; status comes from the `last_snapshot` the watchdog already writes — near-zero extra load on the supervisor. The “Refresh (live poll)” button calls `sysinfo.collect()` on demand.
- **Actions** — same as the bot: `startgame` / `stopgame` / `restartgame` (restart + login) / `restartsteam` / `login` / `watchdog on|off` / `restartvm` / `stopbot` (confirmed) plus `restarttask` (clean restart of the `SigmaSteamBot` task from a detached process) and `testalert` (a test message to admins via `bot.push_alert`). Long operations run as a job the panel polls.
- **Servers** — the same Steam-lobby list (`serverlist.fetch`, 45 s cache), AstralSigma pinned to the top.
- **Roles** — editing `telegram.allowed_user_ids` / `moderator_user_ids` / `super_admin_id` / `default_lang` / `alerts_enabled`. Saving writes `config.json` as plain UTF-8 (`common.save_config`, no BOM) and **applies the roles live** (`Bot.apply_roles`) — no restart needed. `login_flow` coordinates are intentionally not editable from the web.
- **Logs** — tail of `supervisor.log` (level filter, auto-refresh, download), the panel audit (`webui_audit.log` — who/when/what, separate from the Telegram audit) and a gallery of login-sequence screenshots (`logs/nav/*.png`).
- **Players** — local-server player list and online status (see below).

The interface is bilingual (ru/en, header toggle, choice in the browser `localStorage`), with a dark/light theme. Disable the panel entirely with `webui.enabled = false` in `config.json`.

### Server chat & events

The **"Chat"** tab (sub-tabs):

- **Server chat** — public chat `chat_0..3.txt` (all channels; channel filter + search; auto-refresh 5 s), nicknames link to cards.
- **Events** — a merged feed of `join`/`leave`/`register` (`analytics.txt`), `death` (`dead_user.txt`), `land` (`delete_land*.txt`) + role-grant history (`user_role.txt`).
- **DMs** — all server private messages (`chat_privat.txt`) behind the panel password + audit.

### Stats

The **"Stats"** tab — server-wide analytics:

- **Export / backup** — the player list as **CSV** (no passwords); a **world-folder backup** zip behind the panel password: `state` (`analytics` + `Data\{users,units,game}` + `Logs`, without the binary `map*.dt`, ~2 MB) or `full` (everything). The last 5 are kept in `logs\backups\`.
- **Online (7 days)** — online count reconstructed over time from `enter`/`exit`, 7-day peak, now.
- **Growth** — registrations and DAU per day, retention D1 / D7.
- **Tops** — by level, by hours; **clans** (`clans.json` by rating); **ban list**; **staff** + role history; **monthly top** (`reward_order.txt`); level and country distributions.
- **World · maps** — online / avatars / territories per map; **territories** — every `userTerritories` of the 286 players with owners (map filter). Wildlife lives in the binary `map*.dt` and isn't available as JSON.
- **Server health** — last `Server ready` (`world_performance.txt`: `startupMs`, clusters, `managedMb`) + slow startup phases; **lag events** (slow ticks from `time_shedule*.txt` — total, per day, top functions); connection errors (`error_game*.txt`). `memory_log.txt` isn't populated by the game.

### Twinks

The **"Twinks"** tab detects accounts that connected from the same IP, from `Logs\log_net_ip.txt` (`nick = IP = InterNetwork = port`). Opens after you enter **your own panel password** (IPs + linking accounts are sensitive; the attempt is audited). Shows groups: IP → list of accounts (ID, nickname linking to the card, connection count, first/last seen, the account's other IPs), sorted by group size. The "min accounts per IP" threshold is adjustable (default 2). `config.json → players.twink_ignore_ips` — IPs to skip (e.g. the local relay `127.0.0.2` that all clients go through on the current VM — there are no real client IPs in the log there).

### Local server players

The panel's "Players" tab (`players.py`). Reads the files the game's local server writes itself, from the active world folder under `%USERPROFILE%\AppData\LocalLow\Crematorium of Time\SigmaWorld\SigmaWorld\LocalServer\<world>\`:

| File | Used for |
|---|---|
| `analytics.txt` | log `DD.MM.YYYY H:MM:SS: register\|enter\|exit <id> [<sec>]` (the hour may be single-digit). Online = the player's last event is `enter`; `exit` carries the session length in seconds; also the recent-events feed |
| `Data\users\user_list.json` | `id → name` (the `Code` field is the **player's password — never exposed**) |
| `Data\users\user<N>.json` | profile: total hours (`timeGame`), level (`unitLevel`), role (`role`: 0 player, 1 moderator, 2 admin, 3 GM), ban (`isBlock`/`timeBan`), map, clan, country |
| `Logs\game_state.txt` | authoritative online counts per map (no names) — shown next to the `analytics.txt` estimate |

The world folder is set via `config.json → players`: `localserver_root` (empty = the default path above), `world` / `world_dir` (empty = the world with the freshest `analytics.txt`). The panel caches this for 15 s. Passwords (`code` / `Code`) are stripped server-side and never reach the list/card. Disable with `players.enabled = false`.

**Player card** (click a nickname in the table or feed) — a modal with:

- **Profile:** level, rating, country, GPU, screen resolution, total hours, "last session N h ago", ban + when it lifts
- **Clan** (from `Data\game\clans.json`): name, rating, members with names (👑 leader) and cross-links · **Friends** (from `friends.json`)
- **Research:** current + est. time left (via `serverTime` from `Data\game\settings.json`), techs done, booster · **Missions**
- **Position:** map / coords / respawn point / territory list
- **Avatar:** stats (`paramList`) as bars, skills, named abilities (from `Data\ability.json`), stash and carried inventory with item names (from `Data\items.json`)
- **Sessions:** total / hours online / avg / max, activity histogram by hour of day, last 15 sessions
- **History:** role changes (`user_role.txt`), deaths/resets (`dead_user.txt`), land removals (`delete_land*.txt`), monthly rewards (`reward_order.txt`)
- **Player chat:** their public messages from `chat_0..3.txt` (channel + time)
- **Behind the panel admin's own password** (re-checked via `auth.verify` + throttle + audit): a **"Show password"** button for the player's `code`, and a **"DMs & IP"** button — the player's private messages (`chat_privat.txt`) and IP history (`log_net_ip.txt`). Every reveal is written to `webui_audit.log`.
- **Inventory editing — offline players only** (last `analytics.txt` event is `exit`; re-checked right before the write; if the player logs in the write is aborted). Also behind the panel admin's password. **Give to stash** (`user<N>.json → Inventory.items`): item by name/id from `Data\items.json` + quantity, split by `stack`, `life` = `items.life × 90000` (or copied from an existing entry of the same type), tools get full `durability`. **Take** from stash or from the carried inventory (`unit<id>.json → Inventory.items`): decrements `count`, never more than present. Before every write — a backup of the file to `logs\game_edits\<ts>\`; only `Inventory.items` is touched; atomic write; `mtime` guard (aborts if the file changed between read and write). Everything is written to `webui_audit.log` ("ИНВЕНТАРЬ игрока #N: give/take …").

### Install

Full guide — [`deploy/README.md`](deploy/README.md). In short: copy `deploy\` to the VM → `setup.bat [BASE] [/autologon USER PASS]` → fill in `%BASE%\config.json` → `selftest.py` → `Start-ScheduledTask -TaskName SigmaSteamBot`. Config template — [`deploy/app/config.example.json`](deploy/app/config.example.json).

### Key `config.json` fields

| Field | Purpose |
|---|---|
| `game_appid` | `1690980` |
| `base_dir` | install folder (with `\\`) |
| `steam_api_dll` | path to the **game’s** `steam_api64.dll` (for `/servers` and the monitor) |
| `steam_web_api_key` | Steam Web API key — fallback path for the server list |
| `python_exe` | python for child scripts; empty → auto (`sys.executable`, `pythonw`→`python`) |
| `monitor` | `{ enabled, server_name, interval_seconds, misses_before_alert, repeat_alert_seconds }` |
| `webui` | `{ enabled, host, port }` — web panel; `0.0.0.0:8080` by default. Credentials live in `webui_auth.json`, not in the config |
| `players` | `{ enabled, localserver_root, world, world_dir }` — "Players" tab; all empty = auto-detect the world folder |
| `telegram.token` | @BotFather token |
| `telegram.allowed_user_ids` / `moderator_user_ids` / `super_admin_id` | roles (also editable from the web panel) |
| `telegram.default_lang` | `ru` \| `en` |
| `telegram.proxy` | `socks5h://HOST:PORT` — the client always goes through SOCKS5 |
| `game_window_size` / `login_flow` | login click coordinates (hard-tuned for 1024×768) |
| `watchdog.*` | keep-alive, restart cap, auto-login timings |

`config.json` and `key.txt` are in `.gitignore` (secrets). Template — `deploy/app/config.example.json`.

### Operations

```powershell
Start-ScheduledTask   -TaskName SigmaSteamBot   # start
Stop-ScheduledTask    -TaskName SigmaSteamBot   # stop now
Disable-ScheduledTask -TaskName SigmaSteamBot   # don't start at logon
Get-Content <BASE>\logs\supervisor.log -Tail 40 -Wait
<BASE>\venv\Scripts\python.exe <BASE>\selftest.py
```

After `/stopbot` (task disabled, Telegram won’t help): the **“Запустить SigmaSteamBot”** desktop shortcut on the VM, or over SSH — `deploy/start-bot.sh`, or manually `Enable-ScheduledTask` + `Start-ScheduledTask`.

### Limitations

- `login_flow` coordinates are tuned for a **1024×768** window; a different resolution → re-measure the coordinates (`deploy/README.md`, §5).
- All UI interaction needs an **active console session**; on RDP connect+disconnect the session is returned by `SigmaConsoleGuard`.
- The SOCKS5 proxy must be reachable; if it isn’t, the bot retries silently and the watchdog keeps the game up locally.
- The public lobby list usually has a single entry (niche game) — the multi-entry `/servers` view is tested on synthetic data.
- The game’s language occasionally flips to Polski by itself (a click on the language arrow) — it does not affect coordinates.
