"""Daily snapshot archive: what ``--save-history`` writes and ``qf-screen
history`` reads.

``qf-screen run momentum_trend --save-history`` appends one session's
ranked members to a plain CSV per preset under
``$QF_SCREEN_HISTORY_DIR`` (default
``~/.local/share/qf_screen/history/``)::

    asof,symbol,rank,score
    2026-11-24,SH600519,1,0.9831
    2026-11-24,SZ000858,2,0.9512
    2026-11-25,SH600519,1,0.9910

Writing is idempotent per ``(asof, preset)``: a second run of the same
session replaces that session's rows (a re-run with another ``--top`` or
a ``--live`` row wins), so a snapshot is never doubled.  Nothing is
deleted — the archive only grows by a session a day (some 3 KB per
session for a 30-name preset).

``qf-screen history PRESET [--days N]`` turns the archive into
persistence: for every CURRENT member, ``streak`` is how many consecutive
archived sessions it has been in the list (ending today), ``present`` how
many of the last ``N`` sessions it was in at all.  A "session" here is an
archived one: the count is over the days the screen was actually run, so
a day skipped is not a broken streak.  Names that left inside the window
are listed too, with the run they had before they left.
"""
from __future__ import annotations

import hashlib
import logging
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional, Sequence

import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)

__all__ = [
    "COLUMNS", "HISTORY_ENV", "HistoryError", "SnapshotView", "history_dir", "history_file",
    "history_view", "load_history", "save_snapshot",
]

#: environment variable naming the archive directory
HISTORY_ENV = "QF_SCREEN_HISTORY_DIR"
#: the archive's columns
COLUMNS = ["asof", "symbol", "rank", "score"]
_UNSAFE = re.compile(r"[^\w.-]+", re.UNICODE)


class HistoryError(ValueError):
    """An archive that cannot be read or written."""


def history_dir(explicit=None) -> Path:
    """Where snapshots are archived: ``DIR`` when given, else
    ``$QF_SCREEN_HISTORY_DIR``, else ``$XDG_DATA_HOME/qf_screen/history/``
    (default ``~/.local/share/qf_screen/history/``)."""
    raw = str(explicit).strip() if explicit else ""
    if not raw:
        raw = (os.environ.get(HISTORY_ENV) or "").strip()
    if raw:
        return Path(os.path.expanduser(raw))
    data = os.environ.get("XDG_DATA_HOME") or os.path.expanduser("~/.local/share")
    return Path(data) / "qf_screen" / "history"


def history_file(directory, preset: str) -> Path:
    """The archive file of a preset: one CSV per preset, named after it.
    A name the filesystem cannot take literally (spaces, ``/`` …) is
    sanitised and given a short hash so two such names never share a
    file."""
    name = str(preset)
    safe = _UNSAFE.sub("_", name).strip("._") or "preset"
    if safe != name:
        safe += "-" + hashlib.sha1(name.encode("utf-8")).hexdigest()[:8]
    return history_dir(directory) / f"{safe}.csv"


def _empty() -> pd.DataFrame:
    return pd.DataFrame({column: pd.Series(dtype=object) for column in COLUMNS})


def _date(value) -> str:
    return str(pd.Timestamp(value).date())


def load_history(directory, preset: str) -> pd.DataFrame:
    """A preset's archive: ``asof, symbol, rank, score`` sorted by session
    and rank — empty when nothing was ever archived for it."""
    path = history_file(directory, preset)      # ``directory=None``: the default
    if not path.exists():
        return _empty()
    try:
        frame = pd.read_csv(path, dtype={"asof": str, "symbol": str})
    except (OSError, ValueError) as exc:
        raise HistoryError(f"{path}: unreadable archive ({exc})") from exc
    if list(frame.columns) != COLUMNS or len(frame) == 0:
        raise HistoryError(f"{path}: not a qf-screen archive (expected the columns "
                           f"{', '.join(COLUMNS)})")
    frame["rank"] = pd.to_numeric(frame["rank"], errors="coerce")
    frame["score"] = pd.to_numeric(frame["score"], errors="coerce")
    if frame["rank"].isna().any():
        raise HistoryError(f"{path}: a row without a rank")
    return frame.sort_values(["asof", "rank"], kind="stable").reset_index(drop=True)


def save_snapshot(directory, preset: str, asof, members: Sequence[str],
                  scores: Optional[Sequence[float]] = None) -> Dict[str, object]:
    """Append one session's ranked members to the preset's archive.

    ``members`` is in rank order (first = rank 1); ``scores`` the matching
    composite scores (missing scores are left blank).  A snapshot of the
    same ``(asof, preset)`` already in the archive is replaced.  Returns
    ``{"file", "rows", "replaced", "sessions"}``; the file is swapped in
    atomically, so a run killed mid-write leaves the previous archive.
    """
    date = _date(asof)
    path = history_file(directory, preset)
    symbols = [str(symbol) for symbol in members]
    if len(set(symbols)) != len(symbols):
        raise HistoryError(f"{preset}: a member appears twice in the snapshot")
    before = load_history(directory, preset)
    same = before["asof"] == date
    values = np.full(len(symbols), np.nan)
    if scores is not None:
        values[: len(symbols)] = [float(score) for score in list(scores)[: len(symbols)]]
    fresh = pd.DataFrame({"asof": date, "symbol": symbols,
                          "rank": np.arange(1, len(symbols) + 1), "score": values})
    kept = before[~same]                       # every OTHER session, as archived
    out = pd.concat([kept, fresh], ignore_index=True) if len(kept) else fresh
    out = out.sort_values(["asof", "rank"], kind="stable").reset_index(drop=True)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    out.to_csv(tmp, index=False)
    os.replace(tmp, path)
    return {"file": path, "rows": len(symbols), "replaced": int(same.sum()),
            "sessions": int(out["asof"].nunique())}


def _runs_ending(mask: np.ndarray) -> np.ndarray:
    """For each row, the length of the run of ``True`` cells that ends at
    each column (``runs[:, j]`` counts consecutive ``True`` up to and
    including ``j``)."""
    runs = np.zeros(mask.shape, dtype=int)
    run = np.zeros(mask.shape[0], dtype=int)
    for column in range(mask.shape[1]):
        run = np.where(mask[:, column], run + 1, 0)
        runs[:, column] = run
    return runs


@dataclass
class SnapshotView:
    """What :func:`history_view` reads out of an archive.

    ``members``: the latest session's list, each with ``streak`` (sessions
    in a row up to and including the latest), ``present`` (sessions in the
    window), ``stayed`` (``present / sessions``), ``first_seen`` (the first
    session of the window it is in) and ``score``.  ``exited``: the names
    that were members inside the window but not at its end, with the run
    they had and the rank they held when they left.  ``sizes`` is how many
    members each session of the window archived.
    """

    preset: str
    asof: str
    sessions: List[str]
    members: pd.DataFrame
    exited: pd.DataFrame
    sizes: Dict[str, int]

    @property
    def new_today(self) -> int:
        return int((self.members["streak"] == 1).sum()) if len(self.members) else 0

    @property
    def caution(self) -> str:
        """Why the counts may overstate change: the archive is only as
        good as the runs that wrote it, and a session archived with a
        smaller ``--top`` makes every name beyond it look new the next day."""
        if not self.sizes:
            return ""
        least, most = min(self.sizes.values()), max(self.sizes.values())
        if least and most > 2 * least:
            return (f"member counts vary across the window ({least} to {most} a session): a "
                    "session archived with a smaller --top makes the names beyond it look "
                    "new the day after")
        return ""


def history_view(frame: pd.DataFrame, preset: str = "", days: int = 30,
                 names: Optional[Dict[str, str]] = None) -> SnapshotView:
    """Persistence over the last ``days`` archived sessions of ``frame``
    (:func:`load_history` output)."""
    if days < 1:
        raise HistoryError(f"--days must be 1 or more, got {days}")
    sessions = sorted(frame["asof"].unique()) if len(frame) else []
    window = sessions[-int(days):]
    latest = window[-1] if window else ""
    columns = ["symbol", "name", "rank", "score", "streak", "present", "stayed", "first_seen"]
    gone = ["symbol", "name", "last_seen", "run", "last_rank"]
    if not window:
        return SnapshotView(preset, "", [], pd.DataFrame(columns=columns),
                            pd.DataFrame(columns=gone), {})
    inside = frame[frame["asof"].isin(window)]
    table = inside.pivot_table(index="symbol", columns="asof", values="rank", aggfunc="first")
    table = table.reindex(columns=window)
    present = table.notna().to_numpy()
    runs = _runs_ending(present)
    current = inside[inside["asof"] == latest].sort_values("rank")
    scores = dict(zip(current["symbol"], current["score"]))
    ranks = dict(zip(current["symbol"], current["rank"]))
    position = table.index.get_indexer(current["symbol"])
    first = [window[int(np.argmax(present[at]))] for at in position]
    rows = [{"symbol": str(symbol), "name": (names or {}).get(str(symbol), ""),
             "rank": int(ranks[symbol]), "score": float(scores[symbol]),
             "streak": int(runs[at, -1]), "present": int(present[at].sum()),
             "stayed": float(present[at].mean()), "first_seen": seen}
            for symbol, at, seen in zip(current["symbol"], position, first)]
    left = []
    for at, symbol in enumerate(table.index):
        if present[at, -1] or not present[at].any():
            continue
        last = int(np.flatnonzero(present[at])[-1])
        was = table.iloc[at, last]
        left.append({"symbol": str(symbol), "name": (names or {}).get(str(symbol), ""),
                     "last_seen": window[last], "run": int(runs[at, last]),
                     "last_rank": int(was) if pd.notna(was) else 0})
    left.sort(key=lambda row: (row["last_seen"], row["run"]), reverse=True)
    sizes = {session: int(count) for session, count in inside.groupby("asof").size().items()}
    return SnapshotView(preset, latest, window, pd.DataFrame(rows, columns=columns),
                        pd.DataFrame(left, columns=gone), sizes)
