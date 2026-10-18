//! Placement kernels: the quench's airwire crossing test and its minimum
//! spanning tree, as `py_placer/placement/quench.py` computes them, done in
//! Rust because the quench calls them for every candidate pose.
//!
//! Both are the Python computations they replace, operation for operation:
//! the same float expressions in the same order (Rust does not fuse a
//! multiply into an add), the same tie rules, the same pair order. A placement
//! is therefore the same with them as it was without them.

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
// against pads and against each other, and the congestion window. Each gives
// the values the Python code it replaced gave, bit for bit: the same float
// operations in the same order, the same tie rules, numpy's own semantics
// where numpy computed it (its min/max on signed zeros, its pairwise sum).

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
    if n >= 2 {
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
        for i in 0..m.nrows() {
            let ri = row(&m, i);
            for j in 0..o.nrows() {
                if !skip.contains(&ol[j]) && proper_cross(ri, row(&o, j)) {
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
    for i in 0..s.nrows() {
        if skip_n.contains(&labs[i]) {
            continue;
        }
        let (x0, y0, x1, y1) = (s[[i, 0]], s[[i, 1]], s[[i, 2]], s[[i, 3]]);
        let (sx0, sx1) = (np_min(x0, x1), np_max(x0, x1));
        let (sy0, sy1) = (np_min(y0, y1), np_max(y0, y1));
        let dx = x1 - x0;
        let dy = y1 - y0;
        for j in 0..p.nrows() {
            if skip_o.contains(&po[j]) {
                continue;
            }
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

/// numpy's pairwise float64 sum (`DOUBLE_pairwise_sum`): fewer than 8 values
/// added in order; up to 128 in eight running sums joined as
/// ((r0 + r1) + (r2 + r3)) + ((r4 + r5) + (r6 + r7)) and the remainder added
/// after; more, split at half rounded down to a multiple of 8.
fn pairwise_sum(a: &[f64]) -> f64 {
    let n = a.len();
    if n < 8 {
        let mut res = 0.0f64;
        for v in a {
            res += *v;
        }
        res
    } else if n <= 128 {
        let mut r = [a[0], a[1], a[2], a[3], a[4], a[5], a[6], a[7]];
        let mut i = 8;
        while i < n - (n % 8) {
            for k in 0..8 {
                r[k] += a[i + k];
            }
            i += 8;
        }
        let mut res = ((r[0] + r[1]) + (r[2] + r[3])) + ((r[4] + r[5]) + (r[6] + r[7]));
        while i < n {
            res += a[i];
            i += 1;
        }
        res
    } else {
        let mut n2 = n / 2;
        n2 -= n2 % 8;
        pairwise_sum(&a[..n2]) + pairwise_sum(&a[n2..])
    }
}

/// `simple.Model._over2_on`: capacity per cell, cap = 0 then for each layer
/// cap += clip(cell2 - use, 0); overflow = clip(demand - cap / pitch, 0); the
/// overflow squared summed row by row as numpy's `.sum()` does.
#[pyfunction]
pub fn over2(dw: PyReadonlyArray2<f64>, uses: Vec<PyReadonlyArray2<f64>>, cell2: f64, pitch: f64) -> f64 {
    let d = dw.as_array();
    let us: Vec<_> = uses.iter().map(|u| u.as_array()).collect();
    let (h, w) = (d.nrows(), d.ncols());
    let mut sq: Vec<f64> = Vec::with_capacity(h * w);
    for i in 0..h {
        for j in 0..w {
            let mut cap = 0.0f64;
            for u in &us {
                cap += clip0(cell2 - u[[i, j]]);
            }
            let o = clip0(d[[i, j]] - cap / pitch);
            sq.push(o * o);
        }
    }
    pairwise_sum(&sq)
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

/// `simple.Model._net_demand` from a priced tree: the edges' box grown by `h`
/// (x from columns 0 and 2, y from 1 and 3: numpy `min()` of each column,
/// then Python's `min` of the two), its grid window as `_win` clips it, and
/// the block outer(cy, cx) * (length / box area), cx and cy each cell's
/// overlap with the box (`ex`, `ey` the grid lines). None when the tree is
/// empty or the window is.
#[pyfunction]
#[allow(clippy::too_many_arguments)]
pub fn net_demand<'py>(
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
) -> Option<(i64, i64, i64, i64, Bound<'py, PyArray2<f64>>)> {
    let e = e.as_array();
    let ex = ex.as_array();
    let ey = ey.as_array();
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
    let s = length / ((x1 - x0) * (y1 - y0));
    let mut b: Vec<f64> = Vec::with_capacity(cx.len() * cy.len());
    for yv in &cy {
        for xv in &cx {
            b.push((yv * xv) * s);
        }
    }
    let blk = Array2::from_shape_vec((cy.len(), cx.len()), b).expect("block");
    Some((i0, i1, j0, j1, blk.into_pyarray(py)))
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
