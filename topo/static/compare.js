// The logic of the topology page's Compare: which link is which, and what changed between two maps. No page or
// network access, so dev/regress.py runs it with node (R59). Only the topology page loads it.

// A link that is gone in a compare gets its own id: a live BGP link between the same two can carry the same label.
function edgeKey(e) { return [e.from, e.to, e.layer, e.label].join('|') + (e.cmp === 'gone' ? '|gone' : ''); }

// Which link in the older map is which in the newer one. A BGP/OSPF/IPsec label holds the state, so those are matched by
// ends and protocol (same label first, then in order); every other link by ends, layer and label.
function matchEdges(oldEdges, newEdges) {
    var STATE_LAYERS = { bgp: true, ospf: true, ipsec: true };
    function base(e) { return STATE_LAYERS[e.layer] ? [e.from, e.to, e.layer].join('|') : edgeKey(e); }
    var left = {};
    oldEdges.forEach(function (e) { (left[base(e)] = left[base(e)] || []).push(e); });
    var pairs = newEdges.map(function (e) { return { now: e, was: null }; });
    [true, false].forEach(function (sameLabel) {
        pairs.forEach(function (p) {
            var list = left[base(p.now)];
            if (p.was || !list) { return; }
            for (var i = 0; i < list.length; i++) {
                if (!sameLabel || list[i].label === p.now.label) { p.was = list.splice(i, 1)[0]; return; }
            }
        });
    });
    var gone = [];
    Object.keys(left).forEach(function (k) { gone = gone.concat(left[k]); });
    return { pairs: pairs, gone: gone };
}

// The newer map plus what only the older one has, each node and link marked added / gone / changed / same (.cmp, .cmpWhy).
// part(map) is the page's filter: only what is shown counts.
function buildCompare(older, newer, part) {
    var a = part(older), b = part(newer);
    function mark(item, status, why) { return Object.assign({}, item, { cmp: status, cmpWhy: why }); }
    var matched = matchEdges(a.edges, b.edges);
    var edges = matched.pairs.map(function (p) {
        if (!p.was) { return mark(p.now, 'added', 'new link'); }
        var why = [];
        if ((p.was.up === false) !== (p.now.up === false)) { why.push(p.now.up === false ? 'went down' : 'came up'); }
        if (p.was.label !== p.now.label) { why.push('was: ' + p.was.label); }
        if (!!p.was.flapping !== !!p.now.flapping) { why.push(p.now.flapping ? 'started flapping' : 'stopped flapping'); }
        return mark(p.now, why.length ? 'changed' : 'same', why.join('; '));
    }).concat(matched.gone.map(function (e) { return mark(e, 'gone', 'link is gone'); }));
    var names = {};
    a.nodes.concat(b.nodes).forEach(function (n) { names[n.id] = n.label; });
    var linkNotes = {};   // node id -> what happened to its links
    edges.forEach(function (e) {
        if (e.cmp === 'same') { return; }
        [[e.from, e.to], [e.to, e.from]].forEach(function (p) {
            (linkNotes[p[0]] = linkNotes[p[0]] || []).push(e.layer + ' link to ' + (names[p[1]] || '?') + ' ' + e.cmp);
        });
    });
    var old = {};
    a.nodes.forEach(function (n) { old[n.id] = n; });
    var nodes = b.nodes.map(function (n) {
        var was = old[n.id];
        delete old[n.id];
        if (!was) { return mark(n, 'added', 'new on the map'); }
        var why = [];
        if ((was.reachable === 0) !== (n.reachable === 0)) { why.push(n.reachable === 0 ? 'stopped answering ping' : 'answers ping again'); }
        why = why.concat(linkNotes[n.id] || []);
        return mark(n, why.length ? 'changed' : 'same', why.join('\n'));
    }).concat(Object.keys(old).map(function (id) { return mark(old[id], 'gone', 'gone from the map'); }));
    return { nodes: nodes, edges: edges, positions: Object.assign({}, a.positions, b.positions),
             from: older.taken_at, to: newer.taken_at };
}

if (typeof module !== 'undefined') { module.exports = { edgeKey: edgeKey, matchEdges: matchEdges, buildCompare: buildCompare }; }
