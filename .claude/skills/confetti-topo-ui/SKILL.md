---
name: confetti-topo-ui
description: Authoring checklist for changes to confetti-topo's pages (topo/templates/, static/theme.css, layout.css, theme.js, the vis-network graph) — what must stay shared, how colours reach the canvas, how the graph refreshes. Read before editing anything under topo/templates/ or topo/static/, not after.
origin: confetti-topo
---

# Pages: authoring checklist

`dashboard-ui-checker` looks at a finished change. This is what to get right while writing it. The same
stack as confetti-traffic's hub: Flask + Jinja2, vanilla JS, no build step, nothing from a
CDN.

## A new page
- One entry in `all_pages` in `base.html` and a route in `app/app.py` (or `tools_routes.py`). Check the menu order and
  the MS-DOS hotkeys (R6).
- Add it to `PAGES` in `dev/regress.py` (R6 and R20 use it), to the page list in the `dashboard-ui-checker` agent,
  and to `TOUR` in `scripts/capture_readme.py` if it belongs in the README.
- Right-click: rows with `data-ctx` and a `page` menu. Tables: `data-sortable`; column widths come by themselves.
- A script file in `static/` that a check runs with node must work without a page (see `compare.js`, R59).

## Shared structure
- Every page `{% extends "base.html" %}` and holds only its own content, CSS and JS. The header, nav list,
  theme and layout pickers, footer and Retro 95 taskbar live in `base.html` once. A new page: see "A new page" below.
- Colours live **only** in `static/theme.css` (a dark `:root` plus one `:root[data-theme=NAME]` block per
  theme). A page must not define its own `:root` colours; use the CSS variables. A new theme = one block in
  `theme.css` + one entry in the `THEMES` list in `theme.js`.
- `static/layout.css` carries the structure and the four layouts (Classic, Modern, Retro 95, MS-DOS; MS-DOS has two
  colour variants, blue and Neon Green, `data-variant="green"`). It loads **after** `theme.css` on purpose. Use `.section > .section-header + .section-body`, not
  your own card CSS.
- Collapsible boxes: give a `.section` `data-fold="<id>"` (add `data-fold-default="closed"` to start folded); `static/fold.js` adds the button and keeps the choice in `confetti-topo-folded`. A Columns chooser is `colPick.attach(table, btn, key, hiddenIdx)` (`static/colpick.js`). Extra header buttons go in `.hdr-actions`.
- Topology Compare (its own toolbar row): `confetti-topo-cmp-group`, `confetti-topo-cmp-only-dev`, `confetti-topo-cmp-only-edge`. Its colours are `--green` / `--red` / `--orange`; every theme block (theme.css, and the Retro 95 and MS-DOS blocks in layout.css) must define each variable the pages use.
- localStorage keys: `confetti-topo-folded`, `confetti-topo-devices-columns`, `confetti-topo-theme`, `confetti-topo-layout`, `confetti-topo-dos-variant` (MS-DOS colour variant), `confetti-topo-dashboard-layout` / `confetti-topo-device-layout` (dashgrid.js, one per page), `confetti-topo-theme-shuffle`, `confetti-topo-layers` (the topology's layer
  toggles that are on; `confetti-topo-layers-known` records which layers existed then, so a layer added later starts on) and `confetti-topo-hide-unknown` (the "Unknown nodes" toggle: hides ghost nodes and their edges in the browser only; a yellow FILTERED marker over the map shows while any layer is off or unknown nodes are hidden; its "Show all" resets all of these) and `confetti-topo-hidden-nodes` (nodes hidden from the click-a-node card; viewer-side only, like the unknown-nodes toggle), `confetti-topo-changed-24h` (the Changed 24h glow, off by default), `confetti-topo-device-icons` and `confetti-topo-vendor-icons` (the Device icons / Vendor icons toggles, on by default) and `confetti-topo-hidden-icons` (the node card's Hide icon checkbox) and `confetti-topo-dashboard-devices` (the dashboard Devices widget's columns and sort, `{cols, sort}`), `confetti-topo-layout-mode` (`free`, `tree` or `site`; `site` = By site, whose places ARE saved, unlike the tree), `confetti-topo-bundle-links` (By site: bundle the links between sites, `0` = off), `confetti-topo-devices-group` / `-devices-folded` (Devices page grouping and folded sites), `confetti-topo-dashboard-problems` (dashboard Devices: problems only) and `confetti-topo-changes-fold` (Recent changes: fold flapping), `confetti-topo-master` (the node id set by right-click Focus on device: lays the map out as a tree from it; a view setting, no FILTERED marker, Show all leaves it alone; the tree places are never saved to `node_positions`, so `savePositions`, `tidyAsAreas` and `rearrange` do nothing while `treeOn`). The icon keys are look settings, not filters: no FILTERED marker, and Show all leaves them alone. Right-click menus use the shared `static/ctxmenu.js` (the same file as confetti-traffic;
  fill `data-*` attributes with `ctxMenu.attr()`, R29). On the map: vis-network's `oncontext` event plus
  `getNodeAt`, then `ctxMenu.open(x, y, spec)` with window coordinates; the browser's own menu is blocked on the map.
- Flask caches templates; restart the server after an edit. A literal `{{` in inline JS needs
  `{% raw %}...{% endraw %}` (constraint 6).

## The devices page
- A tick, cross or dash beside the poll state shows the last poll result: `fail_count > 0` is a cross, `last_poll_ok`
  a tick, neither a dash, `role=node` none. It reuses the `pill ok/bad/idle` classes so every theme styles it.

## The topology page
- Layers are `topology.LAYERS` on the server and the `button[data-layer]` row on the page. A new layer needs: button,
  `LAYER_COLOR`, `BEND`, legend row, `LINK_KIND`, `LINK_FIELDS`, the big-map default, and `matchEdges` in `compare.js` if
  its label holds a state. A new colour goes in every theme block and both colour layouts (R60). Compare logic is
  `static/compare.js` (R59); nothing is saved while Compare or History is shown.
- Geometry that a check should run (AS area boxes, where a far shared unknown node goes) is in `static/mapgeo.js` (R62, R63).
  Anything that moves a node writes the shared saved places: its trigger must be false after the move.
- Fit: `fitMap(ids)`; the ▾ menu is `openFitMenu`.
- vis.js draws on a canvas, so it needs literal colour strings. Read them with `themeColor('--blue')` each time
  the graph is built, and rebuild on the `themechange` event (the layout picker fires it too).
- Refresh every 20 s by **updating the DataSets in place** (`syncSet`: `remove` ids that vanished, `update`
  the rest). Creating a new `vis.Network` resets layout and zoom. Edge ids are stable: `from|to|layer|label` (`edgeKey()`); clicking a link opens a card built from the edge's `details`.
- Known devices are drawn as SVG icons by `device_type` (router, switch, firewall, linux, host); ghosts get a "?" icon. A vendor mark and name sit to the right (`VENDOR_MARKS`, `VENDOR_NAMES`, `VENDOR_ALIASES` in topology.html; add a vendor to the ones that apply). The legend's icon row is `fillLegendIcons`.
- With icons off, ghost nodes are dashed dots; unreachable devices (ping) get a red outline; an edge with `up === false` (BGP/OSPF down, or an LLDP cable with a down port)
  is red and dashed. BGP and OSPF edges bend opposite ways (`curvedCW`, roundness ±0.25) so lines between the
  same two devices do not sit on top of each other. Keep the legend in step with any new style.
- Empty state: a plain message and `network.destroy()`.

## Keep the siblings in step
The look is copied by hand from confetti-traffic's hub. After a theme, picker or
`confettiBlast()` change, run the `confetti-interop-checker` agent. Re-record `docs/img/` with
`scripts/capture_readme.py` after any visible change.
