#!/usr/bin/env python3
"""tool_records: the kicad_routing_tools records live in the project AND on
the board, and every reader takes the newer copy.

KiCad's own project save rewrites the .kicad_pro from the copy it loaded, so a
record the GUI wrote there after load is gone after the user's next save; the
board's own property `kicad_routing_tools` (written into the .kicad_pcb by that
same save) is the copy that survives. Each copy carries `records_rev`; a writer
stores one past the copy it started from.

wx-free, on fixture files (the board copy written in KiCad's own escaping) and
a fake board. The real pcbnew save is tests/gui_parity/test_records_survive_save.py.

Standalone, no pytest:  python3 tests/test_tool_records.py
"""
import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                '..', 'py_router'))

import tool_records as T                                         # noqa: E402
import protected_nets as P                                       # noqa: E402

_checks = 0
_fails = 0


def check(cond, label, detail=''):
    global _checks, _fails
    _checks += 1
    print(f"  {'PASS' if cond else 'FAIL'}  {label}" + (f" -- {detail}" if detail and not cond else ''))
    if not cond:
        _fails += 1


def board(d, name, ns=None, footprint_ns=None):
    """A .kicad_pcb with the board copy `ns` where KiCad 10 writes it (one tab
    in, after setup), and optionally a FOOTPRINT property of the same name."""
    p = os.path.join(d, name + '.kicad_pcb')
    lines = ['(kicad_pcb (version 20241229) (generator "pcbnew")', '\t(setup)']
    if ns is not None:
        lines.append('\t' + T.board_property_sexpr(ns))
    if footprint_ns is not None:
        lines += ['\t(footprint "R"', '\t\t' + T.board_property_sexpr(footprint_ns), '\t)']
    lines.append(')')
    with open(p, 'w', encoding='utf-8') as f:
        f.write('\n'.join(lines) + '\n')
    return p


def project(pcb, ns=None):
    pro = os.path.splitext(pcb)[0] + '.kicad_pro'
    doc = {'board': {'design_settings': {'rules': {}}}, 'meta': {'version': 1}}
    if ns is not None:
        doc['kicad_routing_tools'] = ns
    with open(pro, 'w', encoding='utf-8') as f:
        json.dump(doc, f)
    return pro


class FakeBoard:
    """GetFileName / GetProperties / SetProperties, as pcbnew's BOARD."""

    def __init__(self, path):
        self.path, self.props = path, {}

    def GetFileName(self):
        return self.path

    def GetProperties(self):
        return dict(self.props)

    def SetProperties(self, m):
        self.props = dict(m)


def main():
    with tempfile.TemporaryDirectory() as d:
        print("which copy a reader gets")
        a = board(d, 'pro_only')
        project(a, {'protected_nets': {'/A': 'diff-pair'}})
        check(T.effective_namespace(a).get('protected_nets') == {'/A': 'diff-pair'},
              "a project with no board copy reads as before")
        b = board(d, 'board_only', {'protected_nets': {'/B': 'x'}, 'records_rev': 1})
        check(T.effective_namespace(b).get('protected_nets') == {'/B': 'x'},
              "a board copy with no project at all is read")
        c = board(d, 'saved', {'protected_nets': {'/C': 'new'}, 'records_rev': 4})
        project(c, {'protected_nets': {'/C': 'old'}, 'records_rev': 3})
        check(T.effective_namespace(c)['protected_nets'] == {'/C': 'new'},
              "after a KiCad save (project rewritten from its load-time copy), "
              "the newer board copy wins")
        e = board(d, 'cli_out', {'protected_nets': {'/E': 'old'}, 'records_rev': 4})
        project(e, {'protected_nets': {'/E': 'new'}, 'records_rev': 5})
        check(T.effective_namespace(e)['protected_nets'] == {'/E': 'new'},
              "after a CLI step (project one past the board copy it carried), "
              "the project wins")
        t = board(d, 'tie', {'protected_nets': {'/T': 'board'}, 'records_rev': 2})
        project(t, {'protected_nets': {'/T': 'pro'}, 'records_rev': 2})
        check(T.effective_namespace(t)['protected_nets'] == {'/T': 'pro'},
              "a tie goes to the project")
        z = board(d, 'kicadsaved', {'protected_nets': {'/Z': 'x'}})
        project(z)                                   # KiCad wrote no namespace
        check(T.effective_namespace(z).get('protected_nets') == {'/Z': 'x'},
              "a project with no namespace loses to a board copy that has one")

        print("\nthe board copy, read off the file")
        tricky = {'protected_nets': {'/Q"uote': 'a\\b', 'μ': 'x'}, 'records_rev': 1}
        q = board(d, 'escaped', tricky)
        check(T.board_namespace(q) == tricky,
              "quotes, backslashes and non-ASCII survive KiCad's escaping",
              f"{T.board_namespace(q)}")
        f = board(d, 'fp_only', footprint_ns={'protected_nets': {'/F': 'x'}})
        check(T.board_namespace(f) is None,
              "a footprint property of the same name is not the board's")

        print("\nwriters start from the newer copy and store one past it")
        w = board(d, 'writer', {'protected_nets': {'/W': 'board'}, 'records_rev': 7})
        pw = project(w)
        P.persist_protected_nets(pw, {'/W2': 'length-matched'}, verbose=False)
        with open(pw, encoding='utf-8') as fh:
            ns = json.load(fh)['kicad_routing_tools']
        check(ns['protected_nets'] == {'/W': 'board', '/W2': 'length-matched'},
              "a CLI record write keeps what the board copy carried",
              f"{ns}")
        check(ns.get('records_rev') == 8, "and is one revision past it",
              f"{ns.get('records_rev')}")
        check(P.read_protected_nets(pw) == ns['protected_nets'],
              "and the readers then take the project's copy")

        print("\nthe live board (the GUI)")
        lb_path = board(d, 'live')
        lpro = project(lb_path)
        lb = FakeBoard(lb_path)
        P.persist_protected_nets(lpro, {'/L': 'diff-pair'}, verbose=False, board=lb)
        on_board = T.live_board_namespace(lb) or {}
        check(on_board.get('protected_nets') == {'/L': 'diff-pair'},
              "a GUI record write lands on the board", f"{on_board}")
        check(P.read_protected_nets(lpro) == {'/L': 'diff-pair'},
              "and in the project, for an in-session reader")
        project(lb_path)                    # KiCad's save rewrites the project...
        board(d, 'live', on_board)          # ...and writes the board copy out
        check(P.read_protected_nets(lpro) == {'/L': 'diff-pair'},
              "after the save the CLI readers still see it")
        nb = FakeBoard(board(d, 'nofile_dir'))
        nb.path = ''                                # an unsaved board (#1238)
        P.persist_protected_nets('', {'/U': 'x'}, verbose=False, board=nb)
        check((T.live_board_namespace(nb) or {}).get('protected_nets') == {'/U': 'x'},
              "an unsaved board records on the board alone")

        mb_path = board(d, 'mirror')
        mpro = project(mb_path, {'fab_floor_origin': {'min_via_diameter': 0.5},
                                 'records_rev': 1})
        mb = FakeBoard(mb_path)
        check(T.mirror_project_to_live_board(mb)
              and (T.live_board_namespace(mb) or {}).get('fab_floor_origin')
              == {'min_via_diameter': 0.5},
              "a file writer's record is mirrored onto the board")
        check(not T.mirror_project_to_live_board(mb),
              "and mirroring again changes nothing")

        print("\na repo-private rule (min_via_drill) rides in the records too")
        import fix_kicad_drc_settings as F
        rr = getattr(F, '_effective_rules', None)
        dpr = getattr(F, 'declared_project_rule', None)
        check(rr is not None and dpr is not None,
              "the effective rule reader exists")
        if rr is not None and dpr is not None:
            vp = board(d, 'viadrill')
            vpro = project(vp)
            with open(vpro, encoding='utf-8') as fh:
                doc = json.load(fh)
            doc['board']['design_settings']['rules']['min_via_drill'] = 0.3
            with open(vpro, 'w', encoding='utf-8') as fh:
                json.dump(doc, fh)
            check(dpr(vp, 'min_via_drill') == 0.3,
                  "a project's own rule reads as before")
            changes = F.apply_targets_to_project(doc, {'min_via_drill': 0.2}, {})
            check(doc['board']['design_settings']['rules'].get('min_via_drill') == 0.2
                  and (doc.get('kicad_routing_tools') or {}).get('repo_rules')
                  == {'min_via_drill': 0.2},
                  "the file writer lowers the rule and mirrors it into the records",
                  f"{changes} {doc.get('kicad_routing_tools')}")
            del doc['board']['design_settings']['rules']['min_via_drill']
            check(rr(doc).get('min_via_drill') == 0.2,
                  "with the rule dropped (KiCad's save), the records' copy is read")
            F.apply_targets_to_project(doc, {'min_via_drill': 0.25}, {})
            check('min_via_drill' not in doc['board']['design_settings']['rules'],
                  "and a larger target is not written over it (only-lower)")
            doc['board']['design_settings']['rules']['min_via_drill'] = 0.3
            check(rr(doc).get('min_via_drill') == 0.2,
                  "a stale rule KiCad kept beside a lower record reads the lower")

        print("\n#581 on the live board")
        sb = FakeBoard(board(d, 'snpc'))
        spro = project(sb.path)
        try:
            P.persist_same_net_pad_clearance(spro, 0.15, verbose=False, board=sb)
        except TypeError:
            pass
        check((T.live_board_namespace(sb) or {}).get('same_net_pad_clearance') == 0.15
              and P.read_same_net_pad_clearance(spro) == 0.15,
              "a GUI #581 record lands on the board and in the project")

    print(f"\n{_checks - _fails}/{_checks} checks passed")
    return 1 if _fails else 0


if __name__ == '__main__':
    sys.exit(main())
