"""Per-platform commands and parsers.

A device's `platform` column picks its entry here. The poller itself does not
care: every platform produces the same parsed shapes for the same task names
(version, interfaces, lldp, bgp, ospf), so the apply steps, events and
topology work unchanged for all of them. Adding a vendor means adding an entry
here plus its parsers (Junos is planned, see docs/ROADMAP.md).

Status of each platform:
  cisco_ios / cisco_xe  verified against real IOS-XE 17.3 and 15.4 hardware
  paloalto_panos        XML API (transport "xml_api"); fixtures captured from a real PA-VM, PAN-OS 11.1

An unknown platform string falls back to the IOS entry (netmiko is still given
the platform string as its device_type).
"""

from .parsers import ios
from .parsers import panos

IOS = {
    "transport": "ssh",
    "commands": {
        "version": "show version",
        # Addresses only: the topology resolves a BGP/OSPF peer IP against them.
        "interfaces": "show interfaces",
        "lldp": "show lldp neighbors detail",
        "bgp": "show bgp summary",
        "ospf": "show ip ospf neighbor",
    },
    "parsers": {
        "version": ios.parse_version,
        "interfaces": ios.parse_interfaces,
        "lldp": ios.parse_lldp_neighbors,
        "bgp": ios.parse_bgp_summary,
        "ospf": ios.parse_ospf_neighbors,
    },
    # Older IOS leaves "Local Intf" out of the detail output; this table has it.
    "lldp_brief_command": "show lldp neighbors",
}

# Palo Alto PAN-OS over the XML API (collectors/panos.py): the commands are XML operations, read-only
# `<show>` ones only. BGP needs two: the peer list, and the summary that holds the router's own AS.
PANOS = {
    "transport": "xml_api",
    "commands": {
        "version": "<show><system><info/></system></show>",
        "interfaces": "<show><interface>all</interface></show>",
        "lldp": "<show><lldp><neighbors>all</neighbors></lldp></show>",
        "bgp": "<show><routing><protocol><bgp><peer/></bgp></protocol></routing></show>",
        "ospf": "<show><routing><protocol><ospf><neighbor/></ospf></protocol></routing></show>",
    },
    "parsers": {
        "version": panos.parse_version,
        "interfaces": panos.parse_interfaces,
        "lldp": panos.parse_lldp_neighbors,
        "bgp": panos.parse_bgp_peers,
        "ospf": panos.parse_ospf_neighbors,
    },
    "bgp_summary_command": "<show><routing><protocol><bgp><summary/></bgp></protocol></routing></show>",
}

PLATFORMS = {
    "cisco_ios": IOS,
    "cisco_xe": IOS,
    "paloalto_panos": PANOS,
}


def get(platform):
    return PLATFORMS.get(platform or "cisco_ios", IOS)


def tasks(plat):
    return tuple(plat["commands"])
