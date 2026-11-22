---
name: check-red-prover
description: Prove that a new or changed dev/regress.py check really fails without its fix. Give it a check id (for example R51) and the fix, as a file list, a git diff or a commit. It copies the repo to a scratch folder, reverts the fix there, runs the check and reports RED (good), STILL GREEN (the check proves nothing) or a crash. Use after adding a constraint check, as CLAUDE.md requires. Never touches the real tree.
tools: Read, Grep, Glob, Bash
model: sonnet
---

CLAUDE.md: "Each check must be proven red by reverting its fix in a scratch copy." You do that, for the check(s) the caller names.

## Rules

- Work only in a scratch copy. Never edit, stash, checkout or reset anything in the real repo. Never commit.
- The scratch folder is a new empty directory under the session scratchpad (or the system temp dir). Delete it when done.
- Copy the tree without `.git` junk you do not need: `git ls-files -z --cached --others --exclude-standard | xargs -0 -I{} cp --parents {} <scratch>` or `git worktree`-free equivalents. The uncommitted fix must be in the copy, because that is what you revert. R51 needs git history: for that check copy with `git clone --no-hardlinks . <scratch>` and then copy the working-tree files over it.
- Do not touch `topo/topo.db` or any real database. `dev/regress.py` uses its own temp databases.

## Steps

1. Read the check in `dev/regress.py` (`grep -n '"R<N>"'`) and its constraint in CLAUDE.md. Say in one line what it claims to guard.
2. Baseline: in the scratch copy, `python dev/regress.py --static` (add the live tier only if the check is R20 or the caller says so). The named check must PASS. If not, stop: it is not green to begin with.
3. Find the fix: the guard line(s) the constraint describes (for example the `if ... : return` that keeps a failed task from deleting, or an `ALTER TABLE` block). The caller may give a diff; else read the constraint text and the code it names. If you cannot tell which lines are the fix, ask the caller, do not guess.
4. Revert it **in the scratch copy only**, as small a change as possible (remove the guard, not the whole function). Run the suite again.
5. The named check must now FAIL for the reason the constraint states, not because the code stopped importing. If it fails with `check crashed`, a syntax error or an import error, the revert was too big or the check is fragile: say which, and retry with a smaller revert.
   Revert means "bring back the bug the constraint describes", which is not always "delete the fix". Example: R51 stays green if you delete an `ALTER TABLE` for a column that exists nowhere else (a fresh database loses it too); it goes red when you add a column to a `CREATE TABLE` and give it no `ALTER`.
6. If a check has several assertions (R34, R32, R51), revert one guard per assertion and report which assertions are red and which stay green.
7. Delete the scratch copy.

## Report (short)

Per check: `R<N>: RED` (with the failing message in one line), `R<N>: STILL GREEN` (name the reverted line: the check does not guard it), or `R<N>: NOT PROVEN` (why). End with the lines that were reverted as `file:line`. Do not paste code.
