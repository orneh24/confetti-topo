"""Database helpers shared by app.py, poller.py, reach.py and identity.py.

Split out of app.py because several non-Flask modules (the poller thread,
the ICMP checker, identity resolution) need a connection without importing
Flask machinery.
"""

import sqlite3
from datetime import datetime, timezone

from flask import g

from . import config


def get_db():
    """Get a database connection for the current Flask request."""
    if "db" not in g:
        g.db = connect()
    return g.db


def connect():
    """Open a new connection with the pragmas every writer must set.

    busy_timeout first: journal_mode itself can contend with another writer,
    so setting the timeout after it would leave that one statement
    unprotected. See config.BUSY_TIMEOUT_MS — this file has several writers
    (Flask, the poller's thread pool, the ICMP checker, the discovery timer).
    """
    db = sqlite3.connect(config.DB_PATH)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA busy_timeout={:d}".format(config.BUSY_TIMEOUT_MS))
    db.execute("PRAGMA journal_mode=WAL")
    db.execute("PRAGMA foreign_keys=ON")
    return db


def close_db(exc=None):
    db = g.pop("db", None)
    if db is not None:
        db.close()


def init_db():
    """Create tables if they don't exist. Safe to call on every startup."""
    db = connect()
    db.executescript("""
        CREATE TABLE IF NOT EXISTS devices (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            key             TEXT NOT NULL UNIQUE,
            hostname        TEXT NOT NULL,
            mgmt_ip         TEXT,
            vendor          TEXT NOT NULL DEFAULT 'cisco',
            platform        TEXT NOT NULL DEFAULT 'cisco_ios',
            model           TEXT,
            serial          TEXT,
            os_version      TEXT,
            role            TEXT NOT NULL DEFAULT 'router',
            site            TEXT NOT NULL DEFAULT '',
            enabled         INTEGER NOT NULL DEFAULT 1,
            poll_interval_s INTEGER NOT NULL DEFAULT 300,
            next_poll_at    TEXT NOT NULL DEFAULT '',
            poll_state      TEXT NOT NULL DEFAULT 'idle',
            fail_count      INTEGER NOT NULL DEFAULT 0,
            first_seen      TEXT NOT NULL,
            last_seen       TEXT NOT NULL,
            last_poll_ok    TEXT,
            reachable       INTEGER,
            last_ping_at    TEXT,
            ping_fail       INTEGER NOT NULL DEFAULT 0
        );
        CREATE INDEX IF NOT EXISTS idx_devices_next_poll ON devices(enabled, poll_state, next_poll_at);

        -- Every identifier ever observed for a device, across every ingest
        -- path. This is what lets identity.py recognise "the same box" seen
        -- twice under two different names. See identity.py for the merge rule.
        CREATE TABLE IF NOT EXISTS device_aliases (
            device_id  INTEGER NOT NULL REFERENCES devices(id) ON DELETE CASCADE,
            kind       TEXT NOT NULL,
            value      TEXT NOT NULL,
            first_seen TEXT NOT NULL,
            PRIMARY KEY (kind, value)
        );
        CREATE INDEX IF NOT EXISTS idx_aliases_device ON device_aliases(device_id);

        -- Which ingest path(s) have seen this device. Many rows per device —
        -- a device found by both a subnet sweep and a seed file is one devices
        -- row with two device_sources rows.
        CREATE TABLE IF NOT EXISTS device_sources (
            device_id  INTEGER NOT NULL REFERENCES devices(id) ON DELETE CASCADE,
            source     TEXT NOT NULL,
            ref        TEXT NOT NULL DEFAULT '',
            first_seen TEXT NOT NULL,
            last_seen  TEXT NOT NULL,
            PRIMARY KEY (device_id, source, ref)
        );

        -- Written when an ingest path's identifiers match more than one
        -- existing device. Ambiguity is never auto-resolved — see
        -- identity.py. An operator resolves these from /conflicts.
        CREATE TABLE IF NOT EXISTS merge_conflicts (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            seen_at     TEXT NOT NULL,
            source      TEXT NOT NULL,
            candidate   TEXT NOT NULL,
            device_ids  TEXT NOT NULL,
            detail      TEXT,
            resolved    INTEGER NOT NULL DEFAULT 0
        );

        -- Per-device override of the environment's default credentials.
        -- Plaintext in v1 — topo.db must be 0600.
        CREATE TABLE IF NOT EXISTS credentials (
            device_id      INTEGER PRIMARY KEY REFERENCES devices(id) ON DELETE CASCADE,
            username       TEXT,
            password       TEXT,
            enable_secret  TEXT,
            updated_at     TEXT NOT NULL
        );

        -- Server-wide settings set from the UI (default SSH credentials). Plaintext in v1,
        -- same as credentials. Sits between a device's own row and the TOPO_SSH_* env.
        CREATE TABLE IF NOT EXISTS settings (
            key         TEXT PRIMARY KEY,
            value       TEXT,
            updated_at  TEXT NOT NULL
        );

        -- What each device reports about its own interfaces. The topology
        -- uses the addresses to resolve a BGP/OSPF peer IP to a device.
        CREATE TABLE IF NOT EXISTS interfaces (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            device_id     INTEGER NOT NULL REFERENCES devices(id) ON DELETE CASCADE,
            name          TEXT NOT NULL,
            description   TEXT NOT NULL DEFAULT '',
            ip            TEXT,
            prefix_len    INTEGER,
            network       TEXT,
            vrf           TEXT NOT NULL DEFAULT 'default',
            admin_status  TEXT,
            oper_status   TEXT,
            speed         TEXT,
            duplex        TEXT,
            mtu           INTEGER,
            last_seen     TEXT NOT NULL,
            UNIQUE(device_id, name)
        );
        CREATE INDEX IF NOT EXISTS idx_if_ip ON interfaces(ip);

        CREATE TABLE IF NOT EXISTS lldp_neighbors (
            id               INTEGER PRIMARY KEY AUTOINCREMENT,
            device_id        INTEGER NOT NULL REFERENCES devices(id) ON DELETE CASCADE,
            local_if         TEXT NOT NULL,
            remote_chassis   TEXT,
            remote_sysname   TEXT,
            remote_port      TEXT,
            remote_mgmt_ip   TEXT,
            remote_device_id INTEGER,
            remote_descr     TEXT,
            last_seen        TEXT NOT NULL,
            UNIQUE(device_id, local_if, remote_port)
        );

        CREATE TABLE IF NOT EXISTS adjacencies (
            id             INTEGER PRIMARY KEY AUTOINCREMENT,
            device_id      INTEGER NOT NULL REFERENCES devices(id) ON DELETE CASCADE,
            proto          TEXT NOT NULL,
            peer_ip        TEXT NOT NULL,
            peer_device_id INTEGER,
            remote_as      INTEGER,
            local_as       INTEGER,
            area           TEXT,
            state          TEXT NOT NULL,
            uptime         TEXT,
            prefixes       INTEGER,
            local_if       TEXT,
            last_seen      TEXT NOT NULL,
            UNIQUE(device_id, proto, peer_ip)
        );
        CREATE INDEX IF NOT EXISTS idx_adj_state ON adjacencies(state);

        -- Append-only, age-pruned like confetti-traffic's results table. One row
        -- per poll task (version/interfaces/lldp/bgp/ospf), not per device —
        -- a device's LLDP task failing must not hide that its interfaces task
        -- just succeeded.
        CREATE TABLE IF NOT EXISTS poll_history (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            device_id    INTEGER NOT NULL,
            transport    TEXT NOT NULL,
            task         TEXT NOT NULL,
            ok           INTEGER NOT NULL,
            duration_ms  INTEGER,
            error        TEXT,
            output       TEXT,
            started_at   TEXT NOT NULL,
            received_at  TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_poll_received ON poll_history(received_at);
        CREATE INDEX IF NOT EXISTS idx_poll_device ON poll_history(device_id, task);

        -- State changes seen by the poller (see events.py). Age-pruned.
        CREATE TABLE IF NOT EXISTS events (
            id        INTEGER PRIMARY KEY AUTOINCREMENT,
            at        TEXT NOT NULL,
            device_id INTEGER REFERENCES devices(id) ON DELETE CASCADE,
            kind      TEXT NOT NULL,
            severity  TEXT NOT NULL,
            subject   TEXT NOT NULL,
            detail    TEXT NOT NULL DEFAULT ''
        );
        CREATE INDEX IF NOT EXISTS idx_events_at ON events(at);
        CREATE INDEX IF NOT EXISTS idx_events_device ON events(device_id, at);

        -- Where each node sits on the topology page, shared by every viewer.
        -- node_id is the graph id ('dev:5', 'ghost:lldp:sw1'), not a devices FK.
        CREATE TABLE IF NOT EXISTS node_positions (
            node_id    TEXT PRIMARY KEY,
            x          REAL NOT NULL,
            y          REAL NOT NULL,
            updated_at TEXT NOT NULL
        );
    """)
    # CREATE TABLE IF NOT EXISTS never adds a column to a table that exists already.
    if "remote_descr" not in [r[1] for r in db.execute("PRAGMA table_info(lldp_neighbors)")]:
        db.execute("ALTER TABLE lldp_neighbors ADD COLUMN remote_descr TEXT")
    if "local_as" not in [r[1] for r in db.execute("PRAGMA table_info(adjacencies)")]:
        db.execute("ALTER TABLE adjacencies ADD COLUMN local_as INTEGER")
    if "mgmt_mac" not in [r[1] for r in db.execute("PRAGMA table_info(devices)")]:
        db.execute("ALTER TABLE devices ADD COLUMN mgmt_mac TEXT")
    db.commit()
    db.close()


def sqlite_now():
    """UTC timestamp in SQLite's own comparable format: 'YYYY-MM-DD HH:MM:SS'.

    Everything that leaves a device or another system arrives in ISO-8601 or
    some other shape. Comparing that against datetime('now', ...) is a string
    comparison in which 'T' (0x54) sorts above ' ' (0x20), so any same-day
    row would pass any window. Every table here stamps its own timestamps in
    this format instead, and filters on that. See confetti-traffic CLAUDE.md
    constraints 2 and 18 — this bug bit them twice.
    """
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def iso(sqlite_ts):
    """Render a stored 'YYYY-MM-DD HH:MM:SS' UTC value as ISO-8601 with Z.

    Browsers parse a space-separated timestamp as *local* time, which would
    skew every "how long ago" display by the viewer's UTC offset.
    """
    return sqlite_ts.replace(" ", "T") + "Z" if sqlite_ts else sqlite_ts


def sqlite_ts_arg(value):
    """Normalise an ISO-8601 or SQLite timestamp argument for comparison.

    Accepts either shape a caller might hand in (including our own iso()
    output coming back from a drill-down link) and normalises inward, rather
    than string-comparing mixed formats — the same trap sqlite_now() exists
    to avoid. Returns None for anything that isn't a timestamp, so the caller
    drops the bound rather than comparing against garbage.
    """
    if not value:
        return None
    v = value.strip()
    if v.endswith("Z"):
        v = v[:-1]
    try:
        parsed = datetime.fromisoformat(v.replace(" ", "T"))
    except ValueError:
        return None
    if parsed.tzinfo is not None:
        parsed = parsed.astimezone(timezone.utc).replace(tzinfo=None)
    return parsed.strftime("%Y-%m-%d %H:%M:%S")


def like_arg(value):
    r"""Wrap a substring search for LIKE, escaping its wildcards.

    Without this a search for "100%" or "GigabitEthernet0_1" silently
    matches far more than the caller typed.
    """
    escaped = value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return "%{}%".format(escaped)
