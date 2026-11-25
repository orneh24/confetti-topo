"""Plain-English reasons for a failed poll (constraint 38).

explain(exc, via, task, timeout) turns what a collector caught into ONE line for an operator: what failed, then what to
check ("SSH login rejected: wrong username or password. Check ..."). short() is the first sentence, for an event's subject.
Netmiko, paramiko, requests and net-snmp each say it their own way (and Netmiko puts every socket error behind one
"TCP connection ... failed" timeout), so the OS error is looked for in the exception chain and named.

A message never carries a password, key or token: the login errors are fixed texts and the library's own text is only used
for negotiation and unknown errors, one line, cut short. Pure functions, no I/O.
"""
import errno
import socket

import requests
from netmiko.exceptions import NetmikoAuthenticationException, NetmikoTimeoutException, ReadTimeout

MAX_DETAIL = 140
# A task error that starts like this is not a failure: the device does not have the command (poller.py, task_status).
UNSUPPORTED = "not supported: "
# errno values by name differ per OS (Linux 111, Windows 10061), so both are listed.
_REFUSED = {errno.ECONNREFUSED, 10061}
_NO_ROUTE = {errno.EHOSTUNREACH, errno.ENETUNREACH, 10065, 10051}
_RESET = {errno.ECONNRESET, 10054}
_LABEL = {"ssh": "SSH (port 22)", "https": "HTTPS (port 443)", "snmp": "SNMP (UDP 161)", "hub": "the Confetti Traffic hub"}


def _one_line(text, limit=MAX_DETAIL):
    text = " ".join(str(text).split())
    return text if len(text) <= limit else text[:limit - 3] + "..."


def _chain(exc):
    """The exception and what caused it, each once."""
    seen = []
    while exc is not None and not any(exc is s for s in seen) and len(seen) < 8:
        seen.append(exc)
        exc = exc.__cause__ or exc.__context__
    return seen


def _os_cause(exc):
    """'refused', 'timeout', 'dns', 'noroute', 'reset' or None: the socket-level reason somewhere in the chain."""
    for e in _chain(exc):
        code = getattr(e, "errno", None)
        if isinstance(e, ConnectionRefusedError) or code in _REFUSED:
            return "refused"
        if isinstance(e, socket.gaierror):
            return "dns"
        if code in _NO_ROUTE:
            return "noroute"
        if isinstance(e, ConnectionResetError) or code in _RESET:
            return "reset"
        if isinstance(e, (socket.timeout, TimeoutError, requests.exceptions.Timeout)):
            return "timeout"
    return None


def _sentence(text, hint):
    return "{}. {}".format(text, hint) if hint else text


def short(message):
    """The reason without its advice: what goes in an event's subject."""
    return (message or "").split(". ", 1)[0].rstrip(".")


def unsupported(command, why):
    """The task error for a device that does not know a command."""
    return _one_line("{}the device does not know '{}' ({})".format(UNSUPPORTED, command, why), 200)


def explain(exc, via="ssh", task=None, timeout=None):
    """One line for a failed poll task. via is 'ssh', 'https' (PAN-OS) or 'snmp'; task names the command that failed;
    timeout is the connect timeout in seconds, when known."""
    text = str(exc)
    kind = type(exc).__name__
    label = _LABEL.get(via, via)

    # A login that was refused. The library's own text names the device and may quote what was sent: never used.
    if isinstance(exc, NetmikoAuthenticationException):
        return _sentence("SSH login rejected: wrong username or password",
                         "Check this device's credentials, or the defaults on the Settings page")
    if kind == "ApiError":   # collectors/panos.py: the firewall answered, but not with success
        if text == "authentication failed":
            return _sentence("API login rejected: wrong username or password, or the account has no XML API role",
                             "Check this device's credentials, or the defaults on the Settings page")
        return _one_line("firewall answered: " + text)
    if kind == "HubError":   # collectors/hub.py: the hub answered with something a node poll cannot use
        return _one_line(text)
    if kind == "SnmpError":   # collectors/snmp.py
        low = text.lower()
        if "timeout" in low or "timed out" in low or "no response" in low:
            return _sentence("no SNMP answer",
                             "The community may be wrong, SNMP may be off on the device, or UDP 161 is blocked")
        if "authorization" in low or "unknown user" in low or "authentication" in low:
            return _sentence("SNMP request refused", "Check the community string and that this server may query the device")
        return _one_line("SNMP: " + text)
    if isinstance(exc, requests.exceptions.SSLError):
        return _sentence("TLS error talking to the firewall", "A self-signed certificate? See TOPO_PANOS_VERIFY_TLS")

    # A command that did not finish is not a connection problem (Netmiko's ReadTimeout is not a NetmikoTimeoutException).
    if isinstance(exc, ReadTimeout):
        return _sentence("the command for {} did not finish in time".format(task) if task else "a command did not finish in time",
                         "The device may be slow, or its prompt was not recognised")

    # Could not connect, or lost the connection: say which.
    cause = _os_cause(exc)
    low = text.lower()
    if cause == "refused":
        return _sentence("{} refused the connection".format(label),
                         "The service may be off on the device, or this server is not allowed to reach it")
    if cause == "timeout":
        return _sentence("no answer on {}{}".format(label, " within {} s".format(timeout) if timeout else ""),
                         "The device may be down, the IP wrong, or a firewall drops the traffic")
    if cause == "dns" or "dns failure" in low:
        return _sentence("the device name does not resolve", "Use its IP address as the management address")
    if cause == "noroute":
        return _sentence("no route to the device", "Check the management IP and this server's network")
    if cause == "reset":
        return _sentence("the device closed the connection", "Too many SSH sessions, or an ACL or login limit on the device")
    if via == "hub" and isinstance(exc, requests.exceptions.ConnectionError):
        return _sentence("could not connect to the Confetti Traffic hub", "Check that the hub is running and its address is right")
    if via == "https" and isinstance(exc, requests.exceptions.ConnectionError):
        return _sentence("could not connect to HTTPS (port 443)", "The management web interface may be off, or the IP is wrong")

    if "no existing session" in low:
        return _sentence("the SSH session did not start in time", "The device may be busy or slow to answer")
    if "paramiko sshexception" in low or kind == "SSHException":
        lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
        return _sentence("SSH negotiation failed: " + _one_line(lines[-1] if lines else text, 100),
                         "An old IOS may need a legacy key exchange or cipher enabled on this server's SSH client")
    if isinstance(exc, NetmikoTimeoutException) or "tcp connection to device failed" in low:
        return _sentence("could not connect to {}".format(label), "Check the management IP, the port and any firewall")
    if "failed to enter enable mode" in low:
        return _sentence("enable mode refused", "The enable secret is wrong or missing")
    if kind == "RuntimeError" and low.startswith("device answered:"):
        return _one_line("the device refused the command: " + text[len("device answered:"):])
    if "not a complete running-config" in low:
        return _sentence("the running-config read was cut short", "It was not stored; the next poll tries again")

    return _one_line("{}: {}".format(kind, text.strip().splitlines()[0] if text.strip() else "no detail"))
