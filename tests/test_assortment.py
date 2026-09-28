# -*- coding: utf-8 -*-
"""Подбор пород: правило 10-20-30 при ограничениях узких полос и тени.

    python tests/test_assortment.py
"""
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "src"))

import yaml                                       # noqa: E402
import assortment as A                            # noqa: E402


def _catalog():
    with open(os.path.join(ROOT, "config", "species.yaml"), encoding="utf-8") as f:
        return yaml.safe_load(f)["trees"]


def test_constraints_and_caps_together():
    # Как на Наташинском: четверть деревьев в узких полосах, часть в тени.
    # Раньше узкие полосы и тень переназначались поверх общего подбора,
    # и семейство набирало 36% при потолке 30%.
    trees = _catalog()
    compact = [t for t in trees if t.get("crown_m", 8) <= 5]
    shaded = [t for t in compact if t.get("shade_tolerance") in ("medium", "high")]
    allowed = [compact] * 24 + [shaded] * 5 + [trees] * 86
    out = A.assign_constrained(allowed)
    assert all(out[i] in allowed[i] for i in range(len(out)))
    idx = {t["id"]: t for t in trees}
    rc = A.rule_check([{"species_id": s["id"], "species_name": s["name"]}
                       for s in out], catalog_index=idx)
    assert rc["species"]["ok"] and rc["genus"]["ok"] and rc["family"]["ok"], rc


def test_no_species_left_empty():
    trees = _catalog()[:2]            # пород заведомо мало для правила
    out = A.assign_constrained([trees] * 30)
    assert all(s is not None for s in out)
    assert A.assign_constrained.relaxed_level > 0


if __name__ == "__main__":
    failed = 0
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print(f"ok    {name}")
            except AssertionError as e:
                failed += 1
                print(f"FAIL  {name}\n{e}")
    sys.exit(1 if failed else 0)
