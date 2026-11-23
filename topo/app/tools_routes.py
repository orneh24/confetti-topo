"""IPAM, config templates and the pull endpoint, running-config
backups, drift, interface error history and the vCenter import.

A Blueprint, so app.py keeps only the routes that were topo's own. Nothing here ever writes to a device:
templates are served for a device to pull (`copy http://<server>/configs/<key>.cfg running-config`).
"""

import difflib
import re

from flask import Blueprint, Response, jsonify, render_template, request

from . import config
from . import db
from . import drift
from . import ipam
from . import rendering
from .collectors import ssh as ssh_collector
from .collectors import vcenter as vcenter_collector

bp = Blueprint("tools", __name__)


# ---------------------------------------------------------------------------
# Pages
# ---------------------------------------------------------------------------

@bp.route("/ipam")
def ipam_page():
    return render_template("ipam.html")


@bp.route("/templates")
def templates_page():
    return render_template("templates_editor.html")


@bp.route("/backups")
def backups_page():
    # Not under /configs/: that prefix is the unauthenticated pull endpoint (constraint 28).
    return render_template("backups.html")


# ---------------------------------------------------------------------------
# Drift, interface error history, running-config backups
# ---------------------------------------------------------------------------

@bp.route("/api/devices/<int:device_id>/drift", methods=["GET"])
def api_device_drift(device_id):
    """Live drift check of the device's latest config backup against its template."""
    conn = db.get_db()
    if not conn.execute("SELECT 1 FROM devices WHERE id = ?", (device_id,)).fetchone():
        return jsonify({"error": "not found"}), 404
    return jsonify(drift.evaluate(conn, device_id))


@bp.route("/api/devices/<int:device_id>/interface-stats", methods=["GET"])
def api_interface_stats(device_id):
    """Counter samples for one interface: ?name=GigabitEthernet1&hours=24. Each sample has the error counters and the
    byte counters; in_bps / out_bps is the rate since the sample before (None for the first, and after a counter reset)."""
    name = request.args.get("name", "")
    try:
        hours = max(1, min(int(request.args.get("hours", "24")), 24 * config.STATS_RETENTION_DAYS))
    except ValueError:
        hours = 24
    rows = db.get_db().execute(
        """SELECT at, input_errors, crc_errors, in_bytes, out_bytes FROM interface_stats
           WHERE device_id = ? AND name = ? AND at >= datetime('now', ? || ' hours') ORDER BY id""",
        (device_id, name, "-{:d}".format(hours))).fetchall()
    rate_at = {at: (i, o) for at, i, o in ssh_collector.rates(rows)}
    return jsonify([{"at": db.iso(r["at"]), "input_errors": r["input_errors"], "crc_errors": r["crc_errors"],
                     "in_bytes": r["in_bytes"], "out_bytes": r["out_bytes"],
                     "in_bps": rate_at.get(r["at"], (None, None))[0], "out_bps": rate_at.get(r["at"], (None, None))[1]}
                    for r in rows])


# ---------------------------------------------------------------------------
# Config backups across devices (the Backups page)
# ---------------------------------------------------------------------------

@bp.route("/api/backups", methods=["GET"])
def api_backups():
    """The latest running-config backup of every polled device: when, how big, how many versions, and its template
    drift. A device with no backup yet is listed with captured_at null."""
    conn = db.get_db()
    rows = conn.execute(
        """SELECT d.id AS device_id, d.hostname, d.platform, d.role, d.template_name, d.drift_status,
                  v.id AS version_id, v.captured_at, length(v.body) AS size,
                  (SELECT COUNT(*) FROM config_versions c WHERE c.device_id = d.id) AS versions
           FROM devices d
           LEFT JOIN config_versions v ON v.id = (SELECT MAX(id) FROM config_versions WHERE device_id = d.id)
           WHERE d.role <> 'node'
           ORDER BY d.hostname COLLATE NOCASE"""
    ).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        d["captured_at"] = db.iso(d["captured_at"]) if d["captured_at"] else None
        out.append(d)
    return jsonify(out)


SEARCH_MAX_LINES = 20


@bp.route("/api/backups/search", methods=["GET"])
def api_backups_search():
    """Search the latest backup of every device: ?q=text (or a regular expression with &regex=1), &invert=1 lists the
    devices whose latest config has NO matching line. The text searched is the masked one (secrets are never
    matched or returned). Each match carries its line number and its parent line (the "interface ..." above it)."""
    q = (request.args.get("q") or "")[:200]
    if not q.strip():
        return jsonify({"error": "q is required"}), 400
    if request.args.get("regex") == "1":
        try:
            pat = re.compile(q, re.I)
        except re.error as exc:
            return jsonify({"error": "not a valid regular expression: {}".format(exc)}), 400
    else:
        pat = re.compile(re.escape(q), re.I)
    invert = request.args.get("invert") == "1"
    conn = db.get_db()
    rows = conn.execute(
        """SELECT d.id AS device_id, d.hostname, v.captured_at, v.body
           FROM devices d JOIN config_versions v ON v.id = (SELECT MAX(id) FROM config_versions WHERE device_id = d.id)
           ORDER BY d.hostname COLLATE NOCASE"""
    ).fetchall()
    out = []
    for r in rows:
        hits, parent, count = [], "", 0
        for n, line in enumerate(rendering.redact_secrets(r["body"]).splitlines(), 1):
            if line and not line[0].isspace():
                parent = line
            if pat.search(line):
                count += 1
                if len(hits) < SEARCH_MAX_LINES:
                    hits.append({"line": n, "text": line, "parent": parent if line[:1].isspace() else ""})
        if bool(count) != invert:
            out.append({"device_id": r["device_id"], "hostname": r["hostname"], "captured_at": db.iso(r["captured_at"]),
                        "count": count, "lines": hits})
    return jsonify({"query": q, "invert": invert, "devices": out, "searched": len(rows)})


def _config_body(row, raw):
    return row["body"] if raw else rendering.redact_secrets(row["body"])


@bp.route("/api/devices/<int:device_id>/configs", methods=["GET"])
def api_device_configs(device_id):
    conn = db.get_db()
    rows = conn.execute(
        "SELECT id, sha256, captured_at, length(body) AS size FROM config_versions "
        "WHERE device_id = ? ORDER BY id DESC", (device_id,),
    ).fetchall()
    return jsonify([{"id": r["id"], "sha256": r["sha256"], "size": r["size"],
                     "captured_at": db.iso(r["captured_at"])} for r in rows])


@bp.route("/api/devices/<int:device_id>/configs/<int:version_id>", methods=["GET"])
def api_device_config(device_id, version_id):
    """Secrets are masked unless ?raw=1 (the unmasked text is the backup)."""
    conn = db.get_db()
    row = conn.execute("SELECT * FROM config_versions WHERE id = ? AND device_id = ?",
                       (version_id, device_id)).fetchone()
    if not row:
        return jsonify({"error": "not found"}), 404
    return jsonify({"id": row["id"], "sha256": row["sha256"],
                    "captured_at": db.iso(row["captured_at"]),
                    "body": _config_body(row, request.args.get("raw") == "1")})


@bp.route("/api/devices/<int:device_id>/configs/diff", methods=["GET"])
def api_device_config_diff(device_id):
    """Unified diff of two stored versions (?a=<id>&b=<id>, a older). Secrets are
    masked, so a changed secret shows no difference."""
    conn = db.get_db()
    rows = {}
    for key in ("a", "b"):
        try:
            vid = int(request.args.get(key, ""))
        except ValueError:
            return jsonify({"error": "a and b must be version ids"}), 400
        rows[key] = conn.execute("SELECT * FROM config_versions WHERE id = ? AND device_id = ?",
                                 (vid, device_id)).fetchone()
        if not rows[key]:
            return jsonify({"error": "version {} not found".format(vid)}), 404
    diff = difflib.unified_diff(
        _config_body(rows["a"], False).splitlines(), _config_body(rows["b"], False).splitlines(),
        fromfile="version {} ({})".format(rows["a"]["id"], db.iso(rows["a"]["captured_at"])),
        tofile="version {} ({})".format(rows["b"]["id"], db.iso(rows["b"]["captured_at"])),
        lineterm="",
    )
    return jsonify({"diff": "\n".join(diff)})


# ---------------------------------------------------------------------------
# IPAM: discovery-first, see ipam.py. interfaces is the source of truth; these routes only read it
# and the findings ipam.analyze() computes.
# ---------------------------------------------------------------------------

@bp.route("/api/ipam/addresses", methods=["GET"])
def api_ipam_addresses():
    conn = db.get_db()
    rows = conn.execute(
        """SELECT d.id AS device_id, d.hostname, d.site, i.name AS interface, i.ip, i.prefix_len,
                  i.network, {} AS vrf, i.admin_status, i.oper_status, i.last_seen
           FROM interfaces i JOIN devices d ON d.id = i.device_id
           WHERE i.ip IS NOT NULL
           ORDER BY i.network, i.ip""".format(ipam.VRF_SQL)
    ).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        d["last_seen"] = db.iso(d["last_seen"])
        out.append(d)
    return jsonify(out)


@bp.route("/api/ipam/prefixes", methods=["GET"])
def api_ipam_prefixes():
    """One row per (VRF, network) in use: how many interfaces / devices sit on it, size (usable hosts), used (distinct
    interface addresses seen in it), free = size - used, util = used / size in percent, how many interfaces are up,
    the sites of its devices, and two marks: dup (an address held by more than one interface in it, live) and overlap
    (named by the last analysis' overlap findings). The VRF is the one of the interface's connected route (ipam.VRF_SQL)."""
    conn = db.get_db()
    rows = conn.execute(
        """SELECT vrf, network, COUNT(*) AS interfaces, COUNT(DISTINCT device_id) AS devices, COUNT(DISTINCT ip) AS used,
                  SUM(oper_status = 'up') AS up, GROUP_CONCAT(DISTINCT NULLIF(site, '')) AS sites
           FROM (SELECT {} AS vrf, i.network, i.device_id, i.ip, i.oper_status, d.site
                 FROM interfaces i JOIN devices d ON d.id = i.device_id WHERE i.network IS NOT NULL) x
           GROUP BY vrf, network
           ORDER BY network, vrf""".format(ipam.VRF_SQL)
    ).fetchall()
    overlapping = ipam.overlapping_networks(conn)
    out = []
    for r in rows:
        d = dict(r)
        d["sites"] = sorted(d["sites"].split(",")) if d["sites"] else []
        d["up"] = d["up"] or 0
        d["size"] = ipam.usable_hosts(d["network"])
        d["free"] = None if d["size"] is None else max(0, d["size"] - d["used"])
        d["util"] = None if not d["size"] else min(100, round(100.0 * d["used"] / d["size"]))
        d["dup"] = d["interfaces"] > d["used"]
        d["overlap"] = d["network"] in overlapping
        out.append(d)
    return jsonify(out)


@bp.route("/api/ipam/findings", methods=["GET"])
def api_ipam_findings():
    conn = db.get_db()
    rows = conn.execute(
        "SELECT id, checked_at, kind, a, b, detail FROM ipam_findings ORDER BY checked_at DESC, id"
    ).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        d["checked_at"] = db.iso(d["checked_at"])
        out.append(d)
    return jsonify(out)


@bp.route("/api/ipam/analyze", methods=["POST"])
def api_ipam_analyze():
    conn = db.get_db()
    count = ipam.analyze(conn)
    conn.commit()
    return jsonify({"status": "ok", "findings": count})


# ---------------------------------------------------------------------------
# Config templates. Pull-only: nothing here writes to a device. A template renders against a device's
# own polled facts (rendering.build_context) and GET /configs/<key>.cfg serves the result as plain text.
# The endpoint is intentionally unauthenticated (constraint 28); check_hardcoded_secrets on save is the
# matching rule that templates must not carry real credentials.
# ---------------------------------------------------------------------------

def template_row(r):
    d = dict(r)
    d["updated_at"] = db.iso(d["updated_at"])
    return d


@bp.route("/api/templates", methods=["GET"])
def api_list_templates():
    conn = db.get_db()
    rows = conn.execute(
        "SELECT name, note, updated_at, version FROM templates ORDER BY name"
    ).fetchall()
    return jsonify([template_row(r) for r in rows])


@bp.route("/api/templates", methods=["POST"])
def api_create_template():
    data = request.get_json(force=True, silent=True)
    if not isinstance(data, dict) or not data.get("name"):
        return jsonify({"error": "name is required"}), 400

    name = data["name"]
    body = data.get("body", "")
    conn = db.get_db()
    if conn.execute("SELECT 1 FROM templates WHERE name = ?", (name,)).fetchone():
        return jsonify({"error": "a template named '{}' already exists".format(name)}), 409

    now = db.sqlite_now()
    conn.execute(
        "INSERT INTO templates (name, body, note, updated_at, version) VALUES (?, ?, ?, ?, 1)",
        (name, body, data.get("note", ""), now),
    )
    conn.execute(
        "INSERT INTO template_versions (name, version, body, saved_at) VALUES (?, 1, ?, ?)",
        (name, body, now),
    )
    conn.commit()
    warnings = rendering.check_hardcoded_secrets(body)
    return jsonify({"status": "ok", "name": name, "warnings": warnings}), 201


@bp.route("/api/templates/<name>", methods=["GET"])
def api_get_template(name):
    conn = db.get_db()
    row = conn.execute(
        "SELECT name, body, note, updated_at, version FROM templates WHERE name = ?", (name,)
    ).fetchone()
    if not row:
        return jsonify({"error": "not found"}), 404
    return jsonify(template_row(row))


@bp.route("/api/templates/<name>", methods=["PUT"])
def api_update_template(name):
    """Every save appends a template_versions row: a bad edit must be recoverable."""
    data = request.get_json(force=True, silent=True)
    if not isinstance(data, dict):
        return jsonify({"error": "Body must be a JSON object"}), 400

    conn = db.get_db()
    row = conn.execute("SELECT version FROM templates WHERE name = ?", (name,)).fetchone()
    if not row:
        return jsonify({"error": "not found"}), 404

    body = data.get("body")
    if body is None:
        return jsonify({"error": "body is required"}), 400
    next_version = row["version"] + 1
    now = db.sqlite_now()

    conn.execute(
        "UPDATE templates SET body = ?, note = COALESCE(?, note), updated_at = ?, version = ? "
        "WHERE name = ?",
        (body, data.get("note"), now, next_version, name),
    )
    conn.execute(
        "INSERT INTO template_versions (name, version, body, saved_at) VALUES (?, ?, ?, ?)",
        (name, next_version, body, now),
    )
    drift.refresh_template(conn, name)
    conn.commit()
    warnings = rendering.check_hardcoded_secrets(body)
    return jsonify({"status": "ok", "name": name, "version": next_version, "warnings": warnings})


@bp.route("/api/templates/<name>", methods=["DELETE"])
def api_delete_template(name):
    conn = db.get_db()
    cur = conn.execute("DELETE FROM templates WHERE name = ?", (name,))
    conn.execute("DELETE FROM template_versions WHERE name = ?", (name,))
    conn.commit()
    if cur.rowcount == 0:
        return jsonify({"error": "not found"}), 404
    return jsonify({"status": "deleted", "name": name})


@bp.route("/api/templates/<name>/versions", methods=["GET"])
def api_template_versions(name):
    conn = db.get_db()
    rows = conn.execute(
        "SELECT id, version, saved_at FROM template_versions WHERE name = ? ORDER BY version DESC",
        (name,),
    ).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        d["saved_at"] = db.iso(d["saved_at"])
        out.append(d)
    return jsonify(out)


@bp.route("/api/templates/<name>/preview", methods=["POST"])
def api_template_preview(name):
    """Render a template against a device's real polled facts. Body:
    {"device_id": N} — or {"body": "..."} to preview unsaved edits."""
    data = request.get_json(force=True, silent=True) or {}
    device_id = data.get("device_id")
    if not device_id:
        return jsonify({"error": "device_id is required"}), 400

    conn = db.get_db()
    if "body" in data:
        body = data["body"]
    else:
        row = conn.execute("SELECT body FROM templates WHERE name = ?", (name,)).fetchone()
        if not row:
            return jsonify({"error": "template not found"}), 404
        body = row["body"]

    try:
        result = rendering.preview(conn, int(device_id), body)
    except rendering.RenderError as exc:
        return jsonify({"error": "render failed: {}".format(exc)}), 400
    return jsonify(result)


@bp.route("/api/devices/<int:device_id>/vars", methods=["GET"])
def api_get_device_vars(device_id):
    conn = db.get_db()
    rows = conn.execute(
        "SELECT key, value FROM template_vars WHERE device_id = ? ORDER BY key", (device_id,)
    ).fetchall()
    return jsonify({r["key"]: r["value"] for r in rows})


@bp.route("/api/devices/<int:device_id>/vars", methods=["PUT"])
def api_set_device_vars(device_id):
    """Replace the full var set for a device with the given object (a template editor round-trips the whole set)."""
    data = request.get_json(force=True, silent=True)
    if not isinstance(data, dict):
        return jsonify({"error": "Body must be an object of key: value"}), 400

    conn = db.get_db()
    if not conn.execute("SELECT 1 FROM devices WHERE id = ?", (device_id,)).fetchone():
        return jsonify({"error": "not found"}), 404

    conn.execute("DELETE FROM template_vars WHERE device_id = ?", (device_id,))
    for key, value in data.items():
        conn.execute(
            "INSERT INTO template_vars (device_id, key, value) VALUES (?, ?, ?)",
            (device_id, key, str(value)),
        )
    conn.commit()
    return jsonify({"status": "ok"})


@bp.route("/configs/")
def configs_index():
    """Index of devices with an assigned template, for a human browsing."""
    conn = db.get_db()
    rows = conn.execute(
        "SELECT key, hostname, template_name FROM devices WHERE template_name IS NOT NULL ORDER BY hostname"
    ).fetchall()
    lines = ["{}  ->  /configs/{}.cfg  (template: {})".format(r["hostname"], r["key"], r["template_name"])
             for r in rows]
    return Response("\n".join(lines) + "\n", mimetype="text/plain")


@bp.route("/configs/<device_key>.cfg")
def serve_config(device_key):
    """The pull endpoint: a device's console runs 'copy http://<server>/configs/<key>.cfg running-config'
    against this. Unauthenticated by design (constraint 28, also with TOPO_AUTH_TOKEN set): templates must
    not carry real secrets, enforced by check_hardcoded_secrets at save time, not by locking this down."""
    conn = db.get_db()
    device = conn.execute("SELECT id, template_name FROM devices WHERE key = ?", (device_key,)).fetchone()
    if not device:
        return Response("no device with key '{}'\n".format(device_key), mimetype="text/plain", status=404)
    if not device["template_name"]:
        return Response("device '{}' has no template assigned\n".format(device_key),
                        mimetype="text/plain", status=404)

    row = conn.execute("SELECT body FROM templates WHERE name = ?", (device["template_name"],)).fetchone()
    if not row:
        return Response("assigned template '{}' no longer exists\n".format(device["template_name"]),
                        mimetype="text/plain", status=500)

    try:
        rendered = rendering.render_for_device(conn, device["id"], row["body"])
    except rendering.RenderError as exc:
        return Response("template render failed: {}\n".format(exc), mimetype="text/plain", status=500)
    return Response(rendered, mimetype="text/plain")


# ---------------------------------------------------------------------------
# vCenter import (inventory only, never polled until credentials are set)
# ---------------------------------------------------------------------------

@bp.route("/api/discover/vcenter", methods=["POST"])
def api_discover_vcenter():
    """Body: {"url", "username", "password", "name_filter" (optional)}."""
    data = request.get_json(force=True, silent=True) or {}
    if not all(data.get(k) for k in ("url", "username", "password")):
        return jsonify({"error": "url, username and password are required"}), 400

    conn = db.get_db()
    try:
        device_ids, conflicts = vcenter_collector.discover(
            conn, data["url"].rstrip("/"), data["username"], data["password"],
            name_filter=data.get("name_filter"),
        )
    except Exception as exc:
        return jsonify({"error": "vCenter request failed: {}".format(exc)}), 502
    conn.commit()
    return jsonify({"status": "ok", "device_ids": device_ids, "conflicts": conflicts})
