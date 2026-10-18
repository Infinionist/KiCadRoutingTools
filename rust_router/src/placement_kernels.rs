//! Placement kernels: the airwire trees, crossings, foreign-pad hits, group
//! gaps and congestion that `py_placer/placement/quench.py` and
//! `simple.py` price for every candidate pose, done in Rust because they run
//! once per move.
//!
//! Each computes what its docstring's Python names, with the same tie rules.
//! The trees and gap trees keep the Python's float expressions in order;
//! the crossing and pad-hit kernels skip pairs whose boxes cannot meet, and
//! the congestion kernels (`net_box`, `pad_use`, `cong_delta`) sum a move's
//! change over the cells it reaches rather than the whole window, so their
//! totals can differ from a Python sum in the last bits.

use std::collections::{HashMap, HashSet};

use numpy::ndarray::Array2;
use numpy::{IntoPyArray, PyArray1, PyArray2, PyReadonlyArray1, PyReadonlyArray2};
use pyo3::prelude::*;

/// `np.minimum` / `np.maximum`: a NaN in either operand gives NaN.
#[inline]
fn np_min(a: f64, b: f64) -> f64 {
    if a.is_nan() || b.is_nan() {
        f64::NAN
    } else if b < a {
        b
    } else {
        a
    }
}

#[inline]
fn np_max(a: f64, b: f64) -> f64 {
    if a.is_nan() || b.is_nan() {
        f64::NAN
    } else if b > a {
        b
    } else {
        a
    }
}

/// quench's `ccw`: `(cy - ay) * (bx - ax) > (by - ay) * (cx - ax)`.
#[inline]
fn ccw(ax: f64, ay: f64, bx: f64, by: f64, cx: f64, cy: f64) -> bool {
    (cy - ay) * (bx - ax) > (by - ay) * (cx - ax)
}

/// Every (a row, b row) pair of airwires that cross, in row-major order --
/// `quench._crossing_hits` filtered to its hits: boxes that meet (`a`'s grown
/// by 1e-6), segments that cross by the orientation test, never two of one
/// net, never two sharing an endpoint within 1 um. Rows are
/// (x1, y1, x2, y2, net). With `net_w` (a net-id-indexed weight table) each
/// pair carries max(net_w[net_a], net_w[net_b]); without, 1.0.
///
/// Returns (ii, jj, w).
#[pyfunction]
#[pyo3(signature = (a, b, net_w=None))]
pub fn crossing_pairs<'py>(
    py: Python<'py>,
    a: PyReadonlyArray2<'py, f64>,
    b: PyReadonlyArray2<'py, f64>,
    net_w: Option<PyReadonlyArray1<'py, f64>>,
) -> (
    Bound<'py, PyArray1<i64>>,
    Bound<'py, PyArray1<i64>>,
    Bound<'py, PyArray1<f64>>,
) {
    let a = a.as_array();
    let b = b.as_array();
    let w = net_w.as_ref().map(|w| w.as_array());
    let m = 1e-6;
    let eps = 0.001;
    // b's boxes once.
    let nb = b.nrows();
    let mut bb: Vec<[f64; 4]> = Vec::with_capacity(nb);
    for j in 0..nb {
        bb.push([
            np_min(b[[j, 0]], b[[j, 2]]),
            np_max(b[[j, 0]], b[[j, 2]]),
            np_min(b[[j, 1]], b[[j, 3]]),
            np_max(b[[j, 1]], b[[j, 3]]),
        ]);
    }
    let mut ii: Vec<i64> = Vec::new();
    let mut jj: Vec<i64> = Vec::new();
    let mut ww: Vec<f64> = Vec::new();
    for i in 0..a.nrows() {
        let (a1x, a1y, a2x, a2y, an) = (a[[i, 0]], a[[i, 1]], a[[i, 2]], a[[i, 3]], a[[i, 4]]);
        let ax0 = np_min(a1x, a2x) - m;
        let ax1 = np_max(a1x, a2x) + m;
        let ay0 = np_min(a1y, a2y) - m;
        let ay1 = np_max(a1y, a2y) + m;
        for j in 0..nb {
            let [bx0, bx1, by0, by1] = bb[j];
            if !(ax1 >= bx0 && ax0 <= bx1 && ay1 >= by0 && ay0 <= by1) {
                continue;
            }
            let (b1x, b1y, b2x, b2y, bn) = (b[[j, 0]], b[[j, 1]], b[[j, 2]], b[[j, 3]], b[[j, 4]]);
            if an == bn {
                continue;
            }
            let shared = ((a1x - b1x).abs() < eps && (a1y - b1y).abs() < eps)
                || ((a1x - b2x).abs() < eps && (a1y - b2y).abs() < eps)
                || ((a2x - b1x).abs() < eps && (a2y - b1y).abs() < eps)
                || ((a2x - b2x).abs() < eps && (a2y - b2y).abs() < eps);
            if shared {
                continue;
            }
            let inter = (ccw(a1x, a1y, b1x, b1y, b2x, b2y) != ccw(a2x, a2y, b1x, b1y, b2x, b2y))
                && (ccw(a1x, a1y, a2x, a2y, b1x, b1y) != ccw(a1x, a1y, a2x, a2y, b2x, b2y));
            if !inter {
                continue;
            }
            ii.push(i as i64);
            jj.push(j as i64);
            ww.push(match &w {
                Some(t) => np_max(t[an as i64 as usize], t[bn as i64 as usize]),
                None => 1.0,
            });
        }
    }
    (
        ii.into_pyarray(py),
        jj.into_pyarray(py),
        ww.into_pyarray(py),
    )
}

/// `connectivity.compute_mst_edges(points)` (Euclidean), exactly: Prim from
/// node 0, the lowest-numbered node among equal keys, a key lowered only by a
/// strictly shorter edge, each distance `sqrt(dx*dx + dy*dy)`. Computed row by
/// row, so it holds O(n) memory where the numpy form holds the n x n matrix.
/// Returns [(i, j, d)] in the order the edges join the tree.
#[pyfunction]
pub fn mst_prim(xs: PyReadonlyArray1<f64>, ys: PyReadonlyArray1<f64>) -> Vec<(i64, i64, f64)> {
    let xs = xs.as_array();
    let ys = ys.as_array();
    let n = xs.len();
    let mut edges: Vec<(i64, i64, f64)> = Vec::new();
    if n < 2 {
        return edges;
    }
    let dist = |i: usize, j: usize| -> f64 {
        let dx = xs[j] - xs[i];
        let dy = ys[j] - ys[i];
        (dx * dx + dy * dy).sqrt()
    };
    let mut in_tree = vec![false; n];
    let mut nearest = vec![0usize; n];
    let mut min_dist: Vec<f64> = (0..n).map(|j| dist(0, j)).collect();
    in_tree[0] = true;
    min_dist[0] = f64::INFINITY;
    for _ in 0..(n - 1) {
        // np.argmin: the first index of the smallest value (a NaN wins).
        let mut best = 0usize;
        for k in 1..n {
            let (v, bv) = (min_dist[k], min_dist[best]);
            if bv.is_nan() {
                break;
            }
            if v.is_nan() || v < bv {
                best = k;
            }
        }
        let best_dist = min_dist[best];
        if !best_dist.is_finite() {
            break;
        }
        in_tree[best] = true;
        min_dist[best] = f64::INFINITY;
        edges.push((nearest[best] as i64, best as i64, best_dist));
        for k in 0..n {
            if !in_tree[k] {
                let d = dist(best, k);
                if d < min_dist[k] {
                    min_dist[k] = d;
                    nearest[k] = best;
                }
            }
        }
    }
    edges
}

// ---------------------------------------------------------------------------
// The simple path's model (`py_placer/placement/simple.py`), priced for every
// move of its annealer: a net's Manhattan tree, a group's gap tree, tree edges
// against pads and against each other, and congestion. The trees keep the
// Python's float operations in order, its tie rules and numpy's own semantics
// where numpy computed it (its min/max on signed zeros); the module header
// says which kernels sum in another order.

/// `simple.mst_l1`: the Manhattan minimum spanning tree of (xs, ys) by Prim
/// from point 0 -- the lowest index among equal keys, a key lowered only by
/// a strictly shorter edge, each distance `|xi - xj| + |yi - yj|`, the total
/// summed in the order the points join. Returns (total, [(from, to)]).
#[pyfunction]
pub fn mst_l1(xs: PyReadonlyArray1<f64>, ys: PyReadonlyArray1<f64>) -> (f64, Vec<(i64, i64)>) {
    let xs = xs.as_array();
    let ys = ys.as_array();
    let n = xs.len();
    let mut edges: Vec<(i64, i64)> = Vec::new();
    if n < 2 {
        return (0.0, edges);
    }
    let dist = |i: usize, j: usize| -> f64 { (xs[i] - xs[j]).abs() + (ys[i] - ys[j]).abs() };
    let mut done = vec![false; n];
    let mut frm = vec![0usize; n];
    let mut best: Vec<f64> = (0..n).map(|j| dist(0, j)).collect();
    done[0] = true;
    best[0] = f64::INFINITY;
    let mut total = 0.0f64;
    for _ in 0..(n - 1) {
        let mut k = usize::MAX;
        let mut bk = f64::INFINITY;
        for i in 0..n {
            if !done[i] && best[i] < bk {
                k = i;
                bk = best[i];
            }
        }
        if k == usize::MAX {
            break;
        }
        done[k] = true;
        total += bk;
        edges.push((frm[k] as i64, k as i64));
        for i in 0..n {
            if !done[i] {
                let d = dist(k, i);
                if d < best[i] {
                    best[i] = d;
                    frm[i] = k;
                }
            }
        }
    }
    (total, edges)
}

/// Python's built-in `max(a, b)` on floats: `b` only when it compares
/// strictly greater, so the first argument wins ties (and a NaN `b`).
#[inline]
fn py_max(a: f64, b: f64) -> f64 {
    if b > a {
        b
    } else {
        a
    }
}

/// Python's built-in `min(a, b)` on floats: `b` only when strictly less.
#[inline]
fn py_min(a: f64, b: f64) -> f64 {
    if b < a {
        b
    } else {
        a
    }
}

/// `simple.rect_gap`: the Manhattan gap between two rects (x0, y0, x1, y1),
/// `max(0, max(a0, b0) - min(a2, b2)) + max(0, max(a1, b1) - min(a3, b3))`
/// with Python's `max`/`min`.
#[inline]
fn rect_gap(a: [f64; 4], b: [f64; 4]) -> f64 {
    py_max(0.0, py_max(a[0], b[0]) - py_min(a[2], b[2]))
        + py_max(0.0, py_max(a[1], b[1]) - py_min(a[3], b[3]))
}

/// `simple.gap_tree`: the minimum spanning tree over rects (rows x0, y0, x1,
/// y1), each edge weighted by `rect_gap`, by Prim from rect 0 -- the next
/// rect the first one not in the tree whose key no later one beats strictly
/// (Python's `min(..., key=)`), a key lowered only by a strictly smaller gap,
/// the total summed in the order the rects join. Returns (total, [(from, to,
/// gap)]) in that order.
#[pyfunction]
pub fn gap_tree(rects: PyReadonlyArray2<f64>) -> (f64, Vec<(i64, i64, f64)>) {
    let r = rects.as_array();
    let n = r.nrows();
    let mut edges: Vec<(i64, i64, f64)> = Vec::new();
    if n < 2 {
        return (0.0, edges);
    }
    let rs: Vec<[f64; 4]> = (0..n).map(|i| [r[[i, 0]], r[[i, 1]], r[[i, 2]], r[[i, 3]]]).collect();
    let mut done = vec![false; n];
    let mut frm = vec![0usize; n];
    let mut best: Vec<f64> = (0..n).map(|i| rect_gap(rs[0], rs[i])).collect();
    done[0] = true;
    let mut total = 0.0f64;
    for _ in 0..(n - 1) {
        let mut k = usize::MAX;
        for i in 0..n {
            if !done[i] && (k == usize::MAX || best[i] < best[k]) {
                k = i;
            }
        }
        done[k] = true;
        total += best[k];
        edges.push((frm[k] as i64, k as i64, best[k]));
        for i in 0..n {
            if !done[i] {
                let g = rect_gap(rs[k], rs[i]);
                if g < best[i] {
                    best[i] = g;
                    frm[i] = k;
                }
            }
        }
    }
    (total, edges)
}

/// `simple.mst_l1` over points already in the order `sorted()` gives them,
/// with each point's owning part: the same Prim as `mst_l1` (point 0 first,
/// the lowest index among equal keys, a key lowered only by a strictly
/// shorter edge, the total summed in the order the points join). Returns
/// (total, edges m x 4 as (x0, y0, x1, y1), ends m x 2 as the two points'
/// owners) -- the arrays `simple.Model._price_net` keeps.
#[pyfunction]
pub fn mst_l1_tree<'py>(
    py: Python<'py>,
    xs: PyReadonlyArray1<'py, f64>,
    ys: PyReadonlyArray1<'py, f64>,
    owners: PyReadonlyArray1<'py, i64>,
) -> (f64, Bound<'py, PyArray2<f64>>, Bound<'py, PyArray2<i64>>) {
    let xs = xs.as_array();
    let ys = ys.as_array();
    let own = owners.as_array();
    let n = xs.len();
    let mut ev: Vec<f64> = Vec::new();
    let mut nv: Vec<i64> = Vec::new();
    let mut total = 0.0f64;
    if n > SPARSE_MST_MIN {
        // a big net (a ground with hundreds of pads): Kruskal over each
        // point's nearest neighbour per octant, O(n log n) where Prim is O(n^2)
        let (xv, yv) = (xs.to_vec(), ys.to_vec());
        for (w, a, b) in manhattan_mst(&xv, &yv) {
            total += w;
            ev.extend_from_slice(&[xv[a], yv[a], xv[b], yv[b]]);
            nv.extend_from_slice(&[own[a], own[b]]);
        }
    } else if n >= 2 {
        let dist = |i: usize, j: usize| -> f64 { (xs[i] - xs[j]).abs() + (ys[i] - ys[j]).abs() };
        let mut done = vec![false; n];
        let mut frm = vec![0usize; n];
        let mut best: Vec<f64> = (0..n).map(|j| dist(0, j)).collect();
        done[0] = true;
        best[0] = f64::INFINITY;
        for _ in 0..(n - 1) {
            let mut k = usize::MAX;
            let mut bk = f64::INFINITY;
            for i in 0..n {
                if !done[i] && best[i] < bk {
                    k = i;
                    bk = best[i];
                }
            }
            if k == usize::MAX {
                break;
            }
            done[k] = true;
            total += bk;
            let f = frm[k];
            ev.extend_from_slice(&[xs[f], ys[f], xs[k], ys[k]]);
            nv.extend_from_slice(&[own[f], own[k]]);
            for i in 0..n {
                if !done[i] {
                    let d = dist(k, i);
                    if d < best[i] {
                        best[i] = d;
                        frm[i] = k;
                    }
                }
            }
        }
    }
    let m = nv.len() / 2;
    let e = Array2::from_shape_vec((m, 4), ev).expect("m x 4");
    let o = Array2::from_shape_vec((m, 2), nv).expect("m x 2");
    (total, e.into_pyarray(py), o.into_pyarray(py))
}

/// `simple.Model._local_crossings_bd`'s tally: every pair that crosses
/// properly between `m` and `o` (row-major), keyed (mlab[i], olab[j]), then
/// every pair of `m` with itself (i < j, different labels), keyed
/// (mlab[i], mlab[j]); counted per key in the order keys are first met.
/// `o` rows whose label is in `skip` are left out. Returns (pairs, [(a, b,
/// count)]).
#[pyfunction]
#[pyo3(signature = (m, mlab, o=None, olab=None, skip=None))]
pub fn seg_cross_counts(
    m: PyReadonlyArray2<f64>,
    mlab: PyReadonlyArray1<i64>,
    o: Option<PyReadonlyArray2<f64>>,
    olab: Option<PyReadonlyArray1<i64>>,
    skip: Option<PyReadonlyArray1<i64>>,
) -> (i64, Vec<(i64, i64, i64)>) {
    let skip: HashSet<i64> = skip.map(|s| s.as_array().iter().copied().collect()).unwrap_or_default();
    let m = m.as_array();
    let ml = mlab.as_array();
    let row = |a: &numpy::ndarray::ArrayView2<f64>, i: usize| [a[[i, 0]], a[[i, 1]], a[[i, 2]], a[[i, 3]]];
    let mut out: Vec<(i64, i64, i64)> = Vec::new();
    let mut at: HashMap<(i64, i64), usize> = HashMap::new();
    let mut total = 0i64;
    let mut add = |a: i64, b: i64, out: &mut Vec<(i64, i64, i64)>| {
        total += 1;
        match at.get(&(a, b)) {
            Some(&k) => out[k].2 += 1,
            None => {
                at.insert((a, b), out.len());
                out.push((a, b, 1));
            }
        }
    };
    if let (Some(o), Some(ol)) = (o, olab) {
        let o = o.as_array();
        let ol = ol.as_array();
        // two segments whose boxes do not overlap cannot cross properly: each
        // `o` row is tested against the union box of `m`, then row by row
        let mb: Vec<[f64; 4]> = (0..m.nrows()).map(|i| seg_box(row(&m, i))).collect();
        let ub = mb.iter().fold([f64::INFINITY, f64::INFINITY, f64::NEG_INFINITY, f64::NEG_INFINITY],
                                |u, b| [u[0].min(b[0]), u[1].min(b[1]), u[2].max(b[2]), u[3].max(b[3])]);
        for j in 0..o.nrows() {
            if skip.contains(&ol[j]) {
                continue;
            }
            let rj = row(&o, j);
            let bj = seg_box(rj);
            if !boxes_meet(&ub, &bj) {
                continue;
            }
            for i in 0..m.nrows() {
                if boxes_meet(&mb[i], &bj) && proper_cross(row(&m, i), rj) {
                    add(ml[i], ol[j], &mut out);
                }
            }
        }
    }
    for i in 0..m.nrows() {
        let ri = row(&m, i);
        for j in (i + 1)..m.nrows() {
            if ml[i] != ml[j] && proper_cross(ri, row(&m, j)) {
                add(ml[i], ml[j], &mut out);
            }
        }
    }
    (total, out)
}

/// A segment's box (x0, y0, x1, y1 as min x, min y, max x, max y).
#[inline]
fn seg_box(a: [f64; 4]) -> [f64; 4] {
    [a[0].min(a[2]), a[1].min(a[3]), a[0].max(a[2]), a[1].max(a[3])]
}

/// Two closed boxes (min x, min y, max x, max y) share a point.
#[inline]
fn boxes_meet(a: &[f64; 4], b: &[f64; 4]) -> bool {
    a[0] <= b[2] && b[0] <= a[2] && a[1] <= b[3] && b[1] <= a[3]
}

/// `simple.Model._pad_breakdown`: the segments' hits on rects (the
/// `seg_rect_hits` test, row-major) kept when foreign -- another net than the
/// pad's and neither of the segment's two end parts the pad's owner -- with
/// each pad's weight summed per (segment's net, pad's owner), starting at 0.0
/// and in hit order, keys in the order first met. Segments of a net in
/// `skip_nets` and pads of an owner in `skip_owners` are left out. Returns
/// [(net, owner, w)].
#[pyfunction]
#[pyo3(signature = (s, ends, labs, p, w, pnets, owners, skip_nets=None, skip_owners=None))]
#[allow(clippy::too_many_arguments)]
pub fn pad_hit_sums(
    s: PyReadonlyArray2<f64>,
    ends: PyReadonlyArray2<i64>,
    labs: PyReadonlyArray1<i64>,
    p: PyReadonlyArray2<f64>,
    w: PyReadonlyArray1<f64>,
    pnets: PyReadonlyArray1<i64>,
    owners: PyReadonlyArray1<i64>,
    skip_nets: Option<PyReadonlyArray1<i64>>,
    skip_owners: Option<PyReadonlyArray1<i64>>,
) -> Vec<(i64, i64, f64)> {
    let skip_n: HashSet<i64> = skip_nets.map(|s| s.as_array().iter().copied().collect()).unwrap_or_default();
    let skip_o: HashSet<i64> = skip_owners.map(|s| s.as_array().iter().copied().collect()).unwrap_or_default();
    let s = s.as_array();
    let ends = ends.as_array();
    let labs = labs.as_array();
    let p = p.as_array();
    let w = w.as_array();
    let pn = pnets.as_array();
    let po = owners.as_array();
    let mut out: Vec<(i64, i64, f64)> = Vec::new();
    let mut at: HashMap<(i64, i64), usize> = HashMap::new();
    // the segments kept, and the rects their union box meets (NaN rows,
    // which `_index_part` leaves for rows a part no longer has, meet none)
    let segs: Vec<usize> = (0..s.nrows()).filter(|&i| !skip_n.contains(&labs[i])).collect();
    let mut ub = [f64::INFINITY, f64::INFINITY, f64::NEG_INFINITY, f64::NEG_INFINITY];
    for &i in &segs {
        let b = seg_box([s[[i, 0]], s[[i, 1]], s[[i, 2]], s[[i, 3]]]);
        ub = [ub[0].min(b[0]), ub[1].min(b[1]), ub[2].max(b[2]), ub[3].max(b[3])];
    }
    let rects: Vec<usize> = (0..p.nrows())
        .filter(|&j| {
            !skip_o.contains(&po[j]) && boxes_meet(&ub, &[p[[j, 0]], p[[j, 1]], p[[j, 2]], p[[j, 3]]])
        })
        .collect();
    for &i in &segs {
        let (x0, y0, x1, y1) = (s[[i, 0]], s[[i, 1]], s[[i, 2]], s[[i, 3]]);
        let (sx0, sx1) = (np_min(x0, x1), np_max(x0, x1));
        let (sy0, sy1) = (np_min(y0, y1), np_max(y0, y1));
        let dx = x1 - x0;
        let dy = y1 - y0;
        for &j in &rects {
            let (px0, py0, px1, py1) = (p[[j, 0]], p[[j, 1]], p[[j, 2]], p[[j, 3]]);
            if !(sx0 <= px1 && sx1 >= px0 && sy0 <= py1 && sy1 >= py0) {
                continue;
            }
            let mut lo = 0.0f64;
            let mut hi = 1.0f64;
            let mut ok = true;
            for (d, q) in [(-dx, x0 - px0), (dx, px1 - x0), (-dy, y0 - py0), (dy, py1 - y0)] {
                let zero = d.abs() < 1e-12;
                if zero && q < 0.0 {
                    ok = false;
                }
                let t = q / if zero { 1.0 } else { d };
                if !zero && d < 0.0 {
                    lo = np_max(lo, t);
                }
                if !zero && d > 0.0 {
                    hi = np_min(hi, t);
                }
            }
            if !(ok && lo <= hi) {
                continue;
            }
            if labs[i] == pn[j] || ends[[i, 0]] == po[j] || ends[[i, 1]] == po[j] {
                continue;
            }
            let key = (labs[i], po[j]);
            match at.get(&key) {
                Some(&k) => out[k].2 += w[j],
                None => {
                    at.insert(key, out.len());
                    out.push((key.0, key.1, 0.0 + w[j]));
                }
            }
        }
    }
    out
}

/// numpy's `np.maximum(x, 0.0)` as `np.clip(x, 0.0, None)` gives it: x when
/// it is greater, else +0.0 (a -0.0 included).
#[inline]
fn clip0(x: f64) -> f64 {
    if x > 0.0 || x.is_nan() {
        x
    } else {
        0.0
    }
}

/// numpy's `np.minimum` / `np.maximum` on two floats: a NaN wins, and -0.0
/// is less than +0.0.
#[inline]
fn npy_min(a: f64, b: f64) -> f64 {
    if a.is_nan() || b.is_nan() {
        f64::NAN
    } else if a < b || (a == b && a.is_sign_negative()) {
        a
    } else {
        b
    }
}

#[inline]
fn npy_max(a: f64, b: f64) -> f64 {
    if a.is_nan() || b.is_nan() {
        f64::NAN
    } else if a > b || (a == b && a.is_sign_positive()) {
        a
    } else {
        b
    }
}

/// `simple.Model._seg_hits`: every (segment i, rect j) that touch -- a bounding
/// box test, then Liang-Barsky clipping of the segment to the rect, a step
/// with |d| < 1e-12 taken as parallel. Segments are (x0, y0, x1, y1), rects
/// (x0, y0, x1, y1). Returns (ii, jj) in row-major order.
#[pyfunction]
pub fn seg_rect_hits<'py>(
    py: Python<'py>,
    s: PyReadonlyArray2<'py, f64>,
    p: PyReadonlyArray2<'py, f64>,
) -> (Bound<'py, PyArray1<i64>>, Bound<'py, PyArray1<i64>>) {
    let s = s.as_array();
    let p = p.as_array();
    let mut ii: Vec<i64> = Vec::new();
    let mut jj: Vec<i64> = Vec::new();
    for i in 0..s.nrows() {
        let (x0, y0, x1, y1) = (s[[i, 0]], s[[i, 1]], s[[i, 2]], s[[i, 3]]);
        let (sx0, sx1) = (np_min(x0, x1), np_max(x0, x1));
        let (sy0, sy1) = (np_min(y0, y1), np_max(y0, y1));
        let dx = x1 - x0;
        let dy = y1 - y0;
        for j in 0..p.nrows() {
            let (px0, py0, px1, py1) = (p[[j, 0]], p[[j, 1]], p[[j, 2]], p[[j, 3]]);
            if !(sx0 <= px1 && sx1 >= px0 && sy0 <= py1 && sy1 >= py0) {
                continue;
            }
            let mut lo = 0.0f64;
            let mut hi = 1.0f64;
            let mut ok = true;
            for (d, q) in [(-dx, x0 - px0), (dx, px1 - x0), (-dy, y0 - py0), (dy, py1 - y0)] {
                let zero = d.abs() < 1e-12;
                if zero && q < 0.0 {
                    ok = false;
                }
                let t = q / if zero { 1.0 } else { d };
                if !zero && d < 0.0 {
                    lo = np_max(lo, t);
                }
                if !zero && d > 0.0 {
                    hi = np_min(hi, t);
                }
            }
            if ok && lo <= hi {
                ii.push(i as i64);
                jj.push(j as i64);
            }
        }
    }
    (ii.into_pyarray(py), jj.into_pyarray(py))
}

/// numpy's sign of `simple._orient`: (bx - ax)*(cy - ay) - (by - ay)*(cx - ax).
#[inline]
fn orient(ax: f64, ay: f64, bx: f64, by: f64, cx: f64, cy: f64) -> f64 {
    let v = (bx - ax) * (cy - ay) - (by - ay) * (cx - ax);
    if v > 0.0 {
        1.0
    } else if v < 0.0 {
        -1.0
    } else {
        v // 0.0, or NaN as np.sign gives it
    }
}

#[inline]
fn proper_cross(a: [f64; 4], b: [f64; 4]) -> bool {
    let o1 = orient(a[0], a[1], a[2], a[3], b[0], b[1]);
    let o2 = orient(a[0], a[1], a[2], a[3], b[2], b[3]);
    if !(o1 * o2 < 0.0) {
        return false;
    }
    let o3 = orient(b[0], b[1], b[2], b[3], a[0], a[1]);
    let o4 = orient(b[0], b[1], b[2], b[3], a[2], a[3]);
    o3 * o4 < 0.0
}

/// `simple.crossings`: how many (a row, b row) pairs cross properly
/// (each strictly on either side of the other's line). With `labels`
/// (`b` None) the pairs are those of `a` with itself, i < j, of different
/// labels -- a set of nets' edges among themselves.
#[pyfunction]
#[pyo3(signature = (a, b=None, labels=None))]
pub fn seg_crossings(
    a: PyReadonlyArray2<f64>,
    b: Option<PyReadonlyArray2<f64>>,
    labels: Option<PyReadonlyArray1<i64>>,
) -> i64 {
    let a = a.as_array();
    let row = |m: &numpy::ndarray::ArrayView2<f64>, i: usize| [m[[i, 0]], m[[i, 1]], m[[i, 2]], m[[i, 3]]];
    let mut count = 0i64;
    match b {
        Some(b) => {
            let b = b.as_array();
            for i in 0..a.nrows() {
                let ra = row(&a, i);
                for j in 0..b.nrows() {
                    if proper_cross(ra, row(&b, j)) {
                        count += 1;
                    }
                }
            }
        }
        None => {
            let lab = labels.map(|l| l.as_array().to_vec());
            for i in 0..a.nrows() {
                let ra = row(&a, i);
                for j in (i + 1)..a.nrows() {
                    if let Some(l) = &lab {
                        if l[i] == l[j] {
                            continue;
                        }
                    }
                    if proper_cross(ra, row(&a, j)) {
                        count += 1;
                    }
                }
            }
        }
    }
    count
}

/// `seg_crossings`, returning the crossing pairs instead of their count:
/// (ii, jj) over (a row, b row) in row-major order, or with `labels` (`b`
/// None) over a's rows with i < j of different labels.
#[pyfunction]
#[pyo3(signature = (a, b=None, labels=None))]
pub fn seg_cross_pairs<'py>(
    py: Python<'py>,
    a: PyReadonlyArray2<'py, f64>,
    b: Option<PyReadonlyArray2<'py, f64>>,
    labels: Option<PyReadonlyArray1<'py, i64>>,
) -> (Bound<'py, PyArray1<i64>>, Bound<'py, PyArray1<i64>>) {
    let a = a.as_array();
    let row = |m: &numpy::ndarray::ArrayView2<f64>, i: usize| [m[[i, 0]], m[[i, 1]], m[[i, 2]], m[[i, 3]]];
    let mut ii: Vec<i64> = Vec::new();
    let mut jj: Vec<i64> = Vec::new();
    match b {
        Some(b) => {
            let b = b.as_array();
            for i in 0..a.nrows() {
                let ra = row(&a, i);
                for j in 0..b.nrows() {
                    if proper_cross(ra, row(&b, j)) {
                        ii.push(i as i64);
                        jj.push(j as i64);
                    }
                }
            }
        }
        None => {
            let lab = labels.map(|l| l.as_array().to_vec());
            for i in 0..a.nrows() {
                let ra = row(&a, i);
                for j in (i + 1)..a.nrows() {
                    if let Some(l) = &lab {
                        if l[i] == l[j] {
                            continue;
                        }
                    }
                    if proper_cross(ra, row(&a, j)) {
                        ii.push(i as i64);
                        jj.push(j as i64);
                    }
                }
            }
        }
    }
    (ii.into_pyarray(py), jj.into_pyarray(py))
}

/// The window `_win` clips the box [x0, x1] x [y0, y1] to, and each cell's
/// overlap with the box along x (`cx`) and y (`cy`), `ex` / `ey` the grid
/// lines. None when the window is empty.
#[allow(clippy::too_many_arguments)]
fn box_cover(
    x0: f64,
    x1: f64,
    y0: f64,
    y1: f64,
    ex: &numpy::ndarray::ArrayView1<f64>,
    ey: &numpy::ndarray::ArrayView1<f64>,
    gx0: f64,
    gy0: f64,
    cell: f64,
    nx: i64,
    ny: i64,
) -> Option<(i64, i64, i64, i64, Vec<f64>, Vec<f64>)> {
    let clamp = |v: f64, hi: i64| -> i64 { (v as i64).clamp(0, hi) };
    let j0 = clamp(((x0 - gx0) / cell).floor(), nx);
    let j1 = clamp(((x1 - gx0) / cell).floor() + 1.0, nx);
    let i0 = clamp(((y0 - gy0) / cell).floor(), ny);
    let i1 = clamp(((y1 - gy0) / cell).floor() + 1.0, ny);
    if i1 <= i0 || j1 <= j0 {
        return None;
    }
    let cx: Vec<f64> = (j0..j1)
        .map(|j| clip0(npy_min(ex[(j + 1) as usize], x1) - npy_max(ex[j as usize], x0)))
        .collect();
    let cy: Vec<f64> = (i0..i1)
        .map(|i| clip0(npy_min(ey[(i + 1) as usize], y1) - npy_max(ey[i as usize], y0)))
        .collect();
    Some((i0, i1, j0, j1, cy, cx))
}

/// `simple.Model._net_demand` as its two factors: a tree's share of the
/// demand is s * outer(cy, cx) over the window (i0, i1, j0, j1) -- the
/// edges' box grown by `h`, each cell's overlap with it along y and x, and
/// s = length / box area. None when the tree or the window is empty.
#[pyfunction]
#[allow(clippy::too_many_arguments)]
#[allow(clippy::type_complexity)]
pub fn net_box<'py>(
    py: Python<'py>,
    e: PyReadonlyArray2<'py, f64>,
    length: f64,
    h: f64,
    ex: PyReadonlyArray1<'py, f64>,
    ey: PyReadonlyArray1<'py, f64>,
    gx0: f64,
    gy0: f64,
    cell: f64,
    nx: i64,
    ny: i64,
) -> Option<(i64, i64, i64, i64, Bound<'py, PyArray1<f64>>, Bound<'py, PyArray1<f64>>, f64)> {
    let e = e.as_array();
    let m = e.nrows();
    if m == 0 {
        return None;
    }
    let colmin = |c: usize| (1..m).fold(e[[0, c]], |acc, i| npy_min(acc, e[[i, c]]));
    let colmax = |c: usize| (1..m).fold(e[[0, c]], |acc, i| npy_max(acc, e[[i, c]]));
    let x0 = py_min(colmin(0), colmin(2)) - h;
    let x1 = py_max(colmax(0), colmax(2)) + h;
    let y0 = py_min(colmin(1), colmin(3)) - h;
    let y1 = py_max(colmax(1), colmax(3)) + h;
    let (i0, i1, j0, j1, cy, cx) = box_cover(x0, x1, y0, y1, &ex.as_array(), &ey.as_array(), gx0, gy0, cell, nx, ny)?;
    let s = length / ((x1 - x0) * (y1 - y0));
    Some((i0, i1, j0, j1, cy.into_pyarray(py), cx.into_pyarray(py), s))
}

/// `simple.Model._part_use`: a part's pad rects `a` (rows x0, x1, y0, y1,
/// already grown) and `on` (row p, column L: pad p is on layer L), as the
/// union window of the rects and, per layer some pad is on, the sum over its
/// pads of outer(cy, cx) over that window. None when the window is empty.
#[pyfunction]
#[allow(clippy::too_many_arguments)]
#[allow(clippy::type_complexity)]
pub fn pad_use<'py>(
    py: Python<'py>,
    a: PyReadonlyArray2<'py, f64>,
    on: PyReadonlyArray2<'py, bool>,
    ex: PyReadonlyArray1<'py, f64>,
    ey: PyReadonlyArray1<'py, f64>,
    gx0: f64,
    gy0: f64,
    cell: f64,
    nx: i64,
    ny: i64,
) -> Option<(i64, i64, i64, i64, Vec<(i64, Bound<'py, PyArray2<f64>>)>)> {
    let a = a.as_array();
    let on = on.as_array();
    let k = a.nrows();
    if k == 0 {
        return None;
    }
    let ex = ex.as_array();
    let ey = ey.as_array();
    let fold = |c: usize, f: fn(f64, f64) -> f64| (1..k).fold(a[[0, c]], |acc, i| f(acc, a[[i, c]]));
    let (wx0, wx1, wy0, wy1) = (fold(0, f64::min), fold(1, f64::max), fold(2, f64::min), fold(3, f64::max));
    let (i0, i1, j0, j1, _cy, _cx) = box_cover(wx0, wx1, wy0, wy1, &ex, &ey, gx0, gy0, cell, nx, ny)?;
    let (h, w) = ((i1 - i0) as usize, (j1 - j0) as usize);
    let nl = on.ncols();
    let mut blocks: Vec<Option<Vec<f64>>> = vec![None; nl];
    for p in 0..k {
        let (x0, x1, y0, y1) = (a[[p, 0]], a[[p, 1]], a[[p, 2]], a[[p, 3]]);
        let cx: Vec<f64> = (j0..j1)
            .map(|j| clip0(npy_min(ex[(j + 1) as usize], x1) - npy_max(ex[j as usize], x0)))
            .collect();
        let cy: Vec<f64> = (i0..i1)
            .map(|i| clip0(npy_min(ey[(i + 1) as usize], y1) - npy_max(ey[i as usize], y0)))
            .collect();
        for l in 0..nl {
            if !on[[p, l]] {
                continue;
            }
            let b = blocks[l].get_or_insert_with(|| vec![0.0; h * w]);
            for (r, yv) in cy.iter().enumerate() {
                if *yv == 0.0 {
                    continue;
                }
                let row = &mut b[r * w..(r + 1) * w];
                for (q, xv) in cx.iter().enumerate() {
                    row[q] += yv * xv;
                }
            }
        }
    }
    let out = blocks
        .into_iter()
        .enumerate()
        .filter_map(|(l, b)| {
            b.map(|b| (l as i64, Array2::from_shape_vec((h, w), b).expect("block").into_pyarray(py)))
        })
        .collect();
    Some((i0, i1, j0, j1, out))
}

/// `simple.Model._cong_price`: the change a move makes to the overflow total
/// sum(clip(demand - capacity / pitch, 0)^2) over the window (i0, i1, j0,
/// j1). `d` is the committed demand and `c` the committed capacity per cell
/// (over the layers, clip(cell2 - use, 0) summed), `uses` the committed use
/// per layer. Each demand term (s, i0, j0, cy, cx) adds s * cy[a] * cx[b] at
/// cell (i0 + a, j0 + b); each use term (layer, sign, i0, j0, block) adds
/// sign * block to that layer's use, and the capacity under the use terms is
/// recomputed. Only cells a term reaches are visited.
#[pyfunction]
#[allow(clippy::too_many_arguments)]
#[allow(clippy::type_complexity)]
pub fn cong_delta(
    d: PyReadonlyArray2<f64>,
    c: PyReadonlyArray2<f64>,
    uses: Vec<PyReadonlyArray2<f64>>,
    win: (usize, usize, usize, usize),
    dem: Vec<(f64, usize, usize, PyReadonlyArray1<f64>, PyReadonlyArray1<f64>)>,
    usev: Vec<(usize, f64, usize, usize, PyReadonlyArray2<f64>)>,
    cell2: f64,
    pitch: f64,
) -> f64 {
    let d = d.as_array();
    let c = c.as_array();
    let (i0, i1, j0, j1) = win;
    if i1 <= i0 || j1 <= j0 {
        return 0.0;
    }
    let (h, w) = (i1 - i0, j1 - j0);
    let mut db = vec![0.0f64; h * w];
    for (s, ti0, tj0, cy, cx) in &dem {
        let cy = cy.as_array();
        let cx = cx.as_array();
        for (a, yv) in cy.iter().enumerate() {
            let ya = s * yv;
            if ya == 0.0 {
                continue;
            }
            let r = ti0 + a - i0;
            let row = &mut db[r * w + (tj0 - j0)..r * w + (tj0 - j0) + cx.len()];
            for (q, xv) in cx.iter().enumerate() {
                row[q] += ya * xv;
            }
        }
    }
    // the use terms' union box, its new use per layer, then its capacity
    let (mut ui0, mut ui1, mut uj0, mut uj1) = (usize::MAX, 0usize, usize::MAX, 0usize);
    for (_l, _s, ti0, tj0, b) in &usev {
        let b = b.as_array();
        ui0 = ui0.min(*ti0);
        uj0 = uj0.min(*tj0);
        ui1 = ui1.max(ti0 + b.nrows());
        uj1 = uj1.max(tj0 + b.ncols());
    }
    let mut cap_u: Vec<f64> = Vec::new();
    let (uh, uw) = if ui1 > ui0 && uj1 > uj0 { (ui1 - ui0, uj1 - uj0) } else { (0, 0) };
    if uh > 0 {
        let nl = uses.len();
        let mut du = vec![0.0f64; nl * uh * uw];
        for (l, s, ti0, tj0, b) in &usev {
            let b = b.as_array();
            for r in 0..b.nrows() {
                for q in 0..b.ncols() {
                    du[l * uh * uw + (ti0 - ui0 + r) * uw + (tj0 - uj0 + q)] += s * b[[r, q]];
                }
            }
        }
        let us: Vec<_> = uses.iter().map(|u| u.as_array()).collect();
        cap_u = vec![0.0f64; uh * uw];
        for r in 0..uh {
            for q in 0..uw {
                let mut cap = 0.0f64;
                for (l, u) in us.iter().enumerate() {
                    cap += clip0(cell2 - (u[[ui0 + r, uj0 + q]] + du[l * uh * uw + r * uw + q]));
                }
                cap_u[r * uw + q] = cap;
            }
        }
    }
    let mut acc = 0.0f64;
    for r in 0..h {
        let gi = i0 + r;
        let in_r = uh > 0 && gi >= ui0 && gi < ui1;
        for q in 0..w {
            let gj = j0 + q;
            let dd = db[r * w + q];
            let in_u = in_r && gj >= uj0 && gj < uj1;
            if dd == 0.0 && !in_u {
                continue;
            }
            let dold = d[[gi, gj]];
            let cold = c[[gi, gj]];
            let cnew = if in_u { cap_u[(gi - ui0) * uw + (gj - uj0)] } else { cold };
            let oo = clip0(dold - cold / pitch);
            let on = clip0(dold + dd - cnew / pitch);
            acc += on * on - oo * oo;
        }
    }
    acc
}

/// Above this many points `mst_l1_tree` builds its tree from octant
/// neighbours rather than by Prim: the same length, other ties.
const SPARSE_MST_MIN: usize = 160;

/// f64 to a key that sorts as the float does (no NaN expected).
#[inline]
fn ord_key(v: f64) -> u64 {
    let b = v.to_bits();
    if b >> 63 == 1 {
        !b
    } else {
        b | (1u64 << 63)
    }
}

/// The Manhattan minimum spanning tree of (xs, ys) as (length, a, b) edges:
/// every point's nearest neighbour in each of the eight octants by a sweep
/// (the candidate set contains a minimum spanning tree), then Kruskal over
/// them, lighter first, ties by index.
fn manhattan_mst(xs: &[f64], ys: &[f64]) -> Vec<(f64, usize, usize)> {
    use std::collections::BTreeMap;
    let n = xs.len();
    let (mut px, mut py) = (xs.to_vec(), ys.to_vec());
    let mut id: Vec<usize> = (0..n).collect();
    let mut cand: Vec<(f64, usize, usize)> = Vec::new();
    for k in 0..4 {
        id.sort_by(|&i, &j| (px[i] + py[i]).partial_cmp(&(px[j] + py[j])).unwrap().then(i.cmp(&j)));
        let mut sweep: BTreeMap<(u64, usize), usize> = BTreeMap::new();
        for &i in &id {
            let mut gone = Vec::new();
            for (&key, &j) in sweep.range((ord_key(-py[i]), 0usize)..) {
                if py[i] - py[j] > px[i] - px[j] {
                    break;
                }
                cand.push(((xs[i] - xs[j]).abs() + (ys[i] - ys[j]).abs(), i.min(j), i.max(j)));
                gone.push(key);
            }
            for key in gone {
                sweep.remove(&key);
            }
            sweep.insert((ord_key(-py[i]), i), i);
        }
        for p in 0..n {
            if k & 1 == 1 {
                px[p] = -px[p];
            } else {
                std::mem::swap(&mut px[p], &mut py[p]);
            }
        }
    }
    cand.sort_by(|a, b| a.0.partial_cmp(&b.0).unwrap().then(a.1.cmp(&b.1)).then(a.2.cmp(&b.2)));
    let mut parent: Vec<usize> = (0..n).collect();
    fn find(p: &mut Vec<usize>, mut x: usize) -> usize {
        while p[x] != x {
            p[x] = p[p[x]];
            x = p[x];
        }
        x
    }
    let mut out = Vec::with_capacity(n.saturating_sub(1));
    for (w, a, b) in cand {
        let (ra, rb) = (find(&mut parent, a), find(&mut parent, b));
        if ra != rb {
            parent[ra] = rb;
            out.push((w, a, b));
            if out.len() + 1 == n {
                break;
            }
        }
    }
    out
}
