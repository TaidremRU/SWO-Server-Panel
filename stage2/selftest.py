# -*- coding: utf-8 -*-
"""Быстрая проверка модулей без запуска бесконечного цикла."""
import json
import os
import sys

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

import common
import gamectl
import i18n
import mapdt  # noqa: F401
import players
import serverlist
import serverlist_steam  # noqa: F401  (проверка, что модуль импортируется)
import sysinfo
import webui
from bot import Bot
from watchdog import Watchdog  # noqa: F401

cfg = common.load_config()
print("config OK:", cfg["game_name"], "appid", cfg["game_appid"])

_ru, _en = set(i18n.L["ru"]), set(i18n.L["en"])
assert _ru == _en, "i18n: расхождение ключей ru/en: %s" % (_ru ^ _en)
print("i18n OK:", len(_ru), "ключей x", len(i18n.SUPPORTED), "языка")

tg = cfg["telegram"]
print("роли: админов %d, модераторов %d, super_admin=%s, default_lang=%s"
      % (len(tg.get("allowed_user_ids", [])),
         len(tg.get("moderator_user_ids", [])),
         tg.get("super_admin_id"), tg.get("default_lang", "ru")))

# --- веб-панель: хранилище пароля + применение ролей на лету ---
assert hasattr(Bot, "apply_roles"), "bot: нет метода apply_roles"
assert hasattr(common, "save_config"), "common: нет save_config"
_wa_path = os.path.join(os.path.dirname(__file__), "webui_auth_selftest.json")
try:
    os.remove(_wa_path)
except OSError:
    pass
_wa = webui.AuthStore(_wa_path)
assert _wa.verify("admin", "admin") and _wa.must_change, "webui: дефолт admin/admin не создан"
assert not _wa.verify("admin", "wrong"), "webui: verify пропускает неверный пароль"
_wa.set_password("s3cret-pass")
assert _wa.verify("admin", "s3cret-pass") and not _wa.must_change, "webui: смена пароля не сработала"
os.remove(_wa_path)
_wcfg = cfg.get("webui", {}) or {}
print("webui OK: auth admin/admin+must_change, PBKDF2, host=%s port=%s enabled=%s"
      % (_wcfg.get("host", "0.0.0.0"), _wcfg.get("port", 8080), _wcfg.get("enabled", True)))

# --- вкладка «Игроки»: чтение файлов локального сервера ---
try:
    _psnap = players.snapshot(cfg)
    if _psnap.get("ok"):
        _pt = _psnap["totals"]
        # пароли не должны утечь ни в users, ни в recent
        _blob = str(_psnap["users"]) + str(_psnap["recent"])
        assert "code" not in _blob.lower() or "'code'" not in _blob, "players: пароль в выдаче!"
        print("players OK: world=%r registered=%s online(analytics)=%s online(game_state)=%s recent=%d"
              % (_psnap["world"], _pt["registered"], _pt["online_analytics"],
                 _pt["online_game_state"], len(_psnap["recent"])))
        # карточка игрока (слои 1–4): собирается и НЕ содержит пароль code
        _uid = next((u["id"] for u in _psnap["users"] if u.get("name")), None)
        if _uid is not None:
            _det = players.player_detail(cfg, _uid)
            assert _det.get("ok"), "player_detail: %s" % _det.get("error")
            assert '"code"' not in json.dumps(_det, ensure_ascii=False), "player_detail: пароль в выдаче!"
            for _fn in ("player_chat", "player_sensitive", "player_code", "load_items",
                        "load_abilities", "load_clans", "load_friends", "server_time",
                        "item_catalog", "give_stash_items", "take_items", "_is_offline",
                        "player_set_ban", "player_set_role", "player_set_position",
                        "player_add_tech", "player_set_stat", "player_reset_code",
                        "server_chat", "server_events", "server_private_chat",
                        "stats_bundle", "world_map", "server_health", "twink_report",
                        "players_csv", "make_world_backup", "tech_track_scan", "tech_track_read",
                        "player_item_search", "tech_meta", "tech_label", "mapdt_find",
                        "buff_notepad_save", "buff_notepad_read"):
                assert hasattr(players, _fn), "players: нет %s" % _fn
            _pf = players.player_item_search(cfg, "tech_booster")
            assert _pf.get("ok"), "player_item_search: %s" % _pf.get("error")
            assert '"code"' not in json.dumps(_pf, ensure_ascii=False), "player_item_search: пароль в выдаче"
            _tm = players.tech_meta(players.find_world_dir(cfg))
            assert _tm and all("label" in v for v in _tm.values()), "tech_meta пуст/без label"
            assert _tm.get("e6", {}).get("name"), "tech_meta: нет имён из craft.json (e6)"
            _cat = players.item_catalog(cfg)
            assert _cat.get("ok") and _cat["items"], "item_catalog пуст"
            for _bf, _lbl in ((players.server_chat(cfg, 20), "server_chat"),
                              (players.server_events(cfg, 20), "server_events"),
                              (players.stats_bundle(cfg), "stats_bundle"),
                              (players.world_map(cfg), "world_map"),
                              (players.server_health(cfg), "server_health")):
                assert _bf.get("ok"), "%s: %s" % (_lbl, _bf.get("error"))
            _csv, _ = players.players_csv(cfg)
            assert _csv and b'"Code"' not in _csv and b"code" not in _csv.split(b"\n", 1)[0], \
                "players_csv: пароль в выдаче"
            _tw = players.twink_report(cfg, 2)
            assert _tw.get("ok") and "code_groups" in _tw, "twink_report: нет code_groups"
            assert '"code"' not in json.dumps(_tw, ensure_ascii=False), "twink_report: код в выдаче"
            # бинарный парсер карт: индекс + разбор самой маленькой карты
            _mix = players.mapdt_index(cfg)
            assert _mix.get("ok"), "mapdt_index: %s" % _mix.get("error")
            assert hasattr(players, "mapdt_find") and hasattr(mapdt, "find_item"), \
                "нет mapdt_find/find_item"
            if _mix["maps"]:
                _sm = _mix["maps"][-1]["map"]  # список отсортирован по убыванию размера
                _md = players.mapdt_summary(cfg, _sm)
                assert _md.get("ok"), "mapdt map%s: %s" % (_sm, _md.get("error"))
                assert _md["trailing_bytes"] == 0, "mapdt: %d лишних байт" % _md["trailing_bytes"]
                # поиск предмета по одной (маленькой) карте: не должен падать
                _mf = players.mapdt_find(cfg, _sm, "tech_booster")
                assert _mf.get("ok"), "mapdt_find: %s" % _mf.get("error")
                assert '"code"' not in json.dumps(_mf, ensure_ascii=False)
                print("mapdt OK: %d карт, map%s %dx%d blocks=%d machines=%d trailing=0; "
                      "find(tech_booster)@map%s: %s шт в %s точках"
                      % (len(_mix["maps"]), _sm, _md["w"], _md["h"],
                         _md["blocks_total"], _md["machines_total"],
                         _sm, _mf["total_count"], _mf["spots"]))
            print("server-wide OK: chat=%d events=%d online_now=%s clans=%d health_lag=%d csv=%dB"
                  % (players.server_chat(cfg)["total"], players.server_events(cfg)["total"],
                     players.stats_bundle(cfg)["online"]["now"],
                     players.stats_bundle(cfg)["totals"]["clans"],
                     players.server_health(cfg)["lag"]["total"], len(_csv)))
            _ch = players.player_chat(cfg, _uid, 5)
            print("player_detail OK: #%s %r sessions=%s techs=%s friends=%s chat=%s items(ref)=%d"
                  % (_uid, _det["name"], _det["sessions"]["total"],
                     _det["research"]["done_count"], len(_det["friends"]),
                     _ch.get("count"), len(players.load_items(players.find_world_dir(cfg)))))
    else:
        print("players: каталог мира не найден — %s (root=%s)"
              % (_psnap.get("error"), _psnap.get("root")))
except Exception as _e:  # noqa: BLE001
    print("players: ОШИБКА", _e)

_mon = cfg.get("monitor", {}) or {}
print("монитор сервера: enabled=%s name=%r interval=%ss misses=%s repeat=%ss"
      % (_mon.get("enabled", True), _mon.get("server_name", "AstralSigma"),
         _mon.get("interval_seconds", 300), _mon.get("misses_before_alert", 2),
         _mon.get("repeat_alert_seconds", 3600)))

print("steam_running:", gamectl.steam_running())
print("game_running :", gamectl.game_running(cfg))

try:
    _sok, _sres, _ssrc = serverlist.fetch(cfg)
    print("serverlist   : ok=%s source=%s -> %s"
          % (_sok, _ssrc, (("%d серв." % len(_sres)) if _sok else _sres)))
except Exception as _e:  # noqa: BLE001
    print("serverlist   : ОШИБКА", _e)

snap = sysinfo.collect(cfg)
print("snapshot keys:", sorted(snap.keys()))
print("cpu%%=%s mem%%=%s disk_free=%s rdp=%s console_active=%s"
      % (snap["cpu_percent"], snap["mem"]["percent"], snap["disk_c"]["free"],
         snap["rdp_connected"], snap["console_active"]))
print("steam:", snap["steam"])
print("game :", snap["game"])
print("sessions:", snap["sessions"])

b = Bot(cfg, common.State("state.json"))
print("--- status text (ru / en) ---")
print(b._status_text(snap, "ru"))
print("- - -")
print(b._status_text(snap, "en"))
print("--- меню (admin / moderator) ---")
for _role in ("admin", "moderator"):
    _lbl = [x["text"] for row in b._menu("ru", _role)["inline_keyboard"] for x in row]
    print(" ", _role, "→", " | ".join(_lbl))

print("--- telegram getUpdates via proxy ---")
r = b.tg.get_updates(0, 0)
print("ok=%s err=%s n=%s" % (r.get("ok"), r.get("error"), len(r.get("result", []) or [])))
sys.exit(0 if r.get("ok") else 1)
