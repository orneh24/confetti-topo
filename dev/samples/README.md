# Parser samples

Used by `dev/regress.py`.

- `ios_bgp_summary_15_4.txt` has the **real format** of `show bgp summary` from an IOS-XE 15.4 router (an
  administratively shut neighbor prints as `Idle (Admin)`); the addresses were replaced with documentation
  ranges.

- `panos_*.xml` are the **real answers** of the PAN-OS XML API (`type=op`, one file per command) from a PA-VM on
  PAN-OS 11.1.10-h1: version, interfaces, LLDP, BGP peers, BGP summary (the router's own AS) and OSPF neighbors.
  Hostnames, addresses, MACs, serial and UUID were replaced with placeholders. See the `panos-xml-api` skill.

- `ios_vlan_brief.txt` has the **real format** of `show vlan brief` from a Catalyst 3560 (VLAN 1 with its port list, an
  empty port column, `act/unsup` rows); names were replaced with neutral ones. The continuation line for a long port
  list is **not** in a real capture (the lab switch's list fits on one line); `regress.py` R34 adds one in the format
  IOS documents.

- `ios_trunk.txt` has the **real format** of `show interfaces trunk` from a Catalyst 3560 (three trunk ports, the four
  blocks; the VLAN lists were shortened). A VLAN list that wraps onto an indented line is **not** in a real capture;
  `regress.py` R34 checks it with one written from the documented format.

- `ios_stp_summary.txt` has the **real format** of `show spanning-tree summary` from a Catalyst 3560 (the "Root bridge for"
  list running over an indented line, the per-VLAN table). The lists were shortened and one VLAN was given a blocking
  port by hand, since the lab switch has none.

- `ios_route_17_3.txt` has the **real format** of `show ip route vrf *` from IOS-XE 17.3 (global table, then
  `Routing Table: MGMT`; a route under `is subnetted` with no mask; `O E2` with age and interface; a BGP route
  with no interface). `panos_route.xml` and `panos_fib.xml` have the real shape of the PAN-OS RIB and FIB answers
  (two virtual routers; the multicast and IPv6 entries are there to be skipped). All addresses are documentation
  ranges. `ios_route_not_captured.txt` (ECMP continuation line, long prefix on its own line, `is a summary`) was
  **written from IOS's documented format, not captured**: no lab router had equal-cost paths. Replace it with a real
  capture when one has.

Add a fixture for every parser bug found on a real device (LLDP detail without `Local Intf:`, OSPF, BGP),
sanitised with documentation addresses only (192.0.2.0/24, 198.51.100.0/24, 203.0.113.0/24, 2001:db8::/32).

- `ios_mac_table.txt` has the **real format** of `show mac address-table` from a Catalyst 3560. The MAC addresses were replaced
  with made-up ones and the list was cut to 3 static and 14 dynamic rows.
