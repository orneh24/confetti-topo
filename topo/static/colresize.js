// Column widths for every table with a header row (the Recent changes page's rule, on every page).
// Drag a header's right edge to resize its column; double-click the edge to give every column its natural width back.
// Right-click a table: "Resize (compact)" makes every column as narrow as its widest cell (at most COMPACT_MAX px, longer
// text wraps) and the table only as wide as its columns. Kept per browser and per table in confetti-topo-cols-<page>-<table>
// (a table can name its own key with data-col-key; the device pages share one). A table opts out with data-no-resize.
// topo only (not shared with confetti-traffic). Loaded by base.html after ctxmenu.js.
(function () {
    var COMPACT_MAX = 360, MIN_W = 40;

    function headCells(table) {
        var head = table.tHead;
        if (!head || !head.rows.length) { return null; }
        var cells = Array.prototype.slice.call(head.rows[head.rows.length - 1].cells);
        if (cells.length < 2 || cells.some(function (c) { return c.colSpan > 1; })) { return null; }
        return cells;
    }

    function keyOf(table, index) {
        if (table.getAttribute('data-col-key')) { return table.getAttribute('data-col-key'); }
        var page = window.location.pathname.replace(/\/\d+(?=\/|$)/g, '/N').replace(/[^A-Za-z0-9]+/g, '-').replace(/^-|-$/g, '') || 'home';
        return 'confetti-topo-cols-' + page + '-' + (table.id || 't' + index);
    }

    function attach(table, index) {
        var ths = headCells(table);
        if (!ths) { return; }
        table.classList.add('col-sizable');
        var KEY = keyOf(table, index);
        var last = ths.length - 1;   // takes the rest of the row, unless the table is compact
        var resizedAt = 0;

        function compact() { return table.classList.contains('cols-compact'); }
        function fitTable() {   // a compact table is exactly as wide as its columns
            if (!compact()) { table.style.width = ''; return; }
            var sum = 0;
            ths.forEach(function (th) { if (th.offsetParent !== null) { sum += parseInt(th.style.width, 10) || 0; } });
            table.style.width = sum + 'px';
        }
        // Fix every shown column at its width now (rounded up, so nothing that fits is cut); a hidden one keeps auto.
        function freeze() {
            if (table.classList.contains('resized')) { return; }
            ths.forEach(function (th, i) {
                var w = th.getBoundingClientRect().width;
                if (i < last && w > 0) { th.style.width = Math.ceil(w) + 1 + 'px'; }
            });
            table.classList.add('resized');
        }
        function store() {
            var w = {};
            ths.forEach(function (th, i) { var px = parseInt(th.style.width, 10); if (px > 0) { w[i] = px; } });
            try { localStorage.setItem(KEY, JSON.stringify({ w: w, compact: compact() })); } catch (e) { /* not remembered */ }
        }
        function reset() {
            ths.forEach(function (t) { t.style.width = ''; });
            table.classList.remove('resized', 'cols-compact');
            table.style.width = '';
            try { localStorage.removeItem(KEY); } catch (err) { /* nothing saved */ }
        }
        // Every column as narrow as its widest cell on one line (capped), then fixed there.
        function makeCompact() {
            table.classList.add('col-measure');
            var widths = ths.map(function (th) {
                var w = th.getBoundingClientRect().width;
                return w > 0 ? Math.min(COMPACT_MAX, Math.ceil(w) + 1) : 0;
            });
            table.classList.remove('col-measure');
            ths.forEach(function (th, i) { th.style.width = widths[i] ? widths[i] + 'px' : ''; });
            table.classList.add('resized', 'cols-compact');
            fitTable();
            store();
        }
        table._colCompact = makeCompact;

        var saved = null;
        try { saved = JSON.parse(localStorage.getItem(KEY)); } catch (e) { saved = null; }
        if (saved && typeof saved === 'object') {
            var w = saved.w || saved;   // the Recent changes page kept a bare {index: px} before
            var any = false;
            ths.forEach(function (th, i) { if (w[i] > 0 && (i < last || saved.compact)) { th.style.width = w[i] + 'px'; any = true; } });
            if (any) {
                table.classList.add('resized');
                if (saved.compact) { table.classList.add('cols-compact'); fitTable(); }
            }
        }

        // The click that ends a drag must not sort the column (tablesort.js listens for clicks on the header).
        table.tHead.addEventListener('click', function (e) {
            if (Date.now() - resizedAt < 300) { e.stopPropagation(); e.preventDefault(); }
        }, true);
        ths.forEach(function (th, i) {
            var grip = document.createElement('span');
            grip.className = 'col-grip';
            grip.title = 'Drag to resize this column. Double-click: all columns back to their natural width';
            th.appendChild(grip);
            if (i === last) { grip.classList.add('col-grip-last'); }   // only shown while the table is compact
            grip.addEventListener('click', function (e) { e.stopPropagation(); });
            grip.addEventListener('pointerdown', function (e) {
                if (e.button !== 0) { return; }
                e.preventDefault();
                e.stopPropagation();
                freeze();
                var x0 = e.clientX, w0 = th.getBoundingClientRect().width;
                document.body.classList.add('col-sizing');
                function move(ev) { th.style.width = Math.max(MIN_W, Math.round(w0 + ev.clientX - x0)) + 'px'; fitTable(); }
                function up() {
                    document.removeEventListener('pointermove', move);
                    document.removeEventListener('pointerup', up);
                    document.removeEventListener('pointercancel', up);
                    document.body.classList.remove('col-sizing');
                    resizedAt = Date.now();
                    store();
                }
                document.addEventListener('pointermove', move);
                document.addEventListener('pointerup', up);
                document.addEventListener('pointercancel', up);
            });
            grip.addEventListener('dblclick', function (e) { e.stopPropagation(); reset(); });
        });
    }

    function init() {
        Array.prototype.forEach.call(document.querySelectorAll('table'), function (t, i) {
            if (!t.hasAttribute('data-no-resize') && !t.classList.contains('col-sizable')) { attach(t, i); }
        });
    }
    if (document.readyState === 'loading') { document.addEventListener('DOMContentLoaded', init); } else { init(); }
    // Some pages (Conflicts, Find) build their tables after loading: pick those up too, once per frame at most.
    var queued = false;
    new MutationObserver(function () {
        if (queued) { return; }
        queued = true;
        requestAnimationFrame(function () { queued = false; init(); });
    }).observe(document.documentElement, { childList: true, subtree: true });

    // Right-click anywhere in a table: "Resize (compact)", after the row's own items.
    if (window.ctxMenu && ctxMenu.extend) {
        ctxMenu.extend(function (target) {
            var table = target.closest && target.closest('table.col-sizable');
            if (!table || !table._colCompact) { return null; }
            return [{ label: 'Resize (compact)', action: table._colCompact }];
        });
    }
})();
