// Collapse / Expand for page boxes, loaded once by base.html. A box opts in with data-fold="<id>" on its .section
// (data-fold-default="closed" starts it folded); every dashboard widget (.container > .section[data-box]) is foldable
// too. A folded box shows only its title bar (class "collapsed", CSS in layout.css); dashgrid.js re-sorts an
// auto-sorted dashboard when one folds or opens. The viewer's choice is kept per page in this browser
// (localStorage confetti-topo-folded, {"<page>:<id>": true|false}); a box with no choice keeps its default.
(function () {
    var KEY = 'confetti-topo-folded';
    var page = location.pathname.split('/')[1] || 'dashboard';

    function readAll() {
        try { return JSON.parse(localStorage.getItem(KEY)) || {}; } catch (e) { return {}; }
    }
    function remember(id, folded) {
        var all = readAll();
        all[page + ':' + id] = folded;
        try { localStorage.setItem(KEY, JSON.stringify(all)); } catch (e) { /* not remembered */ }
    }

    function setup(sec) {
        if (sec._fold) { return; }
        var id = sec.getAttribute('data-fold') || sec.getAttribute('data-box');
        var head = sec.querySelector('.section-header');
        if (!id || !head) { return; }
        // The title stays left; the header's own buttons and Collapse sit together on the right.
        var actions = head.querySelector('.hdr-actions');
        if (!actions) {
            actions = document.createElement('span');
            actions.className = 'hdr-actions';
            Array.prototype.slice.call(head.children, 1).forEach(function (el) { actions.appendChild(el); });
            head.appendChild(actions);
        }
        var btn = document.createElement('button');
        btn.type = 'button';
        btn.className = 'btn-small collapse-toggle';
        actions.appendChild(btn);
        function show(folded) {
            sec.classList.toggle('collapsed', folded);
            btn.textContent = folded ? 'Expand' : 'Collapse';
            btn.setAttribute('aria-expanded', folded ? 'false' : 'true');
        }
        var saved = readAll()[page + ':' + id];
        show(typeof saved === 'boolean' ? saved : sec.getAttribute('data-fold-default') === 'closed');
        btn.addEventListener('click', function () {
            var folded = !sec.classList.contains('collapsed');
            show(folded);
            remember(id, folded);
        });
        sec._fold = show;
    }

    function init() {
        Array.prototype.forEach.call(document.querySelectorAll('.section[data-fold], .container > .section[data-box]'), setup);
    }

    // open(id): unfold a box from script (a link that points into it), and remember that.
    window.fold = {
        init: init,
        open: function (id) {
            var sec = document.querySelector('.section[data-fold="' + id + '"], .container > .section[data-box="' + id + '"]');
            if (sec && sec._fold && sec.classList.contains('collapsed')) { sec._fold(false); remember(id, false); }
        }
    };
    init();
})();
