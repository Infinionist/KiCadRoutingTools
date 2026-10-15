#!/usr/bin/env python3
"""Issue #1247: `--length-match-group auto` matched SUBSTRINGS, and was fed
nets the call never asked for.

    python3 tests/test_1247_auto_length_groups.py

The grouper ran `re.search` on unanchored tokens, so `WE` matched inside
"power", `CK` inside `JTAG_TCK`, `A\\d+` inside `Net-(U1-PadA15)`: ulx3s's
command/address group held all 22 `/power/*` nets, and neither ulx3s nor
orangecrab_ext_pll formed a single byte lane (`RAM_D0` has no `DQ`,
`RAM_LDQS+` no digit). Inside a route step it was fed every pre-existing net
the run registered as a rip candidate, so out-of-scope nets were meandered
and recorded as #521 protected nets, and the #766 warning called those
registrations "routed THIS run".

Pinned here, on the boards' REAL net names:

  ulx3s        command_address = the 20 SDRAM_* address/command nets plus
               SDRAM_CLK (the clock joins the group it clocks); D0-7+DQM0 and
               D8-15+DQM1 are byte lanes 0 and 1; nothing from /power/*.
  orangecrab   command_address = the 27 RAM_* address/command/clock nets;
               D0-7+LDM+LDQS+/- and D8-15+UDM+UDQS+/- are lanes 0 and 1;
               IO_A*, /sheetIO/A*, PGOOD_*, *_CMD are named as rejected.
  haasoscope   (the board `auto` was written for) forms four lanes of 11,
               lanes 0 and 1 of channel A and of channel B, each its DQ, DQS
               and DMI (LPDDR4's data-mask-inversion is sampled with its
               byte's DQ on the byte's strobe, so it is part of the lane),
               and ONE command_address (13): the CA bus is shared by both
               channels, so they share its clock. RESET_n and ZQ0 are left
               out, by name.

and, on synthetic results, the two route-step defects: the grouper's input is
the call's scope (an out-of-scope rip-candidate registration never joins), and
an `is_existing_route` registration is never reported as routed this run.
"""
import contextlib
import io
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, 'py_router'))

from kicad_parser import Segment, Net, PCBData, BoardInfo, parse_kicad_pcb
from routing_config import GridRouteConfig
import length_matching
import routing_common
from length_matching import (auto_group_ddr4_nets, classify_ddr_nets,
                             apply_length_matching_to_group)
from run_utils import evidence

KICAD_FILES = os.path.join(ROOT, 'kicad_files')


def _board_names(name):
    path = evidence(os.path.join(KICAD_FILES, name), 'board')
    with contextlib.redirect_stdout(io.StringIO()):
        pcb = parse_kicad_pcb(path)
    return sorted(n.name for n in pcb.nets.values() if n.name)


def _rng(fmt, lo, hi):
    return {fmt.format(i) for i in range(lo, hi + 1)}


def run():
    fails = []

    def check(name, cond, detail=""):
        if not cond:
            fails.append(name)
        print(("  PASS " if cond else "  FAIL ") + name
              + (f"  {detail}" if detail else ""))

    def grouped(c):
        return {k: set(v) for k, v in c['groups'].items()}

    # --- ulx3s (SDR SDRAM) --------------------------------------------------
    print("ulx3s")
    names = _board_names('ulx3s.kicad_pcb')
    check("ulx3s net list is the real one", len(names) > 300, f"{len(names)} nets")
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        auto_group_ddr4_nets(names)
    c = classify_ddr_nets(names)
    g = grouped(c)
    cmd = (_rng('SDRAM_A{}', 0, 12)
           | {'SDRAM_BA0', 'SDRAM_BA1', 'SDRAM_CKE', 'SDRAM_nCAS', 'SDRAM_nCS',
              'SDRAM_nRAS', 'SDRAM_nWE', 'SDRAM_CLK'})
    check("ulx3s groups are command_address + 2 byte lanes",
          set(g) == {'command_address', 'byte_lane_0', 'byte_lane_1'}, str(sorted(g)))
    check("ulx3s command_address = 20 SDRAM_* addr/cmd nets + SDRAM_CLK",
          g.get('command_address') == cmd,
          f"extra={sorted(g.get('command_address', set()) - cmd)} "
          f"missing={sorted(cmd - g.get('command_address', set()))}")
    check("ulx3s byte_lane_0 = SDRAM_D0..D7 + SDRAM_DQM0",
          g.get('byte_lane_0') == _rng('SDRAM_D{}', 0, 7) | {'SDRAM_DQM0'},
          str(sorted(g.get('byte_lane_0', []))))
    check("ulx3s byte_lane_1 = SDRAM_D8..D15 + SDRAM_DQM1",
          g.get('byte_lane_1') == _rng('SDRAM_D{}', 8, 15) | {'SDRAM_DQM1'},
          str(sorted(g.get('byte_lane_1', []))))
    members = set().union(*g.values()) if g else set()
    check("ulx3s: no /power/* net joins any group",
          not any(n.startswith('/power/') for n in members))
    foreign = {'JTAG_TCK', 'SD_CMD', 'OLED_CS', 'ADC_CSn', 'Net-(U1-PadA15)',
               '/flash/FLASH_SCK', '/flash/FLASH_nCS'}
    check("ulx3s: JTAG/SD/OLED/ADC/flash/pad-named nets join no group",
          not (members & foreign), str(sorted(members & foreign)))
    out = buf.getvalue()
    cmd_line = next((ln for ln in out.splitlines()
                     if ln.strip().startswith('command_address (')), '')
    check("members are PRINTED by name when the group is formed",
          'SDRAM_CLK' in cmd_line and 'SDRAM_nWE' in cmd_line, cmd_line[:120])
    check("a rejected DRAM-looking name is printed by name",
          'ADC_CSn' in out and 'OLED_CS' in out)

    # --- orangecrab_ext_pll (DDR3) -------------------------------------------
    print("orangecrab_ext_pll")
    names = _board_names('orangecrab_ext_pll.kicad_pcb')
    check("orangecrab net list is the real one", len(names) > 150, f"{len(names)} nets")
    c = classify_ddr_nets(names)
    g = grouped(c)
    cmd = (_rng('RAM_A{}', 0, 15) | _rng('RAM_BA{}', 0, 2)
           | {'RAM_CAS#', 'RAM_CK+', 'RAM_CK-', 'RAM_CKE', 'RAM_CS#', 'RAM_ODT',
              'RAM_RAS#', 'RAM_WE#'})
    check("orangecrab command_address = the 27 RAM_* addr/cmd/clock nets",
          g.get('command_address') == cmd and len(cmd) == 27,
          f"extra={sorted(g.get('command_address', set()) - cmd)} "
          f"missing={sorted(cmd - g.get('command_address', set()))}")
    check("orangecrab byte_lane_0 = RAM_D0..D7 + LDM + LDQS+/-",
          g.get('byte_lane_0') == _rng('RAM_D{}', 0, 7)
          | {'RAM_LDM', 'RAM_LDQS+', 'RAM_LDQS-'},
          str(sorted(g.get('byte_lane_0', []))))
    check("orangecrab byte_lane_1 = RAM_D8..D15 + UDM + UDQS+/-",
          g.get('byte_lane_1') == _rng('RAM_D{}', 8, 15)
          | {'RAM_UDM', 'RAM_UDQS+', 'RAM_UDQS-'},
          str(sorted(g.get('byte_lane_1', []))))
    check("orangecrab has exactly those three groups",
          set(g) == {'command_address', 'byte_lane_0', 'byte_lane_1'}, str(sorted(g)))
    members = set().union(*g.values()) if g else set()
    bad = sorted(n for n in members
                 if n.startswith(('IO_', '/sheetIO/')) or 'PGOOD' in n
                 or n.endswith('_CMD'))
    check("orangecrab: no IO_*, /sheetIO/*, PGOOD_* or *_CMD member", not bad, str(bad))
    rejected = set(c['not_dram_interface'])
    check("orangecrab: IO_A* and /sheetIO/A* are reported as rejected by name",
          _rng('IO_A{}', 0, 5) | _rng('/sheetIO/A{}', 0, 5) <= rejected,
          str(sorted(rejected)))
    check("orangecrab: RAM_RESET# is reported as an ungrouped interface net",
          'RAM_RESET#' in c['no_group_token'], str(c['no_group_token']))

    # --- haasoscope (the control: the board auto was written for) ------------
    print("haasoscope_pro_max_test (control)")
    allnames = _board_names('haasoscope_pro_max_test.kicad_pcb')
    scope = [n for n in allnames if n.startswith('Net-(U1')]
    c = classify_ddr_nets(scope, allnames)
    g = grouped(c)
    # Why four lanes and one command group (the controller U3's own pin
    # names say it): channel A is U3's lanes 0-1 (DDR_DQ[0:15], DQS[0:1],
    # DM[0:1]) and channel B its lanes 2-3 (DDR_DQ[16:31], DQS[2:3],
    # DM[2:3]). A lane is sampled on its OWN strobe, so A's lane 0 and B's
    # lane 0 are different lanes and are never matched to each other. The
    # command/address bus is SHARED: every CA/CK/CKE net carries both
    # channels' pins (Net-(U1B-CA0_A) is U1's CA0_A and CA0_B), so channel B
    # has no clock of its own and its CS0_B / ODT_CA_B are sampled on the
    # same CK as channel A's -- one command/address group.
    def lane(n, ch):
        lo = 8 * n
        return ({f'Net-(U1A-DQ{i}_{ch})' for i in range(lo, lo + 8)}
                | {f'Net-(U1A-DQS{n}_{p}_{ch})' for p in 'ct'}
                | {f'Net-(U1A-DMI{n}_{ch})'})
    cmd = ({f'Net-(U1B-CA{i}_A)' for i in range(6)}
           | {'Net-(U1B-CK_c_A)', 'Net-(U1B-CK_t_A)', 'Net-(U1B-CKE0_A)',
              'Net-(U1B-CS0_A)', 'Net-(U1B-CS0_B)', 'Net-(U1B-ODT_CA_A)',
              'Net-(U1B-ODT_CA_B)'})
    want = {f'byte_lane_{n} @ Net-(U1A- channel {ch}': lane(n, ch)
            for n in (0, 1) for ch in 'AB'}
    for key, members in want.items():
        check(f"haasoscope {key} = its DQ, DQS and DMI (11)",
              g.get(key) == members and len(members) == 11,
              str(sorted(g.get(key, set()) ^ members)))
    cmd_key = 'command_address @ Net-(U1B-'
    check("haasoscope command_address is one group over both channels (13)",
          g.get(cmd_key) == cmd and len(cmd) == 13,
          str(sorted(g.get(cmd_key, set()) ^ cmd)))
    check("haasoscope has exactly those five groups",
          set(g) == set(want) | {cmd_key}, str(sorted(g)))
    check("the report names the proven channels",
          c.get('channels') == {'Net-(U1A-': ['A', 'B'], 'Net-(U1B-': ['A', 'B']},
          str(c.get('channels')))
    check("haasoscope leaves exactly RESET_n and ZQ0 out, by name",
          set(c['no_group_token']) == {'Net-(U1B-RESET_n)', 'Net-(U1B-ZQ0)'},
          str(c['no_group_token']))
    # The quick test routes only the CA nets: the interface is still read off
    # the BOARD, so a CA-only scope still forms its group.
    ca = [n for n in allnames if n.startswith('Net-(U1B-CA')]
    c = classify_ddr_nets(ca, allnames)
    check("a CA-only scope still forms command_address, keyed as on the "
          "whole board (context = board)",
          set(c['groups'].get(cmd_key, [])) == set(ca) and len(ca) == 6
          and len(c['groups']) == 1, str(c['groups']))

    # --- interfaces: lanes of different interfaces are never one group -------
    print("interfaces and channels")
    # Two channels, each with its OWN clock (an LPDDR4 part wired as two
    # independent channels, JEDEC ball names): lanes AND command/address
    # split by channel.
    dual = ([f'LP4_DQ{i}_{ch}' for i in range(16) for ch in 'AB']
            + [f'LP4_DQS{n}_{p}_{ch}' for n in (0, 1) for p in 'tc' for ch in 'AB']
            + [f'LP4_CA{i}_{ch}' for i in range(6) for ch in 'AB']
            + [f'LP4_CK_{p}_{ch}' for p in 'tc' for ch in 'AB']
            + [f'LP4_CKE0_{ch}' for ch in 'AB'] + [f'LP4_CS0_{ch}' for ch in 'AB'])
    c = classify_ddr_nets(dual)
    g = grouped(c)
    cmd_a = ({f'LP4_CA{i}_A' for i in range(6)}
             | {'LP4_CK_t_A', 'LP4_CK_c_A', 'LP4_CKE0_A', 'LP4_CS0_A'})
    check("each channel with its own clock gets its own command/address group",
          g.get('command_address @ LP4_ channel A') == cmd_a
          and len(g.get('command_address @ LP4_ channel B', ())) == 10,
          str(sorted(g)))
    check("...and its own lanes (lane 0 of A is not lane 0 of B)",
          g.get('byte_lane_0 @ LP4_ channel A')
          == {f'LP4_DQ{i}_A' for i in range(8)} | {'LP4_DQS0_t_A', 'LP4_DQS0_c_A'},
          str(sorted(g.get('byte_lane_0 @ LP4_ channel A', []))))
    # Two interfaces under different prefixes (lpddr5_testbed's spelling):
    # lanes never mix, so the keys name the interface.
    two = [f'/LPDDR5/LPDDR5_{x}.{s}' for x in 'AB'
           for s in [f'DQ{i}' for i in range(16)]
           + ['RDQS0_P', 'RDQS0_N', 'RDQS1_P', 'RDQS1_N', 'CA0', 'CA1', 'CS0',
              'CK_P', 'CK_N']]
    c = classify_ddr_nets(two)
    g = grouped(c)
    check("two prefixes: four lanes and two command groups, none shared",
          set(g) == {f'{k} @ /LPDDR5/LPDDR5_{x}.' for x in 'AB'
                     for k in ('byte_lane_0', 'byte_lane_1', 'command_address')}
          and all(len({n.split('.')[0] for n in v}) == 1 for v in g.values()),
          str(sorted(g)))
    # A lone `_B` is active-low, not a channel: one interface, plain keys.
    c = classify_ddr_nets(['DDR_A0', 'DDR_BA0', 'DDR_CKE', 'DDR_CS_B', 'DDR_WE_B',
                           'DDR_DQ0', 'DDR_DQ1', 'DDR_DQS0_P', 'DDR_CK_P'])
    check("a lone _B suffix is read as active-low, not as channel B",
          'DDR_CS_B' in c['groups'].get('command_address', [])
          and not c['channels'] and 'byte_lane_0' in c['groups'], str(c['groups']))

    # --- token rules on synthetic names --------------------------------------
    print("token rules")
    c = classify_ddr_nets(['/power/WAKE', '/power/PWREN', 'JTAG_TCK',
                           'Net-(U1-PadA15)', 'IO_SCK', 'SPI_SCLK', 'SD0_CMD'])
    check("no WE inside power, no CK inside TCK/SCK, no A15 inside PadA15",
          not c['groups'], str(c['groups']))
    ctx = ['DDR_A0', 'DDR_BA0', 'DDR_CKE', 'DDR_DQ0', 'DDR_DQ8', 'DDR_DQS_P',
           'DDR_DQS_N', 'IO_A0', 'IO_A1']
    c = classify_ddr_nets(ctx)
    check("a whole-token A0 joins only under a DRAM prefix",
          'IO_A0' in c['not_dram_interface']
          and 'DDR_A0' in c['groups'].get('command_address', []), str(c))
    check("a bare DQS with two data lanes in its interface is named, not guessed",
          {'DDR_DQS_P', 'DDR_DQS_N'} <= set(c['lane_undeterminable']),
          str(c['lane_undeterminable']))
    c = classify_ddr_nets(['DDR_A0', 'DDR_BA0', 'DDR_CKE', 'DDR_DQ0', 'DDR_DQ7',
                           'DDR_DQS_P', 'DDR_DQS_N', 'DDR_DM'])
    check("a bare DQS/DM on a one-lane (x8) interface joins that lane",
          {'DDR_DQS_P', 'DDR_DQS_N', 'DDR_DM'} <= set(c['groups'].get('byte_lane_0', [])),
          str(c['groups']))
    c = classify_ddr_nets(['/DDR3/SA0', '/DDR3/SBA0', '/DDR3/SCKE0', '/DDR3/SCKP',
                           '/DDR3/SDQ0', '/DDR3/SDQS0P', '/DDR3/SDQM0', '/X/SD0'])
    check("Allwinner S-prefixed names group; SD0 is not S+D0",
          set(c['groups'].get('command_address', []))
          == {'/DDR3/SA0', '/DDR3/SBA0', '/DDR3/SCKE0', '/DDR3/SCKP'}
          and set(c['groups'].get('byte_lane_0', []))
          == {'/DDR3/SDQ0', '/DDR3/SDQS0P', '/DDR3/SDQM0'}, str(c['groups']))
    dq_bits = [f'/DDR/RAM_DQ{b}_{i}' for b in range(2) for i in range(8)]
    c = classify_ddr_nets(dq_bits + ['/DDR/RAM_BA0', '/DDR/RAM_CKE'])
    check("DQ<byte>_<bit> naming reads the byte as the lane",
          set(c['groups'].get('byte_lane_1', [])) == set(dq_bits[8:]),
          str(sorted(c['groups'].get('byte_lane_1', []))))
    c = classify_ddr_nets(['/PCH/DMI0_TXP', '/PCH/DMI0', '/PCH/DMI1', '/PCH/CLK'])
    check("a DMI lane name outside a DRAM interface joins nothing (PCIe-style DMI)",
          not c['groups'] and '/PCH/DMI0' in c['not_dram_interface'], str(c))
    c = classify_ddr_nets(['/DDR3/~{DDR3_CAS}', '/DDR3/DDR3_A0', '/DDR3/DDR3_BA0',
                           '/DDR3/DDR3_CKE'])
    check("a KiCad overbar does not split the interface prefix",
          '/DDR3/~{DDR3_CAS}' in c['groups'].get('command_address', []),
          str(c['groups']))

    # --- route step: the grouper's input is the CALL's scope -----------------
    print("route-step scope")
    ids = {'DDR_A0': 1, 'DDR_A1': 2, 'DDR_A2': 3, 'DDR_A3': 4, 'DDR_BA0': 5,
           'DDR_CKE': 6, '/power/WAKE': 7}
    pcb_stub = PCBData(footprints={}, nets={i: Net(i, n) for n, i in ids.items()},
                       segments=[], vias=[],
                       board_info=BoardInfo(layers={}, copper_layers=['F.Cu']),
                       pads_by_net={})
    reg = {'new_segments': [], 'new_vias': [], 'iterations': 0,
           'is_existing_route': True}
    routed = {1: {'route_length': 10.0}, 2: {'route_length': 12.0},
              3: dict(reg),             # in scope: an EARLIER step routed it
              4: dict(reg),             # out of scope: a rip candidate only
              5: {'route_length': 9.0},
              7: dict(reg)}             # out of scope, and not DRAM at all
    seen = []

    def _record(net_results, names, *a, **k):
        seen.append(list(names))
        return net_results

    class _Cfg:
        time_matching = False

    saved = routing_common.apply_length_matching_to_group
    routing_common.apply_length_matching_to_group = _record
    try:
        rep = {}
        with contextlib.redirect_stdout(io.StringIO()):
            routing_common.run_length_matching(
                routed, [['auto']], _Cfg(), pcb_stub,
                scope_net_ids={1, 2, 3, 5}, report=rep)
        check("auto groups only the call's scope (no out-of-scope registration)",
              seen and set(seen[0]) == {'DDR_A0', 'DDR_A1', 'DDR_A2', 'DDR_BA0'},
              str(seen))
        check("the formed group is carried in the report (JSON_SUMMARY)",
              rep.get('groups') == [{'name': 'command_address', 'source': 'auto',
                                     'members': ['DDR_A0', 'DDR_A1', 'DDR_A2',
                                                 'DDR_BA0']}], str(rep.get('groups')))
        check("the report says what auto read as the interface",
              'DDR_' in (rep.get('auto') or {}).get('interfaces', {}), str(rep.get('auto')))
        seen.clear()
        with contextlib.redirect_stdout(io.StringIO()):
            routing_common.run_length_matching(routed, [['auto']], _Cfg(), pcb_stub)
        check("with no scope, every is_existing_route registration is left out",
              seen and set(seen[0]) == {'DDR_A0', 'DDR_A1', 'DDR_BA0'}, str(seen))
        seen.clear()
        with contextlib.redirect_stdout(io.StringIO()):
            routing_common.run_length_matching(
                routed, [['DDR_A*']], _Cfg(), pcb_stub, scope_net_ids={1, 2, 3, 5})
        check("named-pattern groups still match earlier-step members (#489 s7)",
              seen and set(seen[0]) == {'DDR_A0', 'DDR_A1', 'DDR_A2', 'DDR_A3'},
              str(seen))
    finally:
        routing_common.apply_length_matching_to_group = saved

    # --- the #766 warning names only members routed THIS run -----------------
    print("#766 routed-this-run attribution")

    def seg(y, x2, net):
        return Segment(start_x=10.0, start_y=y, end_x=x2, end_y=y, width=0.15,
                       layer='F.Cu', net_id=net)

    s1, s2, s3 = [seg(10.0, 40.0, 1)], [seg(20.0, 25.0, 2)], [seg(30.0, 27.0, 3)]
    pcb = PCBData(footprints={},
                  nets={1: Net(1, 'DDR_A0'), 2: Net(2, 'DDR_A1'), 3: Net(3, 'DDR_A2')},
                  segments=s1 + s2 + s3, vias=[],
                  board_info=BoardInfo(layers={}, copper_layers=['F.Cu', 'B.Cu']),
                  pads_by_net={})
    cfg = GridRouteConfig(layers=['F.Cu', 'B.Cu'], track_width=0.15, clearance=0.15)
    cfg.length_match_tolerance = 0.5
    results = {
        'DDR_A0': {'new_segments': s1, 'new_vias': [], 'route_length': 30.0},
        # a pre-existing net registered as a rip candidate: an EARLIER step
        'DDR_A1': {'new_segments': s2, 'new_vias': [], 'iterations': 0,
                   'is_existing_route': True},
        # an in-run result with no route_length (a hybrid escape): THIS run
        'DDR_A2': {'new_segments': s3, 'new_vias': [], 'iterations': 4},
    }
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        apply_length_matching_to_group(results, ['DDR_A0', 'DDR_A1', 'DDR_A2'],
                                       cfg, pcb)
    out = buf.getvalue()
    this_run = next((ln for ln in out.splitlines() if 'routed THIS run' in ln), '')
    earlier = next((ln for ln in out.splitlines() if 'earlier step' in ln), '')
    check("an is_existing_route member is NOT called routed this run",
          'DDR_A1' not in this_run, this_run.strip())
    check("...it is reported as measured from earlier-step copper",
          'DDR_A1' in earlier, earlier.strip())
    check("negative control: a hybrid in-run member IS called routed this run",
          'DDR_A2' in this_run, this_run.strip())

    print()
    if fails:
        print(f"FAILED ({len(fails)}): " + ", ".join(fails))
        return 1
    print("ALL PASS")
    return 0


if __name__ == '__main__':
    sys.exit(run())
