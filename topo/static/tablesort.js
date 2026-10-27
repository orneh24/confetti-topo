// Click-to-sort for any <table data-sortable>. Click a header to sort by that column, click again to reverse.
// A cell may carry data-sort="..." (a raw timestamp, say) when its text is not what should be compared.
// The pages redraw their rows with innerHTML, so the chosen sort is applied again after every redraw.
(function () {
    var IP = /^(\d{1,3})\.(\d{1,3})\.(\d{1,3})\.(\d{1,3})(?:\/(\d+))?$/;
    var NUM = /^-?\d+(?:\.\d+)?$/;

    function cellValue(row, col) {
        var td = row.cells[col];
        if (!td) { return ''; }
        var v = td.getAttribute('data-sort');
        return (v !== null ? v : td.textContent).trim();
    }

    // Addresses compare as numbers (10.0.0.9 before 10.0.0.10), plain numbers as numbers, the rest as text
    // with digits in order (Gi0/2 before Gi0/10).
    function compare(a, b) {
        if (a === b) { return 0; }
        var ia = IP.exec(a), ib = IP.exec(b);
        if (ia && ib) {
            for (var i = 1; i <= 4; i++) {
                if (+ia[i] !== +ib[i]) { return +ia[i] - +ib[i]; }
            }
            return (ia[5] === undefined ? 0 : +ia[5]) - (ib[5] === undefined ? 0 : +ib[5]);
        }
        if (NUM.test(a) && NUM.test(b)) { return parseFloat(a) - parseFloat(b); }
        return a.localeCompare(b, undefined, { numeric: true, sensitivity: 'base' });
    }

    function sortBody(table, col, dir) {
        var tbody = table.tBodies[0];
        var rows = Array.prototype.slice.call(tbody.rows);
        if (!rows.length || rows.some(function (r) { return r.cells.length === 1 && r.cells[0].colSpan > 1; })) {
            return;   // nothing to sort, or the "none" placeholder row
        }
        var keyed = rows.map(function (r, i) { return { row: r, i: i, v: cellValue(r, col) }; });
        keyed.sort(function (x, y) {
            if (x.v === '' || y.v === '') { return x.v === y.v ? x.i - y.i : (x.v === '' ? 1 : -1); }   // empty last
            return (compare(x.v, y.v) * dir) || (x.i - y.i);
        });
        table._sorting = true;
        keyed.forEach(function (k) { tbody.appendChild(k.row); });
        table._sorting = false;
        if (table._observer) { table._observer.takeRecords(); }
    }

    function mark(table, col, dir) {
        Array.prototype.forEach.call(table.tHead.rows[0].cells, function (th, i) {
            if (i === col) { th.setAttribute('aria-sort', dir === 1 ? 'ascending' : 'descending'); }
            else { th.removeAttribute('aria-sort'); }
        });
    }

    function pick(table, col) {
        var dir = table._sort && table._sort.col === col ? -table._sort.dir : 1;
        table._sort = { col: col, dir: dir };
        mark(table, col, dir);
        sortBody(table, col, dir);
    }

    function setup(table) {
        if (table._sortReady || !table.tHead || !table.tBodies[0]) { return; }
        table._sortReady = true;
        Array.prototype.forEach.call(table.tHead.rows[0].cells, function (th, i) {
            if (!th.textContent.trim()) { return; }   // an action column
            th.setAttribute('data-sortable-col', '');
            th.tabIndex = 0;
            th.title = 'Click to sort';
            th.addEventListener('click', function () { pick(table, i); });
            th.addEventListener('keydown', function (e) {
                if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); pick(table, i); }
            });
        });
        table._observer = new MutationObserver(function () {
            if (!table._sorting && table._sort) { sortBody(table, table._sort.col, table._sort.dir); }
        });
        table._observer.observe(table.tBodies[0], { childList: true });
    }

    function init() {
        Array.prototype.forEach.call(document.querySelectorAll('table[data-sortable]'), setup);
    }
    if (document.readyState === 'loading') { document.addEventListener('DOMContentLoaded', init); }
    else { init(); }
})();
