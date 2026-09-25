# -*- coding: utf-8 -*-
"""Защита входа от перебора паролей — одна на админку (webui) и панель игроков.

Счётчики неудач по трём осям:

* **адрес** (``ip:<адрес>``) — 5 неудач за 15 минут -> блок адреса;
* **учётка** (``adm:<логин>`` админки, ``nick:<ник>`` игрока, ``gm:<uid>`` —
  вход стаффа в админку из панели игроков) — 10 неудач за час с любых адресов ->
  блок учётки. Распределённый перебор (много адресов по одному нику) упирается
  сюда. Чтобы этим нельзя было «запереть» чужой ник, блок учётки не действует
  на адреса, с которых в эту учётку уже успешно входили (последние 30 дней);
* **все сразу** — больше 60 неудач за 10 минут = массовый перебор: тревога и
  замедление ответа на каждую неудачу.

Блокировка растёт: 1 мин → 5 → 15 → 1 ч → 6 ч → сутки; уровень забывается
после суток без блокировок. Состояние на диске (``auth_guard.json``) —
перезапуск панели блокировки не снимает. О блокировках учёток, повторных
блокировках адреса, массовом переборе и успешном входе после серии неудач —
тревога главному админу в Telegram.
"""
import ipaddress
import json
import logging
import os
import threading
import time

IP_RULE = (5, 900)            # неудач, за секунд
ACCT_RULE = (10, 3600)
STEPS = (60, 300, 900, 3600, 6 * 3600, 24 * 3600)
LEVEL_FORGET = 24 * 3600
FLOOD = (60, 600)
FAIL_DELAY, FLOOD_DELAY = 1.0, 3.0
KNOWN_TTL, KNOWN_MAX = 30 * 86400, 20
ALERT_EVERY = 3600            # не чаще раза в час по одному ключу


def _ip_obj(s):
    try:
        a = ipaddress.ip_address(str(s).strip().split("%")[0])
    except ValueError:
        return None
    if a.version == 6 and a.ipv4_mapped:
        a = a.ipv4_mapped
    return a


def _in_nets(ip, nets):
    a = _ip_obj(ip)
    return bool(a) and any(a.version == n.version and a in n for n in nets)


def parse_nets(items):
    out = []
    for x in items or []:
        try:
            out.append(ipaddress.ip_network(str(x).strip(), strict=False))
        except ValueError:
            logging.error("trusted_proxies: не адрес/подсеть: %r — пропускаю", x)
    return out


def client_ip(h, trusted):
    """Настоящий адрес клиента. X-Forwarded-For берётся, только если запрос пришёл
    от доверенного прокси (``trusted`` — список подсетей), иначе заголовок —
    подделка и игнорируется. Идём по цепочке справа, пропуская свои прокси."""
    peer = h.client_address[0]
    if not trusted or not _in_nets(peer, trusted):
        return peer
    chain = [x.strip() for x in (h.headers.get("X-Forwarded-For") or "").split(",") if x.strip()]
    for x in reversed(chain):
        if not _ip_obj(x):
            break
        if not _in_nets(x, trusted):
            return str(_ip_obj(x))
    return peer


class Guard:
    def __init__(self, path, alert=None, on_event=None):
        self.path = path
        self.alert = alert            # f(text) — в Telegram главному админу
        self.on_event = on_event      # f(dict) — в журнал активности
        self._lock = threading.Lock()
        self._keys = {}               # key -> {"f": [ts], "until": ts, "lvl": n, "lb": ts, "who": str}
        self._known = {}              # учётка -> {ip: ts успешного входа}
        self._recent = []             # времена всех неудач (массовый перебор)
        self._alerted = {}
        self._flood_since = 0
        try:
            with open(path, "r", encoding="utf-8") as f:
                d = json.load(f)
            self._keys = d.get("keys") or {}
            self._known = d.get("known") or {}
        except FileNotFoundError:
            pass
        except Exception:  # noqa: BLE001
            logging.exception("authguard: %s повреждён — начинаю с чистого", path)

    # ----------------------------------------------------------------- helpers
    def _save(self):
        now = time.time()
        for k in list(self._keys):
            e = self._keys[k]
            rule = IP_RULE if k.startswith("ip:") else ACCT_RULE
            e["f"] = [t for t in e.get("f", []) if now - t < rule[1]]
            if not e["f"] and e.get("until", 0) < now and now - e.get("lb", 0) > LEVEL_FORGET:
                del self._keys[k]
        for a in list(self._known):
            m = {ip: t for ip, t in self._known[a].items() if now - t < KNOWN_TTL}
            if m:
                self._known[a] = dict(sorted(m.items(), key=lambda x: -x[1])[:KNOWN_MAX])
            else:
                del self._known[a]
        tmp = self.path + ".tmp"
        try:
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump({"keys": self._keys, "known": self._known}, f, ensure_ascii=False)
            os.replace(tmp, self.path)
        except OSError:
            logging.exception("authguard: не записать %s", self.path)

    def _alert(self, key, text):
        now = time.time()
        if now - self._alerted.get(key, 0) < ALERT_EVERY:
            return
        self._alerted[key] = now
        logging.warning("authguard: %s", text)
        if self.alert:
            try:
                self.alert("🛡 " + text)
            except Exception:  # noqa: BLE001
                logging.exception("authguard: тревога не ушла")

    def _event(self, **rec):
        if self.on_event:
            try:
                self.on_event(rec)
            except Exception:  # noqa: BLE001
                logging.exception("authguard: журнал")

    def _is_known(self, acct, ip):
        return bool(acct) and ip in (self._known.get(acct) or {})

    # --------------------------------------------------------------------- api
    def check(self, ip, acct=None):
        """-> (можно ли пробовать, секунд до разблокировки, что заблокировано)."""
        now = time.time()
        with self._lock:
            e = self._keys.get("ip:" + ip)
            if e and e.get("until", 0) > now:
                return False, int(e["until"] - now) + 1, "ip:" + ip
            if acct:
                e = self._keys.get(acct)
                if e and e.get("until", 0) > now and not self._is_known(acct, ip):
                    return False, int(e["until"] - now) + 1, acct
        return True, 0, None

    def fail(self, ip, acct=None, who="", panel=""):
        """Неудачная попытка. Сама выдерживает паузу (замедляет перебор)."""
        now = time.time()
        with self._lock:
            for key, rule in (("ip:" + ip, IP_RULE), (acct, ACCT_RULE)):
                if not key:
                    continue
                e = self._keys.setdefault(key, {"f": [], "until": 0, "lvl": 0, "lb": 0})
                e["f"] = [t for t in e.get("f", []) if now - t < rule[1]] + [now]
                e["who"] = (who or "")[:64]
                if len(e["f"]) < rule[0]:
                    continue
                lvl = e.get("lvl", 0) if now - e.get("lb", 0) < LEVEL_FORGET else 0
                dur = STEPS[min(lvl, len(STEPS) - 1)]
                e.update(until=now + dur, lvl=lvl + 1, lb=now, f=[])
                self._event(ev="block", key=key, ip=ip, user=who, dur=dur, lvl=lvl + 1, panel=panel)
                human = "%d мин" % (dur // 60) if dur < 3600 else "%d ч" % (dur // 3600)
                if key.startswith("ip:"):
                    if lvl + 1 >= 2:
                        self._alert(key, "%s: адрес %s заблокирован на %s (блокировка №%d подряд), последний логин «%s»"
                                    % (panel, ip, human, lvl + 1, who))
                else:
                    self._alert(key, "%s: перебор пароля учётки %s — %d неудач за час, вход с новых адресов закрыт на %s"
                                     " (последний адрес %s)" % (panel, key, rule[0], human, ip))
            self._recent = [t for t in self._recent if now - t < FLOOD[1]] + [now]
            flood = len(self._recent) >= FLOOD[0]
            if flood and not self._flood_since:
                self._flood_since = now
                self._event(ev="flood", ip=ip, n=len(self._recent), panel=panel)
            elif not flood:
                self._flood_since = 0
            if flood:
                self._alert("flood", "массовый перебор паролей: %d неудачных входов за 10 мин (последний — %s с %s, %s)"
                            % (len(self._recent), who, ip, panel))
            self._save()
        time.sleep(FLOOD_DELAY if flood else FAIL_DELAY)

    def ok(self, ip, acct=None, who="", panel=""):
        now = time.time()
        with self._lock:
            e = self._keys.get(acct) if acct else None
            n = len((e or {}).get("f", []))
            if n >= 3:
                self._alert("okafter:" + acct, "%s: успешный вход в %s с %s после %d неудач подряд — проверьте, не подобрали ли пароль"
                            % (panel, acct, ip, n))
            for key in ("ip:" + ip, acct):
                if key and key in self._keys:
                    self._keys[key]["f"] = []
            if acct:
                self._known.setdefault(acct, {})[ip] = now
            self._save()

    def status(self):
        now = time.time()
        with self._lock:
            rows = []
            for k, e in self._keys.items():
                rows.append({"key": k, "until": e.get("until", 0), "left": max(0, int(e.get("until", 0) - now)),
                             "level": e.get("lvl", 0), "fails": len(e.get("f", [])), "who": e.get("who", ""),
                             "last_block": e.get("lb", 0)})
            rows.sort(key=lambda r: (-r["left"], -r["fails"]))
            return {"keys": rows, "flood": bool(self._flood_since),
                    "fails_10m": len([t for t in self._recent if now - t < FLOOD[1]]),
                    "rules": {"ip": IP_RULE, "account": ACCT_RULE, "steps": STEPS, "flood": FLOOD}}

    def unblock(self, key):
        with self._lock:
            e = self._keys.pop(key, None)
            self._save()
        return e is not None
