---
name: panos-xml-api
description: Palo Alto PAN-OS XML API as confetti-topo uses it - login (keygen), read-only op show commands for version, interfaces, LLDP, BGP and OSPF, response shapes, quirks found on a real firewall, how to capture and sanitise samples. Use when changing app/collectors/panos.py or app/parsers/panos.py, polling a firewall, or debugging a PAN-OS poll.
origin: confetti-topo
---

# PAN-OS XML API

Everything marked **seen** was run against a real PA-VM on PAN-OS 11.1.10-h1 (fixtures: `dev/samples/panos_*.xml`).
Anything else is from memory of the PAN-OS docs: check it on the device before relying on it (constraint 3).

## Rules (constraint 15, R30)
- **Read-only.** Only `type=keygen` and `type=op` with a `<show>...</show>` command. Never `config`, `commit`, `import`,
  `export`, `user-id` or an op that is not a `show`. `collectors/panos.py` `_op()` is the one place a command is sent
  and refuses anything else.
- **No secret in a URL.** The password goes in the POST body, the API key in the `X-PAN-KEY` header. URLs end up in
  firewall and proxy logs. Neither is stored, logged or put in an error text.
- Credentials: the existing order (device row > Settings page > `TOPO_SSH_*` env); only username and password are used.
  The account needs XML API access to the `show` operations; a dedicated account worked (seen). Give it no more rights than that.
- TLS: a firewall's management certificate is self-signed, so `TOPO_PANOS_VERIFY_TLS` defaults to false.

## Requests (seen)
- Login: `POST https://<host>/api/` body `type=keygen&user=<u>&password=<p>` ->
  `<response status="success"><result><key>...</key></result></response>`. One keygen per poll.
- Operation: `POST https://<host>/api/` body `type=op&cmd=<xml>`, header `X-PAN-KEY: <key>` ->
  `<response status="success"><result>...</result></response>`.
- A failure is `status="error"` with a message under `<msg>` (not captured live, so the exact shape is unverified;
  a deliberately wrong password was not tried, to avoid locking the account).
- An answer with nothing to report is a success with an empty `<result></result>` (seen: a virtual router with no BGP).
  It is a normal answer: the task is ok and the apply step deletes the rows that are gone.

## Commands (`platforms.PANOS`)
| Task | `cmd` | Where the data is |
|---|---|---|
| version | `<show><system><info/></system></show>` | `result/system`: `hostname`, `model`, `serial`, `sw-version`, `mac-address` (stored as `devices.mgmt_mac`, used by the optional LLDP chassis match) |
| interfaces | `<show><interface>all</interface></show>` | `result/ifnet/entry` (`name`, `ip` as `a.b.c.d/n` or `N/A`, `zone`); `result/hw/entry` (`state`, `speed`, `duplex`) |
| lldp | `<show><lldp><neighbors>all</neighbors></lldp></show>` | `result/entry[@name=<local port>]/neighbors/entry` |
| bgp | `<show><routing><protocol><bgp><peer/></bgp></protocol></routing></show>` | `result/entry[@peer][@vr]` |
| bgp (own AS) | `<show><routing><protocol><bgp><summary/></bgp></protocol></routing></show>` | `result/entry[@virtual-router]/local-as` |
| ospf | `<show><routing><protocol><ospf><neighbor/></ospf></protocol></routing></show>` | `result/entry`: `neighbor-address`, `status`, `area-id` |

The bgp task makes both calls. The peer list has no local AS; the summary does, per virtual router. A failed summary
still returns the peers, with `local_as` empty. `<virtual-router>name</virtual-router>` inside `summary`, `peer` and
`neighbor` is accepted (seen) but not used: only the default listing is polled.

## Quirks (seen)
- BGP `peer-address` and `local-address` carry `:port` on an established session (`192.0.2.7:179`); strip it.
- BGP `status` is `Established`, `Connect`, ... as the firewall spells it; `status-duration` is seconds (0 when not up).
  Prefixes received are `prefix-counter/entry/incoming-total`.
- OSPF `status` is lower case (`full`); the parser upper-cases it so it reads like IOS (`FULL`).
- LLDP: the remote port is `port-id` (what the neighbor's own row calls it, e.g. `Gi0/7`). `port-description` is free
  text (`uplink to esx`); it is not the remote port, but is kept as `remote_port_descr` and shown by the map when the
  `port-id` is only a MAC (a Linux host's `port-id` is its MAC).
  `management-address` lists IPv4 and IPv6; the first IPv4 is used. `system-description` can be several lines: the first is kept.
- Interfaces: `hw` has link state only for physical ports. A sub-interface, loopback or tunnel has an address in `ifnet`
  but no state, so `oper_status` is left empty rather than guessed. `admin_status` is not reported. The zone is kept as the description.
- Several virtual routers exist (`fwd` is `vr:<name>`). `adjacencies` is unique per device, protocol and peer address, so
  the same peer address in two routers keeps the first row.

## Known limits
- IPv4 only. No Panorama, no HA state, no vsys selection, no config or commit.
- LLDP rows are unique per local port and remote port name: two neighbors that both call their port `Gi2` on the same
  firewall port overwrite each other (seen in the lab: two routers on one port). The schema is shared with SSH devices.

## Adding or changing a parser
1. Capture first. A throwaway script outside the repo: keygen, then the `<show>` command, save the raw answer. Read the
   credentials from the environment, never from a file or the command history you commit.
2. Sanitise into `dev/samples/panos_<task>.xml`: replace hostnames, addresses (use 192.0.2.0/24, 198.51.100.0/24,
   203.0.113.0/24), MACs (also the plain-hex form inside LLDP entry names), serial, `vm-uuid`, `vm-cpuid`. Search the result
   for every original value before it goes in the repo (the leak-scanner agent also does).
3. Parsers use `xml.etree.ElementTree` (no regex, no new dependency) and fail soft: junk, an error response or an empty
   result gives an empty answer, never an exception. Same row shapes as `parsers/ios.py`.
4. Extend R30 in `dev/regress.py`, and prove the new assertion fails with the parser change reverted in a scratch copy.
5. Validate on the firewall with the `lab-validator` agent; the credentials come from the user in chat.

See also `confetti-topo-add-platform` (the registry and the checklist for any platform) and `confetti-topo-api`.
