"""Import the device list from confetti-butler.

GET <butler_url>/api/devices returns butler's inventory. Each device goes through
identity.ingest on its serial, management IP and hostname, so a router already
known here (by hand, a sweep, LLDP) is merged, not duplicated. Only inventory
is copied: no credentials, no config. Butler's role, vendor, platform and site
are kept, so a role='node' device is still never SSH-polled.
"""

import requests

from .. import identity


def discover(conn, butler_url):
    resp = requests.get("{}/api/devices".format(butler_url.rstrip("/")), timeout=10)
    resp.raise_for_status()
    devices = resp.json()

    device_ids = []
    conflicts = []
    for dev in devices:
        candidates = [(kind, dev[kind]) for kind in ("serial", "mgmt_ip", "hostname") if dev.get(kind)]
        if not candidates:
            continue
        fields = {k: dev.get(k) for k in ("hostname", "mgmt_ip", "vendor", "platform", "model", "serial", "role", "site")}
        try:
            device_id = identity.ingest(conn, candidates, fields, source="butler", ref=butler_url)
        except identity.Conflict as exc:
            conflicts.append({"device": dev.get("hostname") or dev.get("mgmt_ip"), "device_ids": exc.device_ids})
            continue
        device_ids.append(device_id)

    return device_ids, conflicts
