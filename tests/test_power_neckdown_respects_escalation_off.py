"""The power/impedance neck-down ladder obeys --escalation off.

Before: `_power_width_ladder` and the long-trunk neck-down retry consulted
only config.power_tap_neckdown, so a run that promised exact sizes
(`--escalation off`) still shipped 95 of 115 mm of a 1 mm rail at 0.15 mm
(remoteForwarder cm5-io VBUS_IN).
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / 'py_router'))

import fab_tiers  # noqa: E402
from single_ended_routing import _neckdown_allowed  # noqa: E402


class _Cfg:
    power_tap_neckdown = True


def test_escalation_off_forbids_the_ladder():
    prev = fab_tiers.get_escalation_policy()
    try:
        fab_tiers.set_escalation_policy('off')
        assert _neckdown_allowed(_Cfg()) is False
    finally:
        fab_tiers.set_escalation_policy(*prev)


def test_default_policy_keeps_the_ladder():
    prev = fab_tiers.get_escalation_policy()
    try:
        fab_tiers.set_escalation_policy('fab')
        assert _neckdown_allowed(_Cfg()) is True
        cfg = _Cfg()
        cfg.power_tap_neckdown = False
        assert _neckdown_allowed(cfg) is False
    finally:
        fab_tiers.set_escalation_policy(*prev)


TESTS = [test_escalation_off_forbids_the_ladder, test_default_policy_keeps_the_ladder]

if __name__ == '__main__':
    for t in TESTS:
        t()
        print(f'ok {t.__name__}')
