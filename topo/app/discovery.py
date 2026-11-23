"""Scheduled discovery.

Runs the existing discovery paths (confetti-traffic import, subnet sweep, YAML
seed file) on a timer, so a new VM or a new node shows up without anyone
pressing a button. Off by default: TOPO_DISCOVERY_INTERVAL_S=0, and a source
that is not configured is skipped. Everything still goes through
identity.ingest; this module only decides when.

A device that did not exist before a run gets a `device_discovered` event.
"""

import threading
import time

from . import config
from . import db
from . import events
from .collectors import confetti
from .collectors import seedfile
from .collectors import sweep

_thread = None
_start_lock = threading.Lock()
_status = {"last_run": None, "last_error": None, "last_summary": None}


def status():
    out = dict(_status)
    out["enabled"] = config.DISCOVERY_INTERVAL_S > 0
    return out


def _known_ids(conn):
    return {r["id"] for r in conn.execute("SELECT id FROM devices")}


def _announce_new(conn, before, device_ids, source):
    for did in set(device_ids) - before:
        row = conn.execute("SELECT hostname, mgmt_ip FROM devices WHERE id = ?", (did,)).fetchone()
        if row:
            events.emit(conn, did, "device_discovered", "info",
                        "{} ({}): found by {}".format(row["hostname"], row["mgmt_ip"] or "no ip", source))


def run_once():
    """Run every configured source once. Each source gets its own transaction,
    so one failing source cannot discard another's work. Returns a summary."""
    summary = {}
    sources = []
    if config.DISCOVERY_CONFETTI_URL:
        sources.append(("confetti", lambda c: confetti.discover(c, config.DISCOVERY_CONFETTI_URL)))
    for cidr in config.DISCOVERY_SWEEP_CIDRS:
        sources.append(("sweep " + cidr, lambda c, cidr=cidr: sweep.sweep(c, cidr)[1:]))
    if config.DISCOVERY_SEEDFILE:
        sources.append(("seedfile", lambda c: seedfile.load(c, config.DISCOVERY_SEEDFILE)))

    errors = []
    for name, fn in sources:
        conn = db.connect()
        try:
            before = _known_ids(conn)
            device_ids, conflicts = fn(conn)
            _announce_new(conn, before, device_ids, name)
            conn.commit()
            summary[name] = {"devices": len(device_ids), "new": len(set(device_ids) - before),
                             "conflicts": len(conflicts)}
        except Exception as exc:
            conn.rollback()
            errors.append("{}: {}".format(name, exc))
            summary[name] = {"error": str(exc)}
        finally:
            conn.close()
    _status["last_run"] = db.iso(db.sqlite_now())
    _status["last_error"] = "; ".join(errors) or None
    _status["last_summary"] = summary
    return summary


_traffic_status = {"last_run": None, "last_error": None, "devices": 0, "new": 0, "conflicts": 0}
TRAFFIC_INTERVAL_KEY = "traffic_import_interval_min"


def traffic_status():
    return dict(_traffic_status)


def traffic_interval_min(conn):
    """Minutes between automatic imports from the saved Confetti Traffic URL; 0 = off."""
    v = db.get_setting(conn, TRAFFIC_INTERVAL_KEY)
    return int(v) if v and v.isdigit() else 0


def import_traffic(conn):
    """Import the node fleet of the hub saved on the Settings page (settings key url_traffic).
    Raises ValueError when no URL is saved. Does not commit. Returns (device_ids, conflicts)."""
    url = db.get_setting(conn, "url_traffic")
    if not url:
        raise ValueError("no Confetti Traffic URL is saved on the Settings page")
    before = _known_ids(conn)
    device_ids, conflicts = confetti.discover(conn, url)
    _announce_new(conn, before, device_ids, "confetti")
    _traffic_status.update(devices=len(device_ids), new=len(set(device_ids) - before), conflicts=len(conflicts),
                           last_run=db.iso(db.sqlite_now()), last_error=None)
    return device_ids, conflicts


def _traffic_tick():
    """The timer's import: own connection and transaction, status kept for the Settings page."""
    conn = db.connect()
    try:
        env_same = config.DISCOVERY_CONFETTI_URL and config.DISCOVERY_CONFETTI_URL.rstrip("/") == (db.get_setting(conn, "url_traffic") or "").rstrip("/")
        if env_same:
            return   # the env source already imports this hub
        import_traffic(conn)
        conn.commit()
        _traffic_status["last_error"] = None
    except Exception as exc:
        conn.rollback()
        _traffic_status["last_error"] = str(exc)
    finally:
        _traffic_status["last_run"] = db.iso(db.sqlite_now())
        conn.close()


def _loop():
    time.sleep(20)   # let the server finish starting before the first run
    last_env = 0.0
    last_traffic = 0.0
    while True:
        now = time.time()
        try:
            if config.DISCOVERY_INTERVAL_S > 0 and now - last_env >= max(60, config.DISCOVERY_INTERVAL_S):
                last_env = now
                run_once()
            conn = db.connect()
            try:
                minutes = traffic_interval_min(conn)
                has_url = bool(db.get_setting(conn, "url_traffic"))
            finally:
                conn.close()
            if minutes > 0 and has_url and now - last_traffic >= minutes * 60:
                last_traffic = now
                _traffic_tick()
        except Exception as exc:
            _status["last_error"] = str(exc)
        time.sleep(60)


def start():
    """Idempotent; does nothing unless an env interval or the saved import interval is set (the Settings PUT calls this again)."""
    global _thread
    if config.DISCOVERY_INTERVAL_S <= 0:
        conn = db.connect()
        try:
            if traffic_interval_min(conn) <= 0:
                return
        finally:
            conn.close()
    with _start_lock:
        if _thread is not None and _thread.is_alive():
            return
        _thread = threading.Thread(target=_loop, name="discovery", daemon=True)
        _thread.start()
