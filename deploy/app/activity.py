# -*- coding: utf-8 -*-
"""Журнал активности обеих панелей (админки и панели игроков) — для GM.

Пишется всё: входы/неудачи/блокировки, каждый запрос к API (маршрут,
параметры = что искали, код ответа, время), тела POST (пароли и токены
вырезаны), действия в интерфейсе, которые браузер присылает сам (вкладки,
клики, ввод в поля поиска/фильтров), зондирование несуществующих адресов,
записи аудита. Одна строка JSON на событие в ``logs/activity.jsonl``,
ротация по размеру (``.1`` … ``.N``).

Одинаковые GET-запросы одной сессии (автообновление вкладок) пишутся не чаще
раза в 2 минуты, число пропущенных повторов — в поле ``rep``.
"""
import json
import logging
import os
import re
import threading
import time
from datetime import datetime

DEDUP_SEC = 120
_SECRET_RX = re.compile(r"pass|pw|code|secret|token|key|csrf|^old$|^new$", re.I)


def redact(obj, depth=0):
    """Копия тела запроса без секретов, длинные строки обрезаны."""
    if depth > 6:
        return "…"
    if isinstance(obj, dict):
        return {str(k)[:64]: ("***" if _SECRET_RX.search(str(k)) and v not in (None, "", [])
                              else redact(v, depth + 1)) for k, v in list(obj.items())[:60]}
    if isinstance(obj, list):
        out = [redact(v, depth + 1) for v in obj[:60]]
        if len(obj) > 60:
            out.append("… ещё %d" % (len(obj) - 60))
        return out
    if isinstance(obj, str) and len(obj) > 300:
        return obj[:300] + "…(%d)" % len(obj)
    return obj


class ActivityLog:
    def __init__(self, path, max_mb=20, keep=10):
        self.path = path
        self.max_bytes = int(max_mb * 1024 * 1024)
        self.keep = int(keep)
        self._lock = threading.Lock()
        self._dedup = {}          # key -> [t последней записи, пропущено]
        os.makedirs(os.path.dirname(path), exist_ok=True)
        try:
            self._size = os.path.getsize(path)
        except OSError:
            self._size = 0

    def _rotate(self):
        for i in range(self.keep - 1, 0, -1):
            src = "%s.%d" % (self.path, i)
            if os.path.exists(src):
                os.replace(src, "%s.%d" % (self.path, i + 1))
        os.replace(self.path, self.path + ".1")
        self._size = 0

    def write(self, rec):
        now = time.time()
        r = {"t": round(now, 3), "ts": datetime.fromtimestamp(now).strftime("%Y-%m-%d %H:%M:%S")}
        r.update({k: v for k, v in rec.items() if v not in (None, "", {}, [])})
        line = (json.dumps(r, ensure_ascii=False, default=str) + "\n").encode("utf-8")
        try:
            with self._lock:
                if self._size + len(line) > self.max_bytes and self._size:
                    self._rotate()
                with open(self.path, "ab") as f:
                    f.write(line)
                self._size += len(line)
        except OSError:
            logging.exception("activity: не записать %s", self.path)

    def dedup(self, key):
        """True — такой же запрос был недавно, не писать. Иначе -> число пропущенных до этого."""
        now = time.time()
        with self._lock:
            if len(self._dedup) > 20000:
                self._dedup = {k: v for k, v in self._dedup.items() if now - v[0] < DEDUP_SEC}
            e = self._dedup.get(key)
            if e and now - e[0] < DEDUP_SEC:
                e[1] += 1
                return True
            rep = e[1] if e else 0
            self._dedup[key] = [now, 0]
            return rep

    # ------------------------------------------------------------------ чтение
    def files(self):
        out = [self.path] + ["%s.%d" % (self.path, i) for i in range(1, self.keep + 1)]
        return [p for p in out if os.path.exists(p)]

    def query(self, src=None, evs=None, user=None, ip=None, text=None, since=None, before=None, limit=300):
        """Новые сверху. ``before`` — продолжить с записей старше этого t (догрузка)."""
        user = (user or "").lower().strip()
        ip = (ip or "").strip()
        text = (text or "").lower().strip()
        evs = set(evs or [])
        out, scanned = [], 0
        for p in self.files():
            try:
                with open(p, "rb") as f:
                    lines = f.read().decode("utf-8", "replace").splitlines()
            except OSError:
                continue
            for ln in reversed(lines):
                scanned += 1
                if text and text not in ln.lower():
                    continue
                try:
                    r = json.loads(ln)
                except ValueError:
                    continue
                t = r.get("t", 0)
                if before and t >= before:
                    continue
                if since and t < since:
                    return {"rows": out, "scanned": scanned, "more": False}
                if src and r.get("src") != src:
                    continue
                if evs and r.get("ev") not in evs:
                    continue
                if user and user not in str(r.get("user", "")).lower() and user != str(r.get("uid", "")):
                    continue
                if ip and not str(r.get("ip", "")).startswith(ip):
                    continue
                out.append(r)
                if len(out) >= limit:
                    return {"rows": out, "scanned": scanned, "more": True}
        return {"rows": out, "scanned": scanned, "more": False}

    def size(self):
        return sum(os.path.getsize(p) for p in self.files())
