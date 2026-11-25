#!/usr/bin/env python3
"""Capture real output of read-only commands from a device in a confetti-topo database, to write a parser against.

    python dev/capture_output.py --db topo/topo.db --device 3 --out <folder outside the repo> \
        --cmd "show crypto session detail" --cmd "show crypto ipsec sa"
    python dev/capture_output.py --db topo/topo.db --device 8 --out <folder> --cmd "<show><vpn><flow/></vpn></show>"

It logs in the way the app does (the same stored credentials, SSH or the PAN-OS XML API) and never prints them.
Only `show` commands (PAN-OS: `<show>` operations) are sent, and none that print a configuration or keys.
The raw output holds real addresses, names and serials: --out must be outside the repository, and anything that
goes into dev/samples/ must be sanitised by hand first.
"""
import argparse
import os
import re
import sqlite3
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
# Words that print a configuration or key material (a pre-shared key must never reach a file).
REFUSED = re.compile(r"\b(run|running|running-config|startup|startup-config|config|configuration|tech-support|"
                     r"key|keys|keyring|keyrings|secret|password)\b", re.I)


def allowed(cmd, xml):
    if REFUSED.search(cmd) or "\n" in cmd or ";" in cmd:
        return False
    return cmd.startswith("<show>") if xml else cmd.startswith("show ")


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--db", required=True)
    ap.add_argument("--device", required=True, type=int)
    ap.add_argument("--out", required=True)
    ap.add_argument("--cmd", required=True, action="append")
    args = ap.parse_args()

    out = os.path.abspath(args.out)
    if out == ROOT or out.startswith(ROOT + os.sep):
        sys.exit("--out must be outside the repository: raw output holds real lab details")

    sys.dont_write_bytecode = True
    sys.path.insert(0, os.path.join(ROOT, "topo"))
    from app import platforms
    from app.collectors import panos, ssh

    conn = sqlite3.connect("file:{}?mode=ro".format(os.path.abspath(args.db).replace(os.sep, "/")), uri=True)
    conn.row_factory = sqlite3.Row
    device = conn.execute("SELECT * FROM devices WHERE id = ?", (args.device,)).fetchone()
    if device is None:
        sys.exit("no device with id {}".format(args.device))
    creds = ssh.resolve_credentials(conn, args.device)
    conn.close()

    transport = platforms.get(device["platform"])["transport"]
    if transport not in ("ssh", "xml_api") or device["role"] == "node":
        sys.exit("only SSH and PAN-OS XML API devices")
    xml = transport == "xml_api"
    bad = [c for c in args.cmd if not allowed(c, xml)]
    if bad:
        sys.exit("refused (not a read-only show command, or it can print keys): {!r}".format(bad))
    os.makedirs(out, exist_ok=True)

    def save(cmd, text):
        name = "d{}_{}.{}".format(args.device, re.sub(r"[^A-Za-z0-9]+", "_", cmd).strip("_")[:80], "xml" if xml else "txt")
        with open(os.path.join(out, name), "w", encoding="utf-8", newline="\n") as f:
            f.write(text.replace("\r\n", "\n"))
        print("{}  {} bytes".format(name, len(text)))

    if xml:
        import requests
        import urllib3
        urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
        with requests.Session() as session:
            try:
                key = panos._keygen(session, device["mgmt_ip"], creds)
            except Exception as exc:
                sys.exit("login failed: " + panos._error_text(exc))
            for cmd in args.cmd:
                try:
                    save(cmd, panos._op(session, device["mgmt_ip"], key, cmd))
                except Exception as exc:
                    print("FAILED {!r}: {}".format(cmd, str(exc)[:200]))
        return
    from netmiko import ConnectHandler
    try:
        link = ConnectHandler(device_type=device["platform"] or "cisco_ios", host=device["mgmt_ip"],
                              username=creds["username"], password=creds["password"], secret=creds["secret"] or "",
                              timeout=ssh.CONNECT_TIMEOUT_S)
        if creds["secret"] and not link.check_enable_mode():
            link.enable()
    except Exception as exc:
        sys.exit("login failed: " + ssh._error_text(exc))
    try:
        for cmd in args.cmd:
            try:
                save(cmd, link.send_command(cmd, read_timeout=60))
            except Exception as exc:
                print("FAILED {!r}: {}".format(cmd, str(exc)[:200]))
    finally:
        link.disconnect()


if __name__ == "__main__":
    main()
