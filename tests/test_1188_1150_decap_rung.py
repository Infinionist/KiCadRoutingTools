#!/usr/bin/env python3
"""#1150 (c): the quench's tether gate holds a held cap stranded past the
decap search radius.

Under a --decaps-from intent a cap the reference keeps within the 5 mm radius
and the board strands beyond it is a `decap_ungraded` ERROR (#1142), but the
gate built its term `graded: False` and read it as 0 past the radius, so it
let the quench take the cap -- or its IC -- anywhere. Now the term is
`stranded_error` and measured wherever it stands: a move taking it farther
from its IC is refused, one bringing it closer is not; without the held list
the gate is inert on it, as before.

    python3 -X utf8 tests/test_1188_1150_decap_rung.py [case ...]
"""
import contextlib
import io
import os
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
for _d in ('py_router', 'py_placer', 'py_tools'):
    sys.path.insert(0, os.path.join(ROOT, _d))
sys.path.insert(0, TESTS_DIR)

from kicad_parser import parse_kicad_pcb           # noqa: E402
from placement import floorplan as fp              # noqa: E402
from placement.writer import write_placed_output   # noqa: E402

RUN_ALL_TIMEOUT = 900

ESP = os.path.join(ROOT, 'kicad_files', 'esp_prog.kicad_pcb')
SOURCES = ('kicad', 'sheet')
CLEARANCE = 0.2
_TMP = tempfile.TemporaryDirectory(prefix='t1188_')


def _quiet(fn, *a, **kw):
    with contextlib.redirect_stdout(io.StringIO()), \
            contextlib.redirect_stderr(io.StringIO()):
        return fn(*a, **kw)


def _parse(path):
    return _quiet(parse_kicad_pcb, path)


# ---------------------------------------------------------------------------
# #1150 on esp_prog C2
# ---------------------------------------------------------------------------

def _esp_c2_at(x, y):
    """esp_prog with C2 at (x, y) and its own --decaps-from intent: C2 is a
    cap the reference holds within the radius of U1."""
    from placement.portfolio import copy_siblings
    pcb = _parse(ESP)
    out = os.path.join(tempfile.mkdtemp(dir=_TMP.name), 'c2.kicad_pcb')
    _quiet(write_placed_output, ESP, out,
           [{'reference': 'C2', 'new_x': x, 'new_y': y,
             'new_rotation': pcb.footprints['C2'].rotation or 0.0}])
    copy_siblings(ESP, out)
    doc = _quiet(fp.emit_intent, _parse(out), out, decaps_from=ESP)
    assert 'C2' in (doc['decaps'].get('within_radius_refs') or ()), doc
    return out, fp.intent_from_dict(doc)


def test_1150c_the_gate_holds_a_stranded_held_cap():
    from placement import quench as q
    board, intent = _esp_c2_at(118.0, 95.0)
    pcb = _parse(board)

    def gate_for(it):
        gate, _p = fp.resolve_intent_gate(it, pcb, SOURCES)
        st = _quiet(q.QuenchState, pcb, board, CLEARANCE, 0.55, 30.0, 0.5,
                    0.15, 2.0, 2.0, 2.0, 0.1, 0.3, tethers=gate['tethers'])
        return st
    st = gate_for(intent)
    terms = [t for t in st._tether_terms
             if t.rule == 'decap_distance' and t.data['cap'] == 'C2']
    assert terms and not terms[0].data['graded'] \
        and terms[0].data.get('stranded_error'), [t.data for t in terms]
    p = st.parts['C2']
    away = {'C2': (p.x - 2.0, p.y, p.rot)}         # U1 is to the +x side
    home = {'C2': (p.x + 0.5, p.y, p.rot)}
    fails = st.tether_failures(away)
    assert any(f[0] == 'decap_distance' and f[1].startswith('C2')
               for f in fails), fails
    assert not any(f[1].startswith('C2') for f in st.tether_failures(home))
    # The control: the same board with no held list -- `decap_ungraded` a
    # warn for C2 -- leaves the gate inert on it, as it always was.
    doc = dict(intent.decaps)
    doc.pop('within_radius_refs', None)
    doc.pop('within_radius_mm', None)
    loose = fp.intent_from_dict({'schema': 1, 'kind': fp.KIND,
                                 'units': 'mm', 'decaps': doc})
    st2 = gate_for(loose)
    assert not any(f[1].startswith('C2')
                   for f in st2.tether_failures(away)), 'not inert'
    print(f"  PASS: a move 2 mm farther from U1 is refused {fails}; one "
          f"0.5 mm closer is not; with no held list the gate is inert")


TESTS = [v for k, v in sorted(globals().items())
         if k.startswith('test_') and callable(v)]


if __name__ == '__main__':
    want = sys.argv[1:]
    n = 0
    for t in TESTS:
        if want and not any(w in t.__name__ for w in want):
            continue
        print(t.__name__)
        t()
        n += 1
    print(f"ALL PASS ({n} case(s))")
