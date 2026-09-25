# -*- coding: utf-8 -*-
"""Метрики нагрузки для вкладки «Нагрузка» админки.

Раз в ``interval`` секунд (по умолчанию 30) — снимок в ``logs/metrics.jsonl``:

* сервер целиком: CPU (среднее и самое загруженное ядро), RAM, файл подкачки,
  диск (IOPS и МБ/с чтения/записи, свободное место на диске мира), сеть;
* процессы: игра (SigmaWorld.exe), Steam, сама панель — CPU (в % от всей машины),
  RAM (RSS), IOPS и МБ/с чтения/записи, потоки, дескрипторы;
* панели: запросов в минуту и среднее время ответа — отдельно админка и панель игроков;
* игроки онлайн (раз в 2 минуты).

Для графика за период точки усредняются в корзины (среднее и максимум корзины).
"""
import json
import logging
import os
import threading
import time

try:
    import psutil
except Exception:  # noqa: BLE001
    psutil = None

import players

MB = 1024 * 1024


class Metrics:
    def __init__(self, cfg, base, interval=30, max_mb=30):
        self.cfg = cfg
        self.path = os.path.join(base, "logs", "metrics.jsonl")
        self.interval = max(10, int(interval))
        self.max_bytes = int(max_mb * MB)
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._http = {"admin": [0, 0.0], "player": [0, 0.0]}   # запросов, сумма мс с прошлого снимка
        self._procs = {}          # роль -> psutil.Process
        self._prev = None         # (t, системные счётчики, {роль: счётчики процесса})
        self._online = (0, None)  # (когда считали, сколько)
        self.last = None
        self.cores = psutil.cpu_count() if psutil else None

    # ------------------------------------------------------------------ сбор
    def http(self, panel, ms):
        with self._lock:
            c = self._http[panel]
            c[0] += 1
            c[1] += ms

    def start(self):
        if psutil is None:
            logging.warning("metrics: нет psutil — вкладка «Нагрузка» без данных")
            return
        psutil.cpu_percent(None)
        threading.Thread(target=self._loop, name="metrics", daemon=True).start()

    def stop(self):
        self._stop.set()

    def _loop(self):
        while not self._stop.wait(self.interval if self._prev else 5):
            try:
                rec = self._sample()
                if rec:
                    self._write(rec)
            except Exception:  # noqa: BLE001
                logging.exception("metrics: снимок")

    def _proc(self, role, names):
        p = self._procs.get(role)
        try:
            if p and p.is_running():
                return p
        except psutil.Error:
            pass
        self._procs.pop(role, None)
        if role == "panel":
            p = psutil.Process(os.getpid())
        else:
            p = None
            for q in psutil.process_iter(["name"]):
                if (q.info.get("name") or "").lower() in names:
                    p = q
                    break
        if p:
            try:
                p.cpu_percent(None)
            except psutil.Error:
                return None
            self._procs[role] = p
        return p

    def _world_drive(self):
        wd = players.find_world_dir(self.cfg) or self.cfg.get("base_dir") or "C:\\"
        return os.path.splitdrive(os.path.abspath(wd))[0] + "\\" if os.name == "nt" else "/"

    def _sample(self):
        now = time.time()
        game = os.path.basename(self.cfg.get("game_exe") or "SigmaWorld.exe").lower()
        cur_proc = {}
        rec = {"t": int(now)}
        per = psutil.cpu_percent(None, percpu=True)
        rec["cpu"] = round(sum(per) / len(per), 1) if per else None
        rec["cpu_max"] = round(max(per), 1) if per else None
        vm = psutil.virtual_memory()
        rec.update(ram_pct=vm.percent, ram_used=round(vm.used / MB), ram_total=round(vm.total / MB))
        try:
            rec["swap_pct"] = psutil.swap_memory().percent
        except Exception:  # noqa: BLE001
            pass
        try:
            du = psutil.disk_usage(self._world_drive())
            rec.update(disk_free_pct=round(100 - du.percent, 1), disk_free_gb=round(du.free / 1024 / MB, 1))
        except Exception:  # noqa: BLE001
            pass
        dio = psutil.disk_io_counters()
        net = psutil.net_io_counters()
        sysc = {"dr": dio.read_count if dio else 0, "dw": dio.write_count if dio else 0,
                "drb": dio.read_bytes if dio else 0, "dwb": dio.write_bytes if dio else 0,
                "ni": net.bytes_recv if net else 0, "no": net.bytes_sent if net else 0}
        for role, names in (("game", {game}), ("steam", {"steam.exe"}), ("panel", set())):
            p = self._proc(role, names)
            if not p:
                continue
            try:
                with p.oneshot():
                    io = p.io_counters()
                    cur_proc[role] = {"r": io.read_count, "w": io.write_count, "rb": io.read_bytes, "wb": io.write_bytes}
                    rec[role[0] + "_cpu"] = round(p.cpu_percent(None) / (self.cores or 1), 1)
                    rec[role[0] + "_rss"] = round(p.memory_info().rss / MB)
                    rec[role[0] + "_thr"] = p.num_threads()
                    if hasattr(p, "num_handles"):
                        rec[role[0] + "_h"] = p.num_handles()
            except psutil.Error:
                self._procs.pop(role, None)
        if self._prev:
            pt, ps, pp = self._prev
            dt = max(1.0, now - pt)
            d = lambda a, b: max(0, a - b) / dt
            rec.update(d_r_iops=round(d(sysc["dr"], ps["dr"]), 1), d_w_iops=round(d(sysc["dw"], ps["dw"]), 1),
                       d_r_mbs=round(d(sysc["drb"], ps["drb"]) / MB, 3), d_w_mbs=round(d(sysc["dwb"], ps["dwb"]) / MB, 3),
                       n_in_mbs=round(d(sysc["ni"], ps["ni"]) / MB, 3), n_out_mbs=round(d(sysc["no"], ps["no"]) / MB, 3))
            for role, c in cur_proc.items():
                o = pp.get(role)
                if o:
                    k = role[0]
                    rec.update({k + "_r_iops": round(d(c["r"], o["r"]), 1), k + "_w_iops": round(d(c["w"], o["w"]), 1),
                                k + "_r_mbs": round(d(c["rb"], o["rb"]) / MB, 3), k + "_w_mbs": round(d(c["wb"], o["wb"]) / MB, 3)})
            with self._lock:
                for panel, (n, ms) in self._http.items():
                    rec["req_" + panel] = round(n * 60.0 / dt, 1)
                    rec["lat_" + panel] = round(ms / n) if n else None
                self._http = {"admin": [0, 0.0], "player": [0, 0.0]}
        self._prev = (now, sysc, cur_proc)
        if now - self._online[0] >= 120:     # разбор analytics.txt — не чаще раза в 2 мин
            try:
                wd = players.find_world_dir(self.cfg)
                self._online = (now, sum(1 for v in players._online_now(wd).values() if v) if wd else None)
            except Exception:  # noqa: BLE001
                self._online = (now, None)
        rec["online"] = self._online[1]
        self.last = rec
        return rec if len(rec) > 12 and "d_r_iops" in rec else None

    def _write(self, rec):
        line = json.dumps({k: v for k, v in rec.items() if v is not None}, separators=(",", ":")) + "\n"
        try:
            os.makedirs(os.path.dirname(self.path), exist_ok=True)
            with open(self.path, "a", encoding="utf-8") as f:
                f.write(line)
            players._rotate(self.path, self.max_bytes)
        except OSError:
            logging.exception("metrics: запись")

    # ------------------------------------------------------------------ чтение
    def last_saved(self):
        """Последний записанный снимок (сразу после перезапуска, пока нет нового)."""
        for ln in players._read_text(self.path, tail_bytes=4000).splitlines()[::-1]:
            try:
                return json.loads(ln)
            except ValueError:
                continue
        return None

    def series(self, seconds, points=360):
        """Корзины за последние ``seconds``: {поле: [[t, среднее, максимум], ...]}."""
        since = time.time() - seconds
        per_line = 700
        need = int(seconds / self.interval * per_line) + 100_000
        rows = []
        paths = [self.path]
        try:
            if need > os.path.getsize(self.path) and os.path.exists(self.path + ".1"):
                paths.insert(0, self.path + ".1")      # период длиннее текущего файла — захватить прошлый
        except OSError:
            pass
        for path in paths:
            for ln in players._read_text(path, tail_bytes=need).splitlines():
                try:
                    r = json.loads(ln)
                except ValueError:
                    continue
                if r.get("t", 0) >= since:
                    rows.append(r)
        rows.sort(key=lambda r: r["t"])
        step = max(self.interval, seconds / points)
        buckets = {}
        for r in rows:
            b = int((r["t"] - since) // step)
            buckets.setdefault(b, []).append(r)
        out = {}
        for b in sorted(buckets):
            grp = buckets[b]
            t = int(since + (b + 0.5) * step)
            keys = set().union(*(r.keys() for r in grp)) - {"t"}
            for k in keys:
                vals = [r[k] for r in grp if isinstance(r.get(k), (int, float))]
                if vals:
                    out.setdefault(k, []).append([t, round(sum(vals) / len(vals), 3), max(vals)])
        return {"series": out, "samples": len(rows), "step": int(step)}
