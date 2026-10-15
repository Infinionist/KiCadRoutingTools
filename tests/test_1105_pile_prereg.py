#!/usr/bin/env python3
"""#1105: the pile A/B's pre-registrations cannot change without a test edit.

`tests/1105_pile_ab_prereg.json` fixes, before any pile number existed, how
stage 3.5's decap claim is measured on the issue's own basis and what would
make it a default: the boards, the pile stager, the intent's argv, the one
pre-registered ladder candidate, the arbiter's guards and the guards it never
reads (with their tolerances), the eligibility rule, and the stop rule.

`tests/1105_pile_ab_prereg_v2.json` fixes it again for #1142's per-cap signal
(PR #1154), before any pile number on that signal existed: one candidate again
(within-limit), and everything the first file fixed that the signal change
does not force kept as written -- which
`test_v2_keeps_what_the_signal_does_not_force` holds to the first file, key
by key.

A pre-registration that its own regenerator, or the next edit, can rewrite is
not one. So:

* the file's sha256 (newlines normalised, so a CRLF checkout agrees) is
  pinned here -- changing a threshold means changing this constant in the
  same commit, where a reviewer sees it;
* the candidate names real seeder knobs, and the boards are real corpus
  files;
* the pile rows in `tests/test_placement_ab.py` take their boards, candidate,
  guards and tolerances FROM this file, never from a copy of it.

    python3 tests/test_1105_pile_prereg.py
"""
import hashlib
import json
import os
import sys

TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(TESTS_DIR)
for _d in ('py_router', 'py_placer', 'py_tools'):
    sys.path.insert(0, os.path.join(ROOT, _d))
sys.path.insert(0, TESTS_DIR)

PREREG = os.path.join(TESTS_DIR, '1105_pile_ab_prereg.json')

#: sha256 of the pre-registration with CRLF folded to LF, committed with the
#: file before any pile measurement. The file's `base` names the commits the
#: branch's first build sat on (PR #1140's head, since closed); the branch was
#: rebuilt on upstream main without #1140's routing commits, and the record is
#: kept as written rather than edited after the fact.
PREREG_SHA256 = ('c885671e73c0145ac9c7a9ebd4a25be5a6df1fed06ea36542dbef6ceb06e3dce')

PREREG_V2 = os.path.join(TESTS_DIR, '1105_pile_ab_prereg_v2.json')

#: sha256 of the second pre-registration, CRLF folded to LF, committed with
#: the file before any pile measurement on #1142's per-cap signal.
PREREG_V2_SHA256 = ('a49094b27b34ec110efa868c33fefab95951030edeeb35895b5d8031bd73fb3f')


def _bytes(path=PREREG):
    with open(path, 'rb') as fh:
        return fh.read().replace(b'\r\n', b'\n')


def _doc(path=PREREG):
    return json.loads(_bytes(path).decode('utf-8'))


def test_the_preregistration_is_pinned():
    for path, want, name in ((PREREG, PREREG_SHA256, 'PREREG_SHA256'),
                             (PREREG_V2, PREREG_V2_SHA256,
                              'PREREG_V2_SHA256')):
        got = hashlib.sha256(_bytes(path)).hexdigest()
        assert got == want, (
            f"{os.path.basename(path)} changed (sha256 {got}). A "
            f"pre-registration is fixed before its numbers exist; if this "
            f"edit is deliberate, say why in the commit and update {name} "
            f"with it")
        print(f"  PASS: {os.path.basename(path)} pinned ({got[:12]})")


#: The sections each file must carry. The second has no ladder: its pilot
#: measures the candidate itself, and `on_go` says what a GO licenses.
_SECTIONS = ('boards', 'input', 'intent', 'seed', 'signal', 'arbiter_guards',
             'unread_guards', 'eligibility', 'candidate', 'pilot',
             'stop_rule')


def test_the_preregistration_names_real_things():
    for path, extra in ((PREREG, ('ladder', 'decisive')),
                        (PREREG_V2, ('supersedes', 'on_go'))):
        _names_real_things(_doc(path), _SECTIONS + extra)


def _names_real_things(doc, sections):
    from placement import seeder
    for key in sections:
        assert key in doc, key
    for b in doc['boards']:
        assert os.path.isfile(os.path.join(ROOT, 'kicad_files', b)), b
    assert len(set(doc['boards'])) >= 3, doc['boards']
    cand = doc['candidate']
    for k, v in cand.items():
        if k.isupper():
            assert hasattr(seeder, k), f"seeder has no knob {k}"
            assert isinstance(v, type(getattr(seeder, k))), (k, v)
    assert cand['DECAP_LATE_AT'] in ('after_last_owner', 'after_queue'), cand
    unread = {k: v for k, v in doc['unread_guards'].items() if k != 'why'}
    assert not set(unread) & set(doc['arbiter_guards']), (
        "a guard the arbiter reads cannot also be an unread guard")
    for k, tol in unread.items():
        assert set(tol) <= {'rel', 'abs'} and len(tol) == 1, (k, tol)
    print(f"  PASS: {len(doc['boards'])} boards, candidate "
          f"{cand['DECAP_LATE_AT']}+within_limit="
          f"{cand['DECAP_LATE_WITHIN_LIMIT']}, {len(unread)} unread "
          f"guard(s)")


#: The first file's keys the second keeps as written: everything #1142's
#: signal change does not force. `unread_guards` is compared on its
#: tolerances (its `why` named the first file's ladder), and the go rule on
#: its text with the candidate's name: the rule is the first file's, the
#: candidate is not.
_KEPT = ('boards', 'input', 'seed', 'signal', 'ignore_nets',
         'arbiter_guards')


def _go_rule(cand):
    return (f"the CANDIDATE ({cand}) improves -- signal falls, no arbiter "
            f"guard rises, intent_errors do not rise -- on >= max(1, N-1) of "
            f"the N eligible boards")


def test_v2_keeps_what_the_signal_does_not_force():
    from measure_1105_pile_variants import PREREG_V2 as PILOT_DEFAULT
    from measure_1105_pile_variants import _candidate_name
    v1, v2 = _doc(PREREG), _doc(PREREG_V2)
    for k in _KEPT:
        assert v2[k] == v1[k], f"v2 changed {k}: {v1[k]!r} -> {v2[k]!r}"
    for k in ('argv', 'one_file_for', 'refuse_unless'):
        assert v2['intent'][k] == v1['intent'][k], k

    def tol(d):
        return {k: v for k, v in d.items() if k != 'why'}
    assert tol(v2['unread_guards']) == tol(v1['unread_guards'])
    assert v2['supersedes']['file'] == 'tests/' + os.path.basename(PREREG)
    # One candidate, a different one, under the first file's go rule.
    c1, c2 = _candidate_name(v1), _candidate_name(v2)
    assert (c1, c2) == ('after-queue', 'within-limit'), (c1, c2)
    assert v2['candidate']['name'] == c2
    assert v1['pilot']['go_rule'].startswith(_go_rule(c1)), v1['pilot']
    assert v2['pilot']['go_rule'].startswith(_go_rule(c2)), v2['pilot']
    assert v2['pilot']['variants'] == v1['pilot']['variants']
    # The one eligibility clause the signal change forces; the pilot reads
    # it, and runs this file unless told otherwise.
    assert v2['eligibility']['held_list_required'] is True
    assert 'held_list_required' not in v1['eligibility']
    assert os.path.samefile(PILOT_DEFAULT, PREREG_V2), PILOT_DEFAULT
    print(f"  PASS: v2 keeps {len(_KEPT)} keys, the intent's argv and the "
          f"guards' tolerances; candidate {c1} -> {c2}; one eligibility "
          f"clause added")


TESTS = [
    test_the_preregistration_is_pinned,
    test_the_preregistration_names_real_things,
    test_v2_keeps_what_the_signal_does_not_force,
]


if __name__ == '__main__':
    want = sys.argv[1:]
    for t in TESTS:
        if want and not any(w in t.__name__ for w in want):
            continue
        print(f"--- {t.__name__}")
        t()
    print("ALL PASS")
