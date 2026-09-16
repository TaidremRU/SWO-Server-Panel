# -*- coding: utf-8 -*-
"""Навигация по UI игры: клики/клавиши по окну Sigma World Online + скриншот после каждого шага.

Координаты кликов задаются ОТНОСИТЕЛЬНО окна игры (0,0 = левый верхний угол окна,
как на скриншоте из /shot). Скрипт сам добавляет экранное смещение окна.

Требует интерактивную сессию (SendInput). Запускать задачей планировщика в сессии
пользователя, не через голый SSH-exec.

Команды:
    nav.py rect
    nav.py shot [tag]
    nav.py click  <rx> <ry> [left|right] [--dbl] [tag]     # абсолютные пиксели окна
    nav.py clickp <xp> <yp> [left|right] [--dbl] [tag]     # % ширины/высоты окна (0..100)
    nav.py move  <rx> <ry> [tag]
    nav.py key   <name> [tag]
    nav.py seq   [<имя_последовательности>]     # из config.json -> "sequences"
"""
import datetime
import json
import logging
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

import pydirectinput  # noqa: E402
import win32gui  # noqa: E402

pydirectinput.FAILSAFE = False  # угол экрана — не повод падать (в фоне position() бывает 0,0)
pydirectinput.PAUSE = 0

import common  # noqa: E402
import detect  # noqa: E402
import screenshot  # noqa: E402
import sysinfo  # noqa: E402


def _raw_click(x, y, button="left", move_first=True):
    if move_first:
        pydirectinput.moveTo(int(x), int(y))
        time.sleep(0.05)
    pydirectinput.click(button=button)


def _raw_move(x, y):
    pydirectinput.moveTo(int(x), int(y))


def _raw_key(name):
    pydirectinput.press(name)

CFG = common.load_config()
LOG_DIR = os.path.join(CFG["base_dir"], "logs")
SHOT_DIR = os.path.join(LOG_DIR, "nav")
WIN_W, WIN_H = CFG.get("game_window_size", [1024, 768])

_log = logging.getLogger("nav")
if not _log.handlers and __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")


def say(msg):
    _log.info(msg)
    if __name__ == "__main__":
        print(msg)


def _game_pids():
    return [p.pid for p in sysinfo.find_procs(["sigmaworld.exe"], CFG.get("game_install_dir"))]


SW_RESTORE = 9


def _unity_hwnd():
    """hwnd окна класса UnityWndClass у любого процесса игры — даже если свёрнуто."""
    for pid in _game_pids():
        for hwnd, cls, _title, _vis, _sz in screenshot.enum_windows_for_pid(pid):
            if "Unity" in cls:
                return hwnd
    return None


def _restore(hwnd):
    """Развернуть (если свёрнуто) и поставить окно в прямоугольник 0,0,W,H.

    Скрин снимается по GetWindowDC (всё окно), клики считаются от того же
    GetWindowRect — невидимая DWM-рамка (~8px) сокращается сама собой.
    """
    try:
        if win32gui.IsIconic(hwnd):
            win32gui.ShowWindow(hwnd, SW_RESTORE)
            time.sleep(0.6)
        win32gui.MoveWindow(hwnd, 0, 0, WIN_W, WIN_H, True)
        time.sleep(0.4)
        l, t, r, b = win32gui.GetWindowRect(hwnd)
        say("окно нормализовано -> (%d,%d,%d,%d) %dx%d" % (l, t, r, b, r - l, b - t))
    except Exception as e:  # noqa: BLE001
        say("restore warn: %s" % e)


def _hwnd(auto_restore=True):
    h = screenshot.find_game_window(_game_pids())
    if h:
        return h
    h = _unity_hwnd()
    if h and auto_restore:
        print("окно игры свёрнуто — разворачиваю")
        _restore(h)
        return h
    if not h:
        raise RuntimeError("окно игры не найдено (игра не в меню?)")
    return h


def _rect(hwnd):
    l, t, r, b = win32gui.GetWindowRect(hwnd)
    return l, t, r, b


def _force_focus(hwnd):
    """Поставить окно игры на передний план. Использует AttachThreadInput —
    иначе фоновый поток не имеет права на SetForegroundWindow."""
    import ctypes

    u = ctypes.windll.user32
    k = ctypes.windll.kernel32
    screenshot.attach_input_desktop()

    try:
        if win32gui.IsIconic(hwnd):
            win32gui.ShowWindow(hwnd, 9)  # SW_RESTORE
    except Exception:  # noqa: BLE001
        pass

    fg = u.GetForegroundWindow()
    my_tid = k.GetCurrentThreadId()
    fg_tid = u.GetWindowThreadProcessId(fg, None) if fg else 0
    tgt_tid = u.GetWindowThreadProcessId(hwnd, None)
    attached = []
    for other in {fg_tid, tgt_tid}:
        if other and other != my_tid and u.AttachThreadInput(my_tid, other, True):
            attached.append(other)
    try:
        u.BringWindowToTop(hwnd)
        u.ShowWindow(hwnd, 5)  # SW_SHOW
        u.SetForegroundWindow(hwnd)
        u.SetActiveWindow(hwnd)
        u.SetFocus(hwnd)
    except Exception as e:  # noqa: BLE001
        say("focus warn: %s" % e)
    finally:
        for other in attached:
            u.AttachThreadInput(my_tid, other, False)

    time.sleep(0.15)
    now_fg = win32gui.GetForegroundWindow()
    say("foreground hwnd=%s target=%s %s" % (now_fg, hwnd, "OK" if now_fg == hwnd else "NOT-FOCUSED"))


def _prune_shots(keep=80):
    try:
        files = sorted(
            (os.path.join(SHOT_DIR, f) for f in os.listdir(SHOT_DIR) if f.endswith(".png") and f != "latest.png"),
            key=os.path.getmtime,
        )
        for f in files[:-keep]:
            os.remove(f)
    except Exception:  # noqa: BLE001
        pass


def shot(tag="shot", hwnd=None):
    hwnd = hwnd or _hwnd()
    os.makedirs(SHOT_DIR, exist_ok=True)
    _prune_shots()
    stamp = datetime.datetime.now().strftime("%H%M%S_%f")[:-3]
    path = os.path.join(SHOT_DIR, "%s_%s.png" % (tag, stamp))
    method, size = screenshot.capture(path, hwnd)
    latest = os.path.join(SHOT_DIR, "latest.png")
    try:
        import shutil

        shutil.copyfile(path, latest)
    except Exception:  # noqa: BLE001
        pass
    l, t, r, b = _rect(hwnd)
    print("shot %s  method=%s size=%s  window_rect=(%d,%d,%d,%d) %dx%d"
          % (path, method, size, l, t, r, b, r - l, b - t))
    return path


def _to_screen(hwnd, rx, ry):
    # координаты кадра == пиксели GetWindowDC-скрина; смещение окна из GetWindowRect
    l, t, _, _ = _rect(hwnd)
    return l + int(rx), t + int(ry)


def _cur_size(hwnd):
    l, t, r, b = _rect(hwnd)
    return max(1, r - l), max(1, b - t)


def _pct_to_rel(hwnd, xp, yp):
    """Проценты (0..100) окна -> пиксели ОТНОСИТЕЛЬНО окна, по ТЕКУЩЕМУ его размеру.

    В отличие от абсолютных пикселей под фиксированный game_window_size,
    работает независимо от реального разрешения/DPI конкретной машины —
    достаточно, чтобы разметка UI игры была одинаковой пропорционально."""
    w, h = _cur_size(hwnd)
    return (float(xp) / 100.0) * w, (float(yp) / 100.0) * h


def _step_xy(step):
    """(xp,yp) в процентах для шага login_flow. Новый формат — step['xp']/['yp'].
    Старый формат (абсолютные пиксели @ game_window_size из config.json) —
    конвертируется на лету, чтобы не ломать уже настроенные конфиги."""
    if "xp" in step and "yp" in step:
        return float(step["xp"]), float(step["yp"])
    return float(step["x"]) / WIN_W * 100.0, float(step["y"]) / WIN_H * 100.0


def _resolve_template(text):
    """Подставить {account_user}/{account_password} активного аккаунта (cfg.active_account
    в cfg.game_accounts) — для шагов action=type при переключении игровых аккаунтов."""
    if not text:
        return text
    active = CFG.get("active_account")
    acc = next((a for a in (CFG.get("game_accounts") or []) if a.get("label") == active), None)
    acc = acc or {}
    return text.replace("{account_user}", acc.get("user", "")).replace(
        "{account_password}", acc.get("password", ""))


def click_pct(hwnd, xp, yp, button="left", dbl=False):
    rx, ry = _pct_to_rel(hwnd, xp, yp)
    _click_rel(hwnd, rx, ry, button, dbl)


def do_click_pct(xp, yp, button="left", dbl=False, tag="afterclick"):
    hwnd = _hwnd()
    print("click%% rel=(%s,%s)" % (xp, yp))
    click_pct(hwnd, float(xp), float(yp), button, dbl)
    time.sleep(1.3)
    return shot(tag, hwnd)


def do_click(rx, ry, button="left", dbl=False, tag="afterclick"):
    hwnd = _hwnd()
    _force_focus(hwnd)
    sx, sy = _to_screen(hwnd, rx, ry)
    print("click rel=(%s,%s) -> screen=(%s,%s) button=%s dbl=%s" % (rx, ry, sx, sy, button, dbl))
    _raw_click(sx, sy, button=button)
    if dbl:
        time.sleep(0.08)
        _raw_click(sx, sy, button=button, move_first=False)
    time.sleep(1.3)
    return shot(tag, hwnd)


def do_move(rx, ry, tag="aftermove"):
    hwnd = _hwnd()
    _force_focus(hwnd)
    sx, sy = _to_screen(hwnd, rx, ry)
    print("move rel=(%s,%s) -> screen=(%s,%s)" % (rx, ry, sx, sy))
    _raw_move(sx, sy)
    time.sleep(0.6)
    return shot(tag, hwnd)


def do_key(name, tag="afterkey"):
    hwnd = _hwnd()
    _force_focus(hwnd)
    print("key", name)
    _raw_key(name)
    time.sleep(1.0)
    return shot(tag, hwnd)


def current_state(hwnd=None):
    """Скриншот + определение экрана. Возвращает (state, path)."""
    hwnd = hwnd or _hwnd()
    path = shot("state", hwnd)
    st = detect.game_state(path)
    print("state =", st)
    return st, path


def _click_rel(hwnd, rx, ry, button="left", dbl=False):
    """Детерминированный клик по координате ОТНОСИТЕЛЬНО окна (без «гуманизации»).

    Позиция окна перечитывается прямо перед кликом; курсор ставится дважды —
    первый SendInput-move иногда «съедается» при переключении фокуса.
    """
    _force_focus(hwnd)
    l, t, _, _ = _rect(hwnd)
    sx, sy = l + int(rx), t + int(ry)
    say("  click rel=(%s,%s) win@(%d,%d) -> screen=(%d,%d) btn=%s" % (rx, ry, l, t, sx, sy, button))
    pydirectinput.moveTo(sx, sy)
    time.sleep(0.12)
    pydirectinput.moveTo(sx, sy)
    time.sleep(0.10)
    pydirectinput.click(button=button)
    if dbl:
        time.sleep(0.08)
        pydirectinput.click(button=button)


def _wait_state(hwnd, targets, timeout, tag):
    """Опрашивать экран, пока состояние не попадёт в targets. Возвращает увиденное состояние."""
    deadline = time.time() + timeout
    seen = None
    while time.time() < deadline:
        seen = detect.game_state(shot(tag, hwnd))
        if seen in targets:
            return seen
        time.sleep(3)
    return seen


def _check_half(hwnd):
    w, _h = _cur_size(hwnd)
    return max(4, int(round(11 * w / 1024.0)))


def _run_menu_steps(hwnd, steps):
    for i, step in enumerate(steps, 1):
        act = step.get("action", "click")
        tag = "seq%02d_%s" % (i, step.get("tag", act))
        say("--- меню, шаг %d/%d: %s %s" % (i, len(steps), act, step))
        if act == "click":
            xp, yp = _step_xy(step)
            click_pct(hwnd, xp, yp, step.get("button", "left"), step.get("dbl"))
        elif act == "ensure_check":
            xp, yp = _step_xy(step)
            w, h = _cur_size(hwnd)
            px, py, half = int(xp / 100.0 * w), int(yp / 100.0 * h), _check_half(hwnd)
            if detect.is_checked(shot(tag + "_pre", hwnd), px, py, half=half):
                say("  чекбокс уже отмечен — пропуск")
            else:
                click_pct(hwnd, xp, yp)
                time.sleep(0.6)
                w2, h2 = _cur_size(hwnd)
                px2, py2 = int(xp / 100.0 * w2), int(yp / 100.0 * h2)
                ok = detect.is_checked(shot(tag + "_post", hwnd), px2, py2, half=_check_half(hwnd))
                say("  чекбокс " + ("отмечен" if ok else "НЕ отметился"))
        elif act == "type":
            _force_focus(hwnd)
            pydirectinput.typewrite(_resolve_template(step.get("text", "")), interval=0.04)
        elif act == "key":
            _raw_key(step.get("key"))
        time.sleep(step.get("wait", 1.5))
        shot(tag, hwnd)
    return True


def do_seq(name="login"):
    """Приводит игру в состояние 'ingame'. Устойчива к точке входа: menu / loading / login / ingame."""
    flow = CFG.get("login_flow")
    if name != "login" or not flow:
        say("login_flow не задан в config.json")
        return False

    hwnd = _unity_hwnd()
    if not hwnd:
        say("do_seq: окно игры не найдено")
        return False
    _force_focus(hwnd)
    _restore(hwnd)  # окно -> 0,0,W,H, координаты рассчитаны от этого
    _force_focus(hwnd)

    st = detect.game_state(shot("seq00_start", hwnd))
    say("do_seq: старт, состояние = %s" % st)

    if st == "unknown":
        st = _wait_state(hwnd, ("menu", "login", "loading", "ingame"),
                         flow.get("await_menu_timeout", 60), "seq00_wait")
        say("do_seq: после ожидания = %s" % st)

    if st == "ingame":
        say("do_seq: итог = ingame -> OK (уже в игре)")
        return True  # after_ingame НЕ трогаем — вход уже был раньше

    if st == "menu":
        _run_menu_steps(hwnd, flow.get("menu_steps", []))
        st = "loading"

    if st in ("loading", "menu"):
        st = _wait_state(hwnd, ("login", "ingame"), flow.get("await_login_timeout", 120), "seq_awaitlogin")
        say("do_seq: ожидание логина -> %s" % st)

    if st == "login":
        acc_steps = flow.get("account_steps")
        if acc_steps and CFG.get("active_account"):
            say("do_seq: account_steps (аккаунт '%s')" % CFG.get("active_account"))
            _run_menu_steps(hwnd, acc_steps)
        okxp, okyp = _step_xy(flow["ok"])
        deadline = time.time() + flow.get("await_ingame_timeout", 120)
        attempt = 0
        while time.time() < deadline:
            attempt += 1
            say("do_seq: жму OK (попытка %d)" % attempt)
            click_pct(hwnd, okxp, okyp)
            time.sleep(6)
            st = detect.game_state(shot("seq_afterok", hwnd))
            say("do_seq: после OK -> %s" % st)
            if st == "ingame":
                break
            if st not in ("login",):  # ушли на loading/меню — подождём разрешения
                st = _wait_state(hwnd, ("ingame", "login"), 25, "seq_afterok_wait")
                if st == "ingame":
                    break
            time.sleep(4)

    fin = detect.game_state(shot("seq99_final", hwnd))
    success = fin == "ingame"
    say("do_seq: итог = %s -> %s" % (fin, "OK" if success else "НЕ довершена"))
    if success:
        _run_after_ingame(hwnd, flow)
    return success


def _run_after_ingame(hwnd, flow):
    """Шаги после входа в игру (напр. открыть чат), из login_flow.after_ingame."""
    steps = flow.get("after_ingame") or []
    if not steps:
        return
    say("after_ingame: %d шаг(ов)" % len(steps))
    for i, step in enumerate(steps, 1):
        act = step.get("action", "click")
        tag = "post%02d_%s" % (i, step.get("tag", act))
        say("  after_ingame шаг %d: %s" % (i, step))
        _force_focus(hwnd)
        if act == "click":
            xp, yp = _step_xy(step)
            click_pct(hwnd, xp, yp, step.get("button", "left"), step.get("dbl"))
        elif act == "type":
            pydirectinput.typewrite(_resolve_template(step.get("text", "")), interval=0.04)
        elif act == "key":
            _raw_key(step["key"])
        time.sleep(step.get("wait", 1.0))
        shot(tag, hwnd)


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        return
    cmd = sys.argv[1]
    a = sys.argv[2:]
    if cmd == "rect":
        for h in [screenshot.find_game_window(_game_pids())] or []:
            pass
        pids = _game_pids()
        print("game pids:", pids)
        for pid in pids:
            for row in screenshot.enum_windows_for_pid(pid):
                print("  win pid=%d:" % pid, row)
        h = screenshot.find_game_window(pids)
        print("chosen hwnd:", h)
        if h:
            l, t, r, b = _rect(h)
            cl = win32gui.GetClientRect(h)
            cs = win32gui.ClientToScreen(h, (0, 0))
            print("window_rect=(%d,%d,%d,%d) %dx%d" % (l, t, r, b, r - l, b - t))
            print("client_size=%s  client_origin_screen=%s" % (cl, cs))
    elif cmd == "restore":
        h = _unity_hwnd()
        if not h:
            print("окно Unity не найдено")
            return
        _restore(h)
        try:
            win32gui.SetForegroundWindow(h)
        except Exception as e:  # noqa: BLE001
            print("focus warn:", e)
        time.sleep(0.5)
        shot("restored", h)
    elif cmd == "shot":
        shot(a[0] if a else "shot")
    elif cmd == "state":
        current_state()
    elif cmd == "click":
        rx, ry = a[0], a[1]
        rest = a[2:]
        button = "left"
        dbl = "--dbl" in rest
        for x in rest:
            if x in ("left", "right"):
                button = x
        tag = next((x for x in rest if x not in ("left", "right", "--dbl")), "afterclick")
        do_click(rx, ry, button, dbl, tag)
    elif cmd == "clickp":
        xp, yp = a[0], a[1]
        rest = a[2:]
        button = "left"
        dbl = "--dbl" in rest
        for x in rest:
            if x in ("left", "right"):
                button = x
        tag = next((x for x in rest if x not in ("left", "right", "--dbl")), "afterclick")
        do_click_pct(xp, yp, button, dbl, tag)
    elif cmd == "move":
        do_move(a[0], a[1], a[2] if len(a) > 2 else "aftermove")
    elif cmd == "key":
        do_key(a[0], a[1] if len(a) > 1 else "afterkey")
    elif cmd == "seq":
        do_seq(a[0] if a else "login")
    else:
        print("неизвестная команда:", cmd)


if __name__ == "__main__":
    main()
