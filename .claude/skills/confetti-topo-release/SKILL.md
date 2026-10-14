---
name: confetti-topo-release
description: Checklist to run before handing back or committing confetti-topo changes - regression gate, docs drift, leak scan, commit. Use when finishing a change or when asked to commit.
origin: confetti-topo
---

# Before handing back or committing

1. `python dev/regress.py` (or the `regression-gate` agent). Must be CLEAR; say so if CLEAR WITH GAPS.
2. Code change: if it fixes a silent bug, add the constraint, site comment and check (skill
   `constraint-regression`) and prove the check red in a scratch copy.
3. Docs: update CLAUDE.md, README, the `confetti-topo-api` skill and HANDOFF for what changed, then run the
   `docs-drift-checker` agent. Touched themes, the pickers or the confetti-traffic import? Also run
   `confetti-interop-checker`.
4. UI change: run `dashboard-ui-checker`, then re-record `docs/img/` with `scripts/capture_readme.py`
   (`readme-image-capturer` agent).
5. Run the `leak-scanner` agent.
6. Commit **only when the user asks**. There is no remote yet; do not push. The message ends with the
   attribution line from the session and names new check ids.
7. Report in short, simple English. No large code in the reply.
