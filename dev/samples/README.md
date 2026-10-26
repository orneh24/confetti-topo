# Parser samples

Used by `dev/regress.py`.

- `ios_bgp_summary_15_4.txt` has the **real format** of `show bgp summary` from an IOS-XE 15.4 router (an
  administratively shut neighbor prints as `Idle (Admin)`); the addresses were replaced with documentation
  ranges.

- `panos_*.xml` are the **real answers** of the PAN-OS XML API (`type=op`, one file per command) from a PA-VM on
  PAN-OS 11.1.10-h1: version, interfaces, LLDP, BGP peers, BGP summary (the router's own AS) and OSPF neighbors.
  Hostnames, addresses, MACs, serial and UUID were replaced with placeholders. See the `panos-xml-api` skill.

Add a fixture for every parser bug found on a real device (LLDP detail without `Local Intf:`, OSPF, BGP),
sanitised with documentation addresses only (192.0.2.0/24, 198.51.100.0/24, 203.0.113.0/24, 2001:db8::/32).
