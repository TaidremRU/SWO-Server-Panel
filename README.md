<!-- SPDX note: private project — TaidremRU/SWO-Server-Panel (ex sigmabot_win) -->

# SWO Server Panel

*(ex `SigmaSteamBot` — проект перерос простой лаунчер-бот и стал полноценной серверной панелью; история и репозиторий те же, см. [«Переименование»](#переименование--renaming))*

**RU:** Автозапуск, авто-вход и веб-панель + Telegram-бот для управления сервером **Sigma World Online** (Steam AppID `1690980`) на выделенной Windows-VM. Держит Steam и игру запущенными, сам проходит вход в мир, отдаёт статус/скриншоты и список публичных серверов. Плюс — инструментарий аналитики и реверс-инжиниринга игровых данных: карта мира и бинарных `.dt`-карт (террейн, машины, контейнеры, владение землёй), карта космоса и звёздных систем/кластеров, поиск предметов по всему миру и у всех игроков, детект твинков, трекинг исследований, карточки игроков, бэкапы, безопасная правка инвентаря — всё через один браузер, без доступа к серверу.

**EN:** Auto-start, auto-login and a web panel + Telegram bot for running a **Sigma World Online** (Steam AppID `1690980`) server on a dedicated Windows VM. Keeps Steam and the game running, walks the in-game login itself, serves status / screenshots / the public server list. Plus an analytics and reverse-engineering toolkit for the game's own data: a world map and binary `.dt`-map viewer (terrain, machines, containers, land ownership), a space/star-system/cluster map, world- and player-wide item search, twink detection, research tracking, player cards, backups, and safe inventory editing — all from one browser tab, no server shell access needed.

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
| `mapdt.py` | парсер бинарных карт `Data\maps\map<N>.dt` — точный порт сериализации из исходника игры (террейн, блоки, машины, контейнеры, руда, сетка владения землёй) |
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

### Безопасная остановка мира и лок на одновременные действия

Перед `stopgame` / `restartgame` / `restartsteam` / `restartvm` — если игра реально запущена — панель кладёт пустой `exit1.txt` в корень активного мира (сервер сам подхватывает флаг и сохраняется/останавливается штатно) и только потом, спустя **3 минуты**, бьёт по Steam/игре/VM. Работает одинаково что из Telegram, что из веб-панели.

Пока это идёт — **5-минутный лок** на эти 4 действия: если один админ/модератор уже начал одно из них, второй при попытке получает отказ с именем того, кто начал, и сколько ещё осталось ждать, вместо параллельного разрушительного действия поверх первого.

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
- **Действия** — то же, что у бота: `startgame` / `stopgame` / `restartgame` (перезапуск + вход) / `restartsteam` / `login` / `watchdog on|off` / `restartvm` / `stopbot` (с подтверждением) + `restarttask` (перезапуск задачи `SigmaSteamBot` через одноразовую задачу планировщика — переживает `schtasks /End` самого себя, в отличие от прежнего detached-процесса) и `testalert` (тестовое сообщение админам через `bot.push_alert`). Длинные операции идут заданием, панель опрашивает результат.
- **Серверы** — тот же список Steam-лобби (`serverlist.fetch`, кэш 45 c), AstralSigma наверху.
- **Настройки** — полноценный редактор `config.json` прямо из браузера (см. ниже), плюс роли (`allowed_user_ids` / `moderator_user_ids` / `super_admin_id` / `default_lang` / `alerts_enabled`) применяются на лету (`Bot.apply_roles`) без перезапуска.
- **Логи** — хвост `supervisor.log` (фильтр по уровню, автообновление, скачивание), аудит панели (`webui_audit.log` — кто/когда/что нажал, отдельно от Telegram-аудита) и галерея скринов последовательности входа (`logs/nav/*.png`).
- **Игроки** — список и онлайн-статус игроков локального сервера + поиск предмета у всех игроков (см. ниже).
- **Карта** — карта мира, разбор бинарных `.dt`-карт и карта космоса/звёздных систем (см. ниже).

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
- **Здоровье сервера** — последний `Server ready` (`world_performance.txt`: `startupMs`, кластеры, `managedMb`) + медленные фазы старта; **лаг-события** (медленные тики из `time_shedule*.txt` — всего, по дням, топ функций); ошибки коннекта (`error_game*.txt`). `memory_log.txt` игрой не заполняется.

Онлайн/аватары/территории по картам и поиск предметов вынесены в отдельную вкладку **«Карта»** — ниже.

### Карта

Вкладка **«Карта»** — вся работа с картами и бинарными данными мира, реверс-инжиниренными без исходников игры (см. `stage2/mapdt.py`, `stage2/players.py`):

- **Таблица мира** — по каждой карте: онлайн, аватары, территории, размер. **Карта 0 = космос** — игра считает таких игроков онлайн, хотя фактически они могут быть оффлайн. Внемировые (космические) карты подписаны именем и координатами звёздной системы (`🪐 Ryk Xive (x, y)`), которые находятся сопоставлением ID карты с объектом в `Data\world\star<N>.json` — это соответствие подтверждено на всех 26 внемировых картах прод-сервера.
- **Разбор `.dt`** — ссылка «показать карту» у каждой карты → полный разбор бинарного `Data\maps\map<N>.dt` (точный порт сериализации `Map.Load`/`MapCell.Read`/… из исходника игры): гистограмма блоков/растительности (с русскими названиями — тексты вытащены из клиентской локализации, `resources.assets`), машины (печь/дробилка/…), точки руды (те же id-коды и те же русские названия, что у блоков), содержимое всех наземных/подземных контейнеров, сетка владения землёй (8×8-блоки → владелец), газ/заражение, кислородная карта. Основная карта — 512×512, ~13 c на первый разбор, дальше из кэша.
  - **Просмотрщик карты** — PNG-рендер (свой энкодер, без Pillow) с наведением (блок/владелец под курсором), приближением колёсиком мыши (25–400 %) с зумом к курсору, перетаскиванием («рука»), свободным вращением (по умолчанию 315°, центрируется в любом повороте) и наложением сетки владения землёй. Кнопка **«⟳ пересмотреть»** рядом с вращением — принудительно перерисовывает карту заново, игнорируя кэш по времени изменения файла (на случай, если реальные данные обновились, а кэш — нет).
- **Территории** — все `userTerritories` игроков с владельцами, фильтр по карте.
- **Поиск предмета в мире** — id или имя предмета (можно подстроку, напр. `tech_booster`), выбрать карту или «весь мир» → для каждого совпадения координаты `(x, y)`, контейнер (`container:underground` / `machine.fuel` / `block` / `vehicle` / `unit` / `shop` / `block.res`), количество и прочность. Скан всего мира — до ~2 мин (72 карты), кэш по mtime каждой карты. Эндпоинт `GET /api/mapdt-find?map=N|all&item=<id|имя>`.
- **Космос** — звёздные системы и галактика, распарсенные из `Data\space\units.dt` (корабли/метеориты/капсулы — порт `ZData.SpaceUnit.Read`) и `Data\world\star<N>.json`/`cluster<N>.json` (реверс-инжиниринг без исходников, ID объекта = порядковый номер в файле, подтверждён совпадением с игровым ID):
  - переключатель **кластер → звёздная система** (в игре несколько кластеров, в каждом — несколько систем);
  - точечная карта системы (планеты/астероиды/корабли/метеориты/капсулы), приближение и перетаскивание как у `.dt`-карты, координаты под курсором;
  - **поиск объекта по имени/подстроке** в системе — совпадение подсвечивается на карте кольцом;
  - корабли без единого тела (планеты/метеорита) ближе 10000 единиц скрываются из списка и со шкалы — не растягивают масштаб карты «убежавшими» кораблями.

### Твинки

Вкладка **«Твинки»** — детект мультиаккаунтов, после ввода **своего пароля от панели** (чувствительно; попытка в аудит). Три сигнала:

- **По паролю (`code`)** — аккаунты с одинаковым паролем (из `Data\users\user<N>.json`). Самый надёжный: люди переиспользуют пароль на альтах. Показывается только длина пароля и список аккаунтов, **сам пароль не выводится**.
- **По IP** — из `Logs\log_net_ip.txt` (`ник = IP = InterNetwork = порт`); `config.json → players.twink_ignore_ips` — какие IP игнорировать (на текущей VM все клиенты идут через релей `127.0.0.2` — реальных IP нет, эта секция пустая).
- **По железу** — одинаковые `videoCard` + разрешение экрана (много ложных срабатываний, помечено).

Каждая группа — ID + ник (ссылка на карточку). Порог «мин. аккаунтов» настраивается.

### Трекинг техов и бустеров

У игры нет лога исследований/бустеров, поэтому панель **ведёт его сама**: фоновый поток (`players.tech_track` в `config.json`, интервал по умолчанию 600 c) раз в N минут снимает `techList` / `techBooster` / `researchTech` всех игроков и дописывает изменения в `logs\tech_track.jsonl`. События: `tech_gained` (какие техи прибавились), `booster_spent` / `booster_gained`, `research_changed`. Показывается в карточке игрока (по нему) и во вкладке «Статы» (по всем). Работает только «вперёд» — с момента включения.

### Игроки локального сервера

Вкладка «Игроки» веб-панели (`players.py`). Читает файлы, которые пишет сам локальный сервер игры, из каталога активного мира под `%USERPROFILE%\AppData\LocalLow\Crematorium of Time\SigmaWorld\SigmaWorld\LocalServer\<мир>\`:

| Файл | Что берётся |
|---|---|
| `analytics.txt` | журнал `ДД.ММ.ГГГГ Ч:ММ:СС: register\|enter\|exit <id> [<сек>]` (час бывает однозначным). Онлайн = последнее событие игрока `enter`; `exit` несёт длину сессии в секундах; отсюда же лента последних событий |
| `Data\users\user_list.json` | `id → имя` (поле `Code` — **пароль игрока, наружу не отдаётся**) |
| `Data\users\user<N>.json` | профиль: всего часов (`timeGame`), уровень (`unitLevel`), роль (`role`: 0 игрок, 1 модератор, 2 админ, 3 GM), бан (`isBlock`/`timeBan`), карта, клан, страна |
| `Logs\game_state.txt` | авторитетные счётчики онлайна по картам (без имён) — показываются рядом с оценкой по `analytics.txt` |

Каталог мира — `config.json → players`: `localserver_root` (пусто = путь по умолчанию выше), `world` / `world_dir` (пусто = мир с самым свежим `analytics.txt`). Данные кэшируются в панели на 15 c. Пароли (`code` / `Code`) вырезаются на сервере и в выдачу карточки/списка не попадают. Отключить — `players.enabled = false`.

**Поиск предмета у игроков** (карточка «🔎 Поиск предмета у игроков» на этой же вкладке) — id или имя предмета → по каждому игроку количество на складе, при себе и всего, онлайн-статус. В отличие от поиска по картам (вкладка «Карта»), здесь ищется по инвентарям всех аккаунтов, а не по объектам на местности. Эндпоинт `GET /api/player-item-find?item=<id|имя>`.

**Карточка игрока** (клик по нику в таблице или ленте) — модальное окно:

- **Профиль:** уровень, рейтинг, страна, видеокарта, разрешение экрана, всего часов, «последняя сессия N ч назад», бан + когда снят
- **Клан** (из `Data\game\clans.json`): имя, рейтинг, состав с именами (👑 лидер) и переходами на карточки · **Друзья** (из `friends.json`)
- **Исследования:** текущее + ~время до конца (по `serverTime` из `Data\game\settings.json`), изучено техов, бустер · **Миссии**
- **Позиция:** карта / координаты / точка респавна / список территорий
- **Аватар:** статы (`paramList`) полосками, навыки, способности с русскими названиями (slug'и из `Data\ability.json`, тексты — из клиентской локализации, как и у блоков/руды выше), склад и «при себе» с названиями предметов (из `Data\items.json`)
- **Сессии:** всего / часов онлайн / средняя / макс, гистограмма активности по часам суток, последние 15 сессий
- **История:** смены ролей (`user_role.txt`), смерти/сбросы (`dead_user.txt`), снос земель (`delete_land*.txt`), месячные награды (`reward_order.txt`)
- **Чат игрока:** его публичные сообщения из `chat_0..3.txt` (канал + время)
- **Под своим паролем панели** (повторная проверка `auth.verify` + throttle + аудит): кнопка **«Показать пароль»** игрока (`code`) и кнопка **«Приваты и IP»** — приватные сообщения игрока (`chat_privat.txt`) и история IP (`log_net_ip.txt`). Каждый показ пишется в `webui_audit.log`.
- **Правка инвентаря — только для оффлайн-игрока** (последнее событие в `analytics.txt` = `exit`; повторная проверка прямо перед записью; при входе игрока запись отменяется). Тоже под своим паролем панели. **Выдать на склад** (`user<N>.json → Inventory.items`): предмет по имени/id из `Data\items.json` + количество, с разбивкой по `stack`, `life` = `items.life × 90000` (или копируется у уже имеющейся записи того же типа), инструментам ставится полная `durability`. **Изъять** со склада или из инвентаря при себе (`unit<id>.json → Inventory.items`): уменьшает `count`, не больше, чем есть. Перед каждой записью — бэкап файла в `logs\game_edits\<ts>\`; правится только `Inventory.items`; атомарная запись; guard по `mtime` (если файл изменился между чтением и записью — отмена). Всё пишется в `webui_audit.log` («ИНВЕНТАРЬ игрока #N: give/take …»).

### Установка одним скриптом

На чистой Windows-VM, из cmd **от администратора**:

```bat
cd deploy
setup.bat
```

Скрипт сам: находит Python, ставит venv и зависимости, копирует файлы, создаёт `config.json` из шаблона (с уже подставленным `base_dir`), регистрирует все 3 задачи планировщика, ставит правило фаервола на порт панели — и **сам запускает** задачу `SigmaSteamBot`. Веб-панель без Telegram-токена, реального пути к игре и т.п. **уже поднимается и открывается** (`bot.py` переживает пустой/плейсхолдерный токен — просто не сможет слать сообщения, пока его не заполнят). В конце скрипт печатает адрес панели.

Дальше — зайти в браузере на `http://<ip-vm>:8080/`, войти (**`admin` / `admin`**, панель тут же попросит сменить пароль) и донастроить всё во вкладке **«Настройки»**: Telegram-токен и ID админов, прокси, монитор сервера, источник данных «Игроков», игровые аккаунты для авто-входа. Руками редактировать `config.json` для этого не нужно.

Необязательные параметры: `setup.bat [BASE_DIR] [/autologon USER PASSWORD]` — свой путь установки и/или автологон Windows под указанным пользователем (нужен, чтобы задачи `AtLogon` вообще стартовали без физического входа в сессию). Путь к игре (`game_exe` / `steam_exe` / `game_install_dir`) и разрешение окна логина — вне схемы настроек панели (см. «Ограничения» ниже); их правит либо `config.json` вручную, либо (для `login_flow`) продвинутый JSON-редактор во вкладке «Настройки». Подробный гайд с ручными шагами и диагностикой — [`deploy/README.md`](deploy/README.md); шаблон конфига — [`deploy/app/config.example.json`](deploy/app/config.example.json).

### Настройки из панели

Вкладка **«Настройки»** — редактор `config.json` без ручной правки файла: общие параметры, веб-панель, watchdog, монитор сервера, источник данных «Игроков», Telegram (токен полем `secret` — один раз введённый не показывается повторно), Steam/список серверов. Плюс:

- **Игровые аккаунты** — список логин/пароль с выбором активного (для авто-входа несколькими аккаунтами на одной VM);
- **`login_flow` (продвинутое)** — сырой JSON координат клика входа, для нестандартного разрешения окна;
- сохранение пишет `config.json` в чистом UTF-8 без BOM; если правка требует перезапуска — панель предлагает кнопку «Перезапустить задачу» сразу же.

### Ключевые поля `config.json`, которые остаются вне UI

| Поле | Назначение |
|---|---|
| `game_appid` | `1690980` |
| `steam_exe` / `game_exe` / `game_install_dir` | пути Steam и игры — стандартные по умолчанию, менять только если игра стоит не туда |
| `steam_api_dll` | путь к `steam_api64.dll` **игры** (для `/servers` и монитора) |
| `game_window_size` | размер окна для координат `login_flow` (по умолчанию под 1024×768) |
| `base_dir` | папка установки — подставляется автоматически `setup.bat` |

Всё остальное (роли, Telegram, монитор, watchdog, источник данных игроков, Steam Web API ключ, игровые аккаунты, `login_flow`) — во вкладке «Настройки», см. выше. `config.json` и `key.txt` — в `.gitignore` (секреты). Шаблон — `deploy/app/config.example.json`.

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

### Safe world shutdown and a same-action lock

Before `stopgame` / `restartgame` / `restartsteam` / `restartvm` — if the game is actually running — the panel drops an empty `exit1.txt` into the active world's root folder (the server picks up the flag and saves/stops on its own) and only then, after **3 minutes**, touches Steam/the game/the VM. Same behavior whether triggered from Telegram or the web panel.

While that's in progress — a **5-minute lock** on these 4 actions: if one admin/moderator already started one, a second one trying gets refused with who started it and how long is left, instead of a second destructive action landing on top of the first.

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
- **Actions** — same as the bot: `startgame` / `stopgame` / `restartgame` (restart + login) / `restartsteam` / `login` / `watchdog on|off` / `restartvm` / `stopbot` (confirmed) plus `restarttask` (restarts the `SigmaSteamBot` task via a one-time scheduled task — survives its own `schtasks /End`, unlike the old detached-process approach) and `testalert` (a test message to admins via `bot.push_alert`). Long operations run as a job the panel polls.
- **Servers** — the same Steam-lobby list (`serverlist.fetch`, 45 s cache), AstralSigma pinned to the top.
- **Settings** — a full `config.json` editor right in the browser (see below), plus roles (`allowed_user_ids` / `moderator_user_ids` / `super_admin_id` / `default_lang` / `alerts_enabled`) applied live (`Bot.apply_roles`) with no restart needed.
- **Logs** — tail of `supervisor.log` (level filter, auto-refresh, download), the panel audit (`webui_audit.log` — who/when/what, separate from the Telegram audit) and a gallery of login-sequence screenshots (`logs/nav/*.png`).
- **Players** — local-server player list and online status, plus an item search across all players' inventories (see below).
- **Map** — the world map, binary `.dt`-map viewer, and the space/star-system map (see below).

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
- **Server health** — last `Server ready` (`world_performance.txt`: `startupMs`, clusters, `managedMb`) + slow startup phases; **lag events** (slow ticks from `time_shedule*.txt` — total, per day, top functions); connection errors (`error_game*.txt`). `memory_log.txt` isn't populated by the game.

Per-map online/avatars/territories and item search moved to their own **"Map"** tab — below.

### Map

The **"Map"** tab — everything about maps and the game's binary world data, reverse-engineered without access to the game's source (see `stage2/mapdt.py`, `stage2/players.py`):

- **World table** — per map: online, avatars, territories, size. **Map 0 = space** — the game counts these players as online even though they may actually be offline. Off-world (space) maps are labeled with the name and coordinates of their star system (`🪐 Ryk Xive (x, y)`), found by matching the map ID to an object in `Data\world\star<N>.json` — this mapping was verified against all 26 off-world maps on the production server.
- **`.dt` decoding** — a "show map" link per map → full decode of the binary `Data\maps\map<N>.dt` (an exact port of the game's own `Map.Load`/`MapCell.Read`/… serialization): histograms of blocks/vegetation (with Russian display names, pulled from the client's own localization, `resources.assets`), machines (furnace/crusher/…), ore points (same id space and same Russian names as blocks), the contents of every ground/underground container, the land-ownership grid (8×8 blocks → owner), gas/infection, oxygen map. The main map is 512×512, ~13 s for the first parse, cached afterwards.
  - **Map viewer** — a PNG render (own encoder, no Pillow) with hover info (block/owner under the cursor), mouse-wheel zoom-to-cursor (25–400%), drag-to-pan, free rotation (315° by default, stays centered at any angle), and a land-ownership overlay. A **"⟳ rescan"** button next to the rotation controls force-redraws the map, bypassing the file-mtime cache (for when the real data changed but the cache didn't notice).
- **Territories** — every player's `userTerritories` with owners, filterable by map.
- **Find an item in the world** — type an item id or name (a substring works, e.g. `tech_booster`), pick a map or "whole world" → every match lists its `(x, y)`, the container (`container:underground` / `machine.fuel` / `block` / `vehicle` / `unit` / `shop` / `block.res`), the count and durability. A whole-world scan takes up to ~2 min (72 maps); cached per map mtime. Endpoint `GET /api/mapdt-find?map=N|all&item=<id|name>`.
- **Space** — star systems and the galaxy, parsed from `Data\space\units.dt` (ships/meteorites/pods — a port of `ZData.SpaceUnit.Read`) and `Data\world\star<N>.json`/`cluster<N>.json` (reverse-engineered without source; an object's ID is its sequential position in the file, verified to match the in-game ID):
  - a **cluster → star system** picker (the game has several clusters, each with several systems);
  - a scatter map of the system (planets/asteroids/ships/meteorites/pods) with the same zoom/pan as the `.dt` viewer, coordinates under the cursor;
  - **search by name/substring** within a system — a match is highlighted on the map with a ring;
  - ships with no body (planet or meteorite) within 10,000 units are hidden from the list and the scale, so a stray ship doesn't stretch the whole map.

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

**Find an item on players** ("🔎 Find an item on players" card on this same tab) — an item id or name → per player, count in stash, carried, and total, plus online status. Unlike the map-based search (the "Map" tab), this searches every account's inventory rather than objects placed in the world. Endpoint `GET /api/player-item-find?item=<id|name>`.

**Player card** (click a nickname in the table or feed) — a modal with:

- **Profile:** level, rating, country, GPU, screen resolution, total hours, "last session N h ago", ban + when it lifts
- **Clan** (from `Data\game\clans.json`): name, rating, members with names (👑 leader) and cross-links · **Friends** (from `friends.json`)
- **Research:** current + est. time left (via `serverTime` from `Data\game\settings.json`), techs done, booster · **Missions**
- **Position:** map / coords / respawn point / territory list
- **Avatar:** stats (`paramList`) as bars, skills, named abilities in Russian (slugs from `Data\ability.json`, display text pulled from the client's own localization, same as blocks/ore above), stash and carried inventory with item names (from `Data\items.json`)
- **Sessions:** total / hours online / avg / max, activity histogram by hour of day, last 15 sessions
- **History:** role changes (`user_role.txt`), deaths/resets (`dead_user.txt`), land removals (`delete_land*.txt`), monthly rewards (`reward_order.txt`)
- **Player chat:** their public messages from `chat_0..3.txt` (channel + time)
- **Behind the panel admin's own password** (re-checked via `auth.verify` + throttle + audit): a **"Show password"** button for the player's `code`, and a **"DMs & IP"** button — the player's private messages (`chat_privat.txt`) and IP history (`log_net_ip.txt`). Every reveal is written to `webui_audit.log`.
- **Inventory editing — offline players only** (last `analytics.txt` event is `exit`; re-checked right before the write; if the player logs in the write is aborted). Also behind the panel admin's password. **Give to stash** (`user<N>.json → Inventory.items`): item by name/id from `Data\items.json` + quantity, split by `stack`, `life` = `items.life × 90000` (or copied from an existing entry of the same type), tools get full `durability`. **Take** from stash or from the carried inventory (`unit<id>.json → Inventory.items`): decrements `count`, never more than present. Before every write — a backup of the file to `logs\game_edits\<ts>\`; only `Inventory.items` is touched; atomic write; `mtime` guard (aborts if the file changed between read and write). Everything is written to `webui_audit.log` ("ИНВЕНТАРЬ игрока #N: give/take …").

### One-script install

On a clean Windows VM, from an **elevated** cmd prompt:

```bat
cd deploy
setup.bat
```

The script finds Python, creates a venv and installs dependencies, copies the files, creates `config.json` from the template (with `base_dir` already filled in), registers all 3 scheduled tasks, opens a firewall rule for the panel port — and **starts** the `SigmaSteamBot` task itself. The web panel comes up and is reachable **without** a Telegram token, a real game path, etc. already set (`bot.py` tolerates an empty/placeholder token just fine — it simply can't send messages until one is configured). The script prints the panel's address at the end.

From there, open `http://<vm-ip>:8080/` in a browser, log in (**`admin` / `admin`**, the panel immediately asks for a new password) and finish setup in the **"Settings"** tab: the Telegram token and admin IDs, the proxy, the server monitor, the "Players" data source, the game accounts for auto-login. No manual `config.json` editing needed for any of that.

Optional arguments: `setup.bat [BASE_DIR] [/autologon USER PASSWORD]` — a custom install path and/or Windows auto-logon as the given user (needed for the `AtLogon` tasks to actually start without someone physically logging into the session). The game paths (`game_exe` / `steam_exe` / `game_install_dir`) and the login window resolution sit outside the settings schema (see "Limitations" below) — edit `config.json` by hand for those, or use the advanced JSON editor in "Settings" for `login_flow`. Full guide with manual steps and troubleshooting — [`deploy/README.md`](deploy/README.md); config template — [`deploy/app/config.example.json`](deploy/app/config.example.json).

### Settings from the panel

The **"Settings"** tab is a `config.json` editor with no manual file editing: general options, web panel, watchdog, server monitor, the "Players" data source, Telegram (the token is a `secret` field — once set, it's never shown again), Steam/server list. Plus:

- **Game accounts** — a list of login/password pairs with an active one selected (for auto-login with several accounts on one VM);
- **`login_flow` (advanced)** — the raw JSON of login click coordinates, for a non-standard window resolution;
- saving writes `config.json` as plain UTF-8 with no BOM; when a change needs a restart, the panel immediately offers a "Restart task" button.

### Key `config.json` fields left outside the UI

| Field | Purpose |
|---|---|
| `game_appid` | `1690980` |
| `steam_exe` / `game_exe` / `game_install_dir` | Steam and game paths — standard defaults, change only if the game is installed elsewhere |
| `steam_api_dll` | path to the **game's** `steam_api64.dll` (for `/servers` and the monitor) |
| `game_window_size` | window size the `login_flow` coordinates were measured for (default matches 1024×768) |
| `base_dir` | install folder — filled in automatically by `setup.bat` |

Everything else (roles, Telegram, monitor, watchdog, the players data source, the Steam Web API key, game accounts, `login_flow`) lives in the "Settings" tab, see above. `config.json` and `key.txt` are in `.gitignore` (secrets). Template — `deploy/app/config.example.json`.

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

---

### Переименование / Renaming

**RU:** Проект стартовал как `sigmabot_win` — простой авто-логин + Telegram-бот. По мере роста (веб-панель, аналитика, реверс-инжиниринг игровых форматов, карта космоса) он перерос исходный масштаб, и репозиторий был переименован в **`SWO-Server-Panel`** прямо на GitHub — вся история и коммиты сохранены, ссылки со старого имени редиректят на новое.

**EN:** The project started as `sigmabot_win` — a simple auto-login + Telegram bot. As it grew (web panel, analytics, reverse-engineered game formats, the space map) it outgrew that scope, and the GitHub repository was renamed to **`SWO-Server-Panel`** in place — full history and commits are preserved, and links to the old name redirect to the new one.

### Скриншоты / Screenshots

**RU:** Пока не добавлены — появятся, когда панель будет отлажена на тестовом сервере, а не на проде с реальными данными игроков.

**EN:** Not included yet — will be added once the panel is tested on a non-production server, rather than against live player data.
