---
name: confetti-topo-lab-validation
description: Procedure for validating confetti-topo on the real lab routers via its own lab VM, with cleanup and a HANDOFF record. Use when the user says validate, verify on real hardware, or test on the lab.
origin: confetti-topo
---

# Real-hardware validation

The user prefers real routers over mocks. Real addresses are in the auto-memory file
`reference_lab_routers.md` (in the confetti-butler project's memory folder); never commit them.

1. **Confirm** the topo VM (its own Alpine VM, port 80) and the routers are on. Credentials come from the
   user in chat only.
2. **Update the VM:** copy `topo/` over and run `/usr/local/bin/topo-update.sh -y <dir>` (full path is safest: a non-login
   SSH session may not have `/usr/local/bin` in PATH). The SSH server is OpenSSH since 2026-10-14, so `scp`/`sftp` work;
   a VM still on Dropbear is switched by its first `--update` (snapshot it first). After a fresh build the SSH host
   keys are gone until reboot. A first
   deployment uses `build-template.sh` instead.
3. **Add the devices** through the API, set credentials per device, wait for a poll, read results from the API.
4. **Compare with the router itself** (`show lldp neighbors detail`, `show bgp summary`,
   `show ip ospf neighbor` over SSH), not only with confetti-topo's own view.
5. **Work through `docs/TOPOLOGY-MAPPING.md` section 11**, plus: BGP/OSPF peers on a loopback or link address
   now resolve to the neighbor device (compare `/api/adjacencies` `peer_device_id` with what the router shows);
   a session both routers report is one edge on `/topology`, red and dashed when one end is not
   Established/FULL.
6. **Change a router only with a revert ready** (shut a peer, turn LLDP off, a description line). Always undo it
   and confirm confetti-topo sees the revert.
7. **Clean up:** delete devices and credentials; `VACUUM`; grep the DB and the VM for credential text.
8. **Fix real bugs found** in the repo, add the regress check (skill `constraint-regression`), run
   `python dev/regress.py`.
9. **Record** a dated section in `docs/HANDOFF.md` with placeholders only, then run the `leak-scanner` agent.
10. Say plainly what was not verified.
