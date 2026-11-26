"""CLI tests against the synthetic store (hermetic: --data-dir)."""
from __future__ import annotations

import json

import pandas as pd
import pytest

from qf_screen.cli import main

pytestmark = pytest.mark.unit


def _common(store_dir, extra=()):
    return ["--data-dir", str(store_dir), "--workers", "1", *extra]


def test_list(capsys):
    assert main(["list"]) == 0
    out = capsys.readouterr().out
    for name in ("value_dividend", "quality_growth", "momentum_trend",
                 "low_vol_defensive", "smart_money_ashare"):
        assert name in out


def test_run_table_and_csv(store_dir, tmp_path, capsys):
    out = tmp_path / "members.csv"
    assert main(["run", "value_dividend", *_common(store_dir),
                 "--as-of", "2026-06-30", "--out", str(out)]) == 0
    printed = capsys.readouterr().out
    assert "funnel" in printed and "members" in printed
    frame = pd.read_csv(out)
    assert "symbol" in frame.columns and "score" in frame.columns


def test_run_json(store_dir, tmp_path, capsys):
    out = tmp_path / "members.json"
    assert main(["run", "quality_growth", *_common(store_dir),
                 "--as-of", "2026-06-30", "--format", "json", "--out", str(out)]) == 0
    payload = json.loads(out.read_text())
    assert payload["asof"] == "2026-06-30" and "funnel" in payload


def test_run_explain(store_dir, capsys):
    assert main(["run", "quality_growth", *_common(store_dir),
                 "--as-of", "2026-06-30", "--explain", "SH600001"]) == 0
    out = capsys.readouterr().out
    assert "PASS" in out and "explain SH600001" in out


def test_run_unknown_preset_fails(store_dir, capsys):
    """An unknown preset is a one-line error and a non-zero exit, not a
    traceback."""
    with pytest.raises(SystemExit) as exc:
        main(["run", "no_such_preset", *_common(store_dir)])
    assert "no_such_preset" in str(exc.value) and "not found" in str(exc.value)


def test_data_status_json(store_dir, capsys):
    assert main(["data-status", *_common(store_dir), "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["window"]["end"] == "2026-06-30"
    assert payload["universe"]["symbols"] == 5
    assert payload["price"]["lxg_daily"] == 4


def test_pit(store_dir, tmp_path, capsys):
    out = tmp_path / "sched.csv"
    assert main(["pit", "momentum_trend", *_common(store_dir),
                 "--start", "2026-03-01", "--end", "2026-06-30",
                 "--freq", "M", "--out", str(out)]) == 0
    frame = pd.read_csv(out)
    assert list(frame.columns) == ["date", "symbol"]
    assert frame["date"].notna().all()


def test_validate(store_dir, capsys):
    assert main(["validate", "momentum_trend", *_common(store_dir),
                 "--start", "2026-03-01", "--end", "2026-06-30",
                 "--horizons", "5"]) == 0
    out = capsys.readouterr().out
    assert "5d" in out and "spread" in out
