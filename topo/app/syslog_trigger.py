"""A syslog message that says the topology changed asks for an early poll of that device.

Polls come every few minutes; a link, BGP or OSPF change shows up in syslog at once. When a
stored message (received here or pulled from another server) matches a short list of
mnemonics, the device that sent it gets devices.poll_requested_at set. poller._tick then claims
it through the normal 'idle' check. This module never starts a poll itself and never touches
poll_state, next_poll_at or the stored topology: the poll writes the changes and the events as
always, so a spoofed UDP packet costs at most one extra poll (constraint 25).
"""

import re
import threading
import time

from . import config
from . import db

# (facility, mnemonic) of the Cisco %FAC-SEV-MNEMONIC messages that mean a link or a session
# changed. The severity digit is ignored. Extend this list from messages seen on real routers.
TOPOLOGY_MNEMONICS = {
    ("LINEPROTO", "UPDOWN"),
    ("LINK", "UPDOWN"),
    ("BGP", "ADJCHANGE"),
    ("BGP", "NOTIFICATION"),
    ("OSPF", "ADJCHG"),
}
_MNEMONIC_RE = re.compile(r"^%([A-Z0-9_]+)-\d-([A-Z0-9_]+)$")

_lock = threading.Lock()
_last_request = {}          # device_id -> time.monotonic() of the last request
stats = {"triggers": 0}


def is_topology_message(mnemonic):
    m = _MNEMONIC_RE.match(mnemonic or "")
    return bool(m) and (m.group(1), m.group(2)) in TOPOLOGY_MNEMONICS


def device_for(conn, source_ip):
    """The device that owns source_ip: a management-IP alias, else an interface address held
    by exactly one device. Never stored as an alias (constraint 12)."""
    row = conn.execute(
        "SELECT device_id FROM device_aliases WHERE kind = 'mgmt_ip' AND value = ?", (source_ip,)).fetchone()
    if row:
        return row[0]
    owners = conn.execute(
        "SELECT DISTINCT device_id FROM interfaces WHERE ip = ?", (source_ip,)).fetchall()
    return owners[0][0] if len(owners) == 1 else None


def maybe_request(conn, rec, min_s=None):
    """Ask for an early poll of the device behind rec, if the message says the topology changed.

    Returns the device id asked for, or None. Only a device that is enabled, is not a role='node'
    device and is not failing (fail_count = 0) is asked: backoff is never bypassed (constraint 14's
    spirit, and a device that cannot be reached would just fail again). At most one request per
    device per min_s seconds. Commits its own change.
    """
    if not is_topology_message(rec.get("mnemonic")):
        return None
    device_id = device_for(conn, rec["source_ip"])
    if device_id is None:
        return None
    min_s = config.SYSLOG_TRIGGER_MIN_S if min_s is None else min_s
    with _lock:
        now = time.monotonic()
        if now - _last_request.get(device_id, -min_s - 1) < min_s:
            return None
        cur = conn.execute(
            "UPDATE devices SET poll_requested_at = ? "
            "WHERE id = ? AND enabled = 1 AND role != 'node' AND fail_count = 0 AND poll_requested_at = ''",
            (db.sqlite_now(), device_id))
        conn.commit()
        if cur.rowcount != 1:
            return None
        _last_request[device_id] = now
        stats["triggers"] += 1
    return device_id
