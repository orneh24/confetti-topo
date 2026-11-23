"""Poll a Confetti Traffic node through its hub (platform confetti_node, constraint 39).

A node reports to the hub, not to us: its address, build, clock state, a heartbeat, and the LLDP neighbours its own
lldpd sees (the switch and port it is plugged into). One read of the hub's /endpoints and /api/lldp serves every node,
so the answers are kept for a few seconds and shared by the polls of one tick. Read-only: GET, http(s), no redirect,
no credentials sent, a size cap, every field length-limited text. The hub is the one that imported the node
(device_sources source='confetti', ref = hub URL); nothing new to configure.

Same contract as ssh.run_tasks: a generator of (task, ok, raw_output, error, parsed) for version, interfaces, lldp.
"""
import ipaddress
import json
import threading
import time
from datetime import datetime, timezone

import requests

from .. import db
from .. import pollerr
from . import base_url

TASKS = ("version", "interfaces", "lldp")
TIMEOUT_S = 5
CACHE_S = 20
STALE_S = 15 * 60        # a node registers every 5 minutes; this is three missed rounds
MAX_BYTES = 2 * 1024 * 1024

_lock = threading.Lock()
_cache = {}              # hub url -> (fetched at, endpoints, lldp rows, error text)


class HubError(Exception):
    """The hub answered, but not with what a node poll needs."""


def _text(value, limit=200):
    return "" if value is None else " ".join(str(value).split())[:limit]


def _get(url):
    """GET one JSON list from the hub."""
    r = requests.get(url, timeout=TIMEOUT_S, allow_redirects=False)
    if r.status_code != 200:
        raise HubError("the hub answered HTTP {} for {}".format(r.status_code, url.rsplit("/", 1)[-1]))
    if len(r.content) > MAX_BYTES:
        raise HubError("the hub's answer is too large")
    try:
        data = json.loads(r.content)
    except ValueError:
        raise HubError("the hub's answer is not JSON")
    if not isinstance(data, list):
        raise HubError("the hub's answer is not a list")
    return data


def _fetch(hub):
    """(endpoints, lldp rows) for a hub, from the cache when it is fresh. Raises what the request raised."""
    with _lock:
        now = time.monotonic()
        hit = _cache.get(hub)
        if hit and now - hit[0] < CACHE_S:
            if hit[3]:
                raise HubError(hit[3])
            return hit[1], hit[2]
        try:
            endpoints = _get(hub + "/endpoints")
            lldp = _get(hub + "/api/lldp")
        except Exception as exc:
            text = pollerr.explain(exc, "hub", None, TIMEOUT_S)
            _cache[hub] = (now, [], [], text)
            raise HubError(text)
        _cache[hub] = (now, endpoints, lldp, "")
        return endpoints, lldp


def _hub_of(conn, device_id):
    row = conn.execute("SELECT ref FROM device_sources WHERE device_id = ? AND source = 'confetti' "
                       "ORDER BY last_seen DESC LIMIT 1", (device_id,)).fetchone()
    return base_url(row["ref"]) if row and row["ref"] else None


def _age_s(stamp):
    """Seconds since an ISO time like 2026-11-23T01:02:03Z, or None when it cannot be read."""
    try:
        t = datetime.fromisoformat(str(stamp).replace("Z", "+00:00"))
    except ValueError:
        return None
    if t.tzinfo is None:
        t = t.replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - t).total_seconds()


def _ago(seconds):
    minutes = int(seconds // 60)
    return "{} min".format(minutes) if minutes < 120 else "{} h".format(minutes // 60) if minutes < 2880 else "{} days".format(minutes // 1440)


def find_node(endpoints, device):
    """The /endpoints row for this device: its hostname, else its management IP."""
    host = (device.get("hostname") or "").lower()
    for ep in endpoints:
        if isinstance(ep, dict) and host and _text(ep.get("hostname")).lower() == host:
            return ep
    for ep in endpoints:
        if isinstance(ep, dict) and device.get("mgmt_ip") and _text(ep.get("ip")) == device["mgmt_ip"]:
            return ep
    return None


def parse_version(ep):
    """The node's build and a short status text. Never a serial or a hostname: a poll must not rename a device."""
    info = ["clock " + ("synced" if ep.get("clock_synced") else "not synced" if ep.get("clock_synced") is False else "unknown")]
    if ep.get("clock_offset_s") is not None:
        info[0] += " ({} s off)".format(_text(ep["clock_offset_s"], 12))
    if ep.get("update_state"):
        info.append("update " + _text(ep["update_state"], 30))
    return {"model": "Confetti Traffic node",
            "os_version": "build " + _text(ep.get("build"), 40) if ep.get("build") else None,
            "hub_info": "; ".join(info)}


def parse_interfaces(ep, lldp_rows):
    """One row: the node's reported address and subnet. Named after the port its LLDP neighbour sees."""
    ip, prefix_len, network = _text(ep.get("ip"), 45) or None, None, None
    if ip:
        try:
            iface = ipaddress.ip_interface("{}/{}".format(ip, _text(ep.get("subnet"), 45).split("/")[-1] or "32"))
            prefix_len, network = iface.network.prefixlen, str(iface.network)
        except ValueError:
            pass
    name = next((_text(r.get("local_port"), 40) for r in lldp_rows if r.get("local_port")), "") or "eth0"
    return [{"name": name, "description": "Confetti Traffic node", "ip": ip, "prefix_len": prefix_len,
             "network": network, "admin_status": "up", "oper_status": "up", "speed": None, "duplex": None,
             "mtu": None}]


def parse_lldp(lldp_rows):
    return [{"local_if": _text(r.get("local_port"), 40) or "eth0",
             "remote_chassis": _text(r.get("chassis_id"), 80) or None,
             "remote_sysname": _text(r.get("sys_name"), 80) or None,
             "remote_port": _text(r.get("port_id"), 80) or None,
             "remote_mgmt_ip": _text(r.get("mgmt_ip"), 45) or None,
             "remote_descr": _text(r.get("sys_descr"), 200) or None,
             "remote_port_descr": _text(r.get("port_descr"), 120) or None} for r in lldp_rows]


def run_tasks(device, creds, tasks=None):
    tasks = tasks or TASKS
    conn = db.connect()
    try:
        hub = _hub_of(conn, device["id"])
    finally:
        conn.close()
    if not hub:
        for task in tasks:
            yield task, False, None, "no hub known for this node (it was not imported from a Confetti Traffic hub)", None
        return
    try:
        endpoints, lldp = _fetch(hub)
    except HubError as exc:
        for task in tasks:
            yield task, False, None, str(exc), None
        return

    ep = find_node(endpoints, device)
    if ep is None:
        why = "the node is not listed by the hub any more (it is dropped after 6 hours without registering)"
    else:
        age = _age_s(ep.get("last_seen"))
        why = None
        if age is None or age > STALE_S:
            why = "the node stopped reporting to the hub" + (", last seen {} ago".format(_ago(age)) if age is not None else "")
    if why:
        for task in tasks:
            yield task, False, None, why, None
        return

    host = _text(ep.get("hostname")).lower()
    mine = [r for r in lldp if isinstance(r, dict) and _text(r.get("node")).lower() == host]
    answers = {"version": parse_version, "interfaces": lambda e: parse_interfaces(e, mine), "lldp": lambda e: parse_lldp(mine)}
    for task in tasks:
        parsed = answers[task](ep)
        yield task, True, json.dumps(ep, sort_keys=True)[:2000] if task == "version" else "", None, parsed
