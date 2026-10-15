#!/usr/bin/env python3
"""#666 on the GUI front: the Route tab applies the scoped cap move the CLI applies.

    python3 tests/gui_parity/test_666_cap_move_gui.py

(re-execs into KiCad's bundled python automatically, like its siblings)

THE GAP. net_rescue's permissive bare-ball retry ships an escape via that lands
on a MOVABLE cap, and queues the cap's verified relocation on
`pcb_data._pending_cap_moves`. route.py applied it in its FILE-mode block only:
`write_placed_output` on the written board, the move mirrored into pcb_data,
the cap's nets re-welded by `oracle_reconnect` -- all before the plane
finalize. The Route tab (return_results=True, no file) never did: it shipped
the via and left the cap on it, a cap-to-via conflict the CLI resolves.

The board: cap_chain (two 0402s, C1/C2, between J1 and J2) with a B.Cu pour
on DPA_N added through pcbnew, so the plane finalize's oracle leg runs; the
run routes DPA_P and DPA_N on both fronts with the same kwargs, including a
power width, a class clearance and a .kicad_dru layer rule, so every by-name
map the re-weld is handed has a value to compare. The real
permissive retry needs a fine-pitch array whose only escape site is under a
cap, which no small board in the repo has, so the rescue is WRAPPED (the real
one still runs) to queue one move exactly as net_rescue spells it: C1, 0.7 mm
east. `kicad_oracle.oracle_reconnect` is a spy that records what each call was
handed -- its nets, its config, its keyword arguments, and where C1 sits in the
board FILE it was given -- and welds nothing. Arms:

  1. CLI, file mode (the reference): the written board has C1 at the target;
     the re-weld oracle ran on C1's nets; every oracle call saw C1 moved.
  2. GUI engine (return_results=True on pcbnew-built PCBData, with the tab's
     stage_board_fn): results_data['pending_cap_moves'] carries the move and
     the re-weld spec, and that spec is what the CLI handed its re-weld; the
     model was mirrored (C1 and its pads at the target) BEFORE the plane
     finalize, as the CLI's is -- read by the spy when the finalize's oracle
     leg runs -- and that leg's STAGED board, the GUI's stand-in for the
     CLI's written file, already has C1 moved.
  3. GUI apply (the REAL RoutingDialog._apply_results_to_board_body on the
     live pcbnew board): C1 and its pads moved by the offset, C2 untouched;
     the re-weld oracle called once, on C1's nets, with the CLI's config and
     keyword arguments, on a staged save where C1 already sits at the target;
     the dialog's pcb_data agrees.
  4. Non-vacuity: the wrapped rescue queued the move on both fronts, and the
     target differs from C1's pose.

Needs pcbnew; exits 2 when no python with pcbnew+wx is found -- it does NOT
self-skip, so it cannot report a gap it never looked at as closed.
"""
import contextlib
import glob
import io
import os
import shutil
import subprocess
import sys
import tempfile

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

sys.path.insert(0, os.path.join(REPO, 'py_router'))
from kicad_locate import path_version_key  # noqa: E402
del sys.path[0]    # this file orders its own sys.path further down
KICAD_PYTHONS = [
    "/Applications/KiCad/KiCad.app/Contents/Frameworks/Python.framework/"
    "Versions/Current/bin/python3",
    "/usr/bin/python3",
    os.path.expandvars(r"C:\Program Files\KiCad\bin\python.exe"),
    *sorted(glob.glob(r"C:\Program Files\KiCad\*\bin\python.exe"),
            key=path_version_key, reverse=True),
]

BOARD = os.path.join(REPO, 'kicad_files', 'cap_chain.kicad_pcb')
CAP, OTHER = 'C1', 'C2'
DX = 0.7
POUR_NET, POUR_LAYER = 'DPA_N', 'B.Cu'
NETS = ['DPA_P', 'DPA_N']
# Same kwargs on both fronts. A power width, a per-net class clearance and a
# .kicad_dru layer rule give every by-name map the re-weld carries a value, so
# comparing the two fronts' maps is not comparing two empty dicts. The scalars
# sit off the oracle helper's fallbacks (hole-to-hole 0.20, track-to-via 0.8),
# so an argument the applier dropped cannot match by coincidence.
KW = dict(layers=['F.Cu', 'B.Cu'], track_width=0.2, clearance=0.2,
          via_size=0.6, via_drill=0.3, grid_step=0.1,
          hole_to_hole_clearance=0.25, layer_costs=[1.0, 3.0],
          power_nets=['DPB_P'], power_nets_widths=[0.4])
CLASS_NET, CLASS_CLEARANCE = 'DPB_P', 0.25
DRU = ('(version 1)\n(rule "b_tight" (layer "B.Cu") '
       '(constraint clearance (min 0.25mm)))\n')
CFG_FIELDS = ('clearance', 'track_width', 'via_size', 'via_drill', 'grid_step',
              'layers', 'layer_costs', 'layer_clearances')
KW_FIELDS = ('track_via_clearance', 'hole_to_hole_clearance',
             'net_clearances_by_name', 'net_widths_by_name')


def _reexec_into_kicad():
    for cand in KICAD_PYTHONS:
        if cand == sys.executable or not os.path.exists(cand):
            continue
        if subprocess.run([cand, '-c', 'import pcbnew, wx'],
                          capture_output=True).returncode == 0:
            # os.execv re-splits argv on spaces on Windows ("Program Files").
            sys.exit(subprocess.run([cand, os.path.abspath(__file__)]
                                    + sys.argv[1:]).returncode)
    print("ERROR: no python with pcbnew+wx found; this gate does not self-skip")
    sys.exit(2)


FAILURES = []


def check(name, cond, info=""):
    if not cond:
        FAILURES.append(name)
    print(("  PASS " if cond else "  FAIL ") + name + (f"  ({info})" if info else ""))


class _Patched:
    """Swap one module attribute for the duration of a `with`."""

    def __init__(self, module, name, value):
        self.module, self.name, self.value = module, name, value

    def __enter__(self):
        self.orig = getattr(self.module, self.name)
        setattr(self.module, self.name, self.value)
        return self

    def __exit__(self, *exc):
        setattr(self.module, self.name, self.orig)
        return False


def _stage_fixture(d):
    """cap_chain + a pcbnew-authored B.Cu pour on DPA_N, saved with pcbnew."""
    import pcbnew
    from kicad_parser import mm_to_iu
    src = os.path.join(d, 'src.kicad_pcb')
    shutil.copyfile(BOARD, src)
    b = pcbnew.LoadBoard(src)
    z = pcbnew.ZONE(b)
    z.SetNet(b.FindNet(POUR_NET))
    z.SetLayer(b.GetLayerID(POUR_LAYER))
    ol = z.Outline()
    ol.NewOutline()
    for x, y in ((6, 6), (30, 6), (30, 24), (6, 24)):
        ol.Append(mm_to_iu(x), mm_to_iu(y))
    b.Add(z)
    pcbnew.ZONE_FILLER(b).Fill(b.Zones())
    out = os.path.join(d, 'fixture.kicad_pcb')
    pcbnew.SaveBoard(out, b)
    with open(os.path.splitext(out)[0] + '.kicad_dru', 'w') as f:
        f.write(DRU)
    return out


def _class_map(nets):
    """{net id: clearance} for CLASS_NET on THIS front's net numbering."""
    return {nid: CLASS_CLEARANCE for nid, n in nets.items()
            if n.name == CLASS_NET}


def _fresh(d, fixture, tag):
    """A fresh copy (+ siblings) per arm: one path per pcbnew load."""
    sub = os.path.join(d, tag)
    os.makedirs(sub)
    dst = os.path.join(sub, 'b.kicad_pcb')
    stem = os.path.splitext(fixture)[0]
    for ext in ('.kicad_pcb', '.kicad_pro', '.kicad_prl', '.kicad_dru'):
        if os.path.exists(stem + ext):
            shutil.copyfile(stem + ext, os.path.splitext(dst)[0] + ext)
    return dst


class _Arm:
    """One arm's rescue wrapper and oracle spy."""

    def __init__(self, model=None):
        self.queued = None
        self.calls = []
        self.model = model      # the GUI's PCBData, read at each oracle call

    def rescue(self, real):
        def _wrapped(state, *a, **kw):
            out = real(state, *a, **kw)
            pcb = state.pcb_data
            if self.queued is None:
                fp = pcb.footprints[CAP]
                self.queued = {
                    'reference': CAP, 'new_x': fp.x + DX, 'new_y': fp.y,
                    'new_rotation': fp.rotation,
                    'net_ids': sorted({p.net_id for p in fp.pads if p.net_id}),
                    'from': (fp.x, fp.y)}
                q = getattr(pcb, '_pending_cap_moves', None)
                if q is None:
                    q = pcb._pending_cap_moves = []
                q.append({k: v for k, v in self.queued.items() if k != 'from'})
            return out
        return _wrapped

    def oracle(self, board_file, net_names, config, *a, **kw):
        from kicad_parser import parse_kicad_pcb
        try:
            fp = parse_kicad_pcb(board_file).footprints.get(CAP)
            where = (fp.x, fp.y) if fp is not None else None
        except Exception as e:                                  # noqa: BLE001
            where = f"unparsed: {e}"
        mfp = (self.model.footprints.get(CAP)
               if self.model is not None else None)
        self.calls.append({
            'nets': sorted(net_names or []),
            'cfg': {f: _norm(getattr(config, f, None)) for f in CFG_FIELDS},
            'kw': {f: _norm(kw.get(f)) for f in KW_FIELDS},
            'cap_at': where,
            'model_cap_at': (mfp.x, mfp.y) if mfp is not None else None})
        return {'available': True, 'rounds': 0, 'links_routed': 0,
                'links_failed': 0, 'remaining': 0, 'new_segments': [],
                'new_vias': [], 'removed_segments': [], 'removed_vias': []}


def _norm(v):
    if isinstance(v, dict):
        return {k: _norm(x) for k, x in sorted(v.items())}
    if isinstance(v, (list, tuple)):
        return [_norm(x) for x in v]
    if isinstance(v, float):
        return round(v, 9)
    return v


def _near(a, b, tol=1e-6):
    return (a is not None and b is not None and not isinstance(a, str)
            and abs(a[0] - b[0]) < tol and abs(a[1] - b[1]) < tol)


def _quiet(fn):
    """Run fn with its stdout captured; return (result, the #666 lines)."""
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        out = fn()
    lines = [ln.strip() for ln in buf.getvalue().splitlines() if '#666' in ln]
    return out, lines


def main():
    try:
        import wx  # noqa: F401
        import pcbnew  # noqa: F401
    except ImportError:
        _reexec_into_kicad()

    os.environ.setdefault('WXSUPPRESS_SIZER_FLAGS_CHECK', '1')
    import wx
    import pcbnew
    sys.path.insert(0, REPO)
    for sub in ('py_router', 'py_placer', 'py_tools'):
        sys.path.insert(0, os.path.join(REPO, sub))
    sys.path.insert(0, os.path.dirname(REPO))

    app = wx.App(False)  # noqa: F841 - must outlive the dialog
    wx.MessageBox = lambda *a, **k: wx.OK
    import kicad_oracle
    import net_rescue
    from kicad_parser import parse_kicad_pcb, build_pcb_data_from_board
    from kicad_routing_plugin import swig_gui
    from route import batch_route

    d = tempfile.mkdtemp(prefix='test666_')
    try:
        fixture = _stage_fixture(d)
        real_rescue = net_rescue.rescue_failed_nets

        # ---------------------------------------------------------- 1. CLI
        print("-- 1. CLI, file mode (the reference) --")
        cli = _Arm()
        cli_in = _fresh(d, fixture, 'cli')
        cli_out = os.path.join(os.path.dirname(cli_in), 'out.kicad_pcb')
        with _Patched(net_rescue, 'rescue_failed_nets', cli.rescue(real_rescue)), \
                _Patched(kicad_oracle, 'oracle_reconnect', cli.oracle):
            _, lines = _quiet(lambda: batch_route(
                cli_in, cli_out, list(NETS),
                net_clearances=_class_map(parse_kicad_pcb(cli_in).nets), **KW))
        for ln in lines:
            print(f"     | {ln}")
        check("the wrapped rescue queued the move (CLI)", cli.queued is not None)
        if cli.queued is None:
            raise SystemExit(_verdict())
        target = (cli.queued['new_x'], cli.queued['new_y'])
        check("the target differs from C1's pose",
              not _near(target, cli.queued['from']),
              f"{cli.queued['from']} -> {target}")
        written = parse_kicad_pcb(cli_out).footprints
        check("the written board has C1 at the target",
              _near((written[CAP].x, written[CAP].y), target),
              f"({written[CAP].x}, {written[CAP].y})")
        cap_nets = sorted(parse_kicad_pcb(cli_in).nets[n].name
                          for n in cli.queued['net_ids'])
        cli_reweld = [c for c in cli.calls if c['nets'] == cap_nets]
        check("the CLI re-welded C1's nets once", len(cli_reweld) == 1,
              f"{len(cli_reweld)} call(s) on {cap_nets}; all: "
              f"{[c['nets'] for c in cli.calls]}")
        if cli_reweld:
            r0 = cli_reweld[0]
            check("the CLI re-weld's by-name maps all carry a value "
                  "(non-vacuity)",
                  (r0['cfg']['layer_clearances'] or {}).get('B.Cu') == 0.25
                  and (r0['kw']['net_clearances_by_name'] or {}).get(
                      CLASS_NET) == CLASS_CLEARANCE
                  and ((r0['kw']['net_widths_by_name'] or {}).get(
                      'power_net_widths') or {}).get('DPB_P') == 0.4,
                  f"{r0['cfg']['layer_clearances']}, {r0['kw']}")
        check("every CLI oracle call saw C1 at the target",
              cli.calls and all(_near(c['cap_at'], target) for c in cli.calls),
              f"{[(c['nets'], c['cap_at']) for c in cli.calls]}")
        cli_pour = [c for c in cli.calls if POUR_NET in c['nets']]
        check("the CLI's plane finalize ran its oracle leg (non-vacuity)",
              bool(cli_pour), f"{[c['nets'] for c in cli.calls]}")

        # ---------------------------------------------------- 2. GUI engine
        print("-- 2. GUI engine (return_results, the tab's stage_board_fn) --")
        gui_path = _fresh(d, fixture, 'gui')
        live = pcbnew.LoadBoard(gui_path)
        real_getboard = pcbnew.GetBoard
        pcbnew.GetBoard = lambda: live
        dialog = swig_gui.RoutingDialog(None, build_pcb_data_from_board(live),
                                        gui_path)
        dialog._suppress_completion_popups = True
        pcb = dialog.pcb_data
        gui = _Arm(model=pcb)
        c2_before = (pcb.footprints[OTHER].x, pcb.footprints[OTHER].y)
        c1_pads_before = [(p.global_x, p.global_y)
                          for p in pcb.footprints[CAP].pads]
        live_fps = {f.GetReference(): f for f in live.GetFootprints()}
        live_c1_pads = [(pcbnew.ToMM(p.GetPosition().x),
                         pcbnew.ToMM(p.GetPosition().y))
                        for p in live_fps[CAP].Pads()]
        live_c2 = live_fps[OTHER].GetPosition()

        def _stage():
            # The tab's _stage_live_board, minus the worker-thread marshal
            # (this runs on the main thread): a save of the LIVE board.
            with tempfile.NamedTemporaryFile(suffix='.kicad_pcb', dir=d,
                                             delete=False) as f:
                p = f.name
            pcbnew.SaveBoard(p, live, aSkipSettings=True)
            return p

        try:
            with _Patched(net_rescue, 'rescue_failed_nets',
                          gui.rescue(real_rescue)), \
                    _Patched(kicad_oracle, 'oracle_reconnect', gui.oracle):
                (ok, failed, _t, rd), lines = _quiet(lambda: batch_route(
                    gui_path, "", list(NETS), return_results=True,
                    pcb_data=pcb, stage_board_fn=_stage,
                    net_clearances=_class_map(pcb.nets), **KW))
            for ln in lines:
                print(f"     | {ln}")
            check("the wrapped rescue queued the move (GUI)",
                  gui.queued is not None)
            if gui.queued is None:
                raise SystemExit(_verdict())
            check("the GUI queued the same target as the CLI",
                  _near((gui.queued['new_x'], gui.queued['new_y']), target),
                  f"({gui.queued['new_x']}, {gui.queued['new_y']})")
            spec = rd.get('pending_cap_moves') or {}
            check("results_data['pending_cap_moves'] is posted", bool(spec),
                  f"keys: {sorted(rd)}")
            moves = spec.get('moves') or []
            check("it carries the queued move",
                  len(moves) == 1 and moves[0].get('reference') == CAP
                  and _near((moves[0].get('new_x'), moves[0].get('new_y')),
                            target), f"{moves}")
            check("it names C1's nets", spec.get('nets') == cap_nets,
                  f"{spec.get('nets')} vs {cap_nets}")
            if cli_reweld:
                want_cfg, want_kw = cli_reweld[0]['cfg'], cli_reweld[0]['kw']
                got_cfg = {f: _norm(spec.get(f)) for f in CFG_FIELDS}
                got_kw = {f: _norm(spec.get(f)) for f in KW_FIELDS}
                check("the posted re-weld config is what the CLI handed its "
                      "re-weld", got_cfg == want_cfg,
                      f"GUI {got_cfg} vs CLI {want_cfg}")
                check("the posted re-weld kwargs are what the CLI passed",
                      got_kw == want_kw, f"GUI {got_kw} vs CLI {want_kw}")
            fp1 = pcb.footprints[CAP]
            check("the model is mirrored (C1 at the target after the run)",
                  _near((fp1.x, fp1.y), target), f"({fp1.x}, {fp1.y})")
            check("... with its pads moved by the offset",
                  all(_near((p.global_x, p.global_y), (bx + DX, by))
                      for p, (bx, by) in zip(fp1.pads, c1_pads_before)),
                  f"{[(p.global_x, p.global_y) for p in fp1.pads]}")
            gui_pour = [c for c in gui.calls if POUR_NET in c['nets']]
            check("the GUI's plane finalize ran its oracle leg IN-RUN "
                  "(non-vacuity)", bool(gui_pour),
                  f"{[c['nets'] for c in gui.calls]}")
            check("the model already had C1 at the target when it ran (the "
                  "CLI's sequence point: its mirror precedes the finalize)",
                  gui_pour and all(_near(c['model_cap_at'], target)
                                   for c in gui_pour),
                  f"{[c['model_cap_at'] for c in gui_pour]}")
            check("its staged board has C1 at the target, as the CLI's "
                  "written board does",
                  gui_pour and all(_near(c['cap_at'], target) for c in gui_pour),
                  f"{[c['cap_at'] for c in gui_pour]}")

            # ------------------------------------------------- 3. GUI apply
            print("-- 3. GUI apply (the real _apply_results_to_board_body) --")
            gui.calls = []
            with _Patched(kicad_oracle, 'oracle_reconnect', gui.oracle), \
                    _Patched(kicad_oracle, 'find_kicad_cli',
                             lambda *a, **k: '/kicad-cli/stand-in'):
                _, lines = _quiet(lambda: dialog._apply_results_to_board_body(
                    rd, ok, failed, 0.0,
                    {'move_copper_text': False, 'fix_drc_settings': False}))
            for ln in lines:
                print(f"     | {ln}")
            live_fps = {f.GetReference(): f for f in live.GetFootprints()}
            pos = live_fps[CAP].GetPosition()
            check("the live board has C1 at the target",
                  _near((pcbnew.ToMM(pos.x), pcbnew.ToMM(pos.y)), target, 1e-5),
                  f"({pcbnew.ToMM(pos.x)}, {pcbnew.ToMM(pos.y)})")
            pads_now = [(pcbnew.ToMM(p.GetPosition().x),
                         pcbnew.ToMM(p.GetPosition().y))
                        for p in live_fps[CAP].Pads()]
            check("... with its pads moved by the offset",
                  all(_near(a, (bx + DX, by), 1e-5)
                      for a, (bx, by) in zip(pads_now, live_c1_pads)),
                  f"{pads_now} from {live_c1_pads}")
            c2_now = live_fps[OTHER].GetPosition()
            check("C2 is untouched on the live board",
                  (c2_now.x, c2_now.y) == (live_c2.x, live_c2.y))
            reweld = [c for c in gui.calls if c['nets'] == cap_nets]
            check("the apply re-welded C1's nets once", len(reweld) == 1,
                  f"{len(reweld)} call(s); all: {[c['nets'] for c in gui.calls]}")
            if reweld and cli_reweld:
                check("... with the CLI's re-weld config",
                      reweld[0]['cfg'] == cli_reweld[0]['cfg'],
                      f"GUI {reweld[0]['cfg']} vs CLI {cli_reweld[0]['cfg']}")
                check("... and the CLI's re-weld kwargs",
                      reweld[0]['kw'] == cli_reweld[0]['kw'],
                      f"GUI {reweld[0]['kw']} vs CLI {cli_reweld[0]['kw']}")
                check("... on a board where C1 already sits at the target",
                      _near(reweld[0]['cap_at'], target),
                      f"{reweld[0]['cap_at']}")
            fpd = dialog.pcb_data.footprints[CAP]
            check("the dialog's pcb_data agrees (C1 at the target)",
                  _near((fpd.x, fpd.y), target), f"({fpd.x}, {fpd.y})")
            fpo = dialog.pcb_data.footprints[OTHER]
            check("... and C2 where it was",
                  _near((fpo.x, fpo.y), c2_before), f"({fpo.x}, {fpo.y})")
        finally:
            pcbnew.GetBoard = real_getboard
            dialog.Destroy()
    finally:
        shutil.rmtree(d, ignore_errors=True)
    return _verdict()


def _verdict():
    print()
    if FAILURES:
        print(f"FAILED ({len(FAILURES)}): " + "; ".join(FAILURES))
        return 1
    print("ALL PASS -- the Route tab moves the cap and re-welds it, as the "
          "CLI does")
    return 0


if __name__ == '__main__':
    sys.exit(main())
