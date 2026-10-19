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
- localStorage keys: `confetti-topo-theme`, `confetti-topo-layout`, `confetti-topo-theme-shuffle`, `confetti-topo-layers` (the topology's LLDP/BGP/OSPF
  toggles) and `confetti-topo-hide-unknown` (the "Unknown nodes" toggle: hides ghost nodes and their edges in the browser only; a yellow FILTERED marker over the map shows while any layer is off or unknown nodes are hidden; its "Show all" resets all of these) and `confetti-topo-hidden-nodes` (nodes hidden from the click-a-node card; viewer-side only, like the unknown-nodes toggle), `confetti-topo-device-icons` and `confetti-topo-vendor-icons` (the Device icons / Vendor icons toggles, on by default) and `confetti-topo-hidden-icons` (the node card's Hide icon checkbox). The icon keys are look settings, not filters: no FILTERED marker, and Show all leaves them alone. Right-click menu: vis-network's `oncontext` event plus
  `getNodeAt`; the browser's own menu is blocked on the map.
- Flask caches templates; restart the server after an edit. A literal `{{` in inline JS needs
  `{% raw %}...{% endraw %}` (constraint 6).

## The devices page
- A tick, cross or dash beside the poll state shows the last poll result: `fail_count > 0` is a cross, `last_poll_ok`
  a tick, neither a dash, `role=node` none. It reuses the `pill ok/bad/idle` classes so every theme styles it.

## The topology page
- vis.js draws on a canvas, so it needs literal colour strings. Read them with `themeColor('--blue')` each time
  the graph is built, and rebuild on the `themechange` event (the layout picker fires it too).
- Refresh every 20 s by **updating the DataSets in place** (`syncSet`: `remove` ids that vanished, `update`
  the rest). Creating a new `vis.Network` resets layout and zoom. Edge ids are stable: `from|to|layer|label` (`edgeKey()`); clicking a link opens a card built from the edge's `details`.
- Known devices are drawn as SVG icons by `device_type` (router, switch, firewall, linux, host); ghosts get a "?" icon. A vendor mark and name sit to the right (`VENDOR_MARKS`, `VENDOR_NAMES`, `VENDOR_ALIASES` in topology.html; add a vendor to the ones that apply). The legend's icon row is `fillLegendIcons`.
- With icons off, ghost nodes are dashed dots; unreachable devices (ping) get a red outline; a BGP/OSPF edge with `up === false`
  is red and dashed. BGP and OSPF edges bend opposite ways (`curvedCW`, roundness ±0.25) so lines between the
  same two devices do not sit on top of each other. Keep the legend in step with any new style.
- Empty state: a plain message and `network.destroy()`.

## Keep the siblings in step
The look is copied by hand from confetti-traffic's hub and confetti-butler. After a theme, picker or
`confettiBlast()` change, run the `confetti-interop-checker` agent. Re-record `docs/img/` with
`scripts/capture_readme.py` after any visible change.
