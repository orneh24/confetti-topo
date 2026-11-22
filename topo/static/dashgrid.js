// Dashboard widgets you can move and resize. Loaded by dashboard.html only.
//
// Move: drag a widget by its title bar; the other widgets shift to make room (the grid order changes, nothing floats).
// Drop it on another widget (that widget gets an outline; on a full-width widget only its middle band counts) and the two, with
// any widget already on that row, share the row: the columns are split between them. Dropped above or below a full-width
// widget it only changes the order.
// Resize: drag the corner. The width snaps to whole grid columns (the full row at most); the height is free and
// the widget scrolls inside when it is smaller than its content. Double-click the corner to give one widget its size back.
// Auto sort layout (only when the page passes autoSort: true): a best-effort tidy-up that leaves as little empty space as it can.
// Saved heights and widths are dropped. Each widget is measured for the columns its content needs (no sideways scroll) and is
// never made narrower than that. When it leaves less empty space, the tallest widget that is not full width spans several
// grid rows and the next widgets are stacked beside it in the other columns (state.rows); the rest are put in
// rows of similar height (tallest first) and a row that is not full is widened to fill it; up to three short full-width widgets
// share one row. Every widget is then stretched to its row's height (state.fill). You can still move and resize afterwards.
// A page may fold a widget to its title bar by giving it the class "collapsed"; after an Auto sort the layout is sorted
// again whenever a widget is folded or opened, so the others close up or make room.
// Remembered in this browser only (localStorage), so a new browser sees the default layout. "Reset layout" clears it.
//
// A widget is a `.section` with a `data-box="<id>"` and a `.section-header`; the page's own code only touches the
// elements inside the widgets, so its refresh never undoes the arrangement. Pointer events, so touch works too.
// The SAME file is used by confetti-topo and confetti-traffic: keep the two identical.
var DashGrid = (function () {
    var box, cfg, state = { order: [], span: {}, height: {}, row: {}, rows: {}, fill: false };

    var CSS =
        '.dg-bar { display: flex; justify-content: flex-end; padding: 8px 24px 0; }' +
        '.dg-bar button { font-size: 11px; }' +
        '.dg-on .section { position: relative; }' +
        '.dg-on .section-header { cursor: grab; touch-action: none; user-select: none; -webkit-user-select: none; }' +
        // The widget being dragged stays in the grid as the drop place: its content is hidden and it shows a grid pattern.
        '.dg-on .section.dg-drag { outline: 2px dashed var(--text-dim); outline-offset: -2px;' +
        '  background-image: repeating-linear-gradient(0deg, transparent 0 19px, rgba(128,128,128,.35) 19px 20px),' +
        '  repeating-linear-gradient(90deg, transparent 0 19px, rgba(128,128,128,.35) 19px 20px); }' +
        '.dg-on .section.dg-drag > * { visibility: hidden; }' +
        // A copy of it follows the pointer.
        '.dg-ghost { position: fixed; z-index: 10000; margin: 0; opacity: .85; pointer-events: none; box-shadow: 0 8px 24px rgba(0,0,0,.4); }' +
        '.dg-on .section.dg-join { outline: 2px solid var(--blue, var(--text-dim)); outline-offset: -2px; }' +
        'body.dg-moving, body.dg-moving * { user-select: none; -webkit-user-select: none; cursor: grabbing !important; }' +
        'body.dg-sizing, body.dg-sizing * { user-select: none; -webkit-user-select: none; cursor: nwse-resize !important; }' +
        '.dg-resize { position: absolute; right: 0; bottom: 0; width: 16px; height: 16px; cursor: nwse-resize; touch-action: none;' +
        '  background: linear-gradient(135deg, transparent 0 45%, var(--text-dim) 45% 55%, transparent 55% 70%, var(--text-dim) 70% 80%, transparent 80%); opacity: .7; }' +
        '.dg-resize:hover { opacity: 1; }' +
        // After Auto sort a widget is stretched to the height of its grid row, so a short one leaves no gap under it.
        '.dg-on.dg-fill > .section[data-box] { align-self: stretch; }' +
        // A widget the page folded (class "collapsed": only its title bar shows) keeps its own small height and has no grip.
        '.dg-on.dg-fill > .section[data-box].collapsed { align-self: start; }' +
        '.dg-on .section.collapsed > .dg-resize { display: none; }';

    function key() { return cfg.prefix + '-dashboard-layout'; }

    function load() {
        try {
            var s = JSON.parse(localStorage.getItem(key()));
            if (s && typeof s === 'object') {
                state = { order: Array.isArray(s.order) ? s.order : [], span: s.span || {}, height: s.height || {}, row: s.row || {}, rows: s.rows || {}, fill: !!s.fill };
            }
        } catch (e) { /* not remembered */ }
    }

    function save() {
        try { localStorage.setItem(key(), JSON.stringify(state)); } catch (e) { /* not remembered */ }
    }

    function boxes() { return Array.prototype.slice.call(box.querySelectorAll(':scope > .section[data-box]')); }

    function columns() {
        var cols = getComputedStyle(box).gridTemplateColumns.split(' ').filter(Boolean);
        return Math.max(1, cols.length);
    }

    // Put the saved size on one box. A span wider than the columns now on screen is the full row (a span past the
    // last column would make the grid add columns of its own).
    // A widget that starts a row (state.row: the first of widgets that were joined on one row) is pinned to column 1, so the
    // row it was joined on is not shared with the widget before it.
    function colValue(id, span, cols) {
        if (!span) { return ''; }
        if (span >= cols) { return '1 / -1'; }
        return state.row[id] ? '1 / span ' + span : 'span ' + span;
    }

    function applySize(el) {
        var id = el.getAttribute('data-box');
        var cols = columns();
        el.style.gridColumn = colValue(id, state.span[id], cols);
        // Auto sort's tall widget spans the grid rows of the widgets stacked beside it (not on a one-column grid).
        el.style.gridRow = state.rows[id] > 1 && cols > 1 ? 'span ' + state.rows[id] : '';
        var body = el.querySelector('.section-body');
        if (body) {
            body.style.height = state.height[id] ? state.height[id] + 'px' : '';
        }
    }

    function applyAll() {
        var byId = {};
        boxes().forEach(function (el) { byId[el.getAttribute('data-box')] = el; });
        // Saved order first; widgets it does not know (a new one) stay after, in page order.
        var seen = {};
        state.order.forEach(function (id) {
            if (byId[id] && !seen[id]) { box.appendChild(byId[id]); seen[id] = true; }
        });
        boxes().forEach(function (el) {
            if (!seen[el.getAttribute('data-box')]) { box.appendChild(el); }
        });
        boxes().forEach(applySize);
        box.classList.toggle('dg-fill', !!state.fill);
    }

    function saveOrder() {
        state.order = boxes().map(function (el) { return el.getAttribute('data-box'); });
        save();
    }

    // ---- move ----------------------------------------------------------------------------------------------
    // The dragged widget was dropped on the middle of `target`: the widgets on target's row (same top edge) and the
    // dragged one share the columns of that row, first ones getting the extra column. Too many for the columns
    // (or a one-column grid, e.g. a phone) means nothing is resized.
    function joinRow(el, target) {
        var cols = columns();
        var top = target.getBoundingClientRect().top;
        var group = boxes().filter(function (b) { return b === el || Math.abs(b.getBoundingClientRect().top - top) < 6; });
        if (cols < 2 || group.length < 2 || group.length > cols) { return; }
        var base = Math.floor(cols / group.length), extra = cols % group.length;
        group.forEach(function (b, i) {
            var id = b.getAttribute('data-box');
            state.span[id] = base + (i < extra ? 1 : 0);
            if (i === 0) { state.row[id] = true; } else { delete state.row[id]; }
            applySize(b);
        });
    }

    function startMove(ev, el) {
        var x0 = ev.clientX, y0 = ev.clientY, moving = false, join = null, ghost = null, gx = 0, gy = 0;

        function setJoin(t) {
            if (join === t) { return; }
            if (join) { join.classList.remove('dg-join'); }
            join = t;
            if (join) { join.classList.add('dg-join'); }
        }

        // The pointer is over no widget: if it is level with a row, it is in that row's empty space (right of the last
        // widget or left of the first). Put the dragged widget next to them there and the grid fills the free columns.
        function emptySpace(e) {
            var row = boxes().filter(function (b) {
                var r = b.getBoundingClientRect();
                return e.clientY >= r.top && e.clientY <= r.bottom;
            });
            if (!row.length) { return; }
            setJoin(null);
            var last = row[row.length - 1], first = row[0];
            if (e.clientX > last.getBoundingClientRect().right) {
                if (last !== el) { box.insertBefore(el, last.nextSibling); }
            } else if (e.clientX < first.getBoundingClientRect().left) {
                if (first !== el) { box.insertBefore(el, first); }
            }
        }

        function over(e) {
            var t = document.elementFromPoint(e.clientX, e.clientY);
            var target = t && t.closest ? t.closest('.section[data-box]') : null;
            if (!target) { emptySpace(e); return; }
            if (target === el || target.parentNode !== box) { return; }
            var r = target.getBoundingClientRect();
            var fy = (e.clientY - r.top) / r.height;
            var full = r.width > box.clientWidth * 0.8;
            // Share this widget's row: most of a widget that is already beside another one, the middle band of a full-width widget
            // (its top and bottom edges there mean "above" and "below").
            setJoin(full ? (fy > 0.3 && fy < 0.7 ? target : null) : (fy > 0.15 && fy < 0.85 ? target : null));
            // A widget that fills the row is passed on the way down; widgets side by side on the way across.
            var after = full ? e.clientY > r.top + r.height / 2 : e.clientX > r.left + r.width / 2;
            box.insertBefore(el, after ? target.nextSibling : target);
        }

        function move(e) {
            if (!moving) {
                if (Math.abs(e.clientX - x0) + Math.abs(e.clientY - y0) < 5) { return; }
                moving = true;
                var r = el.getBoundingClientRect();
                gx = x0 - r.left; gy = y0 - r.top;
                ghost = el.cloneNode(true);
                ghost.classList.add('dg-ghost');
                ghost.style.width = r.width + 'px';
                ghost.style.height = r.height + 'px';
                ghost.style.gridColumn = '';
                document.body.appendChild(ghost);
                el.classList.add('dg-drag');
                document.body.classList.add('dg-moving');
            }
            ghost.style.left = (e.clientX - gx) + 'px';
            ghost.style.top = (e.clientY - gy) + 'px';
            over(e);   // before scrolling: the scroll moves the widgets under the pointer
            if (e.clientY < 70) { window.scrollBy(0, -14); }
            else if (e.clientY > window.innerHeight - 70) { window.scrollBy(0, 14); }
        }

        function end() {
            document.removeEventListener('pointermove', move);
            document.removeEventListener('pointerup', end);
            document.removeEventListener('pointercancel', end);
            if (moving) {
                el.classList.remove('dg-drag');
                if (ghost) { ghost.remove(); ghost = null; }
                document.body.classList.remove('dg-moving');
                var target = join;
                setJoin(null);
                delete state.row[el.getAttribute('data-box')];   // it moved: no longer pinned to a row start
                delete state.rows[el.getAttribute('data-box')];   // nor spanning the rows of widgets it is no longer beside
                if (target) { joinRow(el, target); }
                saveOrder();
            }
        }

        // On the document, not the header with pointer capture: moving the widget in the DOM (insertBefore) makes the
        // browser drop the capture, and the drag would never see its pointerup.
        document.addEventListener('pointermove', move);
        document.addEventListener('pointerup', end);
        document.addEventListener('pointercancel', end);
    }

    // ---- resize --------------------------------------------------------------------------------------------
    function startSize(ev, el, grip) {
        var id = el.getAttribute('data-box');
        var r0 = el.getBoundingClientRect();
        var body = el.querySelector('.section-body');
        var head = el.querySelector('.section-header');
        var x0 = ev.clientX, y0 = ev.clientY;
        var cs = getComputedStyle(box);
        var gap = parseFloat(cs.columnGap) || 0;
        var unit = parseFloat(cs.gridTemplateColumns.split(' ')[0]) || r0.width;
        var cols = columns();
        document.body.classList.add('dg-sizing');

        function move(e) {
            var w = r0.width + (e.clientX - x0);
            var span = Math.max(1, Math.min(cols, Math.round((w + gap) / (unit + gap))));
            state.span[id] = span;
            el.style.gridColumn = colValue(id, span, cols);
            if (body) {
                var h = Math.max(60, r0.height + (e.clientY - y0) - (head ? head.offsetHeight : 0) - 2);
                state.height[id] = Math.round(h);
                body.style.height = state.height[id] + 'px';
            }
        }

        function end() {
            grip.removeEventListener('pointermove', move);
            grip.removeEventListener('pointerup', end);
            grip.removeEventListener('pointercancel', end);
            document.body.classList.remove('dg-sizing');
            save();
        }

        grip.setPointerCapture(ev.pointerId);
        grip.addEventListener('pointermove', move);
        grip.addEventListener('pointerup', end);
        grip.addEventListener('pointercancel', end);
    }

    function reset() {
        state = { order: [], span: {}, height: {}, row: {}, rows: {}, fill: false };
        try { localStorage.removeItem(key()); } catch (e) { /* nothing saved anyway */ }
        box.classList.remove('dg-fill');
        // Back to the page's own order: the order the widgets had when the page was built.
        original.forEach(function (el) { box.appendChild(el); });
        boxes().forEach(applySize);
    }

    var SHORT_PX = 180;   // a full-width widget this low (an empty list, a few rows) can share its row
    var SHORT_PER_ROW = 3;   // at most this many short full-width widgets in one row
    var TALL_RATIO = 0.5;   // a widget lower than this share of its row's tallest goes to the next row

    // How to stack widgets beside a tall one in the `width` columns it leaves: try 1 .. width widgets per stacked row (each must
    // get the columns it needs), add rows until the stack is as high as the tall widget, and keep the try that leaves the least
    // empty area (in columns x pixels).
    function stackPlan(tall, rest, width, cols, gap) {
        var best = null;
        for (var per = 1; per <= Math.min(width, rest.length); per++) {
            var rows = [], h = 0, i = 0, used = tall.h * tall.need;
            while (i < rest.length && h < tall.h) {
                var row = rest.slice(i, i + per);
                if (row.some(function (it) { return it.need > Math.floor(width / row.length); })) { break; }   // the rest go below
                i += per;
                h += (rows.length ? gap : 0) + Math.max.apply(null, row.map(function (it) { return it.h; }));
                row.forEach(function (it) { used += it.h * width / row.length; });
                rows.push(row);
            }
            if (!rows.length) { continue; }
            var empty = cols * Math.max(tall.h, h) - used;
            if (!best || empty < best.empty) { best = { rows: rows, left: rest.slice(i), empty: empty }; }
        }
        return best;
    }

    // The columns a widget needs to show its content without scrolling sideways: measured at one column, where a table that
    // does not fit overflows its body.
    function needColumns(el, cols) {
        var body = el.querySelector('.section-body');
        if (!body) { return 1; }
        var cs = getComputedStyle(box);
        var gap = parseFloat(cs.columnGap) || 0;
        var unit = parseFloat(cs.gridTemplateColumns.split(' ')[0]) || el.getBoundingClientRect().width;
        var px = el.getBoundingClientRect().width + Math.max(0, body.scrollWidth - body.clientWidth);
        return Math.max(1, Math.min(cols, Math.ceil((px + gap) / (unit + gap) - 0.01)));
    }

    // Measure every widget, then lay them out in rows that fill the width.
    function autoSort() {
        var cols = columns();
        var gap = parseFloat(getComputedStyle(box).rowGap) || 0;
        state.height = {}; state.span = {}; state.row = {}; state.rows = {}; state.fill = false;
        box.classList.remove('dg-fill');   // measure natural heights, not stretched ones
        var els = boxes();
        els.forEach(function (el) { el.style.gridRow = ''; el.style.gridColumn = 'span 1'; });
        var items = els.map(function (el, i) {
            return { el: el, id: el.getAttribute('data-box'), i: i, need: cols < 2 ? 1 : needColumns(el, cols), wide: el.classList.contains('wide') };
        });
        // Heights at the width each widget will roughly get: what it needs, or the full row for a full-width one.
        items.forEach(function (it) { it.el.style.gridColumn = it.wide ? '1 / -1' : 'span ' + it.need; });
        items.forEach(function (it) { it.h = it.el.getBoundingClientRect().height; });
        state.order = [];
        // One row of widgets sharing `width` columns: each gets the columns it needs, the spare ones go round in order.
        // `pin` starts the row in column 1.
        function place(row, width, pin) {
            var spans = row.map(function (it) { return Math.min(it.need, width); });
            var spare = width - spans.reduce(function (a, b) { return a + b; }, 0);
            for (var k = 0; spare > 0; k = (k + 1) % row.length, spare--) { spans[k]++; }
            row.forEach(function (it, k) {
                state.order.push(it.id);
                state.span[it.id] = cols < 2 ? 0 : spans[k];
                if (pin && k === 0 && row.length > 1) { state.row[it.id] = true; }
            });
        }
        function needs(row) { return row.reduce(function (a, it) { return a + it.need; }, 0); }
        // Tallest first; a widget that does not fit the columns left, or is less than half as high as the row's first,
        // starts a new row.
        function narrowRows(list) {
            var cur = null, out = [];
            list.forEach(function (it) {
                if (!cur || needs(cur) + it.need > cols || it.h < cur[0].h * TALL_RATIO) { cur = []; out.push(cur); }
                cur.push(it);
            });
            return out;
        }
        // Empty area of rows that share the columns out evenly (columns x pixels).
        function rowsEmpty(rows) {
            return rows.reduce(function (sum, row) {
                var hi = Math.max.apply(null, row.map(function (it) { return it.h; }));
                return sum + row.reduce(function (a, it) { return a + (hi - it.h) * cols / row.length; }, 0);
            }, 0);
        }
        if (cols < 2) {
            items.forEach(function (it) { place([it], 1, false); });
        } else {
            var narrow = items.filter(function (it) { return !it.wide; });
            narrow.sort(function (a, b) { return (b.h - a.h) || (a.i - b.i); });
            // Stack beside the tallest when that leaves less empty area than plain rows do.
            if (narrow.length > 1 && narrow[0].need < cols) {
                var tall = narrow[0], width = cols - tall.need;
                var plan = stackPlan(tall, narrow.slice(1), width, cols, gap);
                if (plan && plan.empty + rowsEmpty(narrowRows(plan.left)) < rowsEmpty(narrowRows(narrow))) {
                    state.order.push(tall.id);
                    state.span[tall.id] = tall.need;
                    state.row[tall.id] = true;
                    if (plan.rows.length > 1) { state.rows[tall.id] = plan.rows.length; }
                    plan.rows.forEach(function (row) { place(row, width, false); });   // auto-placement puts them beside it
                    narrow = plan.left;
                }
            }
            var rows = narrowRows(narrow);
            // Short full-width widgets share a row, up to SHORT_PER_ROW of them, as far as the columns they need allow.
            var group = null, perRow = Math.min(SHORT_PER_ROW, cols);
            items.filter(function (it) { return it.wide; }).forEach(function (it) {
                if (it.h > SHORT_PX) { rows.push([it]); return; }
                if (!group || group.length >= perRow || needs(group) + it.need > cols) { group = []; rows.push(group); }
                group.push(it);
            });
            rows.forEach(function (row) { place(row, cols, true); });
        }
        state.fill = true;
        applyAll();
        save();
    }

    var original = [];

    function init(options) {
        cfg = options || {};
        box = document.querySelector(cfg.container || '.container');
        if (!box || box.getAttribute('data-dg')) { return; }
        box.setAttribute('data-dg', '1');
        box.classList.add('dg-on');
        var style = document.createElement('style');
        style.textContent = CSS;
        document.head.appendChild(style);
        original = boxes();

        boxes().forEach(function (el) {
            var header = el.querySelector('.section-header');
            if (!header) { return; }
            header.title = 'Drag to move this widget';
            header.addEventListener('pointerdown', function (ev) {
                if (ev.button !== 0 || (ev.target.closest && ev.target.closest('a, button, input, select, textarea'))) { return; }
                startMove(ev, el);
            });
            var grip = document.createElement('div');
            grip.className = 'dg-resize';
            grip.title = 'Drag to resize (width in columns, height free). Double-click: original size';
            grip.addEventListener('pointerdown', function (ev) {
                if (ev.button !== 0) { return; }
                ev.preventDefault();
                startSize(ev, el, grip);
            });
            grip.addEventListener('dblclick', function () {
                var id = el.getAttribute('data-box');
                delete state.span[id];
                delete state.height[id];
                delete state.row[id];
                delete state.rows[id];
                applySize(el);
                save();
            });
            el.appendChild(grip);
        });

        var bar = document.createElement('div');
        bar.className = 'dg-bar';
        var btn = document.createElement('button');
        btn.type = 'button';
        btn.textContent = 'Reset layout';
        btn.title = 'Put the widgets back in their original order and size';
        btn.addEventListener('click', reset);
        bar.appendChild(btn);
        if (cfg.autoSort) {
            var auto = document.createElement('button');
            auto.type = 'button';
            auto.textContent = 'Auto sort layout';
            auto.title = 'Rearrange the widgets to leave as little empty space as possible (sizes you set are dropped)';
            auto.style.marginLeft = '6px';
            auto.addEventListener('click', autoSort);
            bar.appendChild(auto);
        }
        box.parentNode.insertBefore(bar, box);

        load();
        applyAll();
        // Folding or opening a widget (the page toggles "collapsed") re-runs Auto sort while its layout is in use.
        if (window.MutationObserver) {
            var folded = {};
            boxes().forEach(function (el) { folded[el.getAttribute('data-box')] = el.classList.contains('collapsed'); });
            new MutationObserver(function (list) {
                var changed = false;
                list.forEach(function (m) {
                    if (m.target.parentNode !== box || !m.target.hasAttribute('data-box')) { return; }   // a widget, not something inside one
                    var id = m.target.getAttribute('data-box'), now = m.target.classList.contains('collapsed');
                    if (folded[id] !== now) { folded[id] = now; changed = true; }
                });
                if (changed && state.fill) { autoSort(); }
            }).observe(box, { attributes: true, attributeFilter: ['class'], subtree: true });
        }
        var timer = null;
        window.addEventListener('resize', function () {
            clearTimeout(timer);
            timer = setTimeout(function () { boxes().forEach(applySize); }, 100);
        });
    }

    return { init: init };
})();
