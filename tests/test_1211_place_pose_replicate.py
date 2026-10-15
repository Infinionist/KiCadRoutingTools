#!/usr/bin/env python3
"""#1211: `place_pose replicate` repeats one placed sheet channel onto its
sibling instances.

multichannel_mixer has four instances of one channel sheet, and an agent that
wanted them laid out alike had to write its own helper: nothing in the
placement stack used the correspondence the file already carries (every
footprint's `(path "/<sheet>/<symbol>")` ends in the SAME symbol uuid in each
instance). The verb maps parts by that uuid, refuses unless the instances are
wired alike, copies the source's relative poses at one offset, and grades the
result like every other place_pose call.

The synthetic board: three instances S1/S2/S3 of one sheet (an IC, a
resistor, a cap), channel-local nets /Sk/IN and /Sk/OUT, shared GND and VCC,
and one top-level part. S1 is the placed channel; S2 and S3 are scattered.
The arms:

* the copy lands where the arithmetic says (source pose + offset, the
  source's rotation and side), with each default anchor kept in place;
* `--offset` and `--anchor` (a ref of ANY named sheet) move the copy;
* the refusals, each by its reason and exit code: nets not wired alike
  (both the shared-net and the one-to-one arms), a part missing from one
  instance, a footprint that differs, a KiCad-locked target part, an
  unknown or ambiguous sheet, and the usage errors;
* a locked part that the copy does NOT move is not refused;
* both sheet-name spellings are read (KiCad 8+ token, KiCad 6/7 property);
* on the tracked glasgow_revC, the two IO_Buffer instances map 68 for 68 by
  symbol, and the net rule names the I2C address straps that are wired
  differently per instance (a real asymmetry, not a pairing artefact).

Run: python3 -X utf8 tests/test_1211_place_pose_replicate.py [case ...]
"""
import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import run_utils  # noqa: E402

ROOT = run_utils.ROOT_DIR
for _sub in ('py_placer', 'py_router', 'py_tools'):
    _p = os.path.join(ROOT, _sub)
    if _p not in sys.path:
        sys.path.insert(0, _p)

RUN_ALL_TIMEOUT = 600

PLACE_POSE = os.path.join(ROOT, 'py_placer', 'place_pose.py')
GLASGOW = os.path.join(ROOT, 'kicad_files', 'glasgow_revC.kicad_pcb')

SHEET = {'S1': '5a000000-0000-0000-0000-000000000001',
         'S2': '5a000000-0000-0000-0000-000000000002',
         'S3': '6b000000-0000-0000-0000-000000000003'}
SYMBOL = {'U': '11111111-1111-1111-1111-111111111111',
          'R': '22222222-2222-2222-2222-222222222222',
          'C': '33333333-3333-3333-3333-333333333333',
          'T': '44444444-4444-4444-4444-444444444444'}
#: pad offsets per part kind (local mm) and the nets each pad carries:
#: 'IN' / 'OUT' are the instance's own, the rest are shared.
KIND = {'U': ('t:U4', [(-1.5, -1.0), (-1.5, 1.0), (1.5, 1.0), (1.5, -1.0)],
              ['VCC', 'IN', 'OUT', 'GND']),
        'R': ('t:R', [(-0.75, 0.0), (0.75, 0.0)], ['IN', 'OUT']),
        'C': ('t:C', [(-0.75, 0.0), (0.75, 0.0)], ['VCC', 'GND']),
        'T': ('t:T', [(-0.75, 0.0), (0.75, 0.0)], ['VCC', 'GND'])}

#: ref -> (kind, sheet, x, y, rot). S1 is the PLACED channel.
PARTS = {
    'U1': ('U', 'S1', 15.0, 20.0, 0.0),
    'R1': ('R', 'S1', 15.0, 14.0, 0.0),
    'C1': ('C', 'S1', 10.0, 20.0, 90.0),
    'U2': ('U', 'S2', 45.0, 20.0, 0.0),
    'R2': ('R', 'S2', 51.0, 27.0, 90.0),
    'C2': ('C', 'S2', 40.0, 12.0, 0.0),
    'U3': ('U', 'S3', 75.0, 20.0, 0.0),
    'R3': ('R', 'S3', 70.0, 28.0, 0.0),
    'C3': ('C', 'S3', 80.0, 13.0, 90.0),
    'T1': ('T', None, 50.0, 35.0, 0.0),
}


def _fmt(v):
    return ('%.6f' % v).rstrip('0').rstrip('.')


def board(td, name='b', *, drop=(), locked=(), back=(), nets=None,
          fpname=None, names=None, prop_names=False, bare_text=False,
          sheet_after_pad=()):
    """Write the fixture. `nets` overrides pad nets: {(ref, pad#): net};
    `fpname` overrides footprint names {ref: name}; `back` puts parts on
    B.Cu; `names` overrides an instance's sheet name {sheet: name};
    `prop_names` spells the name the KiCad 6/7 way; `bare_text` drops the
    Reference's `(effects ...)`, which the writer's side flip needs (#714);
    `sheet_after_pad` writes those refs' sheet name and file AFTER their
    first pad, where no header field lives."""
    nets = nets or {}
    fpname = fpname or {}
    names = names or {}
    all_nets = ['GND', 'VCC'] + ['/%s/%s' % (s, n) for s in sorted(SHEET)
                                 for n in ('IN', 'OUT')]
    ids = {n: i + 1 for i, n in enumerate(all_nets)}
    out = ['(kicad_pcb (version 20241229) (generator "pcbnew")',
           '  (layers (0 "F.Cu" signal) (31 "B.Cu" signal)'
           ' (36 "B.SilkS" user) (37 "F.SilkS" user)'
           ' (44 "Edge.Cuts" user) (46 "B.CrtYd" user) (47 "F.CrtYd" user))',
           '  (net 0 "")'] + ['  (net %d "%s")' % (ids[n], n) for n in all_nets]
    out.append('  (gr_rect (start 0 0) (end 90 40) (stroke (width 0.1)'
               ' (type default)) (layer "Edge.Cuts"))')
    for ref, (kind, sheet, x, y, rot) in PARTS.items():
        if ref in drop:
            continue
        fp, pads, pad_nets = KIND[kind]
        fp = fpname.get(ref, fp)
        side = 'B' if ref in back else 'F'
        path = ('/%s/%s' % (SHEET[sheet], SYMBOL[kind]) if sheet
                else '/%s' % SYMBOL[kind])
        sname = names.get(sheet, sheet) if sheet else ''
        lines = ['  (footprint "%s" (layer "%s.Cu") (at %s %s %s)%s'
                 % (fp, side, _fmt(x), _fmt(y), _fmt(rot),
                    ' (locked yes)' if ref in locked else ''),
                 '    (property "Reference" "%s" (at 0 0) (layer "%s.SilkS")%s)'
                 % (ref, side, '' if bare_text else
                    ' (effects (font (size 1 1) (thickness 0.15))%s)'
                    % (' (justify mirror)' if side == 'B' else '')),
                 '    (path "%s")' % path]
        sfile = 'strip.kicad_sch' if sheet else 'top.kicad_sch'
        if prop_names:
            sheet_lines = [
                '    (property "Sheetname" "%s" (at 0 0) (layer "F.Fab") '
                '(hide yes))' % sname,
                '    (property "Sheetfile" "%s" (at 0 0) (layer "F.Fab") '
                '(hide yes))' % sfile]
        else:
            sheet_lines = [('    (sheetname "/%s/")' % sname if sname
                            else '    (sheetname "/")'),
                           '    (sheetfile "%s")' % sfile]
        if ref not in sheet_after_pad:
            lines.extend(sheet_lines)
        lines.append('    (fp_rect (start -2.2 -1.6) (end 2.2 1.6) (stroke '
                     '(width 0.05) (type default)) (layer "%s.CrtYd"))' % side)
        for i, ((px, py), role) in enumerate(zip(pads, pad_nets), 1):
            net = nets.get((ref, str(i)))
            if net is None:
                net = ('/%s/%s' % (sheet, role) if role in ('IN', 'OUT')
                       else role)
            lines.append('    (pad "%d" smd rect (at %s %s %s) (size 0.6 0.6)'
                         ' (layers "%s.Cu") (net %d "%s"))'
                         % (i, _fmt(px), _fmt(py), _fmt(rot), side,
                            ids[net], net))
            if i == 1 and ref in sheet_after_pad:
                lines.extend(sheet_lines)
        lines[-1] += ')'
        out.extend(lines)
    out.append(')')
    p = os.path.join(td, name + '.kicad_pcb')
    with open(p, 'w', encoding='utf-8') as fh:
        fh.write('\n'.join(out) + '\n')
    return p


def summary(r):
    found = [json.loads(line[len('JSON_SUMMARY: '):])
             for line in (r.stdout or '').splitlines()
             if line.startswith('JSON_SUMMARY: ')]
    assert len(found) == 1, 'expected one JSON_SUMMARY, got %d:\n%s' % (
        len(found), (r.stdout or '')[-1500:])
    return found[0]


def pose(inp, out, *op, refuse=None, code=None, accept=False, allow=()):
    return run_utils.check([sys.executable, '-X', 'utf8', PLACE_POSE, inp,
                            out] + [str(a) for a in op],
                           refuse=refuse, code=code, accept=accept,
                           allow=allow)


def poses(path):
    from kicad_parser import parse_kicad_pcb
    pcb = parse_kicad_pcb(run_utils.evidence(path, 'written board'))
    return {r: (round(f.x, 4), round(f.y, 4), round(f.rotation % 360.0, 4),
                f.layer) for r, f in pcb.footprints.items()}


def near(a, b, eps=1e-4):
    return all(abs(p - q) <= eps for p, q in zip(a[:3], b[:3])) and (
        len(a) < 4 or len(b) < 4 or a[3] == b[3])


def test_the_copy_lands_at_source_plus_offset():
    with tempfile.TemporaryDirectory() as td:
        inp = board(td)
        out = os.path.join(td, 'out.kicad_pcb')
        r = pose(inp, out, 'replicate', '--from', 'S1', '--to', 'S2',
                 '--to', 'S3', accept=True)
        s = summary(r)
        got = poses(out)
        # Default anchor: the most pads (the U), kept where it is.
        want = {'U2': (45, 20, 0, 'F.Cu'), 'R2': (45, 14, 0, 'F.Cu'),
                'C2': (40, 20, 90, 'F.Cu'),
                'U3': (75, 20, 0, 'F.Cu'), 'R3': (75, 14, 0, 'F.Cu'),
                'C3': (70, 20, 90, 'F.Cu')}
        for ref, w in want.items():
            assert near(got[ref], w), (ref, got[ref], w)
        # Nothing outside the targets moved: the source and the top level.
        for ref in ('U1', 'R1', 'C1', 'T1'):
            k, _sh, x, y, rot = PARTS[ref]
            assert near(got[ref], (x, y, rot)), (ref, got[ref])
        rep = s['replicate'][0]
        assert rep['from']['name'] == 'S1', rep['from']
        t2, t3 = rep['to']
        assert (t2['pairs'], t2['moved'], t2['offset']) == (3, 2, [30.0, 0.0]), t2
        assert (t3['pairs'], t3['moved'], t3['offset']) == (3, 2, [60.0, 0.0]), t3
        assert t2['offset_basis'] == {'kind': 'anchor', 'source': 'U1',
                                      'target': 'U2', 'chosen': 'most pads'}, t2
        assert t2['nets_renamed'] == {'/S1/IN': '/S2/IN',
                                      '/S1/OUT': '/S2/OUT'}, t2
        assert t2['nets_shared'] == ['GND', 'VCC'], t2
        notes = {n['ref']: n for n in s['ops']}
        assert set(notes) == {'U2', 'R2', 'C2', 'U3', 'R3', 'C3'}, sorted(notes)
        assert notes['R2']['source'] == 'R1' and notes['R2']['moved']
        assert not notes['U2']['moved'], notes['U2']
        assert s['no_worse'] is True and s['legal'] is True, s
        assert 'replicate S1 -> S2: 3 parts matched by symbol, 2 moved' in r.stdout
        print('  S2/S3 at S1 + (30,0)/(60,0); U2/U3 kept; S1 and T1 untouched')


def test_offset_and_anchor_move_the_copy():
    with tempfile.TemporaryDirectory() as td:
        inp = board(td)
        out = os.path.join(td, 'o.kicad_pcb')
        pose(inp, out, 'replicate', '--from', 'S1', '--to', 'S2',
             '--offset', '30', '3', accept=True)
        got = poses(out)
        for ref, w in {'U2': (45, 23, 0), 'R2': (45, 17, 0),
                       'C2': (40, 23, 90)}.items():
            assert near(got[ref], w), (ref, got[ref], w)
        # --anchor names a TARGET ref: R2 keeps its (at x y), takes R1's
        # rotation, and the channel is laid around it.
        out2 = os.path.join(td, 'a.kicad_pcb')
        r = pose(inp, out2, 'replicate', '--from', 'S1', '--to', 'S2',
                 '--anchor', 'R2', accept=True)
        got = poses(out2)
        for ref, w in {'R2': (51, 27, 0), 'U2': (51, 33, 0),
                       'C2': (46, 33, 90)}.items():
            assert near(got[ref], w), (ref, got[ref], w)
        b = summary(r)['replicate'][0]['to'][0]['offset_basis']
        assert b == {'kind': 'anchor', 'source': 'R1', 'target': 'R2',
                     'chosen': 'given'}, b
        print('  --offset 30 3 and --anchor R2 land as computed')


def test_nets_not_wired_alike_are_refused():
    with tempfile.TemporaryDirectory() as td:
        # C2's VCC pad wired to the channel input: VCC is on BOTH instances,
        # so it must map to itself.
        inp = board(td, 'shared', nets={('C2', '1'): '/S2/IN'})
        out = os.path.join(td, 'x.kicad_pcb')
        r = pose(inp, out, 'replicate', '--from', 'S1', '--to', 'S2',
                 refuse='not wired alike', code=4)
        s = summary(r)
        assert 'C1.1 VCC -> C2.1 /S2/IN' in r.stderr, r.stderr[-800:]
        assert 'a shared net' in r.stderr, r.stderr[-800:]
        assert s['net_breaks'] and s['moved'] == [], s
        assert not os.path.exists(out), 'a refusal wrote the board'
        # R2's pads swapped: /S1/IN would reach /S2/IN (at U2) AND /S2/OUT.
        inp = board(td, 'swap', nets={('R2', '1'): '/S2/OUT',
                                      ('R2', '2'): '/S2/IN'})
        r = pose(inp, out, 'replicate', '--from', 'S1', '--to', 'S2',
                 refuse='not wired alike', code=4)
        assert 'already maps to' in r.stderr, r.stderr[-800:]
        assert not os.path.exists(out)
        print('  shared-net and one-to-one breaks refused at 4, pads named')


def test_a_missing_part_is_refused():
    with tempfile.TemporaryDirectory() as td:
        inp = board(td, drop=('C2',))
        out = os.path.join(td, 'x.kicad_pcb')
        r = pose(inp, out, 'replicate', '--from', 'S1', '--to', 'S2',
                 refuse='do not hold the same parts', code=4)
        s = summary(r)
        assert s['only_in_source'] == ['C1'] and s['only_in_target'] == [], s
        assert not os.path.exists(out)
        print('  C2 absent: refused at 4, C1 named')


def test_a_different_footprint_is_refused():
    with tempfile.TemporaryDirectory() as td:
        inp = board(td, fpname={'R2': 't:R0805'})
        out = os.path.join(td, 'x.kicad_pcb')
        r = pose(inp, out, 'replicate', '--from', 'S1', '--to', 'S2',
                 refuse='mapped parts differ', code=4)
        assert 'R1 is t:R but R2 is t:R0805' in r.stderr, r.stderr[-600:]
        print('  R2 on another footprint: refused at 4')


def test_a_locked_target_part():
    with tempfile.TemporaryDirectory() as td:
        out = os.path.join(td, 'x.kicad_pcb')
        # R2 must move and is locked: refused, like `set` on a locked part.
        inp = board(td, 'lockedR', locked=('R2',))
        r = pose(inp, out, 'replicate', '--from', 'S1', '--to', 'S2',
                 refuse='locked in the board', code=4)
        # The refusal names the locked part (set's own lock guard does).
        assert summary(r)['locked'] == ['R2'] and not os.path.exists(out)
        # ...and `unlock` in the same call is the escape, as for `set`.
        pose(inp, out, 'unlock', 'R2', 'replicate', '--from', 'S1', '--to',
             'S2', accept=True)
        assert near(poses(out)['R2'], (45, 14, 0))
        # A locked part the copy does NOT move (the anchor) is no obstacle.
        inp = board(td, 'lockedU', locked=('U2',))
        out2 = os.path.join(td, 'y.kicad_pcb')
        pose(inp, out2, 'replicate', '--from', 'S1', '--to', 'S2',
             accept=True)
        from placement.parser import extract_locked_refs
        assert 'U2' in extract_locked_refs(out2), 'the anchor lost its lock'
        print('  moving a locked part refused at 4; unlock frees it; a '
              'locked anchor is fine')


def test_unknown_and_ambiguous_sheets():
    with tempfile.TemporaryDirectory() as td:
        inp = board(td)
        out = os.path.join(td, 'x.kicad_pcb')
        r = pose(inp, out, 'replicate', '--from', 'S9', '--to', 'S2',
                 refuse='is neither a sheet name nor a sheet path', code=2)
        assert [d['name'] for d in summary(r)['sheets']] == ['S1', 'S2', 'S3']
        # A unique start of a path resolves; a shared one is ambiguous.
        pose(inp, out, 'replicate', '--from', '/5a000000-0000-0000-0000-'
             '000000000001', '--to', '6b00', accept=True)
        # ...but not a start too short to mean a path: a mistyped name must
        # not land on whichever uuid happens to begin with it.
        pose(inp, out, 'replicate', '--from', 'S1', '--to', '6b',
             refuse='is neither a sheet name nor a sheet path', code=2)
        r = pose(inp, out, 'replicate', '--from', 'S1', '--to', '5a00',
                 refuse='is ambiguous: it is the start of 2 sheet paths',
                 code=2)
        assert len(summary(r)['sheets']) == 2
        # Two instances carrying one name: ambiguous, not "the first".
        inp2 = board(td, 'dupname', names={'S3': 'S2'})
        pose(inp2, out, 'replicate', '--from', 'S1', '--to', 'S2',
             refuse='is ambiguous: 2 sheet instances carry that name', code=2)
        # Case-insensitive, slashes optional.
        pose(inp, out, 'replicate', '--from', '/s1/', '--to', 's2',
             accept=True)
        print('  unknown -> 2 with the sheets listed; ambiguous name/path -> 2')


def test_usage_errors():
    with tempfile.TemporaryDirectory() as td:
        inp = board(td)
        out = os.path.join(td, 'x.kicad_pcb')
        argparse_exit = ('usage:',)
        pose(inp, out, 'replicate', '--from', 'S1', '--to', 'S2', '--to',
             'S3', '--offset', '1', '2', refuse='would all land on that spot',
             code=2, allow=argparse_exit)
        pose(inp, out, 'replicate', '--from', 'S1', '--to', 'S2',
             '--anchor', 'U2', '--offset', '1', '2',
             refuse='both --offset and --anchor', code=2, allow=argparse_exit)
        pose(inp, out, 'replicate', '--from', 'S1', '--to', 'S1',
             refuse='a sheet is not copied onto itself', code=2)
        pose(inp, out, 'replicate', '--from', 'S1', '--to', 'S2', '--anchor',
             'T1', refuse='is in neither the --from sheet nor a --to sheet',
             code=2)
        # Another op moving a SOURCE part: the copy would describe poses the
        # output does not have.
        pose(inp, out, 'set', 'R1', '16', '14', 'replicate', '--from', 'S1',
             '--to', 'S2', refuse="the copy reads the INPUT board's poses",
             code=2)
        # A target part named by a second op: one part, one pose.
        pose(inp, out, 'set', 'R2', '30', '30', 'replicate', '--from', 'S1',
             '--to', 'S2', refuse='is named by two ops in one call', code=2)
        assert not os.path.exists(out)
        print('  six usage errors refused at 2, nothing written')


def test_side_is_copied():
    with tempfile.TemporaryDirectory() as td:
        inp = board(td, back=('C2',))
        out = os.path.join(td, 'x.kicad_pcb')
        r = pose(inp, out, 'replicate', '--from', 'S1', '--to', 'S2',
                 accept=True)
        assert near(poses(out)['C2'], (40, 20, 90, 'F.Cu')), poses(out)['C2']
        t = summary(r)['replicate'][0]['to'][0]
        assert t['side_flips'] == ['C2 B->F'], t
        # A flip the writer cannot write faithfully is a REFUSAL with a
        # summary, not a traceback.
        inp = board(td, 'bare', back=('C2',), bare_text=True)
        out2 = os.path.join(td, 'y.kicad_pcb')
        r = pose(inp, out2, 'replicate', '--from', 'S1', '--to', 'S2',
                 refuse='the writer cannot write this arrangement', code=4)
        assert summary(r)['output'] is None and not os.path.exists(out2)
        print('  C2 on B.Cu is flipped to S1\'s side (F) at C1\'s rotation; '
              'an unwritable flip refused at 4')


def test_both_sheet_name_spellings_are_read():
    """`Footprint.sheet_name` / `sheet_file` as the FILE spells them, either
    form, and only from the block header: a pad cannot lend a block a sheet.
    (pcbnew's half of the parity is tests/gui_parity/
    test_1211_sheet_name_parity.py.)"""
    from kicad_parser import parse_kicad_pcb
    with tempfile.TemporaryDirectory() as td:
        new = parse_kicad_pcb(board(td, 'tok')).footprints
        old = parse_kicad_pcb(board(td, 'prop', prop_names=True)).footprints
        assert new['R2'].sheet_name == '/S2/' and old['R2'].sheet_name == 'S2', (
            new['R2'].sheet_name, old['R2'].sheet_name)
        assert new['T1'].sheet_name == '/' and old['T1'].sheet_name == '', (
            new['T1'].sheet_name, old['T1'].sheet_name)
        assert (new['R2'].sheet_file, old['R2'].sheet_file) == (
            'strip.kicad_sch', 'strip.kicad_sch'), (new['R2'].sheet_file,
                                                     old['R2'].sheet_file)
        # Escapes are undone, as pcbnew's GetSheetname() returns them.
        esc = parse_kicad_pcb(board(td, 'esc', names={'S2': 'my \\"S2\\"'}))
        assert esc.footprints['R2'].sheet_name == '/my "S2"/', (
            esc.footprints['R2'].sheet_name)
        # A sheet token AFTER the first pad is not the block's: R2 reads none.
        late = parse_kicad_pcb(board(td, 'late', sheet_after_pad=('R2',)))
        assert (late.footprints['R2'].sheet_name,
                late.footprints['R2'].sheet_file) == ('', ''), (
            late.footprints['R2'].sheet_name, late.footprints['R2'].sheet_file)
        assert late.footprints['U2'].sheet_name == '/S2/'
        out = os.path.join(td, 'x.kicad_pcb')
        pose(os.path.join(td, 'prop.kicad_pcb'), out, 'replicate', '--from',
             'S1', '--to', 'S2', accept=True)
        print('  (sheetname "/S2/") and (property "Sheetname" "S2") both read,'
              ' with the sheet file; escapes undone')


def test_glasgow_maps_by_symbol_and_names_the_address_straps():
    """The tracked board with real repeated instances. The two IO_Buffer
    sheets hold 68 parts each, matched one for one by symbol; the copy is
    refused because the I2C expanders' / DAC's / monitor's address pins are
    strapped differently per instance -- the net rule's job is to say so."""
    from kicad_parser import parse_kicad_pcb
    from placement import replicate
    from placement.pose_ops import PoseRefusal
    pcb = parse_kicad_pcb(run_utils.evidence(GLASGOW, 'glasgow board'))
    inst = replicate.sheet_instances(pcb)
    a = replicate.resolve_sheet(pcb, 'IO_Buffer_A', '--from', inst)
    b = replicate.resolve_sheet(pcb, 'IO_Buffer_B', '--to', inst)
    assert (replicate.describe(a, inst[a])['file'],
            replicate.describe(b, inst[b])['file']) == (
        'io_buffer.kicad_sch', 'io_buffer.kicad_sch')
    ma, mb = replicate.members(pcb, a), replicate.members(pcb, b)
    assert len(ma) == len(mb) == 68 and set(ma) == set(mb), (len(ma), len(mb))
    try:
        replicate.correspond(pcb, a, b)
    except PoseRefusal as exc:
        assert exc.code == 4, exc.code
        pads = sorted(x['pads'].split(' ', 1)[0] for x in exc.extra['net_breaks'])
        assert pads == ['U19.2', 'U20.6', 'U21.2'], pads
    else:
        raise AssertionError('glasgow IO_Buffer_A -> B was not refused')
    print('  68 for 68 by symbol; U19.2 / U20.6 / U21.2 named')


TESTS = [
    test_the_copy_lands_at_source_plus_offset,
    test_offset_and_anchor_move_the_copy,
    test_nets_not_wired_alike_are_refused,
    test_a_missing_part_is_refused,
    test_a_different_footprint_is_refused,
    test_a_locked_target_part,
    test_unknown_and_ambiguous_sheets,
    test_usage_errors,
    test_side_is_copied,
    test_both_sheet_name_spellings_are_read,
    test_glasgow_maps_by_symbol_and_names_the_address_straps,
]


if __name__ == '__main__':
    only = sys.argv[1:]
    ran = 0
    for t in TESTS:
        if only and not any(o in t.__name__ for o in only):
            continue
        print(f"--- {t.__name__}")
        t()
        ran += 1
    if only and not ran:
        print(f"NO TEST matches {only}")
        sys.exit(2)
    print('ALL PASS')
