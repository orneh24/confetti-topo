---
name: ipsec-vpn
description: IPsec VPN show commands on Cisco IOS/IOS-XE and Palo Alto PAN-OS as confetti-topo collects them - exact commands, XML API forms, output shapes, session states, failure answers, what is verified on real devices and what is not, and which commands must never be run because they can print keys. Use when changing the vpn poll task or its parsers, debugging a tunnel that is missing or wrong on the map, or adding VPN support for another platform.
origin: confetti-topo
---

# IPsec VPN collection

Marks used here: **official** = vendor documentation, **third-party** = other people's tools, **seen** = run on our own
devices (IOS-XE 17.3.2 and 17.3.8a, IOS 15.4, PA-VM PAN-OS 11.1). Do not treat an unmarked or official-only fact as
proven for a parser: capture it first (constraint 3).

## Safety
- Read-only: IOS `show`, PAN-OS `<show>` operations over the XML API.
- **Never run or read anything that can print a pre-shared key**: no `show running-config`, `show crypto ... key`,
  `show crypto isakmp key`, PAN-OS config reads (`type=config`, `<show><config>`). Never `clear`, `test`, `debug` or `request`.
- Capture with `python dev/capture_output.py --db topo/topo.db --device <id> --out <folder outside the repo> --cmd "..."`.
  It refuses the commands above and never prints credentials. Raw output holds addresses, SPIs and names: keep it
  out of the repo and sanitise before it becomes a fixture (documentation ranges 192.0.2.0/24, 198.51.100.0/24,
  203.0.113.0/24).

## Cisco IOS / IOS-XE

| Command | Used for | |
|---|---|---|
| `show crypto session detail` | the tunnel list (main command of the `vpn` task) | seen |
| `show crypto ipsec sa` | the transform (algorithms); asked only when there are tunnels, may fail | seen |

- Needs privileged EXEC and a crypto feature set. IOS 15.4 without it answers `% Invalid input` (seen): the task
  is `unsupported` (constraint 43): nothing stored or deleted, not a poll error, asked again after
  `TOPO_UNSUPPORTED_RECHECK_H` hours (24) and on "Poll now". Any other `%` answer fails the task.
- IOS-XE 17.3.8a with no VPN answers **nothing at all** (seen). Empty output = no tunnels. Output without the header
  `Crypto session current status` is not understood and fails the task.
- One block per session: `Interface:`, `Session status:`, `Peer: <ip> port <n> fvrf: .. ivrf: ..`,
  `IKEv1 SA:` / `IKEv2 SA:` / `IKE SA:` (bare when only negotiating state exists) with `local <ip>/<port> remote ..` and
  `Active|Inactive`, then `IPSEC FLOW:` blocks with `Active SAs:` and `#pkts dec'ed` / `#pkts enc'ed`.
- Session status values (official): `UP-ACTIVE`, `UP-IDLE` (IKE up, no IPsec SA), `UP-NO-IKE`, `UP-STANDBY`,
  `DOWN-NEGOTIATING`, `DOWN`. The app calls UP-ACTIVE, UP-NO-IKE and UP-STANDBY up.
- Seen: an IKEv1 static VTI, and `show crypto ikev2 sa detailed` empty on it. **Not seen on our hardware:** a down
  session, IKEv2, an inside VRF, crypto maps. Their fixture (`ios_crypto_session_not_captured.txt`) is written from
  Cisco's documented layout.

Official: [Security Command Reference, show crypto s](https://www.cisco.com/c/en/us/td/docs/ios-xml/ios/security/s1/sec-s1-cr-book/sec-cr-s3.html)
(and [the next part](https://www.cisco.com/c/en/us/td/docs/ios-xml/ios/security/s1/sec-s1-cr-book/sec-cr-s4.html)),
[Virtual Tunnel Interface guide](https://www.cisco.com/c/en/us/td/docs/routers/ios/config/17-x/sec-vpn/b-security-vpn/m_sec-ipsec-virt-tunnl-0.html),
[FlexVPN configure and verify](https://www.cisco.com/c/en/us/support/docs/security/flexvpn/223120-configure-and-verify-flexvpn-solution.html).

## Palo Alto PAN-OS

Palo Alto documents the CLI commands (official) but not their XML forms; it tells you to find them with the CLI's
`debug cli on` or the API browser. The forms below are **seen** on our PA-VM. Third-party projects use the same ones
([checkmk-paloalto-api](https://github.com/cledirjusto/checkmk-paloalto-api),
[PaloAlto-VPN-Monitoring-Template](https://github.com/joseparedespy/PaloAlto-VPN-Monitoring-Template)).

| CLI | XML `cmd` | Gives |
|---|---|---|
| `show vpn flow` | `<show><vpn><flow></flow></vpn></show>` | **main**: `num_ipsec`, `IPSec/entry` (`name`, `id`, `gwid`, `state`, `mon`, `inner-if`, `localip`, `peerip`) |
| `show vpn ike-sa` | `<show><vpn><ike-sa></ike-sa></vpn></show>` | which gateway ids have an IKE SA |
| `show vpn gateway` | `<show><vpn><gateway></gateway></vpn></show>` | IKE version per gateway id (a `v1` or `v2` element) |
| `show vpn ipsec-sa` | `<show><vpn><ipsec-sa></ipsec-sa></vpn></show>` | algorithms per tunnel id |
| `show vpn tunnel` | `<show><vpn><tunnel></tunnel></vpn></show>` | proxy IDs (selectors) per tunnel id |
| `show vpn flow tunnel-id N` | `<show><vpn><flow><tunnel-id>N</tunnel-id></flow></vpn></show>` | packet and byte counters, for at most 50 tunnels (`VPN_DETAIL_MAX`) |

- Only the main command can fail the task. When a helper fails, the field stays empty; an unknown IKE state is `""`,
  never "down".
- `gateway` also lists gateways of remote-access VPN and their peer IDs: never use a real one as a fixture.
- Up = `state` is `active` and the tunnel monitor `mon` is not `down`.
- `pkt-encap` / `byte-encap` are outbound, `pkt-decap` / `byte-decap` inbound.
- **Not seen:** a down tunnel, IKEv2, the shape of the answer on a firewall with no tunnels (the parser accepts
  `num_ipsec` 0), Panorama, other PAN-OS versions.

Official: [find XML API syntax with the CLI](https://docs.paloaltonetworks.com/pan-os/11-1/pan-os-panorama-api/get-started-with-the-pan-os-xml-api/explore-the-api/use-the-cli-to-find-xml-api-syntax),
[VPN troubleshooting from the CLI](https://docs.paloaltonetworks.com/network-security/ipsec-vpn/administration/troubleshooting/troubleshooting-site-to-site-vpn-issues-using-cli),
[KB: VPN status CLI commands](https://knowledgebase.paloaltonetworks.com/articles/en_US/Knowledge/CLI-Commands-to-Status-44-Clea-55917).

## How the app uses it (constraint 40, R58)
- Commands and helpers: `topo/app/platforms.py`. Parsers: `parsers/ios.py` `parse_vpn*`, `parsers/panos.py` `parse_vpn*`.
  Collectors: `collectors/ssh.py` (`_apply_vpn` stores both platforms' rows), `collectors/panos.py` `_run_vpn`.
- Table `vpn_tunnels`, one row per (device, tunnel name, peer address). The peer's device is never stored.
- A parser returns `None` for "not understood" (task fails, rows kept) and `[]` for "no tunnels" (rows cleared).
- Map: `topology._ipsec`. The peer resolves at query time to the one device that holds the address (interface, or a
  management IP held by one device). Both ends make one line, up only if both say up. A peer behind NAT stays unknown.
- Events `vpn_added`, `vpn_state`, `vpn_removed`; none on the first poll.
- No CRYPTO syslog message triggers a poll: none was seen from our devices. Add one only from a real message.

## A tunnel is missing or wrong on the map
1. Device page, VPN tunnels box, or `GET /api/vpn?device_id=N`.
2. No rows: Poll now and read the `vpn` result (device page, Poll results). "not supported" or a `%` text means the
   login or the feature set.
3. Rows but no line: the peer address is on no polled interface (NAT, or the interfaces task failed), or on two devices.
4. Two lines for one tunnel: each end resolves the other to a different device, or only one end resolves.
