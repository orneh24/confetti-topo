# Parser samples

Used by `dev/regress.py`.

- `ios_bgp_summary_15_4.txt` has the **real format** of `show bgp summary` from an IOS-XE 15.4 router (an
  administratively shut neighbor prints as `Idle (Admin)`); the addresses were replaced with documentation
  ranges.

- `panos_*.xml` are the **real answers** of the PAN-OS XML API (`type=op`, one file per command) from a PA-VM on
  PAN-OS 11.1.10-h1: version, interfaces, LLDP, BGP peers, BGP summary (the router's own AS) and OSPF neighbors.
  Hostnames, addresses, MACs, serial and UUID were replaced with placeholders. See the `panos-xml-api` skill.

- `ios_route_17_3.txt` has the **real format** of `show ip route vrf *` from IOS-XE 17.3 (global table, then
  `Routing Table: MGMT`; a route under `is subnetted` with no mask; `O E2` with age and interface; a BGP route
  with no interface). `panos_route.xml` and `panos_fib.xml` have the real shape of the PAN-OS RIB and FIB answers
  (two virtual routers; the multicast and IPv6 entries are there to be skipped). All addresses are documentation
  ranges. `ios_route_not_captured.txt` (ECMP continuation line, long prefix on its own line, `is a summary`) was
  **written from IOS's documented format, not captured**: no lab router had equal-cost paths. Replace it with a real
  capture when one has.

Add a fixture for every parser bug found on a real device (LLDP detail without `Local Intf:`, OSPF, BGP),
sanitised with documentation addresses only (192.0.2.0/24, 198.51.100.0/24, 203.0.113.0/24, 2001:db8::/32).
