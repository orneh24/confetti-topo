"""PAN-OS XML API collector: read-only 'show' operations over HTTPS.

Same job and same yield shape as collectors/ssh.py, so poller.py treats both alike. Read-only like SSH:
the only request types sent are `keygen` and `op` with a `<show>` command (R30 fails on anything else).
The password goes in the POST body, the API key in the X-PAN-KEY header: neither is ever in a URL, and
neither is stored or put in an error text.

Every failure mode is caught and reported per task (a failed login fails every task, a failed call only
its task), so one dead firewall never stops the poller.
"""

import re
import xml.etree.ElementTree as ET

import requests
import urllib3

from .. import config
from .. import platforms
from ..parsers import panos as parser

_HOST_OK = re.compile(r"[A-Za-z0-9.-]+")   # a management address or hostname, nothing that could change the URL


class ApiError(Exception):
    """The firewall answered, but not with success."""


def _error_text(exc):
    if isinstance(exc, requests.exceptions.SSLError):
        return "TLS error (self-signed certificate? see TOPO_PANOS_VERIFY_TLS)"
    if isinstance(exc, requests.exceptions.Timeout):
        return "connection timed out"
    if isinstance(exc, requests.exceptions.ConnectionError):
        return "connection failed"
    if isinstance(exc, ApiError):
        return str(exc)
    return "{}: {}".format(type(exc).__name__, exc)


def _call(session, host, data, key=None):
    """POST one request to /api/ and return the <response> element; ApiError unless status is success."""
    r = session.post("https://{}/api/".format(host), data=data, headers={"X-PAN-KEY": key} if key else None,
                     verify=config.PANOS_VERIFY_TLS, timeout=config.PANOS_TIMEOUT_S)
    try:
        root = ET.fromstring(r.text)
    except ET.ParseError:
        raise ApiError("not an XML API answer (HTTP {})".format(r.status_code))
    if root.get("status") != "success":
        msg = " ".join((root.findtext(".//msg") or root.findtext(".//line") or "").split())
        raise ApiError(msg[:200] or "API error (HTTP {})".format(r.status_code))
    return root, r.text


def _op(session, host, key, cmd):
    """The one place an operational command is sent. Only <show> commands: no config, commit or import."""
    if not cmd.startswith("<show>"):
        raise ValueError("only <show> commands are allowed: {!r}".format(cmd[:40]))
    return _call(session, host, {"type": "op", "cmd": cmd}, key)[1]


def _keygen(session, host, creds):
    root, _ = _call(session, host, {"type": "keygen", "user": creds["username"], "password": creds["password"]})
    key = root.findtext(".//key")
    if not key:
        raise ApiError("authentication failed")
    return key


def _run_command(session, host, key, task, plat):
    output = _op(session, host, key, plat["commands"][task])
    if task == "routes":
        # The RIB is the answer; without the FIB (a role that may not run it) the RIB is still worth having.
        try:
            fib = _op(session, host, key, plat["fib_command"])
        except Exception:
            fib = ""
        return output + "\n\n" + fib, plat["parsers"]["routes"](output) + parser.parse_fib(fib)
    if task != "bgp":
        return output, plat["parsers"][task](output)
    # The peer list has no local AS; the summary has it per virtual router. Without the summary (a role
    # that may not run it) the peers are still worth having: local_as stays empty.
    try:
        summary = _op(session, host, key, plat["bgp_summary_command"])
    except Exception:
        summary = ""
    peers = parser.fill_bgp_local_as(plat["parsers"]["bgp"](output), parser.parse_bgp_local_as(summary))
    return output + "\n\n" + summary, peers


def run_tasks(device, creds, tasks=None):
    """Log in once, then run each task's show operation in turn. A generator, like ssh.run_tasks:
    yields (task, ok, raw_output, error, parsed) so the caller commits each task before the next runs.

    ok is True whenever a successful response came back, even an empty one ("BGP not configured" is a
    normal state)."""
    plat = platforms.PANOS
    tasks = tasks or platforms.tasks(plat)

    host = device.get("mgmt_ip")
    if not host or not _HOST_OK.fullmatch(host):
        for task in tasks:
            yield task, False, None, "device has no usable mgmt_ip", None
        return

    if not config.PANOS_VERIFY_TLS:
        urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

    with requests.Session() as session:
        try:
            key = _keygen(session, host, creds)
        except Exception as exc:
            for task in tasks:
                yield task, False, None, _error_text(exc), None
            return
        for task in tasks:
            try:
                output, parsed = _run_command(session, host, key, task, plat)
            except Exception as exc:
                yield task, False, None, _error_text(exc), None
            else:
                yield task, True, output, None, parsed
