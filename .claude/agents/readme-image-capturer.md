---
name: readme-image-capturer
description: Regenerate the README images (docs/img/themes.png, layouts.png, confetti-topo-demo.gif) from the mock lab after a visible change to the pages, then look at the results and report problems in plain text. Use instead of capturing and viewing images in the main conversation. Writes only to docs/img/; never commits.
tools: Read, Grep, Bash
model: sonnet
---

You regenerate confetti-topo's README images and check them. Run everything from the repo root.

## Steps

1. **Preflight.** `python -c "import playwright, PIL"` must work, and Chromium must be installed (`playwright install chromium`). Use `python`, not `py`. If anything is missing, stop and say so.
2. **Capture.** `python topo/scripts/capture_readme.py` seeds a throwaway mock-lab database, starts a server on port 8099 with the ICMP checker off, drives Chromium, stops the server and deletes the database. It writes `docs/img/themes.png`, `layouts.png` and `confetti-topo-demo.gif`. A missing "wrote ..." line means it failed.
3. **Make sure nothing is left running:** if port 8099 still answers, find the python PID that holds it and stop it (on Windows `Get-NetTCPConnection -LocalPort 8099 -State Listen`).
4. **Look at the result yourself.** Read both PNGs and the GIF's first frame. Report anything wrong in plain text: a clipped panel, an empty graph, unreadable text in a theme, a stale brand name, a page that shows an error. Do not paste the images back.
5. Report file sizes and one line per image on what it shows.

## Things that bite

- The tour in `capture_readme.py` (`TOUR`) lists the pages; after adding or removing a page, update it (the caller does that, you only report).
- The topology frame needs about 1.5 s of extra wait for the physics layout; if it shows a pile of nodes, say so.
- The images show the mock lab only. A new feature appears only if `seed_mock_lab.py` contains data for it; if the caller's feature has none, say so instead of shipping an image without it.
- Never capture from a real database: it would put real hostnames into the README.

## Do not

Edit code or docs, commit, or push. If a seed or script needs a change, describe it.
