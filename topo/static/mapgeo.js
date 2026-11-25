// Geometry of the topology map: the boxes of the AS areas, and where a far-off shared unknown node may go. No page or
// network access, so dev/regress.py runs it with node (R62, R63). Only the topology page loads it.

function unionRect(a, b) {
    return { left: Math.min(a.left, b.left), top: Math.min(a.top, b.top), right: Math.max(a.right, b.right), bottom: Math.max(a.bottom, b.bottom) };
}

function rectsMeet(a, b, gap) {
    return a.left < b.right + gap && a.right > b.left - gap && a.top < b.bottom + gap && a.bottom > b.top - gap;
}

// The AS areas. items: [{id, asn, rect}], one per node that has an AS. Returns [{asn, box, ids}].
// One box per AS, unless boxes of two different AS would meet: then the box with the widest empty gap between its nodes
// is split there, until nothing meets or what still meets is single nodes. Parts of one AS that did not need to be
// apart are then joined again. No node moves.
var JOIN_SLACK = 2000;   // px² of extra cover a join may add: a sliver along an edge that already touches
function asAreas(items, gap) {
    function make(members) {
        return { asn: members[0].asn, members: members, box: members.map(function (m) { return m.rect; }).reduce(unionRect) };
    }
    var byAs = {};
    items.slice().sort(function (a, b) { return a.id < b.id ? -1 : 1; }).forEach(function (it) {
        (byAs[it.asn] = byAs[it.asn] || []).push(it);
    });
    var parts = Object.keys(byAs).map(function (asn) { return make(byAs[asn]); });

    function meets(p, q) { return String(p.asn) !== String(q.asn) && rectsMeet(p.box, q.box, gap); }

    // The widest gap between two neighboring node centres, along x or y, and the two halves it separates.
    function widest(part) {
        var best = null;
        [['left', 'right'], ['top', 'bottom']].forEach(function (axis) {
            function mid(m) { return (m.rect[axis[0]] + m.rect[axis[1]]) / 2; }
            var list = part.members.slice().sort(function (a, b) { return mid(a) - mid(b); });
            for (var i = 1; i < list.length; i++) {
                var size = mid(list[i]) - mid(list[i - 1]);
                if (!best || size > best.size) { best = { size: size, halves: [list.slice(0, i), list.slice(i)] }; }
            }
        });
        return best;
    }

    for (;;) {
        var pick = null;
        parts.forEach(function (p, i) {
            if (p.members.length < 2 || !parts.some(function (q) { return meets(p, q); })) { return; }
            var w = widest(p);
            if (!pick || w.size > pick.w.size) { pick = { i: i, w: w }; }
        });
        if (!pick) { break; }
        parts.splice(pick.i, 1, make(pick.w.halves[0]), make(pick.w.halves[1]));
    }

    // How much two boxes cover each other, each drawn gap/2 bigger than its nodes.
    function covered(p, q) {
        var w = Math.min(p.box.right, q.box.right) - Math.max(p.box.left, q.box.left) + gap;
        var h = Math.min(p.box.bottom, q.box.bottom) - Math.max(p.box.top, q.box.top) + gap;
        return w > 0 && h > 0 ? w * h : 0;
    }

    // Join two parts of one AS again when the joined box covers no more of any other AS than its two halves did. (Not "meets
    // nothing new": one touch that cannot be helped would then let the whole big box come back.)
    for (var joined = true; joined;) {
        joined = false;
        for (var i = 0; i < parts.length && !joined; i++) {
            for (var j = i + 1; j < parts.length && !joined; j++) {
                var a = parts[i], b = parts[j];
                if (String(a.asn) !== String(b.asn)) { continue; }
                var both = make(a.members.concat(b.members));
                if (parts.every(function (q) { return String(q.asn) === String(a.asn) || covered(both, q) <= covered(a, q) + covered(b, q) + JOIN_SLACK; })) {
                    parts.splice(j, 1);
                    parts[i] = both;
                    joined = true;
                }
            }
        }
    }
    return parts.map(function (p) {
        return { asn: p.asn, box: p.box, ids: p.members.map(function (m) { return m.id; }) };
    });
}

// The box around the known devices (node ids "dev:..."), grown by margin on every side; null with no device.
function deviceBox(pos, margin) {
    var box = null;
    Object.keys(pos).forEach(function (id) {
        if (id.indexOf('dev:') !== 0) { return; }
        var r = { left: pos[id].x, right: pos[id].x, top: pos[id].y, bottom: pos[id].y };
        box = box ? unionRect(box, r) : r;
    });
    return box && { left: box.left - margin, right: box.right + margin, top: box.top - margin, bottom: box.bottom + margin };
}

function inBox(box, p) { return p.x >= box.left && p.x <= box.right && p.y >= box.top && p.y <= box.bottom; }

// Unknown nodes that may be moved: seen by two or more nodes, all of them known devices, and lying outside box.
// Returns [{id, owners}].
function sharedOutliers(edges, pos, box) {
    var links = {};
    edges.forEach(function (e) {
        [[e.from, e.to], [e.to, e.from]].forEach(function (p) {
            if (p[0].indexOf('ghost:') === 0) { (links[p[0]] = links[p[0]] || {})[p[1]] = true; }
        });
    });
    return Object.keys(links).sort().map(function (id) { return { id: id, owners: Object.keys(links[id]) }; }).filter(function (g) {
        return box && pos[g.id] && !inBox(box, pos[g.id]) && g.owners.length > 1 &&
            g.owners.every(function (o) { return o.indexOf('dev:') === 0 && !!pos[o]; });
    });
}

// Places to try for such a node: rings around the middle of the devices that see it, only those inside box. A place from
// this list is not outside the box, so the next run leaves the node there. Each has .far, its distance from that middle.
function sharedCandidates(owners, pos, box) {
    var cx = 0, cy = 0;
    owners.forEach(function (o) { cx += pos[o].x / owners.length; cy += pos[o].y / owners.length; });
    var out = [{ x: cx, y: cy, far: 0 }];
    var reach = Math.hypot(box.right - box.left, box.bottom - box.top);
    for (var r = 60; r <= reach && out.length < 600; r += 60) {
        for (var k = 0; k < 24; k++) {
            var p = { x: cx + r * Math.cos(k * Math.PI / 12), y: cy + r * Math.sin(k * Math.PI / 12), far: r };
            if (inBox(box, p)) { out.push(p); }
        }
    }
    return out;
}

if (typeof module !== 'undefined') {
    module.exports = { unionRect: unionRect, rectsMeet: rectsMeet, asAreas: asAreas, deviceBox: deviceBox, inBox: inBox,
                       sharedOutliers: sharedOutliers, sharedCandidates: sharedCandidates };
}
