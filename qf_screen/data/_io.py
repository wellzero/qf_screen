"""Tolerant, fast CSV helpers shared by the lxg / ak readers.

The store is written by several downloaders over many years, so nothing here
assumes a file is present, parseable or laid out the way the latest writer
does it:

* every read swallows ``OSError`` / ``ValueError`` (which covers pandas'
  ``EmptyDataError`` -- a BOM-only placeholder file, 728 ``pledge.csv`` at
  the time of writing) and returns ``None``;
* the UTF-8 BOM lxg files carry is stripped from the bytes before parsing;
* :func:`read_bytes` reads only the last rows of a file, which -- with
  :func:`read_csv_typed` -- is what makes the daily snapshot path fast: a
  600-row screen window needs ~100 KB of a 650 KB ``ak/basic.csv``, not all
  of it.

:func:`read_csv_typed` parses straight into typed numpy arrays (no
intermediate DataFrame).  It uses pyarrow's CSV parser when that is
installed (~10x faster than ``pd.read_csv`` on these files) and falls back
to pandas otherwise -- and for the rare file pyarrow rejects (a numeric
column carrying text).  Both backends return the same thing: missing
values as NaN / ``""``, and a column the file simply does not have is
absent from the result.
"""
from __future__ import annotations

import io
from pathlib import Path
from typing import Dict, List, NamedTuple, Optional, Sequence

import numpy as np
import pandas as pd

_READ_ERRORS = (OSError, ValueError, UnicodeDecodeError)
_BOM = b"\xef\xbb\xbf"

#: per-row byte estimate used to size the first tail read; the estimate is
#: corrected from the line count that actually came back
_ROW_BYTES = 160.0

#: internal date unit: calendar days
DAY = "datetime64[D]"


# ------------------------------------------------------------------ bytes
def read_bytes(path, tail_rows: Optional[int] = None) -> Optional[bytes]:
    """A CSV file as BOM-free bytes (header included), or ``None``.

    ``tail_rows=None`` returns the whole file; otherwise EXACTLY the last
    ``tail_rows`` data rows -- however wide the rows are -- so that two
    files with different row widths still yield the same set of dates.
    """
    if path is None:
        return None
    try:
        path = Path(path)
        size = path.stat().st_size
        with path.open("rb") as fh:
            header = fh.readline()
            if tail_rows is None:
                body = fh.read()
            else:
                nbytes = min(size, int(tail_rows * _ROW_BYTES) + 4096)
                while True:
                    if nbytes < size:
                        fh.seek(size - nbytes)
                        body = fh.read()
                        cut = body.find(b"\n")
                        body = body[cut + 1:] if cut >= 0 else b""
                    else:
                        body = fh.read()
                    rows = _count_rows(body)
                    if rows >= tail_rows or nbytes >= size:
                        break
                    wider = tail_rows / max(rows, 1)
                    nbytes = min(size, int(nbytes * wider * 1.15) + 4096)
                if _count_rows(body) > tail_rows:
                    body = _skip_lines(body, _count_rows(body) - tail_rows)
    except _READ_ERRORS:
        return None
    if header.startswith(_BOM):
        header = header[3:]
    data = header + body
    return data if data.strip() else None


def _count_rows(body: bytes) -> int:
    if not body:
        return 0
    return body.count(b"\n") + (0 if body.endswith(b"\n") else 1)


def _skip_lines(body: bytes, count: int) -> bytes:
    pos = 0
    for _ in range(count):
        pos = body.find(b"\n", pos)
        if pos < 0:
            return b""
        pos += 1
    return body[pos:]


# ------------------------------------------------------------------ typed
class ColumnSpec(NamedTuple):
    """One requested column: ``name`` as written in the file, ``kind`` in
    ``float`` / ``date`` / ``str``, and the ``alias`` it is returned under
    (defaults to ``name``)."""
    name: str
    kind: str = "float"
    alias: Optional[str] = None

    @property
    def key(self) -> str:
        return self.name if self.alias is None else self.alias


def _header_names(header: bytes) -> List[str]:
    text = header.decode("utf-8", errors="replace").rstrip("\r\n")
    return [name.strip().strip('"') for name in text.split(",")]


def read_csv_typed(data: Optional[bytes], specs: Sequence[ColumnSpec]) -> Dict[str, np.ndarray]:
    """Parse CSV bytes into typed arrays ``{alias: ndarray}``.

    ``float`` columns come back as ``float64`` with NaN for blanks or
    unparseable text, ``date`` columns as ``datetime64[D]`` with NaT, ``str``
    columns as an object array of ``str`` (missing -> ``""``).  A requested
    column the file does not have is simply absent from the result.
    """
    if not data:
        return {}
    header, _, body = data.partition(b"\n")
    names = _header_names(header)
    wanted = [spec for spec in specs if spec.name in names]
    if not wanted or not body.strip():
        return {}
    try:
        import pyarrow  # noqa: F401
    except ImportError:
        return _read_pandas(header + b"\n" + body, wanted)
    try:
        return _read_pyarrow(header + b"\n" + body, wanted)
    except ValueError:
        # pyarrow's ArrowInvalid (a ValueError) on a column that will not
        # parse as the requested type -- text in a numeric column.  pandas
        # coerces it.  Anything else (a TypeError from a bad option ...) is
        # a bug and must NOT be masked by a silent slow fallback.
        return _read_pandas(header + b"\n" + body, wanted)


def _finish(spec: ColumnSpec, values) -> np.ndarray:
    if spec.kind == "float":
        return np.asarray(values, dtype=float)
    if spec.kind == "str":
        return np.array([("" if v is None else str(v)) for v in values], dtype=object)
    # date: plain YYYY-MM-DD via numpy, anything else (tz-aware ISO) via pandas
    text = [("" if v is None else str(v)) for v in values]
    plain = all(len(v) == 10 for v in text if v)
    if plain:
        with np.errstate(invalid="ignore"):
            return np.asarray(text or [""], dtype=DAY)
    return to_dates(pd.Series(text, dtype=object)).to_numpy().astype(DAY)


def _read_pyarrow(data: bytes, specs: Sequence[ColumnSpec]) -> Dict[str, np.ndarray]:
    import pyarrow as pa
    from pyarrow import csv as pacsv

    table = pacsv.read_csv(
        io.BytesIO(data),
        read_options=pacsv.ReadOptions(use_threads=False),
        parse_options=pacsv.ParseOptions(newlines_in_values=True),
        convert_options=pacsv.ConvertOptions(
            include_columns=[spec.name for spec in specs],
            column_types={spec.name: (pa.float64() if spec.kind == "float" else pa.string())
                          for spec in specs},
        ),
    )
    out: Dict[str, np.ndarray] = {}
    for spec in specs:
        column = table.column(spec.name)
        if spec.kind == "float":
            out[spec.key] = column.to_numpy(zero_copy_only=False).astype(float)
        else:
            out[spec.key] = _finish(spec, column.to_pylist())
    return out


def _read_pandas(data: bytes, specs: Sequence[ColumnSpec]) -> Dict[str, np.ndarray]:
    frame = pd.read_csv(io.BytesIO(data), encoding="utf-8",
                        usecols=[spec.name for spec in specs])
    out: Dict[str, np.ndarray] = {}
    for spec in specs:
        values = frame[spec.name]
        if spec.kind == "float":
            out[spec.key] = pd.to_numeric(values, errors="coerce").to_numpy(dtype=float)
        elif spec.kind == "str":
            out[spec.key] = np.array(["" if pd.isna(v) else str(v) for v in values], dtype=object)
        else:
            out[spec.key] = to_dates(values).to_numpy().astype(DAY)
    return out


class ColumnTable:
    """A dated table: sorted unique ``datetime64[D]`` days plus float
    columns aligned to them (the numpy stand-in for a date-indexed frame).
    Duplicate days keep the LAST row, like ``index.duplicated(keep="last")``.
    """

    __slots__ = ("days", "cols")

    def __init__(self, days: np.ndarray, cols: Dict[str, np.ndarray]):
        self.days = np.asarray(days, dtype=DAY)
        self.cols = cols

    def __len__(self) -> int:
        return len(self.days)

    def __repr__(self) -> str:
        return f"ColumnTable({len(self.days)} rows, cols={sorted(self.cols)})"

    @classmethod
    def from_typed(cls, arrays: Dict[str, np.ndarray], specs: Sequence[ColumnSpec],
                   date_key: str = "date") -> Optional["ColumnTable"]:
        days = arrays.get(date_key)
        if days is None:
            return None
        days = np.asarray(days, dtype=DAY)
        keep = ~pd.isna(days)
        if not keep.any():
            return None
        order = np.argsort(days[keep], kind="stable")
        sorted_days = days[keep][order]
        unique = np.empty(len(sorted_days), dtype=bool)
        unique[-1] = True
        unique[:-1] = sorted_days[1:] != sorted_days[:-1]
        cols = {}
        for spec in specs:
            if spec.key == date_key or spec.key not in arrays:
                continue
            values = arrays[spec.key]
            cols[spec.key] = np.asarray(values[keep], dtype=float)[order][unique]
        return cls(sorted_days[unique], cols)

    def get(self, name: str) -> Optional[np.ndarray]:
        return self.cols.get(name)

    def clip(self, start=None, end=None) -> "ColumnTable":
        """Rows with ``start <= day <= end`` (Timestamps or ``datetime64``)."""
        lo = np.datetime64("1677-09-22", "D") if start is None \
            else np.datetime64(pd.Timestamp(start).date(), "D")
        hi = np.datetime64("2262-04-11", "D") if end is None \
            else np.datetime64(pd.Timestamp(end).date(), "D")
        keep = (self.days >= lo) & (self.days <= hi)
        if keep.all():
            return self
        return ColumnTable(self.days[keep], {k: v[keep] for k, v in self.cols.items()})

    def to_frame(self) -> pd.DataFrame:
        index = pd.DatetimeIndex(pd.to_datetime(self.days), name="date")
        return pd.DataFrame({k: v for k, v in self.cols.items()}, index=index)


# ------------------------------------------------------- pandas utilities
def read_csv(path, **kw) -> Optional[pd.DataFrame]:
    """``pd.read_csv`` that returns ``None`` for a missing, empty, BOM-only
    or otherwise unreadable file instead of raising."""
    if path is None:
        return None
    try:
        df = pd.read_csv(path, encoding="utf-8-sig", **kw)
    except _READ_ERRORS:
        return None
    if df is None or df.empty:
        return None
    return df


def to_dates(values) -> pd.Series:
    """Parse a column of timestamps into tz-naive normalized dates.

    Accepts everything the store contains: ``2002-04-17``,
    ``2002-07-25T00:00:00+08:00`` and ``NaN``.  Plain dates take the fast
    strict-format path; anything that fails it goes through the general
    ISO-8601 path, where aware values are converted to Asia/Shanghai before
    the zone is dropped and naive ones are read as ``+08:00`` (lxg's zone)
    -- ``pd.to_datetime`` treats naive input as UTC, so the +8h shift
    cancels out for the plain-date case.
    """
    series = values if isinstance(values, pd.Series) else pd.Series(values)
    parsed = pd.to_datetime(series, errors="coerce", format="%Y-%m-%d")
    if parsed.isna()[series.notna()].any():
        parsed = pd.to_datetime(series, errors="coerce", utc=True, format="ISO8601")
        parsed = parsed.dt.tz_convert("Asia/Shanghai").dt.tz_localize(None)
    return parsed.dt.normalize()


def numeric(df: pd.DataFrame, columns=None) -> pd.DataFrame:
    """Coerce ``columns`` (default: all) of ``df`` to float, bad text -> NaN.

    Tries one vectorized ``astype(float)`` over all non-float columns and
    falls back to a per-column ``to_numeric`` only when some column carries
    non-numeric text (``"-"`` placeholders and the like).
    """
    cols = list(df.columns) if columns is None else [c for c in columns if c in df.columns]
    pending = [c for c in cols if not pd.api.types.is_float_dtype(df[c])]
    if not pending:
        return df
    out = df.copy()
    try:
        out[pending] = out[pending].astype(float)
    except (ValueError, TypeError):
        for col in pending:
            out[col] = pd.to_numeric(out[col], errors="coerce").astype(float)
    return out


def read_text(path) -> Optional[str]:
    """Whole text file (BOM stripped) or ``None``."""
    if path is None or not Path(path).exists():
        return None
    try:
        text = Path(path).read_text(encoding="utf-8-sig")
    except (OSError, UnicodeDecodeError):
        return None
    return text or None


__all__ = [
    "DAY", "ColumnSpec", "ColumnTable", "read_bytes", "read_csv", "read_csv_typed",
    "read_text", "to_dates", "numeric",
]
