"""Server-initiated device poller.

The inverse of confetti-traffic's node-initiated push: confetti-topo dials out to
devices over SSH rather than waiting for them to report in. One daemon
thread, started by serve.py beside reach.start() with the same
idempotent shape — start() returns quietly if already running, and a bug
inside one tick must never kill the poller permanently, matching
confetti-traffic's rule that a missing background service must never take down
the piece that matters (the dashboard/API staying responsive).
"""

import threading
import time
from concurrent.futures import ThreadPoolExecutor

from . import config
from . import db
from . import events
from . import flap
from . import history
from . import identity
from . import platforms
from . import pollerr
from .collectors import hub as hub_collector
from .collectors import panos as panos_collector
from .collectors import ssh as ssh_collector

_thread = None
_running = threading.Event()
_start_lock = threading.Lock()


def is_running():
    return _running.is_set()


def start():
    """Start the poller thread. Idempotent — a second call is a no-op."""
    global _thread
    with _start_lock:
        if _thread is not None and _thread.is_alive():
            return
        _thread = threading.Thread(target=_loop, name="poller", daemon=True)
        _thread.start()


def _reset_stuck():
    """A device left 'running' by a server that stopped mid-poll would never
    be claimed again — the claim query only takes 'idle'. Nothing can be
    polling at startup, so any 'running' row here is stale."""
    conn = db.connect()
    try:
        conn.execute("UPDATE devices SET poll_state = 'idle' WHERE poll_state = 'running'")
        conn.commit()
    finally:
        conn.close()


def _loop():
    _running.set()
    try:
        _reset_stuck()
    except Exception:
        pass
    executor = ThreadPoolExecutor(max_workers=config.POLL_WORKERS)
    try:
        while True:
            try:
                _tick(executor)
            except Exception:
                # A bug in one tick's scheduling must not end the poller —
                # the next tick gets another chance.
                pass
            time.sleep(config.POLL_TICK_S)
    finally:
        _running.clear()


def _tick(executor):
    """Claim due devices (mark them 'running' so a slow poll can't be
    picked up twice — confetti-traffic constraint 6's overlapping-cycle lock,
    moved server-side) and hand each to the thread pool."""
    conn = db.connect()
    try:
        now = db.sqlite_now()
        rows = conn.execute(
            """SELECT id FROM devices
               WHERE enabled = 1 AND poll_state = 'idle' AND (role != 'node' OR platform = 'confetti_node')
                 AND (next_poll_at = '' OR next_poll_at <= ?
                      OR (poll_requested_at != '' AND fail_count = 0))
               LIMIT ?""",
            (now, config.POLL_WORKERS * 2),
        ).fetchall()
        device_ids = [r["id"] for r in rows]
        if device_ids:
            # A syslog request (syslog_trigger) is answered by this claim; one that arrives while
            # the poll runs stays set and is claimed on the tick after _finish.
            conn.executemany(
                "UPDATE devices SET poll_state = 'running', poll_requested_at = '' WHERE id = ?",
                [(d,) for d in device_ids],
            )
        conn.execute(
            "DELETE FROM poll_history WHERE received_at < datetime('now', ? || ' hours')",
            ("-{:d}".format(config.POLL_HISTORY_RETENTION_HOURS),),
        )
        conn.execute(
            "DELETE FROM events WHERE at < datetime('now', ? || ' days')",
            ("-{:d}".format(config.EVENT_RETENTION_DAYS),),
        )
        if config.SYSLOG_RETENTION_DAYS > 0:
            conn.execute(
                "DELETE FROM syslog WHERE received_at < datetime('now', ? || ' days')",
                ("-{:d}".format(config.SYSLOG_RETENTION_DAYS),),
            )
        conn.execute(
            "DELETE FROM interface_stats WHERE at < datetime('now', ? || ' days')",
            ("-{:d}".format(config.STATS_RETENTION_DAYS),),
        )
        try:
            flap.prune(conn)
            history.maybe_snapshot(conn)
            history.prune(conn)
        except Exception:
            pass   # history is an extra; a failure here must never stop polling
        conn.commit()
    finally:
        conn.close()

    for device_id in device_ids:
        executor.submit(_poll_device, device_id)


def poll_now(device_id):
    """Synchronous single-device poll for POST /api/devices/<id>/poll.

    Runs inline on the request thread rather than through the pool — an
    operator clicking "poll now" wants to see the result, not a 202. Does
    not touch poll_state/next_poll_at scheduling beyond what _finish always
    does, so it composes safely with the background loop picking the same
    device up later.
    """
    return _poll_device(device_id)


def _poll_device(device_id):
    conn = db.connect()
    any_ok = False
    errors = {}
    try:
        device = conn.execute("SELECT * FROM devices WHERE id = ?", (device_id,)).fetchone()
        if device is None:
            return None
        creds = ssh_collector.resolve_credentials(conn, device_id)
        # The platform says how to ask the device; what comes back is applied the same way for all.
        transport = platforms.get(device["platform"])["transport"]
        collector = panos_collector if transport == "xml_api" else hub_collector if transport == "hub_api" else ssh_collector

        results = {}
        started = db.sqlite_now()
        for task, ok, output, error, parsed in collector.run_tasks(dict(device), creds):
            if ok:
                try:
                    ssh_collector.apply_result(conn, device_id, task, parsed)
                    conn.commit()
                    any_ok = True
                except identity.Conflict as exc:
                    conn.rollback()
                    ok = False
                    error = "identity conflict: {}".format(exc)
                except Exception as exc:
                    conn.rollback()
                    ok = False
                    error = "apply failed: {}: {}".format(type(exc).__name__, exc)

            received = db.sqlite_now()
            conn.execute(
                """INSERT INTO poll_history
                       (device_id, transport, task, ok, error, output, started_at, received_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (device_id, transport, task, 1 if ok else 0, error, (output or "")[:20000], started, received),
            )
            conn.commit()
            results[task] = {"ok": ok, "error": error}
            if not ok:
                errors[task] = error or "failed"
            started = received
        return results
    finally:
        # Always release the per-device lock, even if something above raised
        # — otherwise the device stays 'running' and is never claimed again.
        try:
            conn.rollback()
            _finish(conn, device_id, any_ok, errors)
        finally:
            conn.close()


def _reason(errors):
    """One text for a failed poll from {task: error}: the shared text once, or the first with a count of the others."""
    if not errors:
        return "no result from the device"
    texts = list(errors.values())
    first = texts[0]
    if all(t == first for t in texts):
        return first
    return "{} (and {} other task error(s))".format(first, len(set(texts)) - 1)


def _finish(conn, device_id, any_ok, errors=None):
    now = db.sqlite_now()
    row = conn.execute(
        "SELECT poll_interval_s, fail_count FROM devices WHERE id = ?", (device_id,)
    ).fetchone()
    interval = row["poll_interval_s"] if row else config.POLL_DEFAULT_INTERVAL_S

    # Only the transitions: one event when a device stops answering, one when
    # it comes back — not one per failed poll.
    if row:
        name = conn.execute("SELECT hostname FROM devices WHERE id = ?", (device_id,)).fetchone()["hostname"]
        if not any_ok and row["fail_count"] == 0:
            reason = _reason(errors)
            events.emit(conn, device_id, "device_unreachable", "warning",
                        "{}: polling failed: {}".format(name, pollerr.short(reason)), reason)
        elif any_ok and row["fail_count"] > 0:
            events.emit(conn, device_id, "device_recovered", "info",
                        "{}: polling recovered".format(name),
                        "after {} failed poll(s)".format(row["fail_count"]))

    if any_ok:
        conn.execute(
            """UPDATE devices SET poll_state = 'idle', fail_count = 0, last_poll_error = '',
                   last_poll_ok = ?, next_poll_at = datetime(?, ? || ' seconds')
               WHERE id = ?""",
            (now, now, interval, device_id),
        )
    else:
        fail_count = (row["fail_count"] if row else 0) + 1
        backoff = min(interval * (2 ** fail_count), config.POLL_MAX_BACKOFF_S)
        conn.execute(
            """UPDATE devices SET poll_state = 'idle', fail_count = ?, last_poll_error = ?,
                   next_poll_at = datetime(?, ? || ' seconds')
               WHERE id = ?""",
            (fail_count, _reason(errors) if errors else "no result from the device", now, backoff, device_id),
        )
    conn.commit()
