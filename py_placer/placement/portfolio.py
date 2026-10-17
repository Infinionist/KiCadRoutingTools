"""Sibling copies of a written board, and the nets a placement ignores.

`copy_siblings` copies a board's sibling files (`.kicad_pro`, `.kicad_dru`,
the design brief, ...) next to a board a placement tool wrote, so the next
step reads the same DRC floor and declarations (#441); `ignore_net_ids`
resolves `--ignore-nets` patterns to net ids.
"""
from __future__ import annotations

import fnmatch
import os
import shutil
from typing import Optional, Sequence, Set

EPS = 1e-6


# --------------------------------------------------------------------------
# board / state plumbing
# --------------------------------------------------------------------------


def ignore_net_ids(pcb_data, patterns: Optional[Sequence[str]]) -> Set[int]:
    """Net ids matching the --ignore-nets patterns (plane-routed rails)."""
    ids: Set[int] = set()
    if patterns:
        for net_id, net in pcb_data.nets.items():
            if any(fnmatch.fnmatchcase(net.name, p) for p in patterns):
                ids.add(net_id)
    return ids


def copy_siblings(src_board: str, dst_board: str) -> None:
    """Carry the project/rules siblings (#441): a board without its .kicad_pro
    grades at the stock netclass, so every written candidate gets the input's
    siblings -- the probe routes and any later adoption read them."""
    src_base = os.path.splitext(src_board)[0]
    dst_base = os.path.splitext(dst_board)[0]
    from copy_board import SIBLING_EXTS   # ONE list (#711)
    for ext in SIBLING_EXTS:
        s, d = src_base + ext, dst_base + ext
        if not os.path.isfile(s):
            continue
        # IN PLACE the sibling is already its own destination. `copyfile`
        # raises SameFileError there (PermissionError via copy2 on Windows),
        # and every in-place run crashed AFTER writing its board: place_seed
        # --repair/--reseat with nothing to move and the seed path,
        # place_optimize X X, beautify_labels X X. `samefile`, not an abspath
        # compare, so a case-only spelling on a case-insensitive disk counts.
        if os.path.exists(d) and os.path.samefile(s, d):
            continue
        shutil.copyfile(s, d)


# --------------------------------------------------------------------------
# perturbation strategies
# --------------------------------------------------------------------------
# Each strategy PROPOSES poses on the oracle state, validating with
# candidate_valid and applying accepted moves as it goes -- so a later part is
# tested against its already-perturbed peers, and the emitted set is legal as a
# WHOLE, not merely pairwise against the original board. The caller owns the
# snapshot/restore bracket.


# --------------------------------------------------------------------------
# generation
# --------------------------------------------------------------------------


# --------------------------------------------------------------------------
# scoring, ranking, diversity
# --------------------------------------------------------------------------


