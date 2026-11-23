// A text filter for a long table: <input type="search" data-filter-table="TABLE-ID"> hides the body rows whose text does not
// contain what is typed (case ignored). The pages redraw their rows with innerHTML and tablesort.js moves them, so the filter is
// applied again after every change to the table body. A row is hidden with the class "filt-off" (layout.css); a script that acts on
// "the rows shown" skips those. Nothing is stored. topo only (not shared with confetti-traffic).
(function () {
    function setup(input) {
        var table = document.getElementById(input.getAttribute('data-filter-table'));
        if (!table || !table.tBodies[0] || input._filterReady) { return; }
        input._filterReady = true;
        var tbody = table.tBodies[0];
        var note = document.createElement('span');
        note.className = 'msg';
        input.parentNode.insertBefore(note, input.nextSibling);
        function apply() {
            var q = input.value.trim().toLowerCase(), shown = 0, total = 0;
            Array.prototype.forEach.call(tbody.rows, function (tr) {
                if (tr.cells.length === 1 && tr.cells[0].colSpan > 1) { return; }   // a heading or "none" row
                total++;
                var hit = !q || tr.textContent.toLowerCase().indexOf(q) >= 0;
                tr.classList.toggle('filt-off', !hit);
                if (hit) { shown++; }
            });
            note.textContent = q ? shown + ' of ' + total : '';
        }
        input.addEventListener('input', apply);
        new MutationObserver(apply).observe(tbody, { childList: true });
        apply();
    }
    function init() { Array.prototype.forEach.call(document.querySelectorAll('input[data-filter-table]'), setup); }
    if (document.readyState === 'loading') { document.addEventListener('DOMContentLoaded', init); } else { init(); }
})();
