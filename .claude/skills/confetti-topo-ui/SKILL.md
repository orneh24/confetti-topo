---
name: confetti-topo-ui
description: Authoring checklist for changes to confetti-topo's pages (topo/templates/, static/theme.css, layout.css, theme.js, the vis-network graph) — what must stay shared, how colours reach the canvas, how the graph refreshes. Read before editing anything under topo/templates/ or topo/static/, not after.
origin: confetti-topo
---

# Pages: authoring checklist

`dashboard-ui-checker` looks at a finished change. This is what to get right while writing it. The same
stack as confetti-traffic's hub and confetti-butler: Flask + Jinja2, vanilla JS, no build step, nothing from a
CDN.

## Shared structure
- Every page `{% extends "base.html" %}` and holds only its own content, CSS and JS. The header, nav list,
  theme and layout pickers, footer and Retro 95 taskbar live in `base.html` once. A new page = one entry in
  its `pages` list plus a route.
- Colours live **only** in `static/theme.css` (a dark `:root` plus one `:root[data-theme=NAME]` block per
  theme). A page must not define its own `:root` colours; use the CSS variables. A new theme = one block in
  `theme.css` + one entry in the `THEMES` list in `theme.js`.
- `static/layout.css` carries the structure and the five layouts (Classic, Modern, Retro 95, MS-DOS, Neon
  Green CRT). It loads **after** `theme.css` on purpose. Use `.section > .section-header + .section-body`, not
  your own card CSS.
- localStorage keys: `confetti-topo-theme`, `confetti-topo-layout`, `confetti-topo-theme-shuffle`.
- Flask caches templates; restart the server after an edit. A literal `{{` in inline JS needs
  `{% raw %}...{% endraw %}` (constraint 6).

## The topology page
- vis.js draws on a canvas, so it needs literal colour strings. Read them with `themeColor('--blue')` each time
  the graph is built, and rebuild on the `themechange` event (the layout picker fires it too).
- Refresh every 20 s by **updating the DataSets in place** (`syncSet`: `remove` ids that vanished, `update`
  the rest). Creating a new `vis.Network` resets layout and zoom. Edge ids are stable: `from|to|layer|label`.
- Ghost nodes are dashed dots; unreachable devices (ping) get a red outline; a BGP/OSPF edge with `up === false`
  is red and dashed. BGP and OSPF edges bend opposite ways (`curvedCW`, roundness ±0.25) so lines between the
  same two devices do not sit on top of each other. Keep the legend in step with any new style.
- Empty state: a plain message and `network.destroy()`.

## Keep the siblings in step
The look is copied by hand from confetti-traffic's hub and confetti-butler. After a theme, picker or
`confettiBlast()` change, run the `confetti-interop-checker` agent. Re-record `docs/img/` with
`scripts/capture_readme.py` after any visible change.
