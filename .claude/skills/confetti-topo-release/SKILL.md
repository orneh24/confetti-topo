---
name: confetti-topo-release
description: Checklist to run before handing back or committing confetti-topo changes - regression gate, docs drift, leak scan, commit. Use when finishing a change or when asked to commit.
origin: confetti-topo
---

# Before handing back or committing

1. `python dev/regress.py` (or the `regression-gate` agent). Must be CLEAR; say so if CLEAR WITH GAPS.
2. Code change: if it fixes a silent bug, add the constraint, site comment and check (skill
   `constraint-regression`) and prove the check red in a scratch copy (agent `check-red-prover`). Take the next
   free constraint number and check id from CLAUDE.md's "Regression gate" section (it names both). A new task: skill `confetti-topo-add-task`. A new DB column: `ALTER TABLE` in
   `db.init_db` (constraint 34, R51).
3. Docs: update CLAUDE.md, README, the `confetti-topo-api` skill and HANDOFF for what changed, then run the
   `docs-drift-checker` agent. Touched themes, the pickers or the confetti-traffic import? Also run
   `confetti-interop-checker`.
4. UI change: run `dashboard-ui-checker`, then re-record `docs/img/` with `scripts/capture_readme.py`
   (`readme-image-capturer` agent). It shows the mock lab only: seed data for a new feature in
   `scripts/seed_mock_lab.py` first.
5. Run the `leak-scanner` agent. After a capture from real devices, also search the diff, the new fixtures and the
   images for every real name and address in the raw captures.
6. Commit **only when the user asks**. `origin` is GitHub; push only when the user asks. The message ends with the
   attribution line from the session and names new check ids.
7. Report in short, simple English. No large code in the reply.
