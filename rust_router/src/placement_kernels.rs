//! Placement kernels: the quench's airwire crossing test and its minimum
//! spanning tree, as `py_placer/placement/quench.py` computes them, done in
//! Rust because the quench calls them for every candidate pose.
//!
//! Both are the Python computations they replace, operation for operation:
//! the same float expressions in the same order (Rust does not fuse a
//! multiply into an add), the same tie rules, the same pair order. A placement
//! is therefore the same with them as it was without them.

use numpy::{IntoPyArray, PyArray1, PyReadonlyArray1, PyReadonlyArray2};
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
