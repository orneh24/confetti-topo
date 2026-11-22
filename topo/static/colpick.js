// A "Columns" chooser for a plain table, loaded by the pages that use it. colPick.attach(table, button, key, hidden)
// hides columns by their place (a CSS rule per hidden column, so rows redrawn with innerHTML stay right) and keeps the
// viewer's choice in localStorage under `key`. `hidden` lists the column numbers (0 = first) hidden by default; the
// first column and columns with an empty heading (buttons) are always shown.
var colPick = (function () {
    function esc(s) { var d = document.createElement('div'); d.textContent = s; return d.innerHTML; }

    function attach(table, btn, key, hidden) {
        if (!table.id) { table.id = 'colpick-' + Math.random().toString(36).slice(2); }
        var heads = Array.prototype.map.call(table.tHead.rows[0].cells, function (th) { return th.textContent.trim(); });
        var style = document.createElement('style');
        document.head.appendChild(style);
        var off = hidden.slice();
        try {
            var saved = JSON.parse(localStorage.getItem(key));
            if (Array.isArray(saved)) { off = saved; }
        } catch (e) { /* default columns */ }
        function apply() {
            style.textContent = off.map(function (i) {
                return '#' + table.id + ' tr > :nth-child(' + (i + 1) + ') { display: none; }';
            }).join('\n');
        }
        var menu = document.createElement('div');
        menu.className = 'col-menu';
        menu.hidden = true;
        document.body.appendChild(menu);
        function fill() {
            var html = '';
            heads.forEach(function (h, i) {
                if (!h) { return; }
                var fixed = i === 0;
                html += '<label class="' + (fixed ? 'fixed' : '') + '"><input type="checkbox" data-i="' + i + '"' +
                    (off.indexOf(i) < 0 ? ' checked' : '') + (fixed ? ' disabled' : '') + '> ' + esc(h) + '</label>';
            });
            menu.innerHTML = html + '<button type="button" class="col-reset btn-small" data-act="reset">Default columns</button>';
        }
        function save() { try { localStorage.setItem(key, JSON.stringify(off)); } catch (e) { /* not remembered */ } }
        function close() { menu.hidden = true; btn.setAttribute('aria-expanded', 'false'); }
        btn.setAttribute('aria-haspopup', 'true');
        btn.setAttribute('aria-expanded', 'false');
        btn.addEventListener('click', function (ev) {
            ev.stopPropagation();
            if (!menu.hidden) { close(); return; }
            fill();
            menu.hidden = false;
            var r = btn.getBoundingClientRect();
            menu.style.top = (r.bottom + 4) + 'px';
            menu.style.left = Math.max(8, r.right - menu.offsetWidth) + 'px';
            btn.setAttribute('aria-expanded', 'true');
        });
        menu.addEventListener('change', function (ev) {
            var i = parseInt(ev.target.getAttribute('data-i'), 10);
            if (isNaN(i)) { return; }
            off = off.filter(function (x) { return x !== i; });
            if (!ev.target.checked) { off.push(i); }
            save();
            apply();
        });
        menu.addEventListener('click', function (ev) {
            if (ev.target.getAttribute('data-act') !== 'reset') { return; }
            off = hidden.slice();
            try { localStorage.removeItem(key); } catch (e) { /* nothing saved */ }
            fill();
            apply();
        });
        document.addEventListener('pointerdown', function (ev) {
            if (!menu.hidden && !menu.contains(ev.target) && ev.target !== btn) { close(); }
        });
        document.addEventListener('keydown', function (ev) { if (ev.key === 'Escape') { close(); } });
        window.addEventListener('resize', close);
        apply();
    }
    return { attach: attach };
})();
