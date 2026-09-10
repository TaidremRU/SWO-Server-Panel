# SigmaSteamBot — развёртывание на Windows-VM

Автозапуск и авто-вход в игру **Sigma World Online** (Steam) + управление через
Telegram-бота. Держит Steam и игру запущенными, после запуска сам проходит
`Game → Local → ✔ Show server in the public Steam list → Play → окно Login → Ok`
и заходит в мир. Мониторинг и ручное управление — из Telegram.

---

## 1. Требования на целевой VM

| Что | Зачем |
|---|---|
| Windows 10/11, аккаунт **с правами администратора** | автологон, задачи, `tscon` |
| **Python 3.10+** в PATH (галочка «Add to PATH» при установке) | venv для бота |
| **Steam** установлен, вход в аккаунт сохранён (Remember password) | запуск игры |
| **Sigma World Online** установлена через Steam | сама игра |
| В игре: логин/пароль введены и стоит галочка **Remember** | авто-вход жмёт только «Ok» |
| **SOCKS5-прокси** до `api.telegram.org` (если Telegram заблокирован) | бот ходит через него |
| Разрешение сессии, где идёт игра — **1024×768** (см. §5) | координаты кликов рассчитаны под него |
| Telegram-бот от **@BotFather** (token) и ваш numeric ID (**@userinfobot**) | доступ к боту |

> `curl.exe` входит в Windows 10 1803+ — отдельно не нужен.

---

## 2. Установка

1. Скопируйте папку `deploy\` на целевую VM (например в `C:\deploy`).
2. Откройте **cmd от администратора**, перейдите в неё и запустите:

   ```bat
   cd C:\deploy
   setup.bat
   ```

   По умолчанию всё ставится в `%USERPROFILE%\sigmabot`. Свой путь и автологон:

   ```bat
   setup.bat C:\Users\alex\sigmabot /autologon alex МОЙ_ПАРОЛЬ
   ```

   `setup.bat` создаёт venv, ставит зависимости, копирует файлы, регистрирует
   3 задачи планировщика и (при `/autologon`) прописывает автологон.

3. **Отредактируйте `%BASE%\config.json`** (см. §3).

4. Проверьте окружение:

   ```bat
   "%BASE%\venv\Scripts\python.exe" "%BASE%\selftest.py"
   ```

   Должно напечатать состояние VM/Steam/игры и `telegram getUpdates ... ok=True`.

5. Запустите:

   ```powershell
   Start-ScheduledTask -TaskName SigmaSteamBot
   ```

   В Telegram придёт «🟢 SigmaSteamBot запущен». Дальше `/help`.

---

## 3. `config.json` — что заполнить

| Поле | Пример / примечание |
|---|---|
| `steam_exe` | `C:\Program Files (x86)\Steam\Steam.exe` |
| `game_appid` | `1690980` (Sigma World Online) |
| `game_install_dir` | папка игры в `steamapps\common\...` — по ней ищется процесс |
| `base_dir` | **та же папка, куда всё поставлено** (`%BASE%`), с двойными `\\` |
| `steam_api_dll` | путь к `steam_api64.dll` **игры** (`...\Sigma World Online\SigmaWorld_Data\Plugins\x86_64\steam_api64.dll`) — для `/servers` и монитора |
| `steam_web_api_key` | ключ https://steamcommunity.com/dev/apikey — запасной путь списка серверов; можно оставить пустым |
| `python_exe` | python для дочерних скриптов; пусто → авто (`sys.executable`, `pythonw`→`python`) |
| `monitor` | `{ enabled, server_name, interval_seconds, misses_before_alert, repeat_alert_seconds }` — фоновая проверка присутствия сервера в списке Steam-лобби |
| `webui` | `{ enabled, host, port }` — веб-панель (см. §4a). `0.0.0.0:8080` по умолчанию; правило фаервола на порт ставит `install.ps1`. Креды — в `webui_auth.json` (не в конфиге) |
| `players` | `{ enabled, localserver_root, world, world_dir }` — вкладка «Игроки» веб-панели. Всё пусто = автоопределение каталога мира по свежести `analytics.txt` |
| `telegram.token` | токен от @BotFather |
| `telegram.allowed_user_ids` | `[280331544]` — numeric Telegram ID **администраторов** (полный доступ) |
| `telegram.moderator_user_ids` | `[]` — ID **модераторов**: только `/status`, `/shot`, `/restartgame`, `/login`, `/lang`. ID, попавший и сюда, и в `allowed_user_ids`, считается админом |
| `telegram.super_admin_id` | `280331544` — главный админ: получает копию каждой команды/кнопки других админов и модераторов. Пусто → берётся первый из `allowed_user_ids` |
| `telegram.default_lang` | `ru` или `en` — язык по умолчанию; каждый пользователь меняет его командой `/lang` (сохраняется в `state.json`) |
| `telegram.proxy` | `socks5h://HOST:PORT` вашего SOCKS5; если Telegram доступен напрямую — всё равно нужен рабочий SOCKS5 (клиент всегда идёт через него) |
| `game_window_size` | `[1024, 768]` — под него рассчитаны координаты `login_flow` |
| `login_flow.menu_steps` / `ok` | координаты кликов ОТНОСИТЕЛЬНО окна игры (см. §5) |
| `login_flow.after_ingame` | шаги после входа в мир — по умолчанию клик по иконке чата в тулбаре (чтобы читать чат через `/shot`); пустой список = ничего |
| `watchdog.auto_login` | `true` — заходить в игру автоматически после запуска |
| `watchdog.login_settle_seconds` | пауза после появления игры до попытки входа |

---

## 4. Команды бота

Интерфейс двуязычный. `/lang` (или кнопка «🌐 Язык») переключает **ru/en**
персонально для каждого пользователя; выбор хранится в `state.json`. Можно сразу
`/lang ru` / `/lang en`.

| Команда / кнопка | Роль | Действие |
|---|---|---|
| `/status` | админ, модератор | CPU/RAM/диск, аптайм; Steam (+вход в аккаунт); игра (PID, RAM, окно, «в меню / вход выполняется / в игре»); сессия/RDP; счётчики перезапусков |
| `/shot` | админ, модератор | скриншот окна игры (работает и в простаивающей сессии) |
| `/servers` | админ | список публичных серверов (Steam-лобби): имя, игроки, карта, версия; **AstralSigma** подсвечивается 👑 и поднимается наверх |
| `/restartgame` | админ, модератор | **перезапуск игры + сразу авто-вход в мир** (`stop → start → seq login`), в конце — скриншот |
| `/login` | админ, модератор | пройти вход в игру вручную |
| `/lang ru\|en` | админ, модератор | язык интерфейса |
| `/startgame` `/stopgame` | админ | запуск / останов игры без входа |
| `/restartsteam` | админ | перезапуск Steam |
| `/restartvm` | админ | перезагрузка VM (с подтверждением) |
| `/watchdog on\|off` | админ | авто-поддержание игры (модератор watchdog'ом не управляет) |
| `/stopbot` | админ | остановить сам скрипт: отключает задачу `SigmaSteamBot` (авто-рестарта и старта при загрузке VM больше не будет) и завершает процесс. С подтверждением. Игра и Steam продолжают работать. Запуск обратно — только с VM: `Enable-ScheduledTask -TaskName SigmaSteamBot; Start-ScheduledTask -TaskName SigmaSteamBot` |

Модератору команды/кнопки уровня админа не показываются, а при прямом вызове
приходит «⛔ только для администратора».

Имя задачи берётся из `task_name` в `config.json` (по умолчанию `SigmaSteamBot`).

Алерты (падение/перезапуск/итог автологина) шлются всем админам сами — на языке
`default_lang`. **Главный админ** (`super_admin_id`) дополнительно получает строку
`👤 Имя (роль) → /команда` на каждое действие другого админа/модератора, **которое
меняет состояние**: start/stop/restart игры и Steam, `/login`, `/watchdog on|off`,
подтверждённая перезагрузка VM, `/stopbot`. `/status`, `/shot` и `/lang` не
логируются.

> `/restartgame` проходит вход сам; watchdog (если включён) через
> `login_settle_seconds` может продублировать `seq login` — последовательность
> state-aware и на «уже в игре» просто выходит, повтор безвреден.

### Как поднять бота после `/stopbot`

Telegram уже не поможет (бот выключен) — только с VM или по SSH:

* **На VM:** двойной клик по ярлыку **«Запустить SigmaSteamBot»** на рабочем столе
  (его создаёт `install.ps1`; сам скрипт — `start-bot.cmd` в папке бота, при
  необходимости сам просит повышение прав).
* **По SSH с Linux:** `deploy/start-bot.sh` (обёртка над `ssh` + `sshpass`).
  Хост/логин/пароль/имя задачи переопределяются через переменные окружения
  `SIGMA_VM_HOST` / `SIGMA_VM_USER` / `SIGMA_VM_PASS` / `SIGMA_TASK`.
* **Вручную (PowerShell на VM):**
  `Enable-ScheduledTask -TaskName SigmaSteamBot; Start-ScheduledTask -TaskName SigmaSteamBot`
  (`Enable` обязателен — `/stopbot` именно отключает задачу).

### Список серверов (`/servers`) и фоновый монитор

Sigma World Online не публикует game-серверы в мастер-листе Valve — публичный
хост создаёт **Steam-лобби**. Список берётся так:

* `serverlist_steam.py` отдельным коротким процессом грузит `steam_api64.dll`
  **игры** (`config.json` → `steam_api_dll`) через `ctypes`:
  `SteamAPI_InitFlat` → `RequestLobbyList` → `GetLobbyData`
  (`server_name`, `players`, `max_players`, `build`). Нужна **консольная сессия**
  (там запущен Steam) — супервизор в ней и работает, поэтому отдельная задача
  планировщика не требуется.
* Запасной слой — Steam Web API `GetServerList` (`config.json` →
  `steam_web_api_key`). Пусто, пока игра не публикует настоящие серверы.

**Монитор** (`config.json` → `monitor`, поток `srvmonitor` в супервизоре):
первая проверка через ~90 c, дальше каждые `interval_seconds` (300). Если
`server_name` (`AstralSigma`) отсутствует `misses_before_alert` (2) проверок
подряд — рассылка **всем админам И модераторам**
«⚠️ … пропал из публичного списка серверов Steam»; повтор каждые
`repeat_alert_seconds` (3600, `0` = один раз); вернулся → «✅ … снова в списке»
один раз. Ошибка запроса списка — тик пропускается (не «пропажа»). Состояние
(`monitor_astral` в `state.json`) переживает рестарт бота.

---

## 4a. Веб-панель

`webui.py` — HTTP-поток внутри супервизора (отдельная задача планировщика не
нужна). Слушает `webui.host:webui.port` из `config.json` (по умолчанию
`0.0.0.0:8080`) — открывайте `http://<ip-vm>:8080/`. Правило фаервола на этот порт
добавляет `install.ps1` (`New-NetFirewallRule … "SigmaSteamBot Web UI"`). Протокол
обычный **HTTP — панель только для локальной сети**.

**Первый вход.** При старте создаётся `webui_auth.json` в `base_dir` с логином
**`admin`** и паролем **`admin`** и флагом `must_change` — панель сразу показывает
экран смены пароля и не пускает дальше, пока пароль не сменён (минимум 6 символов,
не `admin`). Хэш пароля — PBKDF2-HMAC-SHA256. Сессия — cookie `sid` в памяти
процесса (TTL 12 ч, теряется при перезапуске бота). POST-запросы защищены
CSRF-токеном; после 5 неудачных входов IP блокируется на 60 c. Сбросить пароль —
удалить `webui_auth.json` и перезапустить задачу (снова будет `admin`/`admin`).

**Что умеет** (то же, что бот, плюс роли и логи):

| Раздел | Содержимое |
|---|---|
| Дашборд | карты VM / Steam / игра / watchdog / внутренности бота / монитор сервера + живой скриншот. Автообновление 5 c только при активной вкладке; статус из `state.json` → `last_snapshot`, живой `sysinfo.collect()` — по кнопке «Обновить (живой опрос)» |
| Действия | `startgame` / `stopgame` / `restartgame` (перезапуск + вход) / `restartsteam` / `login` / `watchdog on\|off` / `restartvm` / `stopbot` (с подтверждением) + `restarttask` (чистый перезапуск задачи `SigmaSteamBot` отдельным процессом `cmd`) и `testalert` (тестовое сообщение админам через Telegram). Длинная операция запускается заданием, панель опрашивает результат |
| Серверы | тот же список Steam-лобби (`serverlist.fetch`, кэш 45 c) |
| Роли | правка `telegram.allowed_user_ids` / `moderator_user_ids` / `super_admin_id` / `default_lang` / `alerts_enabled`. Пишет `config.json` в чистом UTF-8 (без BOM) и применяет роли **на лету** — перезапуск не нужен. Координаты `login_flow` из веба не редактируются |
| Логи | хвост `logs\supervisor.log` (фильтр по уровню, автообновление, «Скачать»), аудит панели `webui_audit.log` (кто/когда/что нажал — отдельно от Telegram-аудита), галерея `logs\nav\*.png` (скрины последовательности входа) |
| Игроки | список и онлайн-статус игроков локального сервера из файлов игры: `...\LocalLow\Crematorium of Time\SigmaWorld\SigmaWorld\LocalServer\<мир>\` — `analytics.txt` (журнал `register\|enter\|exit`), `Data\users\user_list.json` (id→имя), `Data\users\user<N>.json` (часы/уровень/роль/бан), `Logs\game_state.txt` (онлайн по картам). Каталог мира — `config.json → players` (`localserver_root` / `world` / `world_dir`; пусто = мир с самым свежим `analytics.txt`). Кэш 15 c. **Пароли игроков (`Code`) не отдаются в список/карточку.** Клик по нику → карточка: профиль, клан + друзья, исследования/миссии, позиция/территории, аватар (статы/навыки/способности/инвентарь с именами), сессии + гистограмма, история (роли/смерти/земли/награды), публичный чат. Под своим паролем панели (повторная проверка + аудит) — кнопки «Показать пароль» игрока и «Приваты и IP» (`chat_privat.txt` + `log_net_ip.txt`) |

Интерфейс двуязычный (ru/en, тумблер в шапке, выбор в `localStorage` браузера),
тёмная/светлая тема. Отключить панель целиком — `"webui": { "enabled": false }` в
`config.json`.

---

## 5. Подгонка координат кликов (если другое разрешение / версия игры)

Координаты в `login_flow` — пиксели ОТНОСИТЕЛЬНО левого-верхнего угла окна игры
при `game_window_size`. Проверить/снять новые:

```bat
REM записать команду для задачи SigmaNav и запустить её
echo shot menu > "%BASE%\nav_cmd.txt"
powershell Start-ScheduledTask -TaskName SigmaNav
REM скрин: %BASE%\logs\nav\latest.png  — по нему меряете кнопки
```

Другие команды `nav.py` (через `nav_cmd.txt` + `Start-ScheduledTask SigmaNav`, либо
`"%BASE%\venv\Scripts\python.exe" "%BASE%\nav.py" <...>`):

* `state` — что на экране (`menu/login/loading/ingame`)
* `click <rx> <ry> [left|right]` — клик + скрин после
* `restore` — развернуть окно в `0,0,W,H`
* `seq login` — вся последовательность входа

Пиксельные пробы распознавания экрана — в `detect.py` (тоже под 1024×768,
масштабируются, но при сильно другом UI сверьте).

---

## 6. Как это устроено

```
Задача SigmaSteamBot (AtLogon, Interactive/Highest, авто-рестарт)
  └─ venv\pythonw.exe supervisor.py         (один процесс)
       ├─ watchdog  — держит Steam+игру; после запуска игры и паузы
       │              login_settle_seconds -> авто-вход; пишет state.json
       ├─ bot       — Telegram long-poll через SOCKS5 (обёртка над curl.exe),
       │              отдельный поток-отправитель, чтобы сеть не блокировала
       ├─ webui     — http.server на 0.0.0.0:8080 (см. §4a): тот же функционал,
       │              + роли + логи; статус из last_snapshot, без нагрузки на цикл
       └─ авто-вход и /login  ->  runner.run_nav("seq login")
              └─ Start-ScheduledTask SigmaNav   (нужен реальный фокус окна —
                     фоновый поток супервизора SetForegroundWindow не может)
                     └─ nav.py: normalize окна -> detect состояния ->
                        клики Game/Local/✔/Play -> ждать сервер ->
                        Ok (в цикле, пока не ingame)

Задача SigmaConsoleGuard (SYSTEM, по событию отключения RDP 24/40)
  └─ console_guard.ps1  ->  tscon <сессия alex> /dest:console
```

**Почему консольная сессия обязательна:** SendInput/фокус в игру работают только
когда сессия с игрой — активная консольная. Если зайти по RDP и отключиться,
`SigmaConsoleGuard` вернёт сессию на консоль. Штатно в эту сессию по RDP не
заходят — смотрят через `/shot`.

Файлы в `%BASE%`: `*.py` (код), `config.json`, `state.json` (счётчики+снапшот),
`webui_auth.json` (логин/хэш пароля панели), `webui_audit.log` (действия из панели),
`logs\supervisor.log` (ротация 5МБ×3), `logs\nav\*.png` (скрины шагов, авто-чистка
до 80), `supervisor.lock`, `nav_cmd.txt`/`nav_out.txt` (обмен с SigmaNav).

---

## 7. Управление и диагностика

```powershell
Start-ScheduledTask  -TaskName SigmaSteamBot     # запустить
Stop-ScheduledTask   -TaskName SigmaSteamBot     # остановить
Disable-ScheduledTask -TaskName SigmaSteamBot    # не стартовать при входе
Get-Content C:\...\sigmabot\logs\supervisor.log -Tail 40 -Wait
```

`start.cmd` — запуск supervisor в консоли (видно stdout) для отладки.

Снять автологон: в `HKLM\SOFTWARE\Microsoft\Windows NT\CurrentVersion\Winlogon`
`AutoAdminLogon=0`, удалить `DefaultPassword`.

Полностью удалить:

```powershell
'SigmaSteamBot','SigmaNav','SigmaConsoleGuard' | % { Unregister-ScheduledTask -TaskName $_ -Confirm:$false }
Remove-Item -Recurse C:\Users\alex\sigmabot
```

---

## 8. Типовые проблемы

| Симптом | Причина / что делать |
|---|---|
| `/shot` присылает ошибку `screen grab failed` | сессия не консольная — зайдите один раз и отключитесь (сработает `SigmaConsoleGuard`), либо перезагрузите VM без RDP |
| Клики в игру не проходят, в логе `NOT-FOCUSED` | то же — нужна активная консольная сессия |
| Авто-вход кликает мимо / открывает язык | окно игры не 1024×768 или другое разрешение сессии — выставьте разрешение и/или пересоберите координаты (§5) |
| `getUpdates: curl rc=28/35` в логе | прокси недоступен/медленный; бот сам ретраит, watchdog не блокируется |
| После ребута бот не заходит в игру | проверьте автологон (должна быть `console <user> Активна` в `qwinsta`) и что Steam помнит пароль, а игра — креды |
| `login_state` застрял в `running` | смотрите `logs\nav\seq*.png` и `nav_out.txt` — на каком шаге встало |
