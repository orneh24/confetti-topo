"""confetti-topo — device inventory and topology map (LLDP, BGP, OSPF).

Unlike confetti-traffic's hub/app/app.py, which keeps nearly everything in one
file, a section that outgrows a screenful moves to its own module under app/
(identity.py, topology.py, poller.py, collectors/, parsers/) and app.py keeps
only the @app.route wiring for it.
"""

import os
import shutil
import subprocess

from flask import Flask, jsonify, render_template, request, Response

from . import config
from . import db
from . import discovery
from . import identity
from . import lldp_crawl
from . import metrics
from . import poller
from . import reach
from . import topology
from .collectors import butler, confetti
from .collectors import seedfile
from .collectors import sweep as sweep_collector

app = Flask(
    __name__,
    template_folder=os.path.join(os.path.dirname(__file__), "..", "templates"),
    static_folder=os.path.join(os.path.dirname(__file__), "..", "static"),
)


@app.teardown_appcontext
def _close_db(exc):
    db.close_db(exc)


# ---------------------------------------------------------------------------
# Clock
#
# Ported from confetti-traffic's /api/time. Poll-history windows and event
# timestamps depend on the server's clock being
# disciplined, same reasoning as confetti-traffic's hub.
# ---------------------------------------------------------------------------

CHRONY_TIMEOUT_S = 3
LOCAL_REFID_PREFIX = "7F7F"


def parse_chrony_tracking(text):
    """Parse `chronyc tracking` output. Returns None if unrecognised."""
    fields = {}
    for line in text.splitlines():
        key, sep, value = line.partition(":")
        if sep:
            fields[key.strip().lower()] = value.strip()

    if "stratum" not in fields and "reference id" not in fields:
        return None

    try:
        stratum = int(fields.get("stratum", ""))
    except ValueError:
        stratum = None

    offset = None
    system_time = fields.get("system time", "")
    parts = system_time.split()
    if parts:
        try:
            offset = float(parts[0])
            if "slow" in system_time:
                offset = -offset
        except ValueError:
            offset = None

    refid = fields.get("reference id", "")
    source = None
    if "(" in refid and ")" in refid:
        source = refid[refid.index("(") + 1:refid.rindex(")")].strip() or None
    if not source:
        source = refid.split()[0] if refid.split() else None

    return {
        "stratum": stratum,
        "system_offset_s": offset,
        "synced": fields.get("leap status", "").lower() == "normal",
        "local_only": refid.upper().startswith(LOCAL_REFID_PREFIX),
        "source": source,
    }


@app.route("/api/time", methods=["GET"])
def api_time():
    """Server clock plus chrony tracking state. Never 500s."""
    out = {"utc": db.iso(db.sqlite_now()), "chrony": None}

    if shutil.which("chronyc") is None:
        out["reason"] = "chronyc not installed"
        return jsonify(out)

    try:
        proc = subprocess.run(
            ["chronyc", "-n", "tracking"],
            capture_output=True, text=True, timeout=CHRONY_TIMEOUT_S,
        )
    except subprocess.TimeoutExpired:
        out["reason"] = "chronyc timed out"
        return jsonify(out)
    except OSError as exc:
        out["reason"] = "chronyc failed: {}".format(exc.__class__.__name__)
        return jsonify(out)

    if proc.returncode != 0:
        out["reason"] = (proc.stderr or proc.stdout or "chronyd not responding").strip()[:120]
        return jsonify(out)

    tracking = parse_chrony_tracking(proc.stdout)
    if tracking is None:
        out["reason"] = "unrecognised chronyc output"
        return jsonify(out)

    out["chrony"] = tracking
    return jsonify(out)


# ---------------------------------------------------------------------------
# Self-health
#
# Ported from confetti-traffic's /api/health. Same never-500 discipline: every
# metric and every service check is independently guarded, and this is
# routinely exercised on a non-Alpine dev box where rc-service doesn't exist.
# ---------------------------------------------------------------------------

def _service_status(name):
    """Query one OpenRC service via `rc-service <name> status`."""
    if shutil.which("rc-service") is None:
        return {"status": None, "detail": "rc-service not installed"}
    try:
        proc = subprocess.run(
            ["rc-service", name, "status"],
            capture_output=True, text=True, timeout=config.HEALTH_SERVICE_TIMEOUT_S,
        )
    except subprocess.TimeoutExpired:
        return {"status": None, "detail": "rc-service timed out"}
    except OSError as exc:
        return {"status": None, "detail": "rc-service failed: {}".format(exc.__class__.__name__)}

    out = (proc.stdout or proc.stderr or "").strip()
    if proc.returncode == 0:
        return {"status": "up", "detail": out[:120] or "started"}
    return {"status": "down", "detail": out[:120] or "not running"}


def _load_avg():
    try:
        one, five, fifteen = os.getloadavg()
        return {"1m": one, "5m": five, "15m": fifteen}
    except (OSError, AttributeError):
        return None


def _memory_info():
    try:
        fields = {}
        with open("/proc/meminfo", "r") as fh:
            for line in fh:
                key, _, rest = line.partition(":")
                rest = rest.strip()
                if rest.endswith("kB"):
                    rest = rest[:-2].strip()
                try:
                    fields[key] = int(rest)
                except ValueError:
                    pass
        total = fields.get("MemTotal")
        available = fields.get("MemAvailable")
        if total is None or available is None:
            return None
        used = total - available
        pct = round(used / total * 100, 1) if total else None
        return {"total_kb": total, "available_kb": available, "used_kb": used, "used_pct": pct}
    except OSError:
        return None


def _disk_info():
    try:
        directory = os.path.dirname(os.path.abspath(config.DB_PATH)) or "."
        usage = shutil.disk_usage(directory)
        pct = round(usage.used / usage.total * 100, 1) if usage.total else None
        return {
            "path": directory,
            "total_bytes": usage.total,
            "used_bytes": usage.used,
            "free_bytes": usage.free,
            "used_pct": pct,
        }
    except OSError:
        return None


def _uptime_s():
    try:
        with open("/proc/uptime", "r") as fh:
            return float(fh.read().split()[0])
    except (OSError, ValueError, IndexError):
        return None


@app.route("/api/health", methods=["GET"])
def api_health():
    """Server self-health: services, load, memory, disk, uptime. Never 500s."""
    services = {}
    for name in config.HEALTH_SERVICES:
        try:
            services[name] = _service_status(name)
        except Exception as exc:
            services[name] = {"status": None, "detail": "check failed: {}".format(exc.__class__.__name__)}

    return jsonify({
        "checked_at": db.iso(db.sqlite_now()),
        "services": services,
        "poller_running": poller.is_running(),
        "reach_running": reach.is_running(),
        "discovery": discovery.status(),
        "load_avg": _load_avg(),
        "memory": _memory_info(),
        "disk": _disk_info(),
        "uptime_s": _uptime_s(),
    })


@app.route("/metrics", methods=["GET"])
def metrics_endpoint():
    """Prometheus text format, built from the database at scrape time."""
    return Response(metrics.render(db.get_db()), mimetype="text/plain; version=0.0.4")


# ---------------------------------------------------------------------------
# Pages
# ---------------------------------------------------------------------------

@app.route("/favicon.ico")
def favicon():
    svg = (
        '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 16 16">'
        '<rect width="16" height="16" rx="3" fill="#0d1117"/>'
        '<rect x="3" y="3" width="4" height="4" fill="#58a6ff"/>'
        '<rect x="9" y="3" width="4" height="4" fill="#58a6ff"/>'
        '<rect x="3" y="9" width="4" height="4" fill="#58a6ff"/>'
        '<rect x="9" y="9" width="4" height="4" fill="#3fb950"/>'
        "</svg>"
    )
    return Response(svg, mimetype="image/svg+xml")


@app.route("/")
def dashboard():
    return render_template("dashboard.html")


@app.route("/devices")
def devices_page():
    return render_template("devices.html")


@app.route("/devices/<int:device_id>")
def device_page(device_id):
    return render_template("device.html", device_id=device_id)


@app.route("/changes")
def changes_page():
    return render_template("changes.html")


@app.route("/conflicts")
def conflicts_page():
    return render_template("conflicts.html")


@app.route("/settings")
def settings_page():
    return render_template("settings.html")


@app.route("/topology")
def topology_page():
    return render_template("topology.html")


# ---------------------------------------------------------------------------
# Devices
#
# The only writer of the devices table other than the poller (which only
# updates poll_state/next_poll_at/fail_count on rows that already exist) —
# every path that can create or enrich a device goes through identity.ingest
# so the four seeding sources named in the plan can never silently produce
# duplicate rows. See identity.py for the merge rule.
# ---------------------------------------------------------------------------

def device_row(r):
    d = dict(r)
    d["last_seen"] = db.iso(d["last_seen"])
    d["first_seen"] = db.iso(d["first_seen"])
    d["next_poll_at"] = db.iso(d["next_poll_at"]) if d["next_poll_at"] else ""
    d["last_poll_ok"] = db.iso(d["last_poll_ok"]) if d.get("last_poll_ok") else None
    if d.get("last_ping_at"):
        d["last_ping_at"] = db.iso(d["last_ping_at"])
    return d


@app.route("/api/devices", methods=["GET"])
def api_list_devices():
    conn = db.get_db()
    rows = conn.execute(
        """SELECT id, key, hostname, mgmt_ip, vendor, platform, model, serial,
                  os_version, role, site, enabled, poll_interval_s, next_poll_at,
                  poll_state, fail_count, first_seen, last_seen, last_poll_ok,
                  reachable, last_ping_at
           FROM devices ORDER BY role, hostname"""
    ).fetchall()
    return jsonify([device_row(r) for r in rows])


@app.route("/api/devices", methods=["POST"])
def api_add_device():
    """Manually add (or enrich, if it already exists) a device.

    A manual add still goes through identity.ingest, so adding a device by
    hand that a sweep already found under a different name updates that same
    row instead of creating a duplicate.
    """
    data = request.get_json(force=True, silent=True)
    if not isinstance(data, dict):
        return jsonify({"error": "Body must be a JSON object"}), 400

    candidates = [
        (kind, data[kind])
        for kind in ("serial", "vmuuid", "mgmt_ip", "hostname")
        if data.get(kind)
    ]
    if not candidates:
        return jsonify({"error": "at least one of serial, vmuuid, mgmt_ip, hostname is required"}), 400

    fields = {k: data.get(k) for k in identity.DEVICE_FIELDS}
    conn = db.get_db()
    try:
        device_id = identity.ingest(conn, candidates, fields, "manual")
    except identity.Conflict as exc:
        conn.commit()
        return jsonify({
            "error": "ambiguous device match",
            "device_ids": exc.device_ids,
            "resolve": "POST /api/devices/<survivor_id>/merge with {\"loser_id\": <id>}",
        }), 409

    if "poll_interval_s" in data and data["poll_interval_s"]:
        conn.execute(
            "UPDATE devices SET poll_interval_s = ? WHERE id = ?",
            (int(data["poll_interval_s"]), device_id),
        )
    conn.commit()

    row = conn.execute("SELECT * FROM devices WHERE id = ?", (device_id,)).fetchone()
    return jsonify(device_row(row)), 201


@app.route("/api/devices/<int:device_id>", methods=["GET"])
def api_get_device(device_id):
    conn = db.get_db()
    row = conn.execute("SELECT * FROM devices WHERE id = ?", (device_id,)).fetchone()
    if not row:
        return jsonify({"error": "not found"}), 404
    out = device_row(row)
    out["aliases"] = [dict(a) for a in conn.execute(
        "SELECT kind, value, first_seen FROM device_aliases WHERE device_id = ? ORDER BY kind", (device_id,)
    ).fetchall()]
    out["sources"] = [dict(s) for s in conn.execute(
        "SELECT source, ref, first_seen, last_seen FROM device_sources WHERE device_id = ? ORDER BY source",
        (device_id,),
    ).fetchall()]
    return jsonify(out)


@app.route("/api/devices/<int:device_id>/interfaces", methods=["GET"])
def api_device_interfaces(device_id):
    conn = db.get_db()
    rows = conn.execute(
        """SELECT name, description, ip, prefix_len, network, vrf, admin_status, oper_status,
                  speed, duplex, mtu, last_seen
           FROM interfaces WHERE device_id = ? ORDER BY name""",
        (device_id,),
    ).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        d["last_seen"] = db.iso(d["last_seen"])
        out.append(d)
    return jsonify(out)


CREDENTIAL_FIELDS = ("username", "password", "enable_secret")
CREDENTIAL_SECRETS = ("password", "enable_secret")


@app.route("/api/devices/<int:device_id>/credentials", methods=["GET"])
def api_get_device_credentials(device_id):
    """What the device page's credentials form needs — never the secrets
    themselves, only whether each one is set. Unset fields fall back to the
    TOPO_SSH_* environment defaults at poll time."""
    conn = db.get_db()
    if not conn.execute("SELECT 1 FROM devices WHERE id = ?", (device_id,)).fetchone():
        return jsonify({"error": "not found"}), 404
    row = conn.execute("SELECT * FROM credentials WHERE device_id = ?", (device_id,)).fetchone()
    out = {"username": row["username"] if row else None,
           "updated_at": db.iso(row["updated_at"]) if row else None}
    for k in CREDENTIAL_SECRETS:
        out[k + "_set"] = bool(row and row[k])
    return jsonify(out)


@app.route("/api/devices/<int:device_id>/credentials", methods=["PUT"])
def api_device_credentials(device_id):
    """Per-device credential override. Plaintext in v1 — see config.py."""
    data = request.get_json(force=True, silent=True)
    if not isinstance(data, dict):
        return jsonify({"error": "Body must be a JSON object"}), 400

    conn = db.get_db()
    if not conn.execute("SELECT 1 FROM devices WHERE id = ?", (device_id,)).fetchone():
        return jsonify({"error": "not found"}), 404

    # Only fields present in the body change: saving a new password must not
    # wipe the stored enable secret. Send "" to clear one.
    if not _set_credentials(conn, device_id, data):
        return jsonify({"error": "no credential fields in body", "fields": list(CREDENTIAL_FIELDS)}), 400
    conn.commit()
    return jsonify({"status": "ok", "device_id": device_id})


def _set_credentials(conn, device_id, data):
    """Write the credential fields present in data to one device's row.
    Returns False when there are none. Does not commit."""
    fields = [k for k in CREDENTIAL_FIELDS if k in data]
    if not fields:
        return False
    now = db.sqlite_now()
    conn.execute(
        "INSERT OR IGNORE INTO credentials (device_id, updated_at) VALUES (?, ?)",
        (device_id, now),
    )
    conn.execute(
        "UPDATE credentials SET {}, updated_at = ? WHERE device_id = ?".format(
            ", ".join("{} = ?".format(k) for k in fields)),
        [data[k] for k in fields] + [now, device_id],
    )
    return True


# Settings-page defaults: body field -> settings key -> env fallback in config.py.
SETTING_KEYS = {"username": "ssh_username", "password": "ssh_password", "enable_secret": "ssh_enable_secret"}
ENV_DEFAULTS = {"username": "DEFAULT_SSH_USERNAME", "password": "DEFAULT_SSH_PASSWORD",
                "enable_secret": "DEFAULT_SSH_SECRET"}


@app.route("/api/settings/credentials", methods=["GET"])
def api_get_default_credentials():
    """The server-wide default credentials. Never returns a secret, only
    whether each is set and where the value in use comes from."""
    conn = db.get_db()
    saved = {r["key"]: r for r in conn.execute("SELECT key, value, updated_at FROM settings")}
    out = {"updated_at": None}
    for field, key in SETTING_KEYS.items():
        row = saved.get(key)
        from_db = bool(row and row["value"])
        from_env = bool(getattr(config, ENV_DEFAULTS[field]))
        out[field + "_source"] = "settings" if from_db else ("env" if from_env else "none")
        if row and row["updated_at"] and (out["updated_at"] is None or row["updated_at"] > out["updated_at"]):
            out["updated_at"] = row["updated_at"]
    out["updated_at"] = db.iso(out["updated_at"]) if out["updated_at"] else None
    out["username"] = (saved.get("ssh_username") or {"value": None})["value"]
    out["username_env"] = config.DEFAULT_SSH_USERNAME
    for k in CREDENTIAL_SECRETS:
        out[k + "_set"] = out[k + "_source"] != "none"
    return jsonify(out)


@app.route("/api/settings/credentials", methods=["PUT"])
def api_put_default_credentials():
    """Partial update, like the per-device PUT: only fields in the body
    change, "" clears one (it then falls back to the environment)."""
    data = request.get_json(force=True, silent=True)
    if not isinstance(data, dict):
        return jsonify({"error": "Body must be a JSON object"}), 400
    fields = [k for k in CREDENTIAL_FIELDS if k in data]
    if not fields:
        return jsonify({"error": "no credential fields in body", "fields": list(CREDENTIAL_FIELDS)}), 400
    conn = db.get_db()
    now = db.sqlite_now()
    for k in fields:
        conn.execute(
            "INSERT INTO settings (key, value, updated_at) VALUES (?, ?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at",
            (SETTING_KEYS[k], str(data[k]), now),
        )
    conn.commit()
    return jsonify({"status": "ok"})


LLDP_MATCH_KEYS = {"chassis": "lldp_match_chassis", "name": "lldp_match_name"}


@app.route("/api/settings/lldp-matching", methods=["GET"])
def api_get_lldp_matching():
    """The two optional LLDP neighbor matches (chassis ID = management MAC, system name = hostname)."""
    return jsonify(topology.lldp_matching(db.get_db()))


@app.route("/api/settings/lldp-matching", methods=["PUT"])
def api_put_lldp_matching():
    """Partial update: body {"chassis": bool, "name": bool}; only the fields given change."""
    data = request.get_json(force=True, silent=True)
    if not isinstance(data, dict):
        return jsonify({"error": "Body must be a JSON object"}), 400
    fields = [k for k in LLDP_MATCH_KEYS if k in data]
    if not fields:
        return jsonify({"error": "no fields in body", "fields": list(LLDP_MATCH_KEYS)}), 400
    if any(not isinstance(data[k], bool) for k in fields):
        return jsonify({"error": "values must be true or false"}), 400
    conn = db.get_db()
    now = db.sqlite_now()
    for k in fields:
        conn.execute(
            "INSERT INTO settings (key, value, updated_at) VALUES (?, ?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at",
            (LLDP_MATCH_KEYS[k], "1" if data[k] else "0", now),
        )
    conn.commit()
    return jsonify(topology.lldp_matching(conn))


@app.route("/api/credentials", methods=["GET"])
def api_list_device_credentials():
    """Which devices have their own credentials row. No secrets."""
    rows = db.get_db().execute(
        """SELECT d.id, d.hostname, d.mgmt_ip, d.role, c.username, c.password, c.enable_secret, c.updated_at
           FROM devices d LEFT JOIN credentials c ON c.device_id = d.id
           WHERE d.role != 'node' ORDER BY d.hostname"""
    ).fetchall()
    return jsonify([{
        "device_id": r["id"], "hostname": r["hostname"], "mgmt_ip": r["mgmt_ip"],
        "has_override": r["updated_at"] is not None,
        "username": r["username"],
        "password_set": bool(r["password"]), "enable_secret_set": bool(r["enable_secret"]),
        "updated_at": db.iso(r["updated_at"]) if r["updated_at"] else None,
    } for r in rows])


def _bulk_ids(data):
    """device_ids from a bulk body, or None when it is not a non-empty list of ints."""
    ids = data.get("device_ids") if isinstance(data, dict) else None
    if not isinstance(ids, list) or not ids or not all(isinstance(i, int) for i in ids):
        return None
    return sorted(set(ids))


def _missing_devices(conn, ids):
    found = {r["id"] for r in conn.execute(
        "SELECT id FROM devices WHERE id IN ({})".format(",".join("?" * len(ids))), ids)}
    return [i for i in ids if i not in found]


@app.route("/api/credentials/bulk", methods=["POST"])
def api_bulk_credentials():
    """Set the same credential fields on several devices in one transaction.
    An unknown device id applies nothing."""
    data = request.get_json(force=True, silent=True)
    ids = _bulk_ids(data)
    if ids is None:
        return jsonify({"error": "device_ids must be a non-empty list of device ids"}), 400
    if not any(k in data for k in CREDENTIAL_FIELDS):
        return jsonify({"error": "no credential fields in body", "fields": list(CREDENTIAL_FIELDS)}), 400
    conn = db.get_db()
    missing = _missing_devices(conn, ids)
    if missing:
        return jsonify({"error": "not found", "device_ids": missing}), 404
    for device_id in ids:
        _set_credentials(conn, device_id, data)
    conn.commit()
    return jsonify({"status": "ok", "updated": len(ids)})


@app.route("/api/credentials/clear", methods=["POST"])
def api_bulk_clear_credentials():
    """Remove the per-device override from several devices, so they use the defaults."""
    data = request.get_json(force=True, silent=True)
    ids = _bulk_ids(data)
    if ids is None:
        return jsonify({"error": "device_ids must be a non-empty list of device ids"}), 400
    conn = db.get_db()
    missing = _missing_devices(conn, ids)
    if missing:
        return jsonify({"error": "not found", "device_ids": missing}), 404
    conn.execute("DELETE FROM credentials WHERE device_id IN ({})".format(",".join("?" * len(ids))), ids)
    conn.commit()
    return jsonify({"status": "ok", "cleared": len(ids)})


@app.route("/api/devices/<int:device_id>/poll", methods=["POST"])
def api_poll_device_now(device_id):
    """Poll a device immediately, inline on this request — see poller.poll_now."""
    conn = db.get_db()
    if not conn.execute("SELECT 1 FROM devices WHERE id = ?", (device_id,)).fetchone():
        return jsonify({"error": "not found"}), 404

    conn.execute("UPDATE devices SET poll_state = 'running' WHERE id = ?", (device_id,))
    conn.commit()
    # poll_now opens its own connection (it can run from the poller thread
    # pool too), so this request's db.get_db() connection must not be held
    # across it — commit above releases the lock this thread was holding.
    results = poller.poll_now(device_id)
    if results is None:
        return jsonify({"error": "not found"}), 404
    return jsonify({"status": "ok", "results": results})


DEVICE_EDITABLE = identity.DEVICE_FIELDS + ("enabled", "poll_interval_s")


@app.route("/api/devices/<int:device_id>", methods=["PUT"])
def api_update_device(device_id):
    """Authoritative operator edit — bypasses the identity ladder entirely.

    Unlike identity.ingest, an explicit PUT can blank a field (e.g. clearing
    a wrong site tag), so empty strings are accepted here rather than
    filtered out.
    """
    data = request.get_json(force=True, silent=True)
    if not isinstance(data, dict):
        return jsonify({"error": "Body must be a JSON object"}), 400

    updates = {k: data[k] for k in DEVICE_EDITABLE if k in data}
    if not updates:
        return jsonify({"error": "no editable fields in body", "editable": list(DEVICE_EDITABLE)}), 400

    conn = db.get_db()
    if not conn.execute("SELECT 1 FROM devices WHERE id = ?", (device_id,)).fetchone():
        return jsonify({"error": "not found"}), 404

    set_clause = ", ".join("{} = :{}".format(c, c) for c in updates)
    updates["id"] = device_id
    conn.execute("UPDATE devices SET {} WHERE id = :id".format(set_clause), updates)
    taken = identity.add_operator_aliases(conn, device_id, updates)
    conn.commit()
    row = conn.execute("SELECT * FROM devices WHERE id = ?", (device_id,)).fetchone()
    out = device_row(row)
    if taken:
        out["alias_warnings"] = [
            "{} {} already belongs to device {} — merge them if they are the same box".format(
                t["kind"], t["value"], t["device_id"])
            for t in taken
        ]
    return jsonify(out)


@app.route("/api/devices/<int:device_id>", methods=["DELETE"])
def api_delete_device(device_id):
    conn = db.get_db()
    cur = conn.execute("DELETE FROM devices WHERE id = ?", (device_id,))
    # Child tables cascade, but these three don't: poll_history has no FK,
    # and other devices' neighbor/peer rows only reference this one. Same
    # three tables identity.merge re-points. A NULLed reference re-renders
    # as a ghost node.
    conn.execute("DELETE FROM poll_history WHERE device_id = ?", (device_id,))
    conn.execute("UPDATE lldp_neighbors SET remote_device_id = NULL WHERE remote_device_id = ?",
                 (device_id,))
    conn.execute("UPDATE adjacencies SET peer_device_id = NULL WHERE peer_device_id = ?",
                 (device_id,))
    conn.execute("DELETE FROM node_positions WHERE node_id IN (?, ?)",
                 ("dev:{}".format(device_id), "group:dev:{}".format(device_id)))
    conn.commit()
    if cur.rowcount == 0:
        return jsonify({"error": "not found"}), 404
    return jsonify({"status": "deleted", "id": device_id})


@app.route("/api/devices/<int:survivor_id>/merge", methods=["POST"])
def api_merge_device(survivor_id):
    """Fold another device into this one. Operator-invoked only — see identity.merge."""
    data = request.get_json(force=True, silent=True) or {}
    loser_id = data.get("loser_id")
    if not loser_id:
        return jsonify({"error": "loser_id is required"}), 400

    conn = db.get_db()
    try:
        identity.merge(conn, survivor_id, int(loser_id))
    except ValueError as exc:
        conn.rollback()
        return jsonify({"error": str(exc)}), 400
    conn.commit()
    row = conn.execute("SELECT * FROM devices WHERE id = ?", (survivor_id,)).fetchone()
    return jsonify(device_row(row))


@app.route("/api/conflicts", methods=["GET"])
def api_conflicts():
    """Unresolved identity conflicts, newest first — see /conflicts page."""
    conn = db.get_db()
    resolved = request.args.get("resolved", "0")
    rows = conn.execute(
        "SELECT id, seen_at, source, candidate, device_ids, detail, resolved "
        "FROM merge_conflicts WHERE resolved = ? ORDER BY seen_at DESC",
        (1 if resolved in ("1", "true") else 0,),
    ).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        d["seen_at"] = db.iso(d["seen_at"])
        out.append(d)
    return jsonify(out)


@app.route("/api/conflicts/<int:conflict_id>/dismiss", methods=["POST"])
def api_dismiss_conflict(conflict_id):
    """Mark a conflict resolved without merging. Needed for single-device
    conflicts (a serial changed under polling, an IP reused by a new box):
    there is no second device to merge, so merge can never clear them."""
    conn = db.get_db()
    cur = conn.execute("UPDATE merge_conflicts SET resolved = 1 WHERE id = ?", (conflict_id,))
    conn.commit()
    if cur.rowcount == 0:
        return jsonify({"error": "not found"}), 404
    return jsonify({"status": "dismissed", "id": conflict_id})


@app.route("/api/discover/seedfile", methods=["POST"])
def api_discover_seedfile():
    """Ingest devices from a YAML seed file. Body: {"path": "seed/devices.yaml"}."""
    data = request.get_json(force=True, silent=True) or {}
    path = data.get("path")
    if not path:
        return jsonify({"error": "path is required"}), 400
    if not os.path.isfile(path):
        return jsonify({"error": "file not found", "path": path}), 400

    conn = db.get_db()
    try:
        device_ids, conflicts = seedfile.load(conn, path)
    except Exception as exc:
        conn.rollback()
        return jsonify({"error": "failed to load seed file: {}".format(exc)}), 400
    conn.commit()
    return jsonify({"status": "ok", "ingested": len(device_ids), "device_ids": device_ids,
                     "conflicts": conflicts})


@app.route("/api/discover/sweep", methods=["POST"])
def api_discover_sweep():
    """Scan a CIDR for hosts with SSH open. Body: {"cidr": "10.0.0.0/24"}.
    A live network scan — runs only when explicitly invoked, never on a
    schedule."""
    data = request.get_json(force=True, silent=True) or {}
    cidr = data.get("cidr")
    if not cidr:
        return jsonify({"error": "cidr is required"}), 400

    conn = db.get_db()
    try:
        found, device_ids, conflicts = sweep_collector.sweep(conn, cidr)
    except ValueError as exc:
        return jsonify({"error": "invalid cidr: {}".format(exc)}), 400
    conn.commit()
    return jsonify({"status": "ok", "hosts_found": len(found), "device_ids": device_ids,
                     "conflicts": conflicts})


@app.route("/api/discover/run", methods=["POST"])
def api_discover_run():
    """Run the configured scheduled-discovery sources now (TOPO_DISCOVERY_*)."""
    return jsonify({"status": "ok", "summary": discovery.run_once()})


@app.route("/api/discover/status", methods=["GET"])
def api_discover_status():
    return jsonify(discovery.status())


@app.route("/api/lldp/unknown", methods=["GET"])
def api_lldp_unknown():
    """LLDP neighbors advertising a management IP no known device owns."""
    return jsonify(lldp_crawl.unknown_neighbors(db.get_db()))


@app.route("/api/lldp/adopt", methods=["POST"])
def api_lldp_adopt():
    """Body: {"ip": "192.0.2.9", "hostname": "optional"}. Adds the neighbor as a
    device with polling off; enable it from its page once credentials are set."""
    data = request.get_json(force=True, silent=True) or {}
    ip = (data.get("ip") or "").strip()
    conn = db.get_db()
    device_id = lldp_crawl.adopt(conn, ip, (data.get("hostname") or "").strip(), "operator")
    if device_id is None:
        conn.rollback()
        return jsonify({"error": "not a usable IPv4 address, or it conflicts with an existing device"}), 400
    conn.commit()
    return jsonify({"status": "ok", "device_id": device_id}), 201


@app.route("/api/discover/confetti", methods=["POST"])
def api_discover_confetti():
    """Body: {"hub_url": "http://10.0.0.100"}. Imports confetti-traffic's node
    fleet as role='node' devices — see collectors/confetti.py."""
    data = request.get_json(force=True, silent=True) or {}
    hub_url = data.get("hub_url")
    if not hub_url:
        return jsonify({"error": "hub_url is required"}), 400

    conn = db.get_db()
    try:
        device_ids, conflicts = confetti.discover(conn, hub_url)
    except Exception as exc:
        return jsonify({"error": "confetti-traffic hub request failed: {}".format(exc)}), 502
    conn.commit()
    return jsonify({"status": "ok", "device_ids": device_ids, "conflicts": conflicts})


@app.route("/api/discover/butler", methods=["POST"])
def api_discover_butler():
    """Body: {"butler_url": "http://10.0.0.101"}. Imports confetti-butler's device
    inventory (no credentials) — see collectors/butler.py."""
    data = request.get_json(force=True, silent=True) or {}
    butler_url = data.get("butler_url")
    if not butler_url:
        return jsonify({"error": "butler_url is required"}), 400

    conn = db.get_db()
    try:
        device_ids, conflicts = butler.discover(conn, butler_url)
    except Exception as exc:
        return jsonify({"error": "confetti-butler request failed: {}".format(exc)}), 502
    conn.commit()
    return jsonify({"status": "ok", "device_ids": device_ids, "conflicts": conflicts})


@app.route("/api/events", methods=["GET"])
def api_events():
    """Change events from the poller (events.py), newest first.

    ?device_id=N, ?kind=, ?severity=info|warning|critical, ?hours=N (default
    168), ?limit=N (default 100, max 1000).
    """
    args = request.args
    where = ["e.at >= datetime('now', ? || ' hours')"]
    try:
        hours = int(args.get("hours", "168"))
    except ValueError:
        hours = 168
    params = ["-{:d}".format(abs(hours))]
    for col, key in (("e.device_id", "device_id"), ("e.kind", "kind"), ("e.severity", "severity")):
        if args.get(key):
            where.append(col + " = ?")
            params.append(args[key])
    try:
        limit = max(1, min(int(args.get("limit", "100")), 1000))
    except ValueError:
        limit = 100
    rows = db.get_db().execute(
        """SELECT e.id, e.at, e.device_id, d.hostname, e.kind, e.severity, e.subject, e.detail
           FROM events e LEFT JOIN devices d ON d.id = e.device_id
           WHERE {} ORDER BY e.id DESC LIMIT ?""".format(" AND ".join(where)),
        params + [limit],
    ).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        d["at"] = db.iso(d["at"])
        out.append(d)
    return jsonify(out)


# ---------------------------------------------------------------------------
# Topology — built in topology.py, see its docstring for the ghost-node and
# draw-each-cable-once rules.
# ---------------------------------------------------------------------------

@app.route("/api/topology", methods=["GET"])
def api_topology():
    """?layer= limits the edges to one source or a comma list (lldp,bgp);
    default overlays all three on the same device nodes."""
    return jsonify(topology.build(db.get_db(), request.args.get("layer")))


@app.route("/api/topology/positions", methods=["PUT"])
def api_topology_positions():
    """Body: {"positions": {"dev:5": {"x": 10, "y": -20}, ...}}. Saves where nodes sit on the page."""
    positions = (request.get_json(silent=True) or {}).get("positions")
    if not isinstance(positions, dict):
        return jsonify({"error": "positions must be an object"}), 400
    rows = []
    for node_id, p in positions.items():
        try:
            x, y = float(p["x"]), float(p["y"])
        except (TypeError, KeyError, ValueError):
            return jsonify({"error": "bad position for {}".format(node_id)}), 400
        if x != x or y != y or abs(x) == float("inf") or abs(y) == float("inf"):
            return jsonify({"error": "bad position for {}".format(node_id)}), 400
        rows.append((str(node_id)[:200], x, y, db.sqlite_now()))
    conn = db.get_db()
    conn.executemany(
        "INSERT INTO node_positions (node_id, x, y, updated_at) VALUES (?, ?, ?, ?) "
        "ON CONFLICT(node_id) DO UPDATE SET x = excluded.x, y = excluded.y, updated_at = excluded.updated_at",
        rows)
    conn.commit()
    return jsonify({"status": "saved", "count": len(rows)})


@app.route("/api/topology/positions", methods=["DELETE"])
def api_topology_positions_clear():
    """Forget every saved position (the page's "Re-arrange")."""
    conn = db.get_db()
    conn.execute("DELETE FROM node_positions")
    conn.commit()
    return jsonify({"status": "cleared"})


@app.route("/api/adjacencies", methods=["GET"])
def api_adjacencies():
    """BGP / OSPF sessions. ?device_id=N limits to one device. peer_device_id is
    the effective one: the poller's match, else a unique interface-IP match."""
    return jsonify(topology.adjacencies(db.get_db(), request.args.get("device_id")))


@app.route("/api/lldp-neighbors", methods=["GET"])
def api_lldp_neighbors():
    """LLDP neighbors. ?device_id=N limits to one device."""
    return jsonify(topology.lldp_neighbors(db.get_db(), request.args.get("device_id")))


# ---------------------------------------------------------------------------
# Startup
# ---------------------------------------------------------------------------

db.init_db()
