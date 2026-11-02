"""Topology history: a snapshot of the whole map, kept when its shape changes.

A snapshot is what GET /api/topology answers (all layers), so it holds no secrets that the page does not already show.
It is written only when the set of nodes and links (or a link's up/down) differs from the newest snapshot, so a quiet
network adds nothing. Old snapshots are pruned by age. The poller tick calls maybe_snapshot().
"""
import hashlib
import json
import time

from . import config
from . import db
from . import topology

_last_check = 0.0


def _shape(data):
    """The part of the map that counts as a change: which nodes, which links, which are up."""
    nodes = sorted((n["id"], bool(n.get("reachable", True))) for n in data["nodes"])
    edges = sorted((e["from"], e["to"], e["layer"], e.get("label", ""), e.get("up") is not False) for e in data["edges"])
    return hashlib.sha256(json.dumps([nodes, edges]).encode()).hexdigest()


def maybe_snapshot(conn, force=False):
    """Store a snapshot if the map changed since the newest one. Returns the new id or None. Does not commit."""
    global _last_check
    if config.SNAPSHOT_INTERVAL_S <= 0 and not force:
        return None
    if not force and time.time() - _last_check < config.SNAPSHOT_INTERVAL_S:
        return None
    _last_check = time.time()
    data = topology.build(conn, None)
    if not data["nodes"]:
        return None
    digest = _shape(data)
    last = conn.execute("SELECT shape FROM topology_snapshots ORDER BY id DESC LIMIT 1").fetchone()
    if last is not None and last["shape"] == digest:
        return None
    cur = conn.execute(
        "INSERT INTO topology_snapshots (taken_at, shape, data) VALUES (?, ?, ?)",
        (db.sqlite_now(), digest, json.dumps(data)))
    return cur.lastrowid


def prune(conn):
    conn.execute("DELETE FROM topology_snapshots WHERE taken_at < datetime('now', ? || ' days')",
                 ("-{:d}".format(config.SNAPSHOT_RETENTION_DAYS),))
