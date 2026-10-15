#!/usr/bin/env python3
"""#1248 parity gate: both parse paths read a pad's pad-to-die length and delay, and KiCad's own DRC agrees with the lengths built on them.

`parse_kicad_pcb` reads `(die_length ...)` / `(die_delay ...)` from the file
text. `build_pcb_data_from_board` reads `PAD.GetPadToDieLength()` /
`GetPadToDieDelay()` from live pcbnew. The GUI routes on the second, the CLI on
the first, so they must agree pad for pad. Arms:

1. **Parity, pad for pad, on three kinds of board.**
   - The issue's text-edited board: rp2350_fpga_eensy_prePlane with
     `(die_length 1.5)` on U6.52, plus a length AND a delay on U6.51.
   - A board pcbnew WROTE after its setters ran: values with micrometre and
     sub-ps precision, on a through-hole pad as well as SMD ones. So the text
     parser reads KiCad's own serialisation, not only our test's spelling.
   - Three tracked boards, which carry no token: 0.0 on both paths.
2. **A native oracle.** pcbnew's own getters on the loaded board equal the
   text-parsed value of every pad. Two paths sharing one wrong reading would
   agree with each other and still fail here.
3. **Witnesses.** The set of pads with a non-zero value equals the set the
   fixture declared, on both paths. A parser reading 0.0 everywhere passes
   every parity arm; this makes it fail.
4. **KiCad's DRC as the referee** (kicad-cli; the arm is required):
   - On the issue's board with its `usb_len` rule, KiCad's reported lengths
     (4.0294 / 2.4927 mm) and skew (1.5366) equal `net_match_length` from
     BOTH parse paths.
   - `route.py --length-match-group` on a synthetic pair of nets, one pad
     declaring `(die_length 3.0)`. KiCad's skew DRC passes the routed board
     at 0.15 mm.
   - The same copper with the token deleted fails it at ~3.0 mm. So the
     router matched what KiCad measures, not what the copper alone measures.
   - `route_diff.py --length-match-group` on two pairs WITHOUT intra-match,
     one making a U-turn (legs ~1.2 mm apart) with 2.0 mm of pad-to-die on
     its N leg. KiCad's per-leg lengths equal `net_match_length`, and the
     pairs match as KiCad's DP tuner measures a pair: its larger leg, each
     leg its own copper and its own end pads' term. The old rule (copper +
     the larger die) is outside the tolerance on the same copper.

Needs pcbnew; re-execs into KiCad's python automatically. Exits 2 when no
pcbnew python (or no kicad-cli) is found, rather than 0, so a mutation battery
using it as a killer cannot score SURVIVED on an environment accident.

    python3 -X utf8 tests/gui_parity/test_1248_die_length_parity.py
"""
import glob
import json
import os
import re
import subprocess
import sys
import tempfile
import uuid

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

sys.path.insert(0, os.path.join(REPO, 'py_router'))
from kicad_locate import path_version_key  # noqa: E402
del sys.path[0]    # this file orders its own sys.path further down
KICAD_PYTHONS = [
    "/Applications/KiCad/KiCad.app/Contents/Frameworks/Python.framework/Versions/Current/bin/python3",
    "/usr/bin/python3",
    os.path.expandvars(r"C:\Program Files\KiCad\bin\python.exe"),
    *sorted(glob.glob(r"C:\Program Files\KiCad\*\bin\python.exe"),
            key=path_version_key, reverse=True),
]

RP = os.path.join(REPO, 'kicad_files', 'rp2350_fpga_eensy_prePlane.kicad_pcb')
TRACKED = [RP, os.path.join(REPO, 'kicad_files', 'esp_prog.kicad_pcb'),
           os.path.join(REPO, 'kicad_files', 'orangecrab_ext_pll.kicad_pcb')]
USB = ['/RP2354A/USB_D+', '/RP2354A/USB_D-']
#: KiCad prints 4 decimals; its value is known to +-0.00005.
KICAD_PRINT = 0.00005 + 1e-9
TOL = 1e-6

FAILURES = []
NOT_RUN = []


def _reexec_into_kicad():
    for cand in KICAD_PYTHONS:
        if cand == sys.executable or not os.path.exists(cand):
            continue
        if subprocess.run([cand, '-c', 'import pcbnew'],
                          capture_output=True).returncode == 0:
            argv = [cand, '-X', 'utf8', os.path.abspath(__file__)] + sys.argv[1:]
            if os.name == 'nt':
                sys.exit(subprocess.run(argv).returncode)
            os.execv(cand, argv)
    print("NOT RUN: no python with pcbnew found (exit 2, not a pass)")
    sys.exit(2)


def check(cond, what, detail=''):
    if cond:
        print('  ok    %s' % what)
    else:
        print('  FAIL  %s%s' % (what, ('  -- ' + str(detail)) if detail else ''))
        FAILURES.append(what)


def die_map(pcb):
    """{(footprint key, pad index): (die_length, die_delay)} for every pad."""
    return {(k, i): (p.die_length, p.die_delay)
            for k, fp in pcb.footprints.items() for i, p in enumerate(fp.pads)}


def nonzero(pcb):
    return sorted((fp.reference, p.pad_number, round(p.die_length, 6), round(p.die_delay, 6))
                  for fp in pcb.footprints.values() for p in fp.pads
                  if p.die_length or p.die_delay)


def compare_paths(name, path, want):
    """Parity + native oracle + witness on one board."""
    import pcbnew
    from kicad_parser import parse_kicad_pcb, build_pcb_data_from_board
    f = parse_kicad_pcb(path)
    board = pcbnew.LoadBoard(path)
    g = build_pcb_data_from_board(board)
    a, b = die_map(f), die_map(g)
    check(set(a) == set(b), '%s: both paths see the same %d pads' % (name, len(a)),
          'only text=%s only pcbnew=%s' % (sorted(set(a) - set(b))[:3],
                                           sorted(set(b) - set(a))[:3]))
    bad = [(k, a[k], b[k]) for k in sorted(set(a) & set(b), key=str)
           if abs(a[k][0] - b[k][0]) > TOL or abs(a[k][1] - b[k][1]) > TOL]
    check(not bad, '%s: die_length / die_delay agree pad for pad' % name, bad[:3])
    check(nonzero(f) == want and nonzero(g) == want,
          '%s: witness, the non-zero pads are exactly the declared ones (%d)'
          % (name, len(want)), 'text=%s pcbnew=%s want=%s' % (nonzero(f), nonzero(g), want))
    per_ps = float(pcbnew.pcbIUScale.IU_PER_PS)
    # Keyed by the PARSED footprint key, not GetReference(): a board with a
    # repeated reference (esp_prog's `Ref*` x2) keys its later blocks `~2`
    # on both parse paths (#726), which pcbnew's reference never says.
    native = {}
    for fp, key in zip(board.GetFootprints(), list(g.footprints)):
        for lp in fp.Pads():
            native.setdefault((key, lp.GetNumber()), []).append(
                (lp.GetPadToDieLength() / 1e6, lp.GetPadToDieDelay() / per_ps))
    wrong = []
    for key, fp in f.footprints.items():
        for p in fp.pads:
            cands = native.get((key, p.pad_number), [])
            if not any(abs(p.die_length - L) <= TOL and abs(p.die_delay - D) <= TOL
                       for L, D in cands):
                wrong.append((key, p.pad_number, p.die_length, p.die_delay, cands[:2]))
    check(not wrong, '%s: every text-parsed pad equals pcbnew\'s own getters' % name,
          wrong[:3])
    return f, g


def find_cli():
    sys.path.insert(0, os.path.join(REPO, 'py_router'))
    try:
        from kicad_oracle import find_kicad_cli
        return find_kicad_cli(warn=False)
    except Exception:
        return None


def kicad_drc(cli, board, rule_text):
    """Run kicad-cli DRC with `rule_text` as the board's .kicad_dru. Returns
    ({net: KiCad length mm}, [skew mm, ...]), locale-agnostic: the numbers are
    read by position, never by the translated words around them."""
    with open(os.path.splitext(board)[0] + '.kicad_dru', 'w', encoding='utf-8') as fh:
        fh.write(rule_text)
    out = board + '.drc.json'
    p = subprocess.run([cli, 'pcb', 'drc', '--format', 'json', '--severity-all',
                        '-o', out, board], capture_output=True, text=True, timeout=600)
    if p.returncode not in (0, 5) or not os.path.isfile(out):
        raise RuntimeError('kicad-cli drc exit %s: %s' % (p.returncode, p.stderr[-400:]))
    with open(out, encoding='utf-8') as fh:
        doc = json.load(fh)
    lengths, skews = {}, []
    for v in doc.get('violations', []):
        nums = [float(x.replace(',', '.'))
                for x in re.findall(r'(-?\d+[.,]\d+) mm', v.get('description', ''))]
        if v.get('type') == 'length_out_of_range' and nums:
            m = re.search(r'\[([^\]]+)\]', (v.get('items') or [{}])[0].get('description', ''))
            if m:
                lengths[m.group(1)] = nums[-1]
        elif v.get('type') == 'skew_out_of_range' and len(nums) >= 2:
            skews.append(abs(nums[1]))
    return lengths, skews


USB_RULE = ("(version 1)\n(rule usb_len\n"
            "  (condition \"A.NetName == '/RP2354A/USB_D+' || A.NetName == '/RP2354A/USB_D-'\")\n"
            "  (constraint length (max 1mm))\n  (constraint skew (max 0.1mm)))\n")
PAIR_RULE = ("(version 1)\n(rule pair_len\n"
             "  (condition \"A.NetName == 'DA_P' || A.NetName == 'DA_N' || "
             "A.NetName == 'DB_P' || A.NetName == 'DB_N'\")\n"
             "  (constraint length (max 1mm)))\n")
SE_RULE = ("(version 1)\n(rule se_len\n"
           "  (condition \"A.NetName == 'SE1' || A.NetName == 'SE2'\")\n"
           "  (constraint length (max 1mm))\n  (constraint skew (max 0.15mm)))\n")


def synthetic_se(path, pads=None):
    """Two straight 40 mm nets; SE1's first pad declares (die_length 3.0). The
    same KiCad-10 single-pad-footprint form as tests/test_1248_pad_to_die.py.
    `pads` replaces the default set (ref, x, y, net, extra tokens)."""
    pads = pads or [('P1', 10, 12, 'SE1', '(die_length 3.0)'), ('P2', 50, 12, 'SE1', ''),
                    ('P3', 10, 18, 'SE2', ''), ('P4', 50, 18, 'SE2', '')]
    parts = ['(kicad_pcb\n\t(version 20260206)\n\t(generator "pcbnew")\n'
             '\t(generator_version "10.0")\n'
             '\t(general (thickness 1.6) (legacy_teardrops no))\n\t(paper "A4")\n'
             '\t(layers (0 "F.Cu" signal) (2 "B.Cu" signal) (1 "F.Mask" user) '
             '(3 "B.Mask" user) (5 "F.SilkS" user "F.Silkscreen") '
             '(7 "B.SilkS" user "B.Silkscreen") (25 "Edge.Cuts" user) '
             '(35 "F.Fab" user) (33 "B.Fab" user))\n'
             '\t(setup (stackup (layer "F.Cu" (type "copper") (thickness 0.035)) '
             '(layer "dielectric 1" (type "core") (thickness 0.15) (material "FR4") '
             '(epsilon_r 4.5) (loss_tangent 0.02)) '
             '(layer "B.Cu" (type "copper") (thickness 0.035))) '
             '(pad_to_mask_clearance 0))']
    for ref, x, y, net, extra in pads:
        parts.append(
            '\t(footprint "DemoPad" (layer "F.Cu") (uuid "%s") (at %s %s)\n'
            '\t\t(property "Reference" "%s" (at 0 -1.5 0) (layer "F.SilkS") '
            '(uuid "%s") (effects (font (size 1 1) (thickness 0.15))))\n'
            '\t\t(attr smd)\n'
            '\t\t(pad "1" smd circle (at 0 0) (size 0.8 0.8) (layers "F.Cu" '
            '"F.Mask") (net "%s") %s (uuid "%s")))'
            % (uuid.uuid4(), x, y, ref, uuid.uuid4(), net, extra, uuid.uuid4()))
    for x1, y1, x2, y2 in ((5, 5, 65, 5), (65, 5, 65, 40), (65, 40, 5, 40), (5, 40, 5, 5)):
        parts.append('\t(gr_line (start %s %s) (end %s %s) (stroke (width 0.1) '
                     '(type default)) (layer "Edge.Cuts") (uuid "%s"))'
                     % (x1, y1, x2, y2, uuid.uuid4()))
    with open(path, 'w', encoding='utf-8') as fh:
        fh.write('\n'.join(parts) + '\n)\n')
    return path


def pair_group_arm(cli, tmp):
    """Two diff pairs matched as one group WITHOUT intra-match, on the board of
    tests/test_1248_pad_to_die.py section 4d: DA's legs are unequal (a U-turn)
    and its N declares 2.0 mm of pad-to-die. KiCad's DP tuner measures a pair
    as its larger leg, each leg its own copper plus its own end pads' term
    (DP_MEANDER_PLACER::origPathLength); KiCad's DRC reports each leg."""
    from net_queries import net_match_length
    from test_1248_pad_to_die import PAIR_GROUP_ARGV, PAIR_GROUP_PADS
    nets = ['DA_P', 'DA_N', 'DB_P', 'DB_N']
    src = synthetic_se(os.path.join(tmp, 'pg.kicad_pcb'), PAIR_GROUP_PADS)
    routed = os.path.join(tmp, 'pg_out.kicad_pcb')
    p = subprocess.run([sys.executable, '-X', 'utf8', 'py_router/route_diff.py',
                        src, routed] + PAIR_GROUP_ARGV,
                       cwd=REPO, capture_output=True, text=True, timeout=600)
    check(p.returncode == 0 and os.path.isfile(routed),
          'route_diff.py --length-match-group routed the two-pair board',
          (p.stdout + p.stderr)[-600:])
    if not os.path.isfile(routed):
        return
    k, _sk = kicad_drc(cli, routed, PAIR_RULE)
    check(set(k) == set(nets), 'KiCad reports all four leg lengths', k)
    if set(k) != set(nets):
        return
    from kicad_parser import parse_kicad_pcb
    pcb = parse_kicad_pcb(routed)
    nid = {n.name: i for i, n in pcb.nets.items()}
    ours = {n: net_match_length(pcb, nid[n]) for n in nets}
    check(all(ours[n] is not None and abs(ours[n] - k[n]) <= KICAD_PRINT
              for n in nets),
          'net_match_length = KiCad DRC on every leg', (ours, k))
    a, b = max(k['DA_P'], k['DA_N']), max(k['DB_P'], k['DB_N'])
    check(abs(k['DA_P'] - k['DA_N']) > 0.3,
          'witness: KiCad sees DA\'s legs unmatched (P %.4f, N %.4f)'
          % (k['DA_P'], k['DA_N']))
    check(abs(a - b) <= 0.15,
          'KiCad: the pairs match as KiCad\'s DP tuner measures them, DA %.4f '
          'vs DB %.4f (each its larger leg, die included)' % (a, b), k)
    with open(routed, encoding='utf-8', newline='') as fh:
        rtext = fh.read()
    stripped = os.path.join(tmp, 'pg_nodie.kicad_pcb')
    with open(stripped, 'w', encoding='utf-8', newline='') as fh:
        fh.write(rtext.replace(' (die_length 2.0)', ''))
    k0, _sk = kicad_drc(cli, stripped, PAIR_RULE)
    if set(k0) == set(nets):
        old = (k0['DA_P'] + k0['DA_N']) / 2 + 2.0
        check(abs(old - a) >= 0.3,
              'witness: the old rule (centreline + the larger die, %.4f) is '
              '%.4f mm off KiCad\'s DA' % (old, abs(old - a)))


def main():
    try:
        import pcbnew  # noqa: F401
    except ImportError:
        _reexec_into_kicad()
    import pcbnew
    for _p in ('', 'py_router', 'py_tools', 'tests'):
        _d = os.path.join(REPO, _p)
        if _d not in sys.path:
            sys.path.insert(0, _d)
    from kicad_parser import parse_kicad_pcb, build_pcb_data_from_board
    from net_queries import net_match_length
    print('KiCad build: %s' % pcbnew.GetBuildVersion())
    tmp = tempfile.mkdtemp(prefix='krt1248g_')

    # -- 1a. the issue's board, text-edited --------------------------------
    print('\nissue board (text-edited)')
    with open(RP, encoding='utf-8', newline='') as fh:
        text = fh.read()
    a52, a51 = '(pinfunction "USB_DP_52")', '(pinfunction "USB_DM_51")'
    check(text.count(a52) == 1 and text.count(a51) == 1, 'fixture anchors U6.52 / U6.51 exist')
    issue = os.path.join(tmp, 'die.kicad_pcb')
    with open(issue, 'w', encoding='utf-8', newline='') as fh:
        fh.write(text.replace(a52, a52 + ' (die_length 1.5)'))
    both = os.path.join(tmp, 'die_both.kicad_pcb')
    with open(both, 'w', encoding='utf-8', newline='') as fh:
        fh.write(text.replace(a52, a52 + ' (die_length 1.5)')
                 .replace(a51, a51 + ' (die_length 0.75) (die_delay 6.25)'))
    f_issue, g_issue = compare_paths('die', issue, [('U6', '52', 1.5, 0.0)])
    compare_paths('die_both', both, [('U6', '51', 0.75, 6.25), ('U6', '52', 1.5, 0.0)])

    # -- 1b. a board pcbnew WROTE ------------------------------------------
    print('\npcbnew-written board')
    wb = pcbnew.LoadBoard(RP)
    per_ps = float(pcbnew.pcbIUScale.IU_PER_PS)
    want = []
    tht = next(((fp, p) for fp in wb.GetFootprints() for p in fp.Pads()
                if p.GetAttribute() == pcbnew.PAD_ATTRIB_PTH), None)
    targets = [(wb.FindFootprintByReference('U6'), '52', 2.345678, 0.0),
               (wb.FindFootprintByReference('U6'), '51', 0.0, 12.5),
               (wb.FindFootprintByReference('R4'), '2', 0.001, 0.25)]
    check(tht is not None, 'the board has a through-hole pad for the THT arm')
    for fp, num, L, D in targets:
        pad = next(p for p in fp.Pads() if p.GetNumber() == num)
        pad.SetPadToDieLength(pcbnew.FromMM(L))
        pad.SetPadToDieDelay(int(round(D * per_ps)))
        want.append((fp.GetReference(), num, round(L, 6), round(D, 6)))
    if tht is not None:
        tht[1].SetPadToDieLength(pcbnew.FromMM(4.2))
        tht[1].SetPadToDieDelay(int(round(30.0 * per_ps)))
        want.append((tht[0].GetReference(), tht[1].GetNumber(), 4.2, 30.0))
    written = os.path.join(tmp, 'pcbnew_written.kicad_pcb')
    pcbnew.SaveBoard(written, wb)
    with open(written, encoding='utf-8') as fh:
        wtext = fh.read()
    check('(die_length 2.345678)' in wtext and '(die_delay 12.5)' in wtext,
          'pcbnew serialised the tokens as mm and ps',
          re.findall(r'\(die_(?:length|delay) [^)]*\)', wtext)[:6])
    compare_paths('pcbnew_written', written, sorted(want))

    # -- 1c. tracked boards carry no token ---------------------------------
    print('\ntracked boards')
    for b in TRACKED:
        compare_paths(os.path.splitext(os.path.basename(b))[0], b, [])

    # -- 4. KiCad's DRC is the referee -------------------------------------
    print('\nKiCad DRC referee')
    cli = find_cli()
    if not cli:
        NOT_RUN.append('kicad-cli not found: the KiCad-referee arm cannot run')
    else:
        lengths, skews = kicad_drc(cli, issue, USB_RULE)
        check(set(lengths) == set(USB) and skews,
              'KiCad reports both USB lengths and a skew violation', (lengths, skews))
        for pcb, path_name in ((f_issue, 'text'), (g_issue, 'pcbnew')):
            nid = {n.name: i for i, n in pcb.nets.items()}
            ours = {n: net_match_length(pcb, nid[n]) for n in USB}
            check(all(n in lengths and abs(ours[n] - lengths[n]) <= KICAD_PRINT for n in USB)
                  and skews and abs(abs(ours[USB[0]] - ours[USB[1]]) - skews[0]) <= 2 * KICAD_PRINT,
                  '%s path: net_match_length and its skew = KiCad DRC' % path_name,
                  'ours=%s kicad=%s skew=%s' % (ours, lengths, skews))

        src = synthetic_se(os.path.join(tmp, 'se.kicad_pcb'))
        routed = os.path.join(tmp, 'se_out.kicad_pcb')
        p = subprocess.run([sys.executable, '-X', 'utf8', 'py_router/route.py', src, routed,
                            '--nets', 'SE1', 'SE2', '--track-width', '0.2',
                            '--clearance', '0.15', '--length-match-group', 'SE*',
                            '--length-match-tolerance', '0.1'],
                           cwd=REPO, capture_output=True, text=True, timeout=600)
        check(p.returncode == 0 and os.path.isfile(routed),
              'route.py --length-match-group routed the synthetic board',
              (p.stdout + p.stderr)[-600:])
        if os.path.isfile(routed):
            _l, sk = kicad_drc(cli, routed, SE_RULE)
            check(not sk, 'KiCad: the matched board has NO skew violation at 0.15 mm', sk)
            with open(routed, encoding='utf-8', newline='') as fh:
                rtext = fh.read()
            check(rtext.count('(die_length 3.0)') == 1, 'the routed board kept the token')
            stripped = os.path.join(tmp, 'se_nodie.kicad_pcb')
            with open(stripped, 'w', encoding='utf-8', newline='') as fh:
                fh.write(rtext.replace(' (die_length 3.0)', ''))
            _l, sk = kicad_drc(cli, stripped, SE_RULE)
            check(sk and abs(sk[0] - 3.0) <= 0.15,
                  'KiCad: the same copper WITHOUT the token is ~3.0 mm skewed '
                  '(the router compensated the die term)', sk)

        pair_group_arm(cli, tmp)

    print('\n%d failure(s)' % len(FAILURES))
    for fl in FAILURES:
        print('  FAILED: %s' % fl)
    for nr in NOT_RUN:
        print('  NOT RUN: %s' % nr)
    if FAILURES:
        return 1
    if NOT_RUN:
        print('exit 2: an arm did not run, so this is not a pass')
        return 2
    print('PASS  #1248 pad-to-die parity (text vs pcbnew vs KiCad DRC)')
    return 0


if __name__ == '__main__':
    sys.exit(main())
