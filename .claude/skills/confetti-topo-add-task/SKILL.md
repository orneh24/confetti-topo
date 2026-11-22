---
name: confetti-topo-add-task
description: Add a new poll task to confetti-topo (a new show command whose output is stored, for example another switch table) - registry, parser, apply step, schema, events, API, device page, fixture, regression checks, docs. Use when asked to poll or store something new from a device. A new vendor is the add-platform skill.
origin: confetti-topo
---

# Adding a poll task

Model: the `stp` task (commit "Spanning tree summary on switches"). Read `_apply_stp` in `topo/app/collectors/ssh.py` first. Names below are
the ones to grep for, not line numbers.

1. **Real output first** (constraint 3). Capture the command from a real device into `dev/samples/<platform>_<task>.txt`, placeholders only
   (`dev/samples/README.md`). Do not write a parser from documentation.
2. **Registry** `topo/app/platforms.py`: the command in `IOS["commands"]`, the parser in `IOS["parsers"]`. A switch-only task also goes in
   `SWITCH_TASKS` (the same file); then `ssh.run_tasks` skips it for a router. `ssh.TASKS` follows from the registry. PAN-OS and SNMP keep
   their own task lists; do not add the task there unless the platform has it.
3. **Parser** `topo/app/parsers/ios.py`, pure regex. An error text (`%`, `Invalid input`) must parse to an empty result.
4. **Apply step** `ssh.py`: a branch in `apply_result` and a `_apply_<task>(conn, device_id, parsed, now)`.
   - Delete rows not seen **only on a successful answer** (constraint 14). Decide what an empty parse means: a parse failure (delete nothing, as
     `interfaces`, `vlans`, `mac`) or a real answer (clear, as `lldp`, `trunks`).
   - `_is_cli_error` already fails a `SWITCH_TASKS` task on `%`; a task outside that list needs its own check.
   - Events: `events.emit` inside the same transaction, never commit, and guard with `events.is_baseline(conn, device_id, "<task>")`
     (constraint 11). Add the kind to `events.describe` (type / change / item / before / after) or the Recent changes page shows it raw.
5. **Schema** `topo/app/db.py`: `CREATE TABLE IF NOT EXISTS` in the script, **and** an `ALTER TABLE ... ADD COLUMN` block for any column added to
   an existing table (constraint 34, R51). A `NOT NULL` column needs a default. Give `device_id` `REFERENCES devices(id) ON DELETE CASCADE`
   (as `stp` does): a device delete and a merge then clean it up. `identity.merge` copies only the tables in its list; poll-derived tables
   like `stp` are not in it, and the survivor's next poll refills them. Add a table there only if it holds operator data.
6. **API and page**: `GET /api/<name>?device_id=N` in `app/app.py` (`/api/stp` is the shape), a box on `templates/device.html` hidden until it has
   rows. Read `confetti-topo-ui` before editing the template. If a value is shown from a secret-ish field, mask it (constraint 31 style).
7. **Mock lab**: add rows to `topo/scripts/seed_mock_lab.py` or the new box has no data in the README images.
8. **Regression** `dev/regress.py`:
   - **R15** compares CLAUDE.md's "Per device, N tasks" and "All N tasks" sentences to `len(ssh.TASKS)` and wants each task name in backticks in
     the per-device sentence. Its number-word table (`words`) stops at "twelve": a 13th task needs "thirteen" added there, or R15 fails for no
     obvious reason.
   - **R19** and **R34** hard-code the IOS task list and the switch-only list; extend them. R34 is the place for fixture parsing, "empty answer
     deletes nothing" and "a `%` answer fails the task".
   - Prove each new assertion red (agent `check-red-prover`).
9. **Docs**: CLAUDE.md (the poller section's count and list, plus a short section like "VLANs, trunks and spanning tree"), README, the
   `confetti-topo-api` skill (route list, task list), `docs/TOPOLOGY-MAPPING.md`, and `dev/samples/README.md` for the fixture. Then agent
   `docs-drift-checker`.
10. **Validate on a real device** (agent `lab-validator`); until then say "not verified on real hardware".
11. Run the `confetti-topo-release` checklist.
