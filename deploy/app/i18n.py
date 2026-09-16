# -*- coding: utf-8 -*-
"""Двуязычные строки Telegram-бота (ru / en).

`t(lang, key, **kw)` -> строка. Неизвестный язык -> DEFAULT, неизвестный ключ ->
строка из DEFAULT, а если и там нет — сам ключ (чтобы ничего не падало).
"""

SUPPORTED = ("ru", "en")
DEFAULT = "ru"


def norm(lang):
    return lang if lang in SUPPORTED else DEFAULT


def t(lang, key, **kw):
    d = L.get(lang) or L[DEFAULT]
    s = d.get(key)
    if s is None:
        s = L[DEFAULT].get(key, key)
    if kw:
        try:
            return s.format(**kw)
        except Exception:  # noqa: BLE001
            return s
    return s


L = {
    "ru": {
        # --- единицы измерения ---
        "unit.bytes": "Б КБ МБ ГБ ТБ ПБ",
        "unit.dur": "д ч м с",
        "dash": "—",

        # --- кнопки меню ---
        "menu.status": "📊 Статус",
        "menu.shot": "📷 Скрин",
        "menu.game_start": "▶️ Запустить игру",
        "menu.game_stop": "⏹ Остановить игру",
        "menu.login": "🎮 Войти в игру",
        "menu.game_restart": "🔄 Перезапуск игры",
        "menu.steam_restart": "♻️ Перезапуск Steam",
        "menu.servers": "🌐 Серверы",
        "menu.wd_toggle": "🐕 Watchdog вкл/выкл",
        "menu.vm_restart": "🖥 Перезагрузить VM",
        "menu.bot_stop": "⛔ Остановить бота",
        "menu.lang": "🌐 Язык",

        # --- подтверждения ---
        "confirm.yes_vm": "✅ Да, перезагрузить",
        "confirm.yes_stop": "⛔ Да, остановить",
        "confirm.cancel": "❌ Отмена",
        "lang.ru": "Русский",
        "lang.en": "English",

        # --- запросы/подсказки ---
        "prompt.vm": "⚠️ Перезагрузить VM? Steam и игра будут закрыты.",
        "prompt.lang": "🌐 Выберите язык интерфейса:",
        "prompt.stop": (
            "⛔ Остановить SigmaSteamBot?\n"
            "Скрипт выключится и <b>сам не перезапустится</b> (ни авто-рестартом, ни при "
            "перезагрузке VM). Игра и Steam останутся работать. Вернуть — только с VM "
            "через SSH/RDP."
        ),

        # --- веб-панель ---
        "webui.info": (
            "🌐 Веб-панель: <code>{url}</code>\n"
            "Забыли пароль? <code>/webui reset</code> сбросит на admin/admin "
            "(панель попросит сменить при следующем входе)."
        ),
        "webui.disabled": "🌐 Веб-панель выключена в config.json (webui.enabled=false).",
        "webui.reset_done": "🔑 Пароль веб-панели сброшен на admin/admin — смените при входе.",

        # --- алерты ---
        "alert.bot_started": "🟢 <b>SigmaSteamBot запущен</b>",
        "alert.vm_back": "✅ <b>VM снова в сети</b> (после перезагрузки)",

        # --- ответы ---
        "reply.denied_cb": "Доступ запрещён",
        "reply.denied_cmd": "⛔ Эта команда доступна только администратору.",
        "reply.your_id": (
            "Ваш Telegram ID: <code>{id}</code>\n"
            "Доступ запрещён — добавьте ID в config.json."
        ),
        "reply.not_understood": "Не понял. /help",
        "reply.canceled": "Отменено.",
        "reply.lang_set": "🌐 Язык переключён на русский.",

        # --- «работаю…» ---
        "wait.startgame": "▶️ Запускаю игру…",
        "wait.stopgame": "⏹ Останавливаю игру…",
        "wait.restartgame": "🔄 Перезапускаю игру и выполняю вход…",
        "wait.restartsteam": "♻️ Перезапускаю Steam…",
        "wait.login": "🎮 Выполняю вход в игру…",
        "wait.vm": "🖥 Перезагрузка VM по команде из Telegram…",
        "wait.botstop": "⛔ Останавливаю бота…",
        "wait.servers": "🌐 Запрашиваю список серверов…",

        # --- вход / рестарт+вход ---
        "login.done": "вошёл в игру",
        "login.failed": "последовательность входа не завершена",
        "login.seq_error": "ошибка последовательности: {err}",
        "restartlogin.done": "игра перезапущена, вход выполнен",
        "restartlogin.restart_failed": "не удалось перезапустить игру: {msg}",
        "restartlogin.login_failed": "игра перезапущена, но вход не завершён",

        # --- watchdog ---
        "wd.state": "🐕 Watchdog сейчас <b>{st}</b>.",
        "wd.toggled": "🐕 Watchdog теперь <b>{st}</b>.",
        "wd.on": "включён",
        "wd.off": "выключен",

        # --- остановка бота ---
        "botstop.ok": (
            "🛑 <b>SigmaSteamBot остановлен.</b>\n"
            "Задача <code>{task}</code> отключена — авто-рестарта не будет.\n\n"
            "Запуск снова (на VM):\n"
            "<code>Enable-ScheduledTask -TaskName {task}; Start-ScheduledTask -TaskName {task}</code>"
        ),
        "botstop.fail": (
            "⚠️ Процесс завершаю, но задачу <code>{task}</code> отключить не вышло:\n"
            "<code>{msg}</code>\n"
            "Планировщик поднимет бота снова через ~2 минуты — отключите задачу вручную."
        ),
        "vm.fail": "🔴 Не удалось: {msg}",

        # --- скриншот ---
        "shot.caption": "Экран VM • {method} • {w}x{h} • {ts}",
        "shot.capture_failed": "🔴 Не удалось снять экран:\n<code>{err}</code>",
        "shot.send_failed": "🔴 Отправка не удалась: {err}",

        # --- список серверов ---
        "servers.head": "🌐 <b>Серверы Sigma World Online</b> — всего {n}",
        "servers.none": "🌐 Публичных серверов сейчас нет.",
        "servers.more": "…и ещё {n}",
        "servers.fail": "🔴 Не удалось получить список серверов:\n<code>{err}</code>",
        "servers.via_webapi": "\n<i>источник: Steam Web API</i>",

        # --- монитор наличия сервера ---
        "monitor.astral_missing": "⚠️ <b>{name}</b> пропал из публичного списка серверов Steam (проверок подряд: {n}).",
        "monitor.astral_back": "✅ <b>{name}</b> снова в публичном списке серверов Steam.",

        # --- аудит для главного админа ---
        "audit.line": "👤 <b>{who}</b> ({role}) → <code>{what}</code>",
        "role.admin": "администратор",
        "role.moderator": "модератор",

        # --- /status ---
        "status.collect_failed": "🔴 Не удалось собрать статус: {err}",
        "status.vm": "🖥 <b>VM</b> • аптайм {up}",
        "status.res": (
            "   CPU {cpu}% ({cores} ядер) • RAM {memp}% ({memu} / {memt}) • "
            "C: своб. {cfree} ({cusedp}% занято)"
        ),
        "status.sess": "   Сессия: {sess}",
        "status.sess_rdp": "RDP подключён",
        "status.sess_console": "консоль активна",
        "status.sess_none": "нет активной сессии",
        "status.steam": "\n🎮 <b>Steam</b>: {run} • {acc} • {li}",
        "status.steam_up": "🟢 работает",
        "status.steam_down": "🔴 не запущен",
        "status.li_yes": "вошёл в аккаунт",
        "status.li_no": "НЕ вошёл",
        "status.li_unknown": "?",
        "status.game_up": (
            "🕹 <b>Игра</b>: 🟢 работает • {ls} • PID {pid} • {dur} • RAM {ram} • CPU {cpu}%"
        ),
        "status.game_down": "🕹 <b>Игра</b>: 🔴 не запущена",
        "status.ls_done": "в игре ✅",
        "status.ls_running": "вход выполняется…",
        "status.ls_idle": "в меню",
        "status.ls_unknown": "?",
        "status.win": "окно: «{title}»",
        "status.minimized": "свёрнуто",
        "status.noresp": "⚠️ не отвечает",
        "status.wd": "\n🐕 Watchdog: {on} • перезапусков с загрузки: игра {g} / Steam {s}",
        "status.wd_on": "вкл",
        "status.wd_off": "выкл",
        "status.last_restart": "   последний перезапуск: {ts}",

        # --- /help ---
        "help.admin": (
            "<b>SigmaSteamBot</b> — управление игрой Sigma World Online на VM\n\n"
            "/status — состояние VM, Steam и игры\n"
            "/shot — скриншот экрана\n"
            "/servers — публичные серверы (Steam-лобби)\n"
            "/startgame · /stopgame\n"
            "/restartgame — перезапустить игру и войти в мир\n"
            "/login — пройти вход в игру (Game→Local→Steam→Play→Ok)\n"
            "/restartsteam — перезапуск Steam\n"
            "/restartvm — перезагрузка VM (с подтверждением)\n"
            "/watchdog on|off — авто-поддержание игры\n"
            "/webui [reset] — адрес веб-панели / сброс её пароля на admin/admin\n"
            "/lang ru|en — язык интерфейса\n"
            "/stopbot — остановить сам скрипт (с подтверждением; запуск обратно — только с VM)\n\n"
            "Ниже — кнопки для того же самого."
        ),
        "help.mod": (
            "<b>SigmaSteamBot</b> — Sigma World Online (роль: модератор)\n\n"
            "/status — состояние VM, Steam и игры\n"
            "/shot — скриншот экрана\n"
            "/restartgame — перезапустить игру и войти в мир\n"
            "/login — пройти вход в игру\n"
            "/lang ru|en — язык интерфейса\n\n"
            "Ниже — кнопки для того же самого."
        ),

        # --- gamectl (результаты операций) ---
        "gc.steam_already_running": "Steam уже запущен",
        "gc.steam_started": "Steam запущен",
        "gc.steam_timeout": "Steam не поднялся за {sec} с",
        "gc.steam_already_stopped": "Steam уже остановлен",
        "gc.steam_stopped": "Steam остановлен",
        "gc.steam_killed": "Steam убит принудительно",
        "gc.steam_restarted": "Steam перезапущен",
        "gc.game_already_running": "Игра уже запущена",
        "gc.game_started": "Игра запущена",
        "gc.game_timeout": "Игра не появилась за {sec} с",
        "gc.game_already_stopped": "Игра уже закрыта",
        "gc.game_stopped": "Игра закрыта",

        # --- алерты watchdog ---
        "wd_alert.steam_exited": "❌ Steam завершился.",
        "wd_alert.game_closed": "❌ Игра <b>{name}</b> закрылась.",
        "wd_alert.steam_down_starting": "⚠️ Steam не запущен — поднимаю…",
        "wd_alert.game_down_starting": "⚠️ Игра не запущена — запускаю…",
        "wd_alert.limit": "🛑 Достигнут лимит перезапусков ({n}/час). Авто-действия приостановлены.",
        "wd_alert.login_ok": "🎮 Автовход в игру выполнен.",
        "wd_alert.login_retry": "⚠️ Автовход не удался — повтор через {sec} c.",
        "wd_alert.result_ok": "✅ {msg}",
        "wd_alert.result_fail": "🔴 {msg}",
    },

    "en": {
        # --- units ---
        "unit.bytes": "B KB MB GB TB PB",
        "unit.dur": "d h m s",
        "dash": "—",

        # --- menu buttons ---
        "menu.status": "📊 Status",
        "menu.shot": "📷 Screenshot",
        "menu.game_start": "▶️ Start game",
        "menu.game_stop": "⏹ Stop game",
        "menu.login": "🎮 Log in",
        "menu.game_restart": "🔄 Restart game",
        "menu.steam_restart": "♻️ Restart Steam",
        "menu.servers": "🌐 Servers",
        "menu.wd_toggle": "🐕 Watchdog on/off",
        "menu.vm_restart": "🖥 Reboot VM",
        "menu.bot_stop": "⛔ Stop bot",
        "menu.lang": "🌐 Language",

        # --- confirmations ---
        "confirm.yes_vm": "✅ Yes, reboot",
        "confirm.yes_stop": "⛔ Yes, stop",
        "confirm.cancel": "❌ Cancel",
        "lang.ru": "Русский",
        "lang.en": "English",

        # --- prompts ---
        "prompt.vm": "⚠️ Reboot the VM? Steam and the game will be closed.",
        "prompt.lang": "🌐 Choose interface language:",
        "prompt.stop": (
            "⛔ Stop SigmaSteamBot?\n"
            "The script shuts down and <b>will not restart itself</b> (no auto-restart, "
            "not on VM reboot). The game and Steam keep running. Bringing it back — only "
            "from the VM via SSH/RDP."
        ),

        # --- web panel ---
        "webui.info": (
            "🌐 Web panel: <code>{url}</code>\n"
            "Forgot the password? <code>/webui reset</code> resets it to admin/admin "
            "(the panel will ask you to change it on next login)."
        ),
        "webui.disabled": "🌐 Web panel disabled in config.json (webui.enabled=false).",
        "webui.reset_done": "🔑 Web panel password reset to admin/admin — change it on login.",

        # --- alerts ---
        "alert.bot_started": "🟢 <b>SigmaSteamBot started</b>",
        "alert.vm_back": "✅ <b>VM is back online</b> (after reboot)",

        # --- replies ---
        "reply.denied_cb": "Access denied",
        "reply.denied_cmd": "⛔ This command is for administrators only.",
        "reply.your_id": (
            "Your Telegram ID: <code>{id}</code>\n"
            "Access denied — add the ID to config.json."
        ),
        "reply.not_understood": "Didn't get that. /help",
        "reply.canceled": "Cancelled.",
        "reply.lang_set": "🌐 Language set to English.",

        # --- "working…" ---
        "wait.startgame": "▶️ Starting the game…",
        "wait.stopgame": "⏹ Stopping the game…",
        "wait.restartgame": "🔄 Restarting the game and logging in…",
        "wait.restartsteam": "♻️ Restarting Steam…",
        "wait.login": "🎮 Logging into the game…",
        "wait.vm": "🖥 Rebooting the VM by Telegram command…",
        "wait.botstop": "⛔ Stopping the bot…",
        "wait.servers": "🌐 Requesting the server list…",

        # --- login / restart+login ---
        "login.done": "logged into the game",
        "login.failed": "login sequence did not complete",
        "login.seq_error": "sequence error: {err}",
        "restartlogin.done": "game restarted, logged in",
        "restartlogin.restart_failed": "could not restart the game: {msg}",
        "restartlogin.login_failed": "game restarted, but login did not complete",

        # --- watchdog ---
        "wd.state": "🐕 Watchdog is currently <b>{st}</b>.",
        "wd.toggled": "🐕 Watchdog is now <b>{st}</b>.",
        "wd.on": "on",
        "wd.off": "off",

        # --- stop bot ---
        "botstop.ok": (
            "🛑 <b>SigmaSteamBot stopped.</b>\n"
            "Scheduled task <code>{task}</code> disabled — no auto-restart.\n\n"
            "Start again (on the VM):\n"
            "<code>Enable-ScheduledTask -TaskName {task}; Start-ScheduledTask -TaskName {task}</code>"
        ),
        "botstop.fail": (
            "⚠️ Stopping the process, but could not disable task <code>{task}</code>:\n"
            "<code>{msg}</code>\n"
            "The scheduler will bring the bot back in ~2 min — disable the task manually."
        ),
        "vm.fail": "🔴 Failed: {msg}",

        # --- screenshot ---
        "shot.caption": "VM screen • {method} • {w}x{h} • {ts}",
        "shot.capture_failed": "🔴 Screen capture failed:\n<code>{err}</code>",
        "shot.send_failed": "🔴 Send failed: {err}",

        # --- server list ---
        "servers.head": "🌐 <b>Sigma World Online servers</b> — {n} total",
        "servers.none": "🌐 No public servers right now.",
        "servers.more": "…and {n} more",
        "servers.fail": "🔴 Could not get the server list:\n<code>{err}</code>",
        "servers.via_webapi": "\n<i>source: Steam Web API</i>",

        # --- server-presence monitor ---
        "monitor.astral_missing": "⚠️ <b>{name}</b> disappeared from the public Steam server list (consecutive checks: {n}).",
        "monitor.astral_back": "✅ <b>{name}</b> is back in the public Steam server list.",

        # --- audit for the super-admin ---
        "audit.line": "👤 <b>{who}</b> ({role}) → <code>{what}</code>",
        "role.admin": "admin",
        "role.moderator": "moderator",

        # --- /status ---
        "status.collect_failed": "🔴 Could not collect status: {err}",
        "status.vm": "🖥 <b>VM</b> • uptime {up}",
        "status.res": (
            "   CPU {cpu}% ({cores} cores) • RAM {memp}% ({memu} / {memt}) • "
            "C: free {cfree} ({cusedp}% used)"
        ),
        "status.sess": "   Session: {sess}",
        "status.sess_rdp": "RDP connected",
        "status.sess_console": "console active",
        "status.sess_none": "no active session",
        "status.steam": "\n🎮 <b>Steam</b>: {run} • {acc} • {li}",
        "status.steam_up": "🟢 running",
        "status.steam_down": "🔴 not running",
        "status.li_yes": "signed in",
        "status.li_no": "NOT signed in",
        "status.li_unknown": "?",
        "status.game_up": (
            "🕹 <b>Game</b>: 🟢 running • {ls} • PID {pid} • {dur} • RAM {ram} • CPU {cpu}%"
        ),
        "status.game_down": "🕹 <b>Game</b>: 🔴 not running",
        "status.ls_done": "in game ✅",
        "status.ls_running": "logging in…",
        "status.ls_idle": "at menu",
        "status.ls_unknown": "?",
        "status.win": "window: «{title}»",
        "status.minimized": "minimized",
        "status.noresp": "⚠️ not responding",
        "status.wd": "\n🐕 Watchdog: {on} • restarts since boot: game {g} / Steam {s}",
        "status.wd_on": "on",
        "status.wd_off": "off",
        "status.last_restart": "   last restart: {ts}",

        # --- /help ---
        "help.admin": (
            "<b>SigmaSteamBot</b> — Sigma World Online control on the VM\n\n"
            "/status — VM, Steam and game state\n"
            "/shot — screen screenshot\n"
            "/servers — public servers (Steam lobbies)\n"
            "/startgame · /stopgame\n"
            "/restartgame — restart the game and log into the world\n"
            "/login — run the in-game login (Game→Local→Steam→Play→Ok)\n"
            "/restartsteam — restart Steam\n"
            "/restartvm — reboot the VM (with confirmation)\n"
            "/watchdog on|off — auto-keep the game up\n"
            "/webui [reset] — web panel URL / reset its password to admin/admin\n"
            "/lang ru|en — interface language\n"
            "/stopbot — stop the script itself (with confirmation; restart only from the VM)\n\n"
            "The buttons below do the same."
        ),
        "help.mod": (
            "<b>SigmaSteamBot</b> — Sigma World Online (role: moderator)\n\n"
            "/status — VM, Steam and game state\n"
            "/shot — screen screenshot\n"
            "/restartgame — restart the game and log into the world\n"
            "/login — run the in-game login\n"
            "/lang ru|en — interface language\n\n"
            "The buttons below do the same."
        ),

        # --- gamectl (operation results) ---
        "gc.steam_already_running": "Steam already running",
        "gc.steam_started": "Steam started",
        "gc.steam_timeout": "Steam did not start within {sec}s",
        "gc.steam_already_stopped": "Steam already stopped",
        "gc.steam_stopped": "Steam stopped",
        "gc.steam_killed": "Steam force-killed",
        "gc.steam_restarted": "Steam restarted",
        "gc.game_already_running": "Game already running",
        "gc.game_started": "Game started",
        "gc.game_timeout": "Game did not appear within {sec}s",
        "gc.game_already_stopped": "Game already closed",
        "gc.game_stopped": "Game closed",

        # --- watchdog alerts ---
        "wd_alert.steam_exited": "❌ Steam has exited.",
        "wd_alert.game_closed": "❌ Game <b>{name}</b> has closed.",
        "wd_alert.steam_down_starting": "⚠️ Steam is down — starting it…",
        "wd_alert.game_down_starting": "⚠️ Game is down — starting it…",
        "wd_alert.limit": "🛑 Restart limit reached ({n}/hour). Auto-actions paused.",
        "wd_alert.login_ok": "🎮 Auto-login completed.",
        "wd_alert.login_retry": "⚠️ Auto-login failed — retry in {sec}s.",
        "wd_alert.result_ok": "✅ {msg}",
        "wd_alert.result_fail": "🔴 {msg}",
    },
}
