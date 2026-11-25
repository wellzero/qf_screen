"""Safe parallel map for library code.

``spawn``-family start methods (``spawn`` and ``forkserver``) re-import
the CALLER's ``__main__`` in every worker before running the task:

* ``python script.py`` without an ``if __name__ == "__main__"`` guard —
  the worker re-executes the script, which builds the store again, which
  spawns workers ... (this really happened during development);
* ``python -`` / piped input — ``__main__.__file__`` is ``<stdin>``, the
  worker's ``runpy`` fails, workers crash-loop and ``pool.map`` hangs;
* a REPL / Jupyter kernel has no ``__main__.__file__`` at all and is safe.

:func:`map_workers` inspects the interpreter and picks the backend
accordingly: a forkserver process pool when the caller's ``__main__`` is
re-importable without side effects, a thread pool otherwise (the GIL makes
threads slower for pure-Python merges, but they are correct everywhere).

``fork`` is never used: the parent runs BLAS/NumExpr thread pools by then,
and a forked child that touches numpy before ``exec`` can deadlock.
"""
from __future__ import annotations

import logging
import os
import sys
from concurrent.futures import ThreadPoolExecutor
from typing import Callable, Optional, Sequence

logger = logging.getLogger(__name__)


def main_is_pool_safe() -> bool:
    """Whether re-importing ``__main__`` (what a spawn-family worker does)
    is side-effect-free in this interpreter."""
    main = sys.modules.get("__main__")
    path = getattr(main, "__file__", None)
    if path is None:
        return True                      # interactive / kernel: not re-imported
    if not os.path.isfile(path):
        return False                     # <stdin> / piped / deleted: unsafe
    try:
        source = open(path, encoding="utf-8", errors="ignore").read()
    except OSError:
        return False
    return "__name__" in source and "__main__" in source


def map_workers(fn: Callable, items: Sequence, workers: int = 8,
                chunksize: int = 16, initializer: Optional[Callable] = None,
                initargs: tuple = ()) -> list:
    """``fn`` over ``items``, parallel when safely possible.

    Uses a forkserver process pool (with ``initializer``) when
    :func:`main_is_pool_safe` and the platform has forkserver; otherwise a
    thread pool (``initializer`` runs inline once first); ``workers <= 1``
    or an empty ``items`` maps sequentially.
    """
    items = list(items)
    if not items or workers <= 1:
        if initializer is not None:
            initializer(*initargs)
        return [fn(item) for item in items]

    if main_is_pool_safe():
        import multiprocessing
        if "forkserver" in multiprocessing.get_all_start_methods():
            ctx = multiprocessing.get_context("forkserver")
            with ctx.Pool(min(workers, multiprocessing.cpu_count() or 1),
                          initializer=initializer, initargs=initargs) as pool:
                return pool.map(fn, items, chunksize=chunksize)
    if initializer is not None:
        initializer(*initargs)
    with ThreadPoolExecutor(max_workers=workers) as pool:
        return list(pool.map(fn, items))
