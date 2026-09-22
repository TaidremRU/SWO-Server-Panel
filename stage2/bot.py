# -*- coding: utf-8 -*-
"""Telegram-бот управления (long polling через SOCKS5-прокси).

Роли:
  * **admin** — Telegram-ID из ``telegram.allowed_user_ids``; полный доступ.
  * **moderator** — ID из ``telegram.moderator_user_ids``; только /status, /shot,
    /restartgame (= перезапуск + вход), /login и /lang. Watchdog, Steam, VM и
    остановка бота — недоступны.

Главный админ (``telegram.super_admin_id``) получает уведомление о каждом
действии другого админа/модератора, **меняющем состояние** (start/stop/restart
игры и Steam, вход в игру, watchdog on|off, перезагрузка VM, /stopbot). Просмотр
статуса, скриншоты и смена языка не логируются.

Интерфейс двуязычный (ru/en). Язык — на пользователя, хранится в ``state.json``
(``user_lang``), меняется командой /lang или кнопкой. По умолчанию —
``telegram.default_lang``.
"""
import datetime
import html
import logging
import os
import queue
import threading
import time

import common
import gamectl
import i18n
import screenshot
import serverlist
import sysinfo

# команды и callback-действия, разрешённые роли moderator
MOD_CMDS = {"start", "help", "menu", "status", "shot", "restartgame", "login", "lang"}
MOD_ACTS = {"status", "shot", "game_restart", "login", "lang", "lang_ru", "lang_en"}

# что уходит в аудит главному админу — только действия, меняющие состояние
# (не /status, /shot, /lang, не открытие диалогов подтверждения и не отказы)
AUDIT_CMDS = {"startgame", "stopgame", "restartgame", "restartsteam", "login", "watchdog", "webui"}
AUDIT_ACTS = {"game_start", "game_stop", "game_restart", "steam_restart", "login",
              "wd_toggle", "vm_yes", "bot_stop_yes"}


def _lan_ip():
    """Локальный LAN-адрес этой машины (без реальной отправки пакетов)."""
    import socket

    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))
        return s.getsockname()[0]
    except OSError:
        return "127.0.0.1"
    finally:
        s.close()


def _fmt_bytes(n, lang="ru"):
    if n is None:
        return i18n.t(lang, "dash")
    units = i18n.t(lang, "unit.bytes").split()
    n = float(n)
    for i, u in enumerate(units):
        if n < 1024 or i == len(units) - 1:
            return ("%.0f %s" % (n, u)) if i == 0 else ("%.1f %s" % (n, u))
        n /= 1024
    return "%.1f %s" % (n, units[-1])


def _fmt_dur(s, lang="ru"):
    if s is None:
        return i18n.t(lang, "dash")
    d_, h_, m_, s_ = i18n.t(lang, "unit.dur").split()
    s = int(s)
    d, s = divmod(s, 86400)
    h, s = divmod(s, 3600)
    m, s = divmod(s, 60)
    if d:
        return "%d%s %d%s %d%s" % (d, d_, h, h_, m, m_)
    if h:
        return "%d%s %d%s" % (h, h_, m, m_)
    if m:
        return "%d%s %d%s" % (m, m_, s, s_)
    return "%d%s" % (s, s_)


class Bot:
    def __init__(self, cfg, state, watchdog_ref=None):
        self.cfg = cfg
        self.state = state
        self.wd = watchdog_ref
        self.web = None  # проставляется супервизором после старта веб-панели (см. supervisor.py)
        tg = cfg["telegram"]
        self._who = {}  # uid -> отображаемое имя (для аудита)
        self.apply_roles(tg)  # _admins / _mods / allowed / _super_admin / _default_lang / alerts_enabled
        self.tg = common.Telegram(tg["token"], tg["proxy"], tg.get("poll_timeout", 50))
        self._offset = 0
        self._last_poll_ok = 0.0  # ts последнего успешного getUpdates (для веб-панели)
        self._stop = threading.Event()
        # (target, text): target=None -> всем админам; int -> конкретному uid
        self._outbox = queue.Queue()

        # фоновый монитор наличия сервера в списке Steam-лобби
        self.apply_monitor(cfg.get("monitor", {}) or {})

    def apply_monitor(self, mon=None):
        """Пересчитать параметры фонового монитора сервера из конфига.

        Вызывается из ``__init__`` и веб-панелью после правки ``config.json`` —
        имя сервера/интервалы/вкл-выкл меняются на лету, без перезапуска
        супервизора (сам поток ``_monitor_loop`` живёт всегда, см. ``run()``,
        и на каждой итерации проверяет ``self._mon_enabled``).
        """
        mon = mon if mon is not None else (self.cfg.get("monitor", {}) or {})
        self._mon_enabled = mon.get("enabled", True)
        self._mon_name = mon.get("server_name", "AstralSigma")
        self._mon_interval = max(60, int(mon.get("interval_seconds", 300)))
        self._mon_misses_before = max(1, int(mon.get("misses_before_alert", 2)))
        self._mon_repeat = int(mon.get("repeat_alert_seconds", 3600))  # 0 = без напоминаний

    def apply_roles(self, tg):
        """Пересчитать роли и язык из telegram-блока конфига.

        Вызывается из ``__init__`` и веб-панелью после правки ``config.json`` —
        роли меняются на лету, без перезапуска супервизора.
        """
        self._admins = set(tg.get("allowed_user_ids", []))
        self._mods = set(tg.get("moderator_user_ids", [])) - self._admins
        self.allowed = self._admins  # получатели широковещательных алертов
        sa = tg.get("super_admin_id")
        if not sa and tg.get("allowed_user_ids"):
            sa = tg["allowed_user_ids"][0]
        self._super_admin = sa
        self._default_lang = i18n.norm(tg.get("default_lang", i18n.DEFAULT))
        self.alerts_enabled = tg.get("alerts_enabled", True)

    # ---------- отправка из любого потока (НЕ блокирует вызывающего) ----------
    def push_alert(self, text):
        if self.alerts_enabled:
            self._outbox.put((None, text))

    def _broadcast_roles(self, key, **kw):
        """Разослать локализованное сообщение всем админам И модераторам."""
        if not self.alerts_enabled:
            return
        for uid in (self._admins | self._mods):
            self._outbox.put((uid, i18n.t(self._lang(uid), key, **kw)))

    def _sender_loop(self):
        while not self._stop.is_set():
            try:
                tgt, text = self._outbox.get(timeout=1.0)
            except queue.Empty:
                continue
            uids = list(self.allowed) if tgt is None else [tgt]
            for uid in uids:
                try:
                    self.tg.send_message(uid, text)
                except Exception:  # noqa: BLE001
                    logging.exception("sender: не удалось отправить сообщение")

    def notify_startup(self):
        try:
            snap = sysinfo.collect(self.cfg, self.state)
        except Exception:  # noqa: BLE001
            snap = None
        lang = self._default_lang
        head = i18n.t(lang, "alert.bot_started")
        if snap and snap["uptime_seconds"] < 300:
            head = i18n.t(lang, "alert.vm_back")
        body = ("\n" + self._status_text(snap, lang)) if snap else ""
        self.push_alert(head + body)

    # ---------- основной цикл ----------
    def run(self):
        logging.info("bot: старт")
        threading.Thread(target=self._sender_loop, name="sender", daemon=True).start()
        threading.Thread(target=self._monitor_loop, name="srvmonitor", daemon=True).start()
        r = self.tg.get_updates(0, 0)
        if r.get("ok") and r.get("result"):
            self._offset = r["result"][-1]["update_id"] + 1
        self.notify_startup()
        timeout = self.cfg["telegram"].get("poll_timeout", 50)
        while not self._stop.is_set():
            try:
                r = self.tg.get_updates(self._offset, timeout)
                if not r.get("ok"):
                    logging.warning("getUpdates: %s", r.get("error") or r.get("description") or r)
                    time.sleep(8)
                    continue
                self._last_poll_ok = time.time()
                for upd in r.get("result", []):
                    self._offset = upd["update_id"] + 1
                    try:
                        self._handle(upd)
                    except Exception:  # noqa: BLE001
                        logging.exception("handle update error")
            except Exception:  # noqa: BLE001
                logging.exception("poll loop error")
                time.sleep(8)

    def stop(self):
        self._stop.set()

    # ---------- роли / язык / аудит ----------
    def _role(self, uid):
        if uid in self._admins:
            return "admin"
        if uid in self._mods:
            return "moderator"
        return None

    def _lang(self, uid):
        try:
            v = self.state.data.get("user_lang", {}).get(str(uid))
        except Exception:  # noqa: BLE001
            v = None
        return i18n.norm(v or self._default_lang)

    def _set_lang(self, uid, lang):
        lang = i18n.norm(lang)
        with self.state.lock:
            self.state.data.setdefault("user_lang", {})[str(uid)] = lang
        self.state.save()

    def _set_lang_and_ack(self, chat, uid, newlang):
        self._set_lang(uid, newlang)
        role = self._role(uid)
        self.tg.send_message(chat, i18n.t(newlang, "reply.lang_set"), self._menu(newlang, role))

    def _remember(self, uid, frm):
        if not uid:
            return
        name = (frm.get("first_name") or "").strip()
        if frm.get("username"):
            name = (name + " @" + frm["username"]).strip()
        if name:
            self._who[uid] = name

    def _lock_or_deny(self, chat, uid, lang, op):
        """Взять action-лок (см. common.try_action_lock) перед stopgame/restartgame/
        restartsteam/restartvm. True — можно выполнять, False — уже занято другим
        админом/модератором, отказ уже отправлен в чат."""
        actor = self._who.get(uid) or ("id %s" % uid)
        ok, info = common.try_action_lock(self.state, actor, op)
        if ok:
            return True
        self.tg.send_message(chat, i18n.t(lang, "action.locked", actor=info["actor"],
                                           op=i18n.op_label(lang, info["op"]), left=info["left"]))
        return False

    def _audit(self, uid, what):
        """Копию действия другого админа/модератора — главному админу."""
        sa = self._super_admin
        if not sa or uid == sa:
            return
        lang = self._lang(sa)
        role = i18n.t(lang, "role.%s" % (self._role(uid) or "moderator"))
        who = self._who.get(uid) or ("id %s" % uid)
        self._outbox.put(
            (sa, i18n.t(lang, "audit.line", who=html.escape(str(who)), role=role, what=html.escape(what)))
        )

    # ---------- обработка ----------
    def _handle(self, upd):
        if "message" in upd:
            msg = upd["message"]
            frm = msg.get("from", {}) or {}
            uid = frm.get("id")
            self._remember(uid, frm)
            text = (msg.get("text") or "").strip()
            chat = msg["chat"]["id"]
            role = self._role(uid)
            if role is None:
                if text.startswith("/start"):
                    self.tg.send_message(chat, i18n.t(self._default_lang, "reply.your_id", id=uid))
                return
            self._command(chat, text, uid, role)
        elif "callback_query" in upd:
            cq = upd["callback_query"]
            frm = cq.get("from", {}) or {}
            uid = frm.get("id")
            self._remember(uid, frm)
            data = cq.get("data", "")
            chat = cq["message"]["chat"]["id"]
            role = self._role(uid)
            act = data.split(":", 1)[1] if ":" in data else data
            if role is None:
                self.tg.answer_callback(cq["id"], i18n.t(self._default_lang, "reply.denied_cb"))
                return
            if role == "moderator" and act not in MOD_ACTS:
                lang = self._lang(uid)
                self.tg.answer_callback(cq["id"], i18n.t(lang, "reply.denied_cb"))
                self.tg.send_message(chat, i18n.t(lang, "reply.denied_cmd"))
                return
            self.tg.answer_callback(cq["id"])
            self._action(chat, act, uid, role)

    def _command(self, chat, text, uid, role):
        parts = text.split()
        cmd = parts[0].lower().lstrip("/").split("@")[0] if parts else ""
        arg = parts[1].lower() if len(parts) > 1 else ""
        lang = self._lang(uid)

        if role == "moderator" and cmd and cmd not in MOD_CMDS:
            self.tg.send_message(chat, i18n.t(lang, "reply.denied_cmd"), self._menu(lang, role))
            return
        if (cmd in AUDIT_CMDS and not (cmd == "watchdog" and arg not in ("on", "off"))
                and not (cmd == "webui" and arg != "reset")):
            self._audit(uid, text.strip()[:120])

        if cmd in ("start", "help", "menu"):
            self.tg.send_message(chat, self._help_text(lang, role), self._menu(lang, role))
        elif cmd == "status":
            self.tg.send_message(chat, self._status_text(lang=lang), self._menu(lang, role))
        elif cmd == "shot":
            self._do_shot(chat, lang)
        elif cmd == "servers":
            self._do_servers(chat, lang, role)
        elif cmd == "startgame":
            self._async(chat, i18n.t(lang, "wait.startgame"),
                        lambda: self._tr(lang, gamectl.start_game(self.cfg)), lang=lang, role=role)
        elif cmd == "stopgame":
            if self._lock_or_deny(chat, uid, lang, "stopgame"):
                self._async(chat, i18n.t(lang, "wait.stopgame"),
                            lambda: self._tr(lang, gamectl.stop_game(self.cfg)), lang=lang, role=role)
        elif cmd == "restartgame":
            if self._lock_or_deny(chat, uid, lang, "restartgame"):
                self._async(chat, i18n.t(lang, "wait.restartgame"),
                            lambda: self._do_restart_and_login(lang), shot_after=True, lang=lang, role=role)
        elif cmd == "restartsteam":
            if self._lock_or_deny(chat, uid, lang, "restartsteam"):
                self._async(chat, i18n.t(lang, "wait.restartsteam"),
                            lambda: self._tr(lang, gamectl.restart_steam(self.cfg)), lang=lang, role=role)
        elif cmd == "login":
            self._async(chat, i18n.t(lang, "wait.login"),
                        lambda: self._do_login(lang), shot_after=True, lang=lang, role=role)
        elif cmd == "restartvm":
            self.tg.send_message(chat, i18n.t(lang, "prompt.vm"), self._confirm_vm(lang))
        elif cmd in ("stopbot", "stop"):
            self.tg.send_message(chat, i18n.t(lang, "prompt.stop"), self._confirm_stop(lang))
        elif cmd == "watchdog":
            if arg in ("on", "off") and self.wd:
                self.wd.set_enabled(arg == "on")
            st = i18n.t(lang, "wd.on") if (self.wd and self.wd.enabled) else i18n.t(lang, "wd.off")
            self.tg.send_message(chat, i18n.t(lang, "wd.state", st=st), self._menu(lang, role))
        elif cmd == "lang":
            if arg in i18n.SUPPORTED:
                self._set_lang_and_ack(chat, uid, arg)
            else:
                self.tg.send_message(chat, i18n.t(lang, "prompt.lang"), self._lang_kb(lang))
        elif cmd == "webui":
            wui = self.cfg.get("webui", {}) or {}
            if not wui.get("enabled", True) or not self.web:
                self.tg.send_message(chat, i18n.t(lang, "webui.disabled"))
            elif arg == "reset":
                self.web.auth.reset()
                self.tg.send_message(chat, i18n.t(lang, "webui.reset_done"))
            else:
                url = "http://%s:%s/" % (_lan_ip(), wui.get("port", 8080))
                self.tg.send_message(chat, i18n.t(lang, "webui.info", url=url))
        else:
            self.tg.send_message(chat, i18n.t(lang, "reply.not_understood"), self._menu(lang, role))

    def _action(self, chat, act, uid, role):
        lang = self._lang(uid)
        if act in AUDIT_ACTS:
            self._audit(uid, "act:" + act)

        if act == "status":
            self.tg.send_message(chat, self._status_text(lang=lang), self._menu(lang, role))
        elif act == "shot":
            self._do_shot(chat, lang)
        elif act == "servers":
            self._do_servers(chat, lang, role)
        elif act == "game_start":
            self._async(chat, i18n.t(lang, "wait.startgame"),
                        lambda: self._tr(lang, gamectl.start_game(self.cfg)), lang=lang, role=role)
        elif act == "game_stop":
            if self._lock_or_deny(chat, uid, lang, "stopgame"):
                self._async(chat, i18n.t(lang, "wait.stopgame"),
                            lambda: self._tr(lang, gamectl.stop_game(self.cfg)), lang=lang, role=role)
        elif act == "game_restart":
            if self._lock_or_deny(chat, uid, lang, "restartgame"):
                self._async(chat, i18n.t(lang, "wait.restartgame"),
                            lambda: self._do_restart_and_login(lang), shot_after=True, lang=lang, role=role)
        elif act == "steam_restart":
            if self._lock_or_deny(chat, uid, lang, "restartsteam"):
                self._async(chat, i18n.t(lang, "wait.restartsteam"),
                            lambda: self._tr(lang, gamectl.restart_steam(self.cfg)), lang=lang, role=role)
        elif act == "login":
            self._async(chat, i18n.t(lang, "wait.login"),
                        lambda: self._do_login(lang), shot_after=True, lang=lang, role=role)
        elif act == "wd_toggle":
            if self.wd:
                self.wd.set_enabled(not self.wd.enabled)
                st = i18n.t(lang, "wd.on") if self.wd.enabled else i18n.t(lang, "wd.off")
                self.tg.send_message(chat, i18n.t(lang, "wd.toggled", st=st), self._menu(lang, role))
        elif act == "vm_restart":
            self.tg.send_message(chat, i18n.t(lang, "prompt.vm"), self._confirm_vm(lang))
        elif act == "vm_yes":
            if self._lock_or_deny(chat, uid, lang, "restartvm"):
                self.push_alert(i18n.t(self._default_lang, "wait.vm"))

                def _vm_worker():
                    ok, msg = gamectl.restart_vm(self.cfg)
                    if not ok:
                        self.tg.send_message(chat, i18n.t(lang, "vm.fail", msg=html.escape(str(msg))))

                threading.Thread(target=_vm_worker, name="vmrestart", daemon=True).start()
        elif act == "vm_no":
            self.tg.send_message(chat, i18n.t(lang, "reply.canceled"), self._menu(lang, role))
        elif act == "bot_stop":
            self.tg.send_message(chat, i18n.t(lang, "prompt.stop"), self._confirm_stop(lang))
        elif act == "bot_stop_yes":
            self._do_stop_bot(chat, lang)
        elif act == "bot_stop_no":
            self.tg.send_message(chat, i18n.t(lang, "reply.canceled"), self._menu(lang, role))
        elif act == "lang":
            self.tg.send_message(chat, i18n.t(lang, "prompt.lang"), self._lang_kb(lang))
        elif act == "lang_ru":
            self._set_lang_and_ack(chat, uid, "ru")
        elif act == "lang_en":
            self._set_lang_and_ack(chat, uid, "en")

    # ---------- клавиатуры ----------
    def _menu(self, lang, role):
        def b(k, d):
            return {"text": i18n.t(lang, k), "callback_data": d}

        rows = [
            [b("menu.status", "act:status"), b("menu.shot", "act:shot")],
            [b("menu.login", "act:login"), b("menu.game_restart", "act:game_restart")],
        ]
        if role == "admin":
            rows.append([b("menu.game_start", "act:game_start"), b("menu.game_stop", "act:game_stop")])
            rows.append([b("menu.steam_restart", "act:steam_restart"), b("menu.servers", "act:servers")])
            rows.append([b("menu.wd_toggle", "act:wd_toggle"), b("menu.vm_restart", "act:vm_restart")])
            rows.append([b("menu.bot_stop", "act:bot_stop")])
        rows.append([b("menu.lang", "act:lang")])
        return {"inline_keyboard": rows}

    def _confirm_vm(self, lang):
        return {"inline_keyboard": [[
            {"text": i18n.t(lang, "confirm.yes_vm"), "callback_data": "act:vm_yes"},
            {"text": i18n.t(lang, "confirm.cancel"), "callback_data": "act:vm_no"},
        ]]}

    def _confirm_stop(self, lang):
        return {"inline_keyboard": [[
            {"text": i18n.t(lang, "confirm.yes_stop"), "callback_data": "act:bot_stop_yes"},
            {"text": i18n.t(lang, "confirm.cancel"), "callback_data": "act:bot_stop_no"},
        ]]}

    def _lang_kb(self, lang):
        return {"inline_keyboard": [[
            {"text": i18n.t(lang, "lang.ru"), "callback_data": "act:lang_ru"},
            {"text": i18n.t(lang, "lang.en"), "callback_data": "act:lang_en"},
        ]]}

    # ---------- действия ----------
    def _tr(self, lang, res):
        """(ok, key, params) от gamectl -> (ok, локализованная строка)."""
        ok, key, params = res
        return ok, i18n.t(lang, key, **(params or {}))

    def _async(self, chat, wait_text, fn, shot_after=False, lang=None, role="admin"):
        lang = i18n.norm(lang or self._default_lang)
        self.tg.send_message(chat, wait_text)

        def worker():
            try:
                ok, msg = fn()
                self.tg.send_message(
                    chat,
                    ("✅ " if ok else "🔴 ") + str(msg) + "\n\n" + self._status_text(lang=lang),
                    self._menu(lang, role),
                )
                if shot_after:
                    self._do_shot(chat, lang)
            except Exception as e:  # noqa: BLE001
                logging.exception("action error")
                self.tg.send_message(chat, "🔴 " + html.escape(str(e)))

        threading.Thread(target=worker, name="action", daemon=True).start()

    def _do_login(self, lang):
        try:
            import runner

            ok, out = runner.run_nav("seq login", timeout=280)
        except Exception as e:  # noqa: BLE001
            logging.exception("login sequence error")
            return False, i18n.t(lang, "login.seq_error", err=e)
        tail = [ln for ln in out.strip().splitlines() if ln.strip()][-3:]
        suffix = ("\n<code>%s</code>" % html.escape("\n".join(tail))) if tail else ""
        return ok, i18n.t(lang, "login.done" if ok else "login.failed") + suffix

    def _do_restart_and_login(self, lang):
        """Перезапустить игру и сразу пройти вход в мир (для admin и moderator)."""
        ok, key, params = gamectl.restart_game(self.cfg)
        if not ok:
            return False, i18n.t(lang, "restartlogin.restart_failed",
                                 msg=i18n.t(lang, key, **(params or {})))
        lok, lmsg = self._do_login(lang)
        if lok:
            return True, i18n.t(lang, "restartlogin.done")
        return False, i18n.t(lang, "restartlogin.login_failed") + (("\n" + lmsg) if lmsg else "")

    def _do_stop_bot(self, chat, lang):
        """Отключить задачу планировщика и завершить супервизор.

        Выполняется прямо в цикле опроса, поэтому сообщения шлём синхронно (через
        _outbox не успеют уйти до выхода процесса).
        """
        self.tg.send_message(chat, i18n.t(lang, "wait.botstop"))
        task = self.cfg.get("task_name", "SigmaSteamBot")
        ok, msg = gamectl.disable_bot_task(task)
        logging.info("bot: /stopbot из Telegram, disable %s ok=%s (%s)", task, ok, msg)
        if ok:
            txt = i18n.t(lang, "botstop.ok", task=task)
        else:
            txt = i18n.t(lang, "botstop.fail", task=task, msg=html.escape(str(msg)))
        self.tg.send_message(chat, txt)
        if self.wd:
            try:
                self.wd.stop()
            except Exception:  # noqa: BLE001
                logging.exception("stop: не удалось остановить watchdog")
        self.stop()

    def _do_shot(self, chat, lang):
        path = os.path.join(self.cfg["base_dir"], "logs", "shot.png")
        try:
            method, size = screenshot.capture_game(self.cfg, path)
        except Exception as e:  # noqa: BLE001
            logging.exception("shot: захват не удался")
            self.tg.send_message(chat, i18n.t(lang, "shot.capture_failed", err=html.escape(str(e))))
            return
        cap = i18n.t(lang, "shot.caption", method=method, w=size[0], h=size[1],
                     ts=datetime.datetime.now().strftime("%H:%M:%S"))
        r = self.tg.send_photo(chat, path, cap)
        if not r.get("ok"):
            self.tg.send_message(
                chat,
                i18n.t(lang, "shot.send_failed",
                       err=html.escape(str(r.get("error") or r.get("description")))),
            )

    # ---------- список игровых серверов ----------
    def _do_servers(self, chat, lang, role):
        self.tg.send_message(chat, i18n.t(lang, "wait.servers"))

        def worker():
            try:
                txt = self._servers_text(lang)
            except Exception as e:  # noqa: BLE001
                logging.exception("servers error")
                txt = i18n.t(lang, "servers.fail", err=html.escape(str(e)))
            self.tg.send_message(chat, txt, self._menu(lang, role))

        threading.Thread(target=worker, name="servers", daemon=True).start()

    def _servers_text(self, lang):
        ok, res, src = serverlist.fetch(self.cfg)
        if not ok:
            return i18n.t(lang, "servers.fail", err=html.escape(str(res)))
        if not res:
            return i18n.t(lang, "servers.none")

        hl_target = self._mon_name.lower().replace(" ", "")

        def is_hl(s):
            return hl_target in (s.get("name") or "").lower().replace(" ", "")

        res.sort(key=lambda s: (not is_hl(s), -(s.get("players") or 0), (s.get("name") or "").lower()))
        shown = res[:40]
        lines = [i18n.t(lang, "servers.head", n=len(res))]
        for s in shown:
            name = html.escape(s.get("name") or "?")
            pl, mx = s.get("players") or 0, s.get("max_players") or 0
            extra = ""
            if s.get("map"):
                extra += " • " + html.escape(str(s["map"]))
            if s.get("version"):
                extra += " • v" + html.escape(str(s["version"]))
            head = ("👑 <b>%s</b>" % name) if is_hl(s) else ("• %s" % name)
            lines.append("%s — %d/%d" % (head, pl, mx))
            lines.append("    <code>%s</code>%s" % (html.escape(s.get("addr") or "?"), extra))
        if len(res) > len(shown):
            lines.append(i18n.t(lang, "servers.more", n=len(res) - len(shown)))
        if src == "webapi":
            lines.append(i18n.t(lang, "servers.via_webapi"))
        return "\n".join(lines)

    # ---------- фоновый монитор наличия сервера в списке ----------
    def _monitor_loop(self):
        # первая проверка — не сразу на старте (дать Steam/игре подняться)
        if self._stop.wait(min(self._mon_interval, 90)):
            return
        while not self._stop.is_set():
            if self._mon_enabled:
                try:
                    self._monitor_check(self._mon_name.lower().replace(" ", ""))
                except Exception:  # noqa: BLE001
                    logging.exception("srvmonitor: ошибка проверки")
            if self._stop.wait(self._mon_interval):
                return

    def _monitor_check(self, target):
        ok, res, src = serverlist.fetch(self.cfg)
        if not ok:
            logging.warning("srvmonitor: список серверов недоступен (%s) — пропускаю тик", res)
            return
        present = any(target in (s.get("name") or "").lower().replace(" ", "") for s in res)

        st = self.state.data.get("monitor_astral") or {}
        alerted = bool(st.get("alerted"))
        misses = int(st.get("misses") or 0)
        last_alert = float(st.get("last_alert_ts") or 0)
        now = time.time()

        if present:
            if alerted:
                self._broadcast_roles("monitor.astral_back", name=self._mon_name)
                logging.info("srvmonitor: %s снова в списке", self._mon_name)
            new_st = {"present": True, "misses": 0, "alerted": False, "last_alert_ts": 0}
        else:
            misses += 1
            fire = (not alerted and misses >= self._mon_misses_before) or (
                alerted and self._mon_repeat and now - last_alert >= self._mon_repeat
            )
            if fire:
                self._broadcast_roles("monitor.astral_missing", name=self._mon_name, n=misses)
                logging.warning("srvmonitor: %s НЕ в списке (проверок подряд: %d, src=%s)",
                                self._mon_name, misses, src)
                alerted, last_alert = True, now
            new_st = {"present": False, "misses": misses,
                      "alerted": alerted, "last_alert_ts": last_alert}

        with self.state.lock:
            self.state.data["monitor_astral"] = new_st
        self.state.save()

    def _help_text(self, lang, role):
        return i18n.t(lang, "help.mod" if role == "moderator" else "help.admin")

    def _status_text(self, snap=None, lang=None):
        lang = i18n.norm(lang or self._default_lang)

        def T(k, **kw):
            return i18n.t(lang, k, **kw)

        if snap is None:
            try:
                snap = sysinfo.collect(self.cfg, self.state)
            except Exception as e:  # noqa: BLE001
                return T("status.collect_failed", err=html.escape(str(e)))
        g = snap["game"]
        s = snap["steam"]
        out = []
        out.append(T("status.vm", up=_fmt_dur(snap["uptime_seconds"], lang)))
        out.append(T(
            "status.res",
            cpu="%.0f" % snap["cpu_percent"], cores=snap["cpu_count"] or "?",
            memp="%.0f" % snap["mem"]["percent"],
            memu=_fmt_bytes(snap["mem"]["used"], lang), memt=_fmt_bytes(snap["mem"]["total"], lang),
            cfree=_fmt_bytes(snap["disk_c"]["free"], lang), cusedp="%.0f" % snap["disk_c"]["percent"],
        ))
        sess = (
            T("status.sess_rdp") if snap["rdp_connected"]
            else (T("status.sess_console") if snap["console_active"] else T("status.sess_none"))
        )
        out.append(T("status.sess", sess=sess))

        li = {True: T("status.li_yes"), False: T("status.li_no"), None: T("status.li_unknown")}[s["logged_in"]]
        out.append(T(
            "status.steam",
            run=T("status.steam_up") if s["running"] else T("status.steam_down"),
            acc=html.escape(s["account"] or "?"), li=li,
        ))

        if g["running"]:
            ls = {
                "done": T("status.ls_done"), "running": T("status.ls_running"), "idle": T("status.ls_idle"),
            }.get(self.state.data.get("login_state"), T("status.ls_unknown"))
            out.append(T(
                "status.game_up",
                ls=ls, pid=g.get("pid"), dur=_fmt_dur(g.get("run_seconds"), lang),
                ram=_fmt_bytes(g.get("rss"), lang), cpu="%.0f" % (g.get("cpu") or 0),
            ))
            extra = []
            if g.get("window_title"):
                extra.append(T("status.win", title=html.escape(g["window_title"])))
            if g.get("minimized"):
                extra.append(T("status.minimized"))
            if g.get("responding") is False:
                extra.append(T("status.noresp"))
            if extra:
                out.append("   " + " • ".join(extra))
        else:
            out.append(T("status.game_down"))

        c = snap.get("counters", {})
        wd_on = self.wd.enabled if self.wd else c.get("watchdog_enabled")
        out.append(T(
            "status.wd",
            on=T("status.wd_on") if wd_on else T("status.wd_off"),
            g=c.get("game_restarts", 0), s=c.get("steam_restarts", 0),
        ))
        if c.get("last_restart_ts"):
            out.append(T(
                "status.last_restart",
                ts=datetime.datetime.fromtimestamp(c["last_restart_ts"]).strftime("%d.%m %H:%M:%S"),
            ))
        return "\n".join(out)
