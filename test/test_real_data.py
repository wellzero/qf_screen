"""Smoke tests against the REAL store under ``QUANT_DATA_DIR``.

Skipped automatically when the tree is absent, so a checkout without the
data volume still produces a green hermetic run (the qf_factor tier
contract).
"""
from __future__ import annotations

import os

import pytest

from qf_screen.data import CNDataStore, build_universe

DATA_DIR = os.environ.get("QUANT_DATA_DIR", "/home/data/quant_free_data")
HAVE_DATA = os.path.isdir(os.path.join(DATA_DIR, "cn", "equity"))

pytestmark = [pytest.mark.data,
              pytest.mark.skipif(not HAVE_DATA, reason="no market data tree")]


def test_universe_builds():
    universe = build_universe(DATA_DIR)
    assert 4000 < len(universe) < 10000
    assert len(universe.st_named) > 50
    assert "SH600519" in universe


def test_tail_store_loads_and_adjusts():
    store = CNDataStore(DATA_DIR, max_workers=4)
    assert len(store.provenance) > 4000
    # the rebuilt adjusted close must exist and stay finite where traded
    close = store.wide("close")
    traded = store.wide("close_raw").notna()
    assert (close.notna() | ~traded).all().all()
    status = store.data_status()
    assert status["price"]["split_adjusted_events"] > 100
