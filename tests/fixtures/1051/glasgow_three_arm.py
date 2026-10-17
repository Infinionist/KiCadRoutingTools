#!/usr/bin/env python3
"""#1051 evidence: glasgow from scratch, four arms, graded against the human.

  a   the current seeder, from the run-32 intent (no arrays)
  b   the same intent + every `check_floorplan --suggest-arrays` row declared
      (the detector's suggestions, accepted wholesale), then seed + quench
  c   "the AI places the key parts": U30, RN1-RN12 and the 17 SN74LVC1T45
      buffers written at their HUMAN pose (kicad_files/glasgow_revC.kicad_pcb)
      and stamped `(locked yes)` in the INPUT board, then the base intent
      seeds the rest (`stage_human_key_parts`)
  cd  the same 30 key poses DECLARED as intent `fixed_poses[]` (stage 0
      seats them) instead of file-locked. U30's human pose overlaps FID8's
      courtyard (FID8 is file-locked at the human pose in the input), which
      stage 0 refuses by design; since #1060 its entry carries
      `accept_courtyard_overlap: ["FID8"]`, a named waiver of that courtyard
      overlap only. Before #1060 U30 had to be file-locked in cd's input as in
      c. So c and cd differ ONLY in how the 30 key parts are held.

Each arm runs `place_seed` (polish ON, clearance 0.2) on seeds 0-4 and is
graded by tools that are not the seeder:

  * render_placement --json-out (clearance 0.2, no ignored nets):
    metrics.crossings / metrics.hpwl, and their ratio to the human board's;
  * check_floorplan under the BASE run-32 intent (one ruler for all arms) and
    under the arm's own intent; place_seed's exit and `unseated`;
  * the ROUTED outcome: tests/test_placement_probe.py --off <a seed N>
    --on <b|c|cd seed N> --extra-nets <the scope below>, read on its own
    verdict ladder (failed nets, open nets, unconnected pad pairs, vias).

The probe's net scope is FIXED before any arm is seeded: every net of fanout
<= DISPLACEMENT_MAX_FANOUT on a pad of the 30 key parts or of a detector-row
member, read off the UNPLACED board. It is identical for every comparison, and
it is not scoped by which parts moved (see test_placement_probe.py). It is
also WIDE: 202 of glasgow's 251 nets. test_placement_probe's own
MAX_SCOPE_FRACTION (0.5) would refuse it, but that cap is applied only on the
--intent path, which this does not use -- so each probe is close to a
whole-board signal route (planes excluded by the fanout cut), not a scoped one.

Crossings/hpwl are render_placement's figures. place_seed's own JSON_SUMMARY
figure is recorded beside them as `seed_run.seed_crossings` and can differ (by up
to 40 crossings, ~1%, at 5712eea2e); the tables use render_placement's.

Provenance: `setup.json` and every record carry the SHA-256 of the unplaced
input board and the commit the run executed at.

Deterministic: place_seed reproduces byte for byte per seed, and the grades
are functions of the written board. Re-running writes the same JSONs except
`seconds`, `commit` and the absolute workdir paths.

Usage (from the repo root):
    python -X utf8 tests/fixtures/1051/glasgow_three_arm.py \
        --src C:/.../wk/run32/glasgow_unplaced.kicad_pcb [--workdir DIR] \
        [--seeds 0 1 2 3 4] [--arms a b c cd] [--probe-seeds 0 1 2]
    python -X utf8 tests/fixtures/1051/glasgow_three_arm.py --table
"""
import argparse
import hashlib
import json
import os
import re
import statistics
import subprocess
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(os.path.dirname(HERE)))
for _p in ('', 'py_placer', 'py_router', 'py_tools'):
    sys.path.insert(0, os.path.join(ROOT, _p))

BASE_INTENT = os.path.join(HERE, 'glasgow_run32.intent.json')
HUMAN = os.path.join(ROOT, 'kicad_files', 'glasgow_revC.kicad_pcb')
OUT = os.path.join(HERE, 'three_arm')
CLEARANCE = '0.2'
#: The parts arm (c) seats at the human's pose: the FPGA, the resistor
#: networks and the level-shifter buffers (#1051's own list).
KEY_VALUE = 'SN74LVC1T45DCKR'


def _run(argv, **kw):
    t = time.time()
    r = subprocess.run([sys.executable, '-X', 'utf8'] + argv, cwd=ROOT,
                       capture_output=True, text=True, encoding='utf-8',
                       errors='replace', **kw)
    return r, round(time.time() - t, 1)


def _json_summary(stdout):
    for line in reversed(stdout.splitlines()):
        if line.startswith('JSON_SUMMARY: '):
            return json.loads(line[len('JSON_SUMMARY: '):])
    return None


def _copy_board(src, dst):
    r, _ = _run([os.path.join('py_router', 'copy_board.py'), src, dst])
    if r.returncode:
        raise SystemExit(f"copy_board failed: {r.stdout}{r.stderr}")


def _sha256(path):
    with open(path, 'rb') as fh:
        return hashlib.sha256(fh.read()).hexdigest()


def key_refs(pcb):
    return sorted(r for r, f in pcb.footprints.items()
                  if r == 'U30' or re.fullmatch(r'RN\d+', r)
                  or (f.value or '') == KEY_VALUE)


def build_intents(src, work):
    """(intents {arm: path}, scope nets, suggestion rows)."""
    from kicad_parser import parse_kicad_pcb
    base = json.load(open(BASE_INTENT))
    sug_path = os.path.join(work, 'suggest.json')
    r, _ = _run([os.path.join('py_tools', 'check_floorplan.py'), src,
                 '--suggest-arrays', '--json', sug_path])
    if r.returncode:
        raise SystemExit(f"--suggest-arrays failed: {r.stdout}{r.stderr}")
    rows = [s['row'] for s in json.load(open(sug_path))['suggestions']]

    human = parse_kicad_pcb(HUMAN)
    unplaced = parse_kicad_pcb(src)
    keys = key_refs(human)
    assert len(keys) == 30, keys          # U30 + RN1-12 + 17 buffers
    fixed = []
    for ref in keys:
        fp = human.footprints[ref]
        assert unplaced.footprints[ref].footprint_name == fp.footprint_name
        fixed.append({'ref': ref, 'x': fp.x, 'y': fp.y,
                      'rot': float(fp.rotation) % 360.0,
                      'side': 'B' if fp.layer.startswith('B') else 'F',
                      'basis': 'declared',
                      'why': 'arm cd: the human pose (glasgow_revC), '
                             'declared by the "AI" before the seed'})
        if ref == 'U30':
            # #1060: the human U30 overlaps FID8's courtyard by 1.15 x 1.15mm
            # (kicad-cli reports it too); waived by name, courtyard only.
            fixed[-1]['accept_courtyard_overlap'] = ['FID8']
            fixed[-1]['why'] += ('; FID8 sits inside its courtyard in the '
                                 'human layout')
    assert len(fixed) == 30, len(fixed)

    intents = {}
    # arm c seeds from the BASE intent on a board whose key parts already
    # stand, file-locked, at the human pose (`stage_human_key_parts`); arm
    # cd declares all 30 as `fixed_poses[]` instead (U30 with its #1060
    # courtyard waiver), on the unplaced board itself.
    for arm, extra in (('a', {}), ('b', {'arrays': rows}), ('c', {}),
                       ('cd', {'fixed_poses': fixed})):
        doc = json.loads(json.dumps(base))
        doc.update(extra)
        if extra:
            doc['min_reader'] = max(int(doc.get('min_reader') or 0), 7)
        path = os.path.join(work, f'intent_{arm}.json')
        with open(path, 'w') as fh:
            json.dump(doc, fh, indent=1)
        intents[arm] = path

    # The probe scope, off the UNPLACED board (pose-blind), fixed for all.
    from placement.routability import DISPLACEMENT_MAX_FANOUT
    owners = {}
    for ref, fp in unplaced.footprints.items():
        for pad in fp.pads:
            if pad.net_id > 0 and pad.net_name:
                owners.setdefault(pad.net_name, set()).add(ref)
    bound = set(keys) | {m for row in rows for m in row['members']}
    nets = sorted({pad.net_name for ref in bound
                   for pad in unplaced.footprints[ref].pads
                   if pad.net_id > 0 and pad.net_name
                   and len(owners[pad.net_name]) <= DISPLACEMENT_MAX_FANOUT})
    return intents, nets, rows, keys


def stage_human_key_parts(src, keys, dst):
    """Arm c's input: the unplaced board with `keys` written at their HUMAN
    pose and stamped `(locked yes)` -- the part a model would place by hand
    before seeding the rest. The seeder holds a file-locked part where it
    stands, so these are never searched, and never refused."""
    import shutil
    from kicad_parser import parse_kicad_pcb
    from placement import seeder
    from placement.writer import write_placed_output
    human = parse_kicad_pcb(HUMAN)
    pl = [{'reference': r, 'new_x': human.footprints[r].x,
           'new_y': human.footprints[r].y,
           'new_rotation': float(human.footprints[r].rotation)}
          for r in keys]
    for ext in ('.kicad_pro', '.kicad_prl', '.design-brief.json'):
        a = os.path.splitext(src)[0] + ext
        if os.path.exists(a):
            shutil.copy2(a, os.path.splitext(dst)[0] + ext)
    write_placed_output(src, dst, pl)
    return seeder.stamp_locked(dst, keys)


def grade(board, intent_own, work_tag):
    rj = work_tag + '_render.json'
    r, secs = _run([os.path.join('py_tools', 'render_placement.py'), board,
                    '-o', work_tag + '.png', '--clearance', CLEARANCE,
                    '--json-out', rj, '--quiet'])
    m = json.load(open(rj))['metrics'] if os.path.exists(rj) else {}
    out = {'crossings': m.get('crossings'),
           'hpwl': None if m.get('hpwl') is None else round(m['hpwl'], 2),
           'render_exit': r.returncode}
    for tag, ipath in (('base', BASE_INTENT), ('own', intent_own)):
        fj = f'{work_tag}_floorplan_{tag}.json'
        r, _ = _run([os.path.join('py_tools', 'check_floorplan.py'), board,
                     '--intent', ipath, '--clearance', CLEARANCE,
                     '--exit-zero', '-q', '--allow-routed', '--json', fj])
        s = _json_summary(r.stdout) or {}
        out[f'floorplan_{tag}_errors'] = s.get('errors')
        # ERRORS ONLY, by rule -- the count `errors` is the sum of.
        by = {}
        if os.path.exists(fj):
            for v in json.load(open(fj)).get('violations') or ():
                if v.get('severity') == 'error':
                    by[v['rule']] = by.get(v['rule'], 0) + 1
        out[f'floorplan_{tag}_errors_by_rule'] = dict(sorted(by.items()))
        # errors AND warnings, by rule (floorplan.summary's own key).
        out[f'floorplan_{tag}_violations_by_rule'] = dict(
            sorted((s.get('violations_by_rule') or {}).items()))
    return out


def seed_one(src, intent, board_out, seed):
    r, secs = _run([os.path.join('py_placer', 'place_seed.py'), src,
                    board_out, '--intent', intent,
                    '--seed', str(seed),
                    '--clearance', CLEARANCE,
                    # The run-32 pile reads as "placed" (its connectors and
                    # holes are locked at their real poses), so place_seed
                    # refuses without --force; nothing else is discarded.
                    '--force'])
    s = _json_summary(r.stdout) or {}
    return {'exit': r.returncode, 'seconds': secs,
            'summary_read': bool(s),
            'unseated': sorted(s.get('unseated_refs') or ()),
            'grade_errors': s.get('grade_errors'),
            'pad_conflicts_seeded': s.get('pad_conflicts_seeded'),
            'arrays_formed': sorted(s.get('arrays_formed') or {}),
            'array_unseated': sorted(s.get('array_unseated') or {}),
            'fixed_seated': sorted(s.get('fixed_seated') or {}),
            'fixed_refused': sorted(s.get('fixed_refused') or {}),
            'rigid_released': s.get('rigid_released') or {},
            'decap_stage': {k: v for k, v in (s.get('decap_stage') or {}
                                              ).items()
                            if k in ('armed', 'scope', 'claimed')},
            'seed_crossings': s.get('crossings'), 'seed_hpwl': s.get('hpwl')}


LADDER = ('failed nets', 'open nets', 'unconnected pad pairs', 'vias')


def probe(off, on, nets, work):
    r, secs = _run([os.path.join('tests', 'test_placement_probe.py'),
                    '--off', off, '--on', on, '--extra-nets'] + nets
                   + ['--route-args', f'--clearance {CLEARANCE}',
                      '--workdir', work])
    rungs = {}
    for line in r.stdout.splitlines():
        for label in LADDER:
            mm = re.match(rf'^{re.escape(label)}\s+(-?\d+)\s+(-?\d+)', line)
            if mm:
                rungs[label] = [int(mm.group(1)), int(mm.group(2))]
    verdict = next((ln.strip() for ln in r.stdout.splitlines()
                    if ln.startswith(('BETTER', 'WORSE', 'NO CHANGE',
                                      'INCONCLUSIVE'))), None)
    return {'exit': r.returncode, 'seconds': secs, 'rungs': rungs,
            'verdict': verdict,
            'tail': None if rungs else (r.stdout + r.stderr)[-800:]}


#: Run 32's routing chain (wk/run32/armA_chain.sh), verbatim but for the
#: board paths and its discarded cap MEASUREMENT step: GND pour on In1, U30
#: dogbone BGA fanout, U1 QFN fanout, diff pairs, then every net. The chain
#: routes at a 0.1 floor, so it is GRADED at the floor it wrote back to the
#: .kicad_pro (check_drc reads it), not at 0.2.
_LAYERS = ['F.Cu', 'In1.Cu', 'In2.Cu', 'B.Cu']
_ROUTE_ENV = {
    'KICAD_GLOBAL_PLAN': '1',
    'KICAD_GLOBAL_PLAN_SEQ': '1', 'KICAD_GLOBAL_PLAN_SEQ_COST': '1.5',
    'KICAD_GLOBAL_PLAN_VIA_COST': '20', 'KICAD_GLOBAL_PLAN_ITERS': '50000',
    'KICAD_GLOBAL_PLAN_ATTRACT': '1', 'KICAD_ATTRACT_POTENTIAL': '65',
    'KICAD_GLOBAL_PLAN_RIVER': '1', 'KICAD_FINALIZE_REAUDIT': '1',
    'KICAD_PACK_INLINE': '1'}


def full_route(board, work, tag):
    """Route `board` through run 32's chain; grade with check_drc and
    check_connected. Returns the per-step record."""
    os.makedirs(work, exist_ok=True)
    p = lambda s: os.path.join(work, f'{tag}_{s}.kicad_pcb')  # noqa: E731
    steps = [
        ('pour', ['py_router/route_planes.py', board, p('pour'), '--nets',
                  'GND', '--plane-layers', 'In1.Cu',
                  '--clearance-ceiling', '0.1'], {}),
        ('bga', ['py_router/bga_fanout.py', p('pour'), '--component', 'U30',
                 '--nets', '*', '!GND', '--layers'] + _LAYERS
         + ['--layer-costs', '1.0', '-1.0', '1.0', '1.0', '--escape-method',
            'dogbone', '--via-size', '0.35', '--via-drill', '0.2',
            '--track-width', '0.0889', '--clearance', '0.1',
            '--output', p('bga')], {}),
        ('qfn', ['py_router/qfn_fanout.py', p('bga'), '--component', 'U1',
                 '--nets', '*', '!GND', '--width', '0.0889', '--clearance',
                 '0.1', '--output', p('qfn')], {}),
        ('diff', ['py_router/route_diff.py', p('qfn'), p('diff'), '--nets',
                  '/IO_Banks/Z*_P', '/IO_Banks/Z*_N', '/USB_P', '/USB_N',
                  '--track-width', '0.1', '--diff-pair-gap', '0.1',
                  '--clearance-ceiling', '0.1', '--via-size', '0.5',
                  '--via-drill', '0.3', '--layers'] + _LAYERS
         + ['--layer-costs', '1.0', '6.0', '1.5', '1.0'],
         {}),
        ('route', ['py_router/route.py', p('diff'), p('route'), '--nets', '*',
                   '--no-bga-zones', '--max-ripup', '5',
                   '--ripped-route-avoidance-cost', '3',
                   '--track-proximity-cost', '2', '--track-width', '0.127',
                   '--clearance-ceiling', '0.1', '--via-size', '0.5',
                   '--via-drill', '0.3', '--hole-to-hole-clearance', '0.25',
                   '--board-edge-clearance', '0.5', '--power-nets', 'GND',
                   '+3V3', '+5V', '+1V2', '/xVBUS', '--power-nets-widths',
                   '0.3', '0.3', '0.4', '0.3', '0.4', '--layers'] + _LAYERS
         + ['--layer-costs', '1.0', '6.0', '1.5', '1.0'], _ROUTE_ENV),
    ]
    rec = {'board': os.path.basename(board), 'steps': []}
    for name, argv, env in steps:
        r, secs = _run(argv, env={**os.environ, **env})
        rec['steps'].append({'step': name, 'exit': r.returncode,
                             'seconds': secs})
        print(f"  route {tag} {name}: exit {r.returncode} ({secs}s)",
              flush=True)
        # A step's exit code is recorded, not obeyed: route.py exits
        # non-zero when nets fail and still writes the board, which is what
        # gets graded. Only a missing output stops the chain.
        if not os.path.exists(p(name)):
            rec['failed_at'] = name
            rec['tail'] = (r.stdout + r.stderr)[-1500:]
            return rec
    routed = p('route')
    dj = os.path.join(work, f'{tag}_drc.json')
    r, _ = _run(['py_router/check_drc.py', routed, '--json', dj])
    d = json.load(open(dj)) if os.path.exists(dj) else {}
    rec['drc'] = {'violations': d.get('violations'),
                  'by_type': d.get('by_type'),
                  'graded_at_clearance': (d.get('graded_at') or {}).get(
                      'clearance')}
    rec['connectivity'] = connectivity(routed)
    return rec


def connectivity(routed):
    """check_connected's verdict on `routed`: every net with an issue,
    split into UNROUTED (no copper at all) and BROKEN (copper, pads apart)."""
    from check_connected import run_connectivity_check
    issues = run_connectivity_check(routed, None, 0.02, True, False, None,
                                    False)
    unrouted = sorted(i.get('net_name') or '' for i in issues
                      if i.get('unrouted'))
    broken = sorted(i.get('net_name') or '' for i in issues
                    if not i.get('unrouted'))
    return {'nets_with_issues': len(issues), 'unrouted': len(unrouted),
            'broken': len(broken), 'unrouted_nets': unrouted,
            'broken_nets': broken}


def pose_identity(board_a, board_b):
    """Every footprint's (x, y, rotation, side, locked) on both boards:
    the refs that differ. Empty = the two seeds wrote the same placement."""
    from kicad_parser import parse_kicad_pcb
    pa, pb = parse_kicad_pcb(board_a), parse_kicad_pcb(board_b)

    def pose(f):
        return (f.x, f.y, float(f.rotation) % 360.0, f.layer, bool(f.locked))
    refs = sorted(set(pa.footprints) | set(pb.footprints))
    return [r for r in refs
            if r not in pa.footprints or r not in pb.footprints
            or pose(pa.footprints[r]) != pose(pb.footprints[r])]


def _commit():
    r = subprocess.run(['git', 'rev-parse', 'HEAD'], cwd=ROOT,
                       capture_output=True, text=True)
    d = subprocess.run(['git', 'status', '--porcelain', '--', 'py_placer',
                        'py_router', 'py_tools'], cwd=ROOT,
                       capture_output=True, text=True)
    return r.stdout.strip() + ('+dirty' if d.stdout.strip() else '')


def table(out_dir):
    human = json.load(open(os.path.join(out_dir, 'human.json')))
    hc, hh = human['crossings'], human['hpwl']
    recs = []
    for fn in sorted(os.listdir(out_dir)):
        if re.fullmatch(r'arm(a|b|c|cd)_s\d+\.json', fn):
            recs.append(json.load(open(os.path.join(out_dir, fn))))
    print(f"human glasgow_revC: crossings {hc}  hpwl {hh}\n")
    print(f"{'arm':<4}{'seed':>5}{'cross':>7}{'x hum':>7}{'hpwl':>9}"
          f"{'x hum':>7}{'err(base)':>10}{'err(own)':>9}{'unseat':>7}"
          f"{'exit':>5}  probe vs a (failed/open/unconn/vias OFF->ON)")
    for r in sorted(recs, key=lambda r: (r['arm'], r['seed'])):
        g, s = r['grade'], r['seed_run']
        if g.get('crossings') is None or g.get('hpwl') is None:
            print(f"{r['arm']:<4}{r['seed']:>5}  NOT GRADED (place_seed exit "
                  f"{s['exit']})")
            continue
        p = r.get('probe_vs_a')
        ptxt = ''
        if p:
            ptxt = ' '.join(f"{a}->{b}" for a, b in
                            (p['rungs'].get(k, ['?', '?']) for k in LADDER))
            ptxt += f"  [{(p.get('verdict') or '').split(' (')[0]}]"
        print(f"{r['arm']:<4}{r['seed']:>5}{g['crossings']:>7}"
              f"{g['crossings'] / hc:>7.2f}{g['hpwl']:>9.1f}"
              f"{g['hpwl'] / hh:>7.2f}{g['floorplan_base_errors']!s:>10}"
              f"{g['floorplan_own_errors']!s:>9}{len(s['unseated']):>7}"
              f"{s['exit']:>5}  {ptxt}")
    routes = [(d, fn) for d in (out_dir, os.path.join(out_dir,
                                                      'recorded_5712eea2e'))
              if os.path.isdir(d)
              for fn in sorted(os.listdir(d))
              if re.fullmatch(r'route_[a-z]+_s\d+\.json', fn)]
    if routes:
        print("\nfull-board route (run 32 chain), graded check_drc at the "
              "written floor + check_connected:")
        for d, fn in routes:
            r = json.load(open(os.path.join(d, fn)))
            if d != out_dir:
                print(f"  [recorded at {r.get('commit', '?')[:9]} on THAT "
                      f"commit's seeds -- not these boards]", end='')
            c = r.get('connectivity') or {}
            print(f"  arm {r['arm']} seed {r['seed']}: DRC "
                  f"{(r.get('drc') or {}).get('violations')} "
                  f"{(r.get('drc') or {}).get('by_type')}  unrouted "
                  f"{c.get('unrouted')}  broken {c.get('broken')}  "
                  f"({r.get('seconds')}s)")
    print()
    for arm in ('a', 'b', 'c', 'cd'):
        rs = [r for r in recs if r['arm'] == arm]
        if not rs:
            continue
        rs = [r for r in rs if r['grade'].get('crossings') is not None]
        if not rs:
            continue
        cs = [r['grade']['crossings'] for r in rs]
        hs = [r['grade']['hpwl'] for r in rs]
        sd = (lambda v: statistics.stdev(v) if len(v) > 1 else 0.0)
        print(f"arm {arm}: n={len(rs)}  crossings {statistics.mean(cs):.0f} "
              f"+- {sd(cs):.0f} (x{statistics.mean(cs) / hc:.2f} human)  "
              f"hpwl {statistics.mean(hs):.0f} +- {sd(hs):.0f} "
              f"(x{statistics.mean(hs) / hh:.2f} human)")


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument('--src', help='glasgow_unplaced.kicad_pcb (run 32)')
    p.add_argument('--workdir', default=None)
    p.add_argument('--out', default=OUT)
    p.add_argument('--seeds', type=int, nargs='*', default=[0, 1, 2, 3, 4])
    p.add_argument('--arms', nargs='*', default=['a', 'b', 'c', 'cd'])
    p.add_argument('--probe-arms', nargs='*', default=['b', 'c', 'cd'])
    p.add_argument('--probe-seeds', type=int, nargs='*', default=[0, 1, 2])
    p.add_argument('--reuse', nargs='*', default=None, metavar='DIR',
                   help='workdirs of earlier invocations: an arm/seed whose '
                        'board is there AND whose JSON is in --out is not '
                        're-seeded (used to probe after parallel arm runs)')
    p.add_argument('--route', nargs='*', metavar='ARM:SEED', default=None,
                   help='full-board route (run 32 chain) of these boards, '
                        'found via --reuse; records route_<arm>_s<seed>.json')
    p.add_argument('--regrade-route', nargs='*', metavar='ARM:SEED',
                   default=None)
    p.add_argument('--regrade', action='store_true',
                   help='re-run the placement grade on the --arms/--seeds '
                        'boards found in --reuse, replacing each record\'s '
                        '`grade` (the grade is a function of the board)')
    p.add_argument('--pose-identity', nargs=2, metavar=('REF_ARM', 'ARM'),
                   default=None,
                   help='compare ARM\'s boards with REF_ARM\'s on --seeds '
                        '(found via --reuse) and record the result in ARM\'s '
                        'records as `pose_identical_to`')
    p.add_argument('--table', action='store_true',
                   help='print the table from the committed JSONs and exit')
    a = p.parse_args(argv)
    if a.table:
        table(a.out)
        return 0
    if a.regrade_route:
        # Re-grade connectivity of already-routed boards (found via --reuse)
        # into their route_<arm>_s<seed>.json.
        for spec in a.regrade_route:
            arm, seed = spec.split(':')
            fn = f'{arm}{seed}_route.kicad_pcb'
            b = next((os.path.join(d, f'route_{arm}_s{seed}', fn)
                      for d in (a.reuse or ())
                      if os.path.exists(os.path.join(
                          d, f'route_{arm}_s{seed}', fn))), None)
            if b is None:
                p.error(f'--regrade-route {spec}: no {fn} under --reuse')
            jf = os.path.join(a.out, f'route_{arm}_s{seed}.json')
            rec = json.load(open(jf))
            rec['connectivity'] = connectivity(b)
            with open(jf, 'w') as fh:
                json.dump(rec, fh, indent=1, sort_keys=True)
            print(spec, {k: v for k, v in rec['connectivity'].items()
                         if not k.endswith('_nets')}, flush=True)
        return 0
    if a.pose_identity:
        ref_arm, arm = a.pose_identity
        for seed in a.seeds:
            found = {}
            for x in (ref_arm, arm):
                fn = f'arm{x}_s{seed}.kicad_pcb'
                found[x] = next((os.path.join(d, fn) for d in (a.reuse or ())
                                 if os.path.exists(os.path.join(d, fn))),
                                None)
                if found[x] is None:
                    p.error(f'--pose-identity: no {fn} in --reuse')
            diff = pose_identity(found[ref_arm], found[arm])
            jf = os.path.join(a.out, f'arm{arm}_s{seed}.json')
            rec = json.load(open(jf))
            rec['pose_identical_to'] = {'arm': ref_arm, 'identical': not diff,
                                        'differing_refs': diff}
            with open(jf, 'w') as fh:
                json.dump(rec, fh, indent=1, sort_keys=True)
            print(f'seed {seed}: {arm} vs {ref_arm}: '
                  f'{"IDENTICAL" if not diff else diff}', flush=True)
        return 0
    if a.regrade:
        for arm in a.arms:
            for seed in a.seeds:
                fn = f'arm{arm}_s{seed}.kicad_pcb'
                b = next((os.path.join(d, fn) for d in (a.reuse or ())
                          if os.path.exists(os.path.join(d, fn))), None)
                jf = os.path.join(a.out, f'arm{arm}_s{seed}.json')
                if b is None or not os.path.exists(jf):
                    p.error(f'--regrade: no {fn} in --reuse, or no {jf}')
                own = os.path.join(os.path.dirname(b), f'intent_{arm}.json')
                rec = json.load(open(jf))
                g = grade(b, own, os.path.splitext(b)[0] + '_rg')
                for k in ('crossings', 'hpwl', 'floorplan_base_errors',
                          'floorplan_own_errors'):
                    if g[k] != rec['grade'][k]:
                        raise SystemExit(f'{fn}: regrade moved {k} '
                                         f'{rec["grade"][k]} -> {g[k]}')
                rec['grade'] = g
                with open(jf, 'w') as fh:
                    json.dump(rec, fh, indent=1, sort_keys=True)
                print(f'regraded {fn}: errors by rule '
                      f'{g["floorplan_base_errors_by_rule"]}', flush=True)
        return 0
    if a.route:
        os.makedirs(a.out, exist_ok=True)
        for spec in a.route:
            arm, seed = spec.split(':')
            fn = f'arm{arm}_s{seed}.kicad_pcb'
            src_b = next((os.path.join(d, fn) for d in (a.reuse or ())
                          if os.path.exists(os.path.join(d, fn))), None)
            if src_b is None:
                p.error(f'--route {spec}: no {fn} in --reuse dirs')
            work = a.workdir or tempfile.mkdtemp(prefix='glasgow_route_')
            rw = os.path.join(work, f'route_{arm}_s{seed}')
            os.makedirs(rw, exist_ok=True)
            b = os.path.join(rw, fn)
            _copy_board(src_b, b)
            t = time.time()
            rec = full_route(b, rw, f'{arm}{seed}')
            rec.update({'arm': arm, 'seed': int(seed), 'commit': _commit(),
                        'seconds': round(time.time() - t, 1)})
            with open(os.path.join(a.out, f'route_{arm}_s{seed}.json'),
                      'w') as fh:
                json.dump(rec, fh, indent=1, sort_keys=True)
            print(json.dumps({k: rec.get(k) for k in
                              ('arm', 'seed', 'drc', 'connectivity',
                               'failed_at', 'seconds')}), flush=True)
        return 0
    if not a.src:
        p.error('--src is required (the run-32 glasgow_unplaced board)')
    os.makedirs(a.out, exist_ok=True)
    work = a.workdir or tempfile.mkdtemp(prefix='glasgow_three_arm_')
    os.makedirs(work, exist_ok=True)
    src = os.path.join(work, 'glasgow_unplaced.kicad_pcb')
    _copy_board(a.src, src)
    intents, nets, rows, keys = build_intents(src, work)
    src_c = os.path.join(work, 'glasgow_c_input.kicad_pcb')
    if 'c' in a.arms:
        n = stage_human_key_parts(src, keys, src_c)
        print(f"arm c input: {n} key part(s) at the human pose, locked",
              flush=True)
    # #1060: cd seeds the UNPLACED board -- every key part, U30 included, is
    # a declared pose now, so nothing is staged by hand.
    inputs = {'a': src, 'b': src, 'c': src_c, 'cd': src}
    commit = _commit()
    src_sha = _sha256(src)
    with open(os.path.join(a.out, 'setup.json'), 'w') as fh:
        json.dump({'commit': commit, 'unplaced_sha256': src_sha,
                   'detector_rows': rows,
                   'key_refs': keys, 'probe_nets': nets,
                   'clearance': float(CLEARANCE)}, fh, indent=1)
    hg = grade(HUMAN, BASE_INTENT, os.path.join(work, 'human'))
    with open(os.path.join(a.out, 'human.json'), 'w') as fh:
        json.dump(hg, fh, indent=1, sort_keys=True)
    print(f"work {work}\ncommit {commit}\nprobe scope {len(nets)} nets; "
          f"{len(rows)} detector rows; {len(keys)} key refs", flush=True)

    boards = {}
    for arm in a.arms:
        for seed in a.seeds:
            b = os.path.join(work, f'arm{arm}_s{seed}.kicad_pcb')
            prior = [os.path.join(d, os.path.basename(b))
                     for d in (a.reuse or ())]
            prior = [x for x in prior if os.path.exists(x)]
            recf = os.path.join(a.out, f'arm{arm}_s{seed}.json')
            if prior and os.path.exists(recf):
                # A board an earlier invocation seeded and recorded (the
                # arms run as parallel processes; the probe runs after).
                boards[(arm, seed)] = prior[0]
                print(f"arm {arm} seed {seed}: reused {prior[0]}", flush=True)
                continue
            sr = seed_one(inputs[arm], intents[arm], b, seed)
            g = grade(b, intents[arm], os.path.join(work, f'arm{arm}_s{seed}'))
            boards[(arm, seed)] = b
            rec = {'arm': arm, 'seed': seed, 'commit': commit,
                   'unplaced_sha256': src_sha, 'seed_run': sr, 'grade': g}
            with open(os.path.join(a.out, f'arm{arm}_s{seed}.json'), 'w') as fh:
                json.dump(rec, fh, indent=1, sort_keys=True)
            print(f"arm {arm} seed {seed}: exit {sr['exit']} "
                  f"crossings {g['crossings']} hpwl {g['hpwl']} "
                  f"err(base) {g['floorplan_base_errors']} "
                  f"unseated {len(sr['unseated'])} ({sr['seconds']}s)",
                  flush=True)
    for seed in a.probe_seeds:
        if ('a', seed) not in boards:
            continue
        for arm in a.probe_arms:
            if (arm, seed) not in boards:
                continue
            pw = os.path.join(work, f'probe_{arm}_s{seed}')
            pr = probe(boards[('a', seed)], boards[(arm, seed)], nets, pw)
            fn = os.path.join(a.out, f'arm{arm}_s{seed}.json')
            rec = json.load(open(fn))
            rec['probe_vs_a'] = pr
            with open(fn, 'w') as fh:
                json.dump(rec, fh, indent=1, sort_keys=True)
            print(f"probe a->{arm} seed {seed}: {pr['rungs']} "
                  f"{pr['verdict']} ({pr['seconds']}s)", flush=True)
    table(a.out)
    return 0


if __name__ == '__main__':
    sys.exit(main())
