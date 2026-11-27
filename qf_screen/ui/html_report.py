"""Self-contained HTML reports: ``--format html`` for ``report``,
``validate`` and ``compare``.

One file and no dependencies: the styles, the charts (inline SVG) and the
interaction (a page-local script: column sorting, a symbol / name /
industry filter, a crosshair + tooltip on every line chart, a tooltip on
every bar) travel inside the document.  Nothing is fetched — no CDN, no
web font, no image — so a report can be mailed, parked on a share or
opened offline and looks and behaves the same.

Rendering only: every number comes from :mod:`qf_screen.engine`; the
callers hand in results and this module hands back markup.

**How the charts are drawn** — one fixed method, so two reports never
disagree about what a colour means:

* *Cumulative value* and *drawdown* are multi-series line charts.  The
  screens are the subject and take the categorical slots in a FIXED order
  (``series-1`` is the first preset named, whatever it ranks); the
  reference series — the equal-weight universe and the indexes — are
  context and are drawn in greys, each with its own dash.  A net-of-cost
  path is the same entity as its gross twin: it keeps the twin's hue and
  differs by its dash.  No ninth colour is ever generated: a comparison
  of more than eight presets charts the first eight and says so.
* *Industry concentration* is a ranked horizontal bar chart in ONE hue:
  industries are nominal categories, so no bar is coloured by its size
  (its length already says it).  Names without a classification sit last,
  in grey.
* Every line chart has a crosshair that snaps to the nearest session and
  a readout listing every series there; every bar is its own hit target.
  Tooltips enhance and never gate: each chart has a table twin, and the
  page's tables sort by any column and filter by the search box.
* The palette is a colour-blind-safe categorical set in a validated
  order (re-run for this report: worst adjacent pair ΔE 11.8 under
  protanopia on the light surface, 10.1 under deuteranopia on the dark
  one).  Four light-mode slots sit below 3:1 on white, which is legal
  only with a relief channel — hence the direct end-labels (up to four
  series, leader lines when they would collide) and the table twin.
  Text never wears a series colour: identity comes from the coloured
  line key beside it.  Dark mode is a selected set of steps for the dark
  surface, not an inversion; print and forced-colours fall back to the
  light tokens with line patterns doing the work of hue (the "Line
  patterns" button turns them on at any time).  A standalone page
  follows the OS for the surface; a host that embeds one in a frame
  chooses it with ``data-theme="light"`` / ``"dark"`` on the root.
"""
from __future__ import annotations

import html
import json
import math
import re
from dataclasses import dataclass
from typing import Callable, Dict, List, Optional, Sequence, Tuple

import numpy as np
import pandas as pd

from qf_screen.ui.render import cell_text

__all__ = [
    "REF_TONES", "SERIES_TONES", "Cell", "Series", "bar_chart", "compare_html",
    "daily_report_html", "data_table", "line_chart", "rank_pill", "stat_tiles", "symbol",
    "validate_html",
]

#: the categorical slots, in their fixed order; a ninth series is never a
#: generated colour (the callers cap a chart at this many screens)
SERIES_TONES: Tuple[str, ...] = tuple(f"series-{i}" for i in range(1, 9))
#: the reference (context) series: greys told apart by their dash as well
REF_TONES: Tuple[str, ...] = ("ref-1", "ref-2", "ref-3")

#: how many series a line chart direct-labels at its right edge
_DIRECT_LABELS = 4
#: the tag a name without an industry classification carries (engine.UNCATEGORIZED)
UNCLASSIFIED = "\u672a\u5206\u7c7b"
#: the bucket the tail of a long industry list folds into
OTHER = "\u5176\u4ed6"


def _esc(value) -> str:
    return html.escape("" if value is None else str(value), quote=True)


def _json(payload) -> str:
    """JSON that is safe inside a ``<script>``: no ``</script>`` can be
    spelled in it."""
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"),
                      default=str).replace("<", "\\u003c").replace("\u2028", "\\u2028")


# ------------------------------------------------------------- the palette
#: colour tokens as CSS custom properties, light and dark (the validated
#: categorical palette, its chart chrome, and the text / tint pairs the
#: badges use — each checked for contrast on its own surface)
_TOKENS: Dict[str, Dict[str, str]] = {
    "light": {
        "page": "#f9f9f9", "surface": "#ffffff", "ink": "#191919", "ink-2": "#666666",
        "ink-3": "#8c8c8c", "grid": "#e7e7e7", "axis": "#c2c2c2",
        "border": "rgba(11,11,11,0.10)", "link": "#1757ac",
        "series-1": "#257de0", "series-2": "#f47230", "series-3": "#00b894",
        "series-4": "#f2a600", "series-5": "#ee7fac", "series-6": "#008a17",
        "series-7": "#6a4ce2", "series-8": "#ec4647",
        "ref-1": "#666666", "ref-2": "#7a7a7a", "ref-3": "#8c8c8c",
        "good-text": "#006c10", "good-tint": "#e3f5e6",
        "bad-text": "#b3261e", "bad-tint": "#fdecea",
        "pill-bg": "rgba(11,11,11,0.06)",
    },
    "dark": {
        "page": "#0b0b0b", "surface": "#191919", "ink": "#efefef", "ink-2": "#9c9c9c",
        "ink-3": "#8f8f8f", "grid": "#303030", "axis": "#474747",
        "border": "rgba(255,255,255,0.10)", "link": "#6aabf1",
        "series-1": "#338ced", "series-2": "#e76624", "series-3": "#00a888",
        "series-4": "#d28e05", "series-5": "#e24d8b", "series-6": "#0aac23",
        "series-7": "#9887ef", "series-8": "#eb6263",
        "ref-1": "#bdbdbd", "ref-2": "#a0a0a0", "ref-3": "#8f8f8f",
        "good-text": "#53c556", "good-tint": "#13291a",
        "bad-text": "#ff8a8a", "bad-tint": "#3a1a1a",
        "pill-bg": "rgba(255,255,255,0.10)",
    },
}


def _tokens(mode: str) -> str:
    return ";".join(f"--{key}:{value}" for key, value in _TOKENS[mode].items())


def _tone_rules() -> str:
    """A tone class sets the stroke (lines, keys) and, on area and bar
    marks, the fill — everything reads the same custom property."""
    rules = []
    for index in range(1, len(SERIES_TONES) + 1):
        rules.append(f".series-{index}{{stroke:var(--series-{index})}}")
        rules.append(f"svg .area.series-{index}{{fill:var(--series-{index})}}")
        rules.append(f"svg .bar.series-{index}{{fill:var(--series-{index})}}")
    for index in range(1, len(REF_TONES) + 1):
        rules.append(f".ref-{index}{{stroke:var(--ref-{index})}}")
        rules.append(f"svg .bar.ref-{index}{{fill:var(--ref-{index})}}")
    return "".join(rules)


_CSS = """
/* The surface follows the OS; a host that embeds a page in a frame (the
   qf_tools website app) picks it instead with data-theme="light"|"dark" on
   the root.  :where() keeps the media rule at :root's own specificity, so
   the attribute rules -- later and more specific -- always win. */
:root{color-scheme:light;__LIGHT__}
@media (prefers-color-scheme:dark){:root:where(:not([data-theme="light"])){color-scheme:dark;__DARK__}}
:root[data-theme="dark"]{color-scheme:dark;__DARK__}
@media print{:root{color-scheme:light;__LIGHT__}}
*{box-sizing:border-box}
html{background:var(--page)}
body{margin:0;background:var(--page);color:var(--ink);font:14px/1.5 system-ui,-apple-system,
"Segoe UI","PingFang SC","Hiragino Sans GB","Microsoft YaHei","Noto Sans CJK SC",sans-serif}
.wrap{max-width:1180px;margin:0 auto;padding:0 24px}
a{color:var(--link)}
h1{margin:4px 0 6px;font-size:24px;line-height:1.25}
h2{margin:0 0 4px;font-size:18px}
h2 code{font:inherit}
p{margin:0}
.eyebrow{font-size:12px;letter-spacing:.08em;text-transform:uppercase;color:var(--ink-2)}
.meta{color:var(--ink-2)}
header.top{background:var(--surface);border-bottom:1px solid var(--border);margin-bottom:22px}
header.top .wrap{padding-top:22px;padding-bottom:16px}
.tools{display:flex;flex-wrap:wrap;align-items:center;gap:10px 16px;margin-top:14px}
.search{display:flex;align-items:center;gap:8px;color:var(--ink-2);font-size:13px}
.search input{font:inherit;color:var(--ink);background:var(--page);border:1px solid var(--axis);
border-radius:6px;padding:6px 10px;min-width:250px}
button{font:inherit;color:var(--ink);background:var(--surface);border:1px solid var(--axis);
border-radius:6px;padding:5px 10px;cursor:pointer}
button:focus-visible,input:focus-visible,summary:focus-visible{outline:2px solid var(--link);
outline-offset:1px}
button[aria-pressed="true"]{background:var(--pill-bg)}
nav.toc{display:flex;flex-wrap:wrap;gap:4px 18px;margin:0 0 18px;font-size:13px}
.tiles{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:12px;
margin:0 0 20px}
.tile{background:var(--surface);border:1px solid var(--border);border-radius:10px;
padding:12px 14px}
.tile-label{font-size:13px;color:var(--ink-2)}
.tile-value{font-size:26px;font-weight:600;line-height:1.25}
.tile-note{font-size:12px;color:var(--ink-2)}
.up{color:var(--good-text)} .down{color:var(--bad-text)}
section{background:var(--surface);border:1px solid var(--border);border-radius:10px;
padding:18px 20px;margin:0 0 20px}
section.quiet h2{color:var(--ink-3)}
.lede{margin:0 0 12px;color:var(--ink-2)}
.lede code{color:var(--ink)}
h3{margin:18px 0 6px;font-size:12px;letter-spacing:.06em;text-transform:uppercase;
color:var(--ink-2)}
.grid2{display:grid;grid-template-columns:repeat(auto-fit,minmax(320px,1fr));gap:4px 24px}
.table-wrap{overflow-x:auto}
table.data{border-collapse:collapse;width:100%;font-size:13px;
font-variant-numeric:tabular-nums}
table.data caption{caption-side:top;text-align:left;color:var(--ink-2);padding:0 0 8px}
table.data caption .count{margin-left:8px}
table.data th,table.data td{padding:6px 10px;text-align:left;white-space:nowrap;
border-bottom:1px solid var(--grid)}
table.data th{color:var(--ink-2);font-weight:600}
table.data tbody tr:hover{background:var(--pill-bg)}
table.data tr[hidden]{display:none}
table.data .num{text-align:right}
table.data td.muted,table.data .muted{color:var(--ink-2)}
table.data tr.none td{text-align:center;color:var(--ink-2);padding:14px}
th button.sort{all:unset;cursor:pointer;font:inherit;color:inherit}
th button.sort::after{content:"\\2195";margin-left:4px;opacity:.35}
th[aria-sort="ascending"] button.sort::after{content:"\\2191";opacity:1}
th[aria-sort="descending"] button.sort::after{content:"\\2193";opacity:1}
th button.sort:focus-visible{outline:2px solid var(--link);outline-offset:2px}
details.chart-data{margin-top:6px}
details.chart-data summary{color:var(--link);font-size:13px;cursor:pointer}
details.chart-data[open] summary{margin-bottom:8px}
.badge{display:inline-block;padding:0 7px;border-radius:999px;font-size:12px;font-weight:600;
line-height:18px;white-space:nowrap}
.badge.new{color:var(--good-text);background:var(--good-tint)}
.badge.out{color:var(--bad-text);background:var(--bad-tint)}
.pill{display:inline-block;padding:0 7px;border-radius:999px;font-size:12px;line-height:18px;
background:var(--pill-bg);white-space:nowrap}
.changes{list-style:none;margin:0;padding:0;font-size:13px}
.changes li{display:flex;align-items:baseline;gap:8px;padding:4px 0;
border-bottom:1px solid var(--grid)}
.changes .who{white-space:nowrap}
.changes .where{color:var(--ink-2);overflow:hidden;text-overflow:ellipsis}
.chart{margin:0}
.chart figcaption{font-size:15px;font-weight:600}
.chart .sub{margin:2px 0 8px;font-size:13px;color:var(--ink-2)}
.chart-body{position:relative;overflow-x:auto}
svg.plot{display:block}
svg.plot text{font:11px system-ui,-apple-system,"Segoe UI","PingFang SC","Microsoft YaHei",
"Noto Sans CJK SC",sans-serif;fill:var(--ink-2);font-variant-numeric:tabular-nums}
svg.plot .gridline{stroke:var(--grid);stroke-width:1}
svg.plot .axis,svg.plot .baseline{stroke:var(--axis);stroke-width:1}
svg.plot .line{fill:none;stroke-width:2;stroke-linejoin:round;stroke-linecap:round}
svg.plot .area{stroke:none;fill-opacity:.1}
svg.plot .dot{stroke:var(--surface);stroke-width:2}
svg.plot .leader{stroke-width:1;stroke-opacity:.7}
svg.plot .key{stroke-width:2;stroke-linecap:round}
svg.plot .hit{fill:transparent;outline:none}
svg.plot .hit:focus-visible{stroke:var(--link)}
svg.plot .end-label{fill:var(--ink)}
svg.plot .end-label .name{fill:var(--ink-2)}
svg.plot .off{opacity:.2}
svg.plot g.cursor[hidden]{display:none}
svg.plot g.bar-row:hover .bar,svg.plot g.bar-row:focus-within .bar{filter:brightness(1.12)}
svg .bar{stroke:none}
__TONES__
.net{stroke-dasharray:2 2}
.ref-2{stroke-dasharray:8 4}
.ref-3{stroke-dasharray:4 3}
__PATTERNS__
.legend{display:flex;flex-wrap:wrap;gap:2px 4px;list-style:none;margin:4px 0 8px;padding:0;
font-size:13px}
.legend button{display:inline-flex;align-items:center;gap:6px;border-color:transparent;
background:none;padding:3px 6px}
.legend button:hover{background:var(--pill-bg)}
.legend li.isolate button{background:var(--pill-bg);border-color:var(--axis)}
.legend li.off{opacity:.35}
.legend svg,.tip svg{flex:none}
.tip{position:absolute;z-index:5;pointer-events:none;min-width:120px;padding:6px 9px;
font-size:12px;background:var(--surface);border:1px solid var(--border);border-radius:6px;
box-shadow:0 2px 8px rgba(0,0,0,.14)}
.tip[hidden]{display:none}
.tip .tip-date{color:var(--ink-2)}
.tip .row{display:flex;align-items:center;gap:6px;white-space:nowrap}
.tip .val{font-weight:600}
.tip .who{color:var(--ink-2)}
.notes{margin:0;padding-left:18px;font-size:13px;color:var(--ink-2)}
.notes li{margin:2px 0}
.foot{color:var(--ink-2);font-size:12px;padding-top:4px;padding-bottom:40px}
@media print{header.top,nav.toc,.tools,.tip{display:none}section{break-inside:avoid}}
""".replace("__LIGHT__", _tokens("light")).replace("__DARK__", _tokens("dark"))
#: one stroke pattern per categorical slot from the second on (the first
#: stays solid): what tells the lines apart when hue cannot -- on request,
#: in print, under forced colours
_DASHES = ("8 3", "2 3", "10 3 2 3", "12 4", "3 2", "10 2", "4 2 1 2")


def _pattern_rules(scope: str = "") -> str:
    """The pattern rules under a scope: a selector prefix goes in front of
    each rule, a ``@media ...{`` opening wraps them all once."""
    rules = [f".dash-{index + 2}{{stroke-dasharray:{pattern}}}"
             for index, pattern in enumerate(_DASHES)]
    if scope.startswith("@media"):
        return scope + "".join(rules) + "}"
    return "".join(scope + rule for rule in rules)


_CSS = (_CSS.replace("__TONES__", _tone_rules())
        .replace("__PATTERNS__", _pattern_rules(".patterns ")
                 + _pattern_rules("@media print{")
                 + _pattern_rules("@media (forced-colors:active){")))


# ------------------------------------------------------------- the script
_SCRIPT = r"""
(function () {
  "use strict";
  function all(nodes) { return Array.prototype.slice.call(nodes); }
  function listed(row) { return !row.classList.contains("none") && !row.classList.contains("fill"); }

  /* ---- sorting: a header button sorts its column; the header that carries
     data-restore puts the rows back in their documented order */
  function sortKey(cell) {
    var raw = cell.getAttribute("data-v");
    if (raw === null) { return { kind: "text", value: cell.textContent.trim() }; }
    var number = Number(raw);
    return raw !== "" && isFinite(number) ? { kind: "number", value: number }
      : { kind: "number", value: Infinity };
  }
  function inOrder(a, b) {
    return Number(a.getAttribute("data-order")) - Number(b.getAttribute("data-order"));
  }
  function mark(table, column, direction) {
    all(table.tHead.rows[0].cells).forEach(function (th) {
      th.setAttribute("aria-sort", th.cellIndex === column
        ? (direction > 0 ? "ascending" : "descending") : "none");
    });
  }
  function reflow(table, rows) {
    var body = table.tBodies[0];
    rows.forEach(function (row) { body.appendChild(row); });
    all(body.rows).filter(function (row) { return !listed(row); })
      .forEach(function (row) { body.appendChild(row); });
  }
  function sortBy(table, column, direction) {
    var rows = all(table.tBodies[0].rows).filter(listed);
    rows.sort(function (a, b) {
      var ka = sortKey(a.cells[column]), kb = sortKey(b.cells[column]), order = 0;
      if (ka.kind === kb.kind) {
        order = ka.kind === "number" ? (ka.value === kb.value ? 0 : ka.value - kb.value)
          : ka.value.localeCompare(kb.value, undefined, { numeric: true });
      } else { order = ka.kind === "text" ? 1 : -1; }
      return direction * (order || inOrder(a, b));
    });
    reflow(table, rows);
    mark(table, column, direction);
  }
  function restore(table) {
    var rows = all(table.tBodies[0].rows).filter(listed);
    rows.sort(inOrder);
    reflow(table, rows);
    mark(table, -1, 1);
  }
  all(document.querySelectorAll("table.sortable")).forEach(function (table) {
    all(table.tHead.querySelectorAll("button.sort")).forEach(function (button) {
      button.addEventListener("click", function () {
        if (button.hasAttribute("data-restore")) { restore(table); return; }
        var column = Number(button.getAttribute("data-col"));
        var rising = button.parentElement.getAttribute("aria-sort") === "ascending";
        sortBy(table, column, rising ? -1 : 1);
      });
    });
  });

  /* ---- the filter: one box, every searchable table on the page */
  function applyFilter() {
    var box = document.getElementById("filter");
    var status = document.getElementById("filter-status");
    if (!box) { return; }
    var query = box.value.trim().toLowerCase(), shown = 0, total = 0;
    all(document.querySelectorAll("table.searchable")).forEach(function (table) {
      var count = table.querySelector("caption .count"), seen = 0, rows = 0;
      all(table.tBodies[0].rows).forEach(function (row) {
        if (!listed(row)) { return; }
        rows += 1;
        var hit = !query || row.textContent.toLowerCase().indexOf(query) >= 0;
        row.hidden = !hit;
        if (hit) { seen += 1; }
      });
      shown += seen;
      total += rows;
      if (count) { count.textContent = query ? seen + " of " + rows + " shown" : ""; }
      var filler = table.querySelector("tr.fill");
      if (filler) { filler.hidden = !query || seen > 0; }
      if (table.hasAttribute("data-scopes")) {
        var section = table.closest("section");
        if (section) { section.classList.toggle("quiet", rows > 0 && seen === 0); }
      }
    });
    if (status) { status.textContent = query ? shown + " of " + total + " rows shown" : ""; }
  }
  var filterBox = document.getElementById("filter");
  if (filterBox) { filterBox.addEventListener("input", applyFilter); }

  /* ---- patterns: the non-colour channel, on request and in print */
  var patterns = document.getElementById("patterns");
  if (patterns) {
    patterns.addEventListener("click", function () {
      var on = document.documentElement.classList.toggle("patterns");
      patterns.setAttribute("aria-pressed", on ? "true" : "false");
    });
  }

  /* ---- showing and hiding: through the attribute, always.  The `.hidden`
     property exists on HTML elements only -- on an SVG group the assignment
     would silently do nothing */
  function show(element) { element.removeAttribute("hidden"); }
  function hide(element) { element.setAttribute("hidden", "hidden"); }

  /* ---- tooltips: built with textContent, because names are data */
  function empty(tip) { while (tip.firstChild) { tip.removeChild(tip.firstChild); } }
  function place(tip, host, clientX, clientY) {
    var rect = host.getBoundingClientRect();
    var x = clientX - rect.left, y = clientY - rect.top, left = x + 16, top = y + 16;
    if (left + tip.offsetWidth > host.clientWidth - 4) { left = x - tip.offsetWidth - 16; }
    if (top + tip.offsetHeight > rect.height - 4) { top = y - tip.offsetHeight - 16; }
    show(tip);
    tip.style.left = Math.max(0, left) + "px";
    tip.style.top = Math.max(0, top) + "px";
  }

  /* ---- line charts: the crosshair snaps to the nearest session and the
     readout lists every visible series there */
  all(document.querySelectorAll(".chart-body")).forEach(function (host) {
    var svg = host.querySelector("svg.plot");
    var source = host.querySelector("script[type='application/json']");
    if (!svg || !source) { return; }
    var spec = JSON.parse(source.textContent), tip = host.querySelector(".tip");
    var cursor = svg.querySelector("g.cursor"), hit = svg.querySelector("rect.hit");
    var dots = all(cursor.querySelectorAll("circle.dot"));
    var groups = all(svg.querySelectorAll("g.series > g"));
    var legend = host.parentNode.querySelector("ul.legend");
    var items = legend ? all(legend.querySelectorAll("li")) : [];
    var isolated = null, index = spec.labels.length - 1;
    function clamp(at) { return Math.max(0, Math.min(spec.labels.length - 1, at)); }
    function readout(value) {
      var moved = (value - spec.baseline) * 100;
      return (moved >= 0 ? "+" : "\u2212") + Math.abs(moved).toFixed(1) + "%";
    }
    function emphasize() {
      spec.series.forEach(function (series, i) {
        var on = isolated === null || series.key === isolated;
        groups[i].classList.toggle("off", !on);
        dots[i].classList.toggle("off", !on);
      });
      items.forEach(function (item) {
        var key = item.getAttribute("data-key");
        item.classList.toggle("off", isolated !== null && key !== isolated);
        item.classList.toggle("isolate", key === isolated);
      });
    }
    function draw(at) {
      empty(tip);
      var date = document.createElement("div");
      date.className = "tip-date";
      date.textContent = spec.labels[at];
      tip.appendChild(date);
      cursor.setAttribute("transform", "translate(" + (spec.left + at * spec.step) + " 0)");
      show(cursor);
      spec.series.forEach(function (series, i) {
        var value = series.values[at], dot = dots[i];
        if (value === null) { dot.setAttribute("visibility", "hidden"); return; }
        dot.setAttribute("cy", spec.top + (spec.hi - value) / (spec.hi - spec.lo) * spec.plotH);
        dot.setAttribute("visibility", "visible");
        if (isolated !== null && series.key !== isolated) { return; }
        var row = document.createElement("div");
        row.className = "row";
        var item = legend && legend.querySelector('li[data-key="' + series.key + '"] svg');
        if (item) { row.appendChild(item.cloneNode(true)); }
        var val = document.createElement("span");
        val.className = "val";
        val.textContent = readout(value);
        var who = document.createElement("span");
        who.className = "who";
        who.textContent = series.name;
        row.appendChild(val);
        row.appendChild(who);
        tip.appendChild(row);
      });
    }
    hit.addEventListener("pointermove", function (event) {
      var rect = svg.getBoundingClientRect(), scale = rect.width / spec.width;
      index = clamp(Math.round(((event.clientX - rect.left) / scale - spec.left) / spec.step));
      draw(index);
      place(tip, host, event.clientX, event.clientY);
    });
    hit.addEventListener("pointerleave", function () { hide(cursor); hide(tip); });
    hit.addEventListener("focus", function () { draw(index); });
    hit.addEventListener("blur", function () { hide(cursor); hide(tip); });
    hit.addEventListener("keydown", function (event) {
      var move = { ArrowLeft: -1, ArrowRight: 1, Home: -index,
                   End: spec.labels.length - 1 - index }[event.key];
      if (move === undefined) { return; }
      event.preventDefault();
      index = clamp(index + move);
      draw(index);
      var rect = svg.getBoundingClientRect(), scale = rect.width / spec.width;
      place(tip, host, rect.left + (spec.left + index * spec.step) * scale,
            rect.top + (spec.top + spec.plotH * 0.5) * scale);
    });
    items.forEach(function (item) {
      item.querySelector("button").addEventListener("click", function () {
        var key = item.getAttribute("data-key");
        isolated = isolated === key ? null : key;
        emphasize();
      });
    });
  });

  /* ---- bars: each row is its own hit target */
  all(document.querySelectorAll("svg.plot g.bar-row")).forEach(function (row) {
    var svg = row.ownerSVGElement, host = svg.parentNode, tip = host.querySelector(".tip");
    var target = row.querySelector("rect.hit");
    function show(clientX, clientY) {
      empty(tip);
      var value = document.createElement("div");
      value.className = "row val";
      value.textContent = row.getAttribute("data-value");
      var name = document.createElement("div");
      name.className = "row who";
      name.textContent = row.getAttribute("data-name");
      tip.appendChild(value);
      tip.appendChild(name);
      place(tip, host, clientX, clientY);
    }
    target.addEventListener("pointermove", function (event) {
      show(event.clientX, event.clientY);
    });
    target.addEventListener("pointerleave", function () { hide(tip); });
    target.addEventListener("focus", function () {
      var box = target.getBoundingClientRect();
      show(box.left + 24, box.top + 10);
    });
    target.addEventListener("blur", function () { hide(tip); });
  });
})();
"""


# ------------------------------------------------------------- page pieces
@dataclass
class Cell:
    """One table cell: its markup, an optional sort key and extra classes."""

    html: str
    sort: Optional[str] = None
    cls: str = ""


def _sort_key(value) -> Optional[str]:
    """The number behind a value, for sorting (missing sorts last)."""
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return repr(number) if np.isfinite(number) else ""


def cell(value, name: str = "") -> Cell:
    """A numeric cell from a plain value: shown in its feature's unit."""
    return Cell(_esc(cell_text(value, name)), _sort_key(value), "num")


def text(value, cls: str = "") -> Cell:
    """A text cell."""
    return Cell(_esc(value), None, cls)


def symbol(value) -> Cell:
    """A stock's code as text, carrying itself in a ``data-symbol``
    attribute -- inert in the standalone page (nothing changes), but a
    HOST that shows the page (qf-screen-web's frame) can turn every
    symbol into a link to that stock's own page without parsing the
    table."""
    return Cell(f'<span data-symbol="{_esc(value)}">{_esc(value)}</span>')


def dashed() -> Cell:
    """A value the report does not have: shown as a dash, sorted last."""
    return Cell('<span class="muted">-</span>', "", "num")


def _shown(value, fmt: Callable) -> Cell:
    return Cell(_esc(fmt(value)), _sort_key(value), "num")


def data_table(columns: Sequence[Tuple[str, str]], rows: Sequence[Sequence], caption: str,
               *, sortable: bool = True, searchable: bool = False, scopes: bool = False,
               restore_first: bool = False, empty: str = "(nothing to show)") -> str:
    """An HTML table.  ``columns`` are ``(label, classes)``; a row is a list
    of :class:`Cell` or of plain values.

    ``sortable`` gives every header a button (with ``restore_first`` the
    first one puts the rows back in their documented order instead of
    sorting — for a metrics-by-preset table); ``searchable`` exposes the
    rows to the page's filter box and ``scopes`` dims the enclosing
    section while none of them match.
    """
    head = []
    for index, (label, cls) in enumerate(columns):
        if sortable:
            restore = ' data-restore="1"' if restore_first and index == 0 else ""
            body = (f'<button type="button" class="sort" data-col="{index}"{restore}>'
                    f'{_esc(label)}</button>')
        else:
            body = _esc(label)
        head.append(f'<th scope="col" class="{cls}" aria-sort="none">{body}</th>')
    body_rows = []
    for order, row in enumerate(rows):
        cells = []
        for index, item in enumerate(row):
            item = item if isinstance(item, Cell) else cell(item)
            attribute = f' data-v="{_esc(item.sort)}"' if item.sort is not None else ""
            cells.append(f'<td class="{columns[index][1]} {item.cls}"{attribute}>{item.html}</td>')
        body_rows.append(f'<tr data-order="{order}">{"".join(cells)}</tr>')
    if body_rows:
        body_rows.append(f'<tr class="fill" hidden><td colspan="{len(columns)}" class="muted">'
                         "No row matches the filter.</td></tr>")
    else:
        body_rows = [f'<tr class="none"><td colspan="{len(columns)}">{_esc(empty)}</td></tr>']
    classes = "data" + (" sortable" if sortable else "") + (" searchable" if searchable else "")
    scope = ' data-scopes="1"' if scopes else ""
    return (f'<div class="table-wrap"><table class="{classes}"{scope}>'
            f'<caption>{caption}<span class="count" aria-live="polite"></span></caption>'
            f'<thead><tr>{"".join(head)}</tr></thead>'
            f'<tbody>{"".join(body_rows)}</tbody></table></div>')


def stat_tiles(items: Sequence[Tuple[str, str, str]], tones: Sequence[str] = ()) -> str:
    """A KPI row of ``(label, value, note)`` tiles.  A value is coloured by
    its sign only where the caller says the sign means good or bad
    (``tones``: ``""`` or ``"auto"`` per tile)."""
    tiles = []
    for index, (label, value, note) in enumerate(items):
        tone = ""
        if index < len(tones) and tones[index] == "auto":
            tone = "up" if value[:1] == "+" else "down" if value[:1] == "\u2212" else ""
        tiles.append(f'<div class="tile"><div class="tile-label">{_esc(label)}</div>'
                     f'<div class="tile-value {tone}">{_esc(value)}</div>'
                     + (f'<div class="tile-note">{_esc(note)}</div>' if note else "")
                     + "</div>")
    return f'<div class="tiles">{"".join(tiles)}</div>'


def badge_new() -> str:
    return '<span class="badge new">+NEW</span>'


def badge_out() -> str:
    return '<span class="badge out">\u2212OUT</span>'


def rank_pill(change: Optional[int]) -> str:
    """``↑+5`` / ``↓−3`` for a member whose rank moved by ``change``
    (positive = up); nothing when it did not move."""
    if not change:
        return ""
    if change > 0:
        return f'<span class="pill">\u2191+{int(change)}</span>'
    return f'<span class="pill">\u2193\u2212{int(abs(change))}</span>'


def section(anchor: str, title: str, body: str, cls: str = "", depth: int = 2) -> str:
    """A page section with an anchor (the table of contents links to it)."""
    return (f'<section id="{_esc(anchor)}" class="{cls}"><h{depth}>{title}</h{depth}>'
            f"{body}</section>")


def _pct(value, digits: int = 1) -> str:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return "-"
    if not np.isfinite(number):
        return "-"
    return f"{'\u2212' if number < 0 else '+'}{abs(number):.{digits}%}"


def _plain_pct(value, digits: int = 0) -> str:
    """An unsigned percentage (``12.3%`` / ``\u22125.0%``); ``-`` when missing."""
    try:
        number = float(value)
    except (TypeError, ValueError):
        return "-"
    if not np.isfinite(number):
        return "-"
    return f"{'\u2212' if number < 0 else ''}{abs(number):.{digits}%}"


def _move(value, digits: int = 1) -> str:
    """A cumulative value as its signed move (``1.12`` -> ``+12.0%``)."""
    try:
        return _pct(float(value) - 1.0, digits)
    except (TypeError, ValueError):
        return "-"


def _num(value, digits: int = 2) -> str:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return "-"
    return f"{number:.{digits}f}" if np.isfinite(number) else "-"


def _count(value) -> str:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return "-"
    return f"{int(number):,}" if np.isfinite(number) else "-"


# ---------------------------------------------------------------- charts
@dataclass
class Series:
    """One line of a chart: a name, a value per session (``None`` = none on
    that session) and how it is drawn."""

    name: str
    values: Sequence[Optional[float]]
    tone: str = "series-1"
    net: bool = False                 # a net-of-cost twin of the line above it

    @property
    def key(self) -> str:
        return self.tone + ("-net" if self.net else "")

    def finite(self) -> bool:
        return any(value is not None for value in self.values)


def _clean(values) -> List[Optional[float]]:
    out: List[Optional[float]] = []
    for value in values:
        try:
            number = float(value)
        except (TypeError, ValueError):
            number = float("nan")
        out.append(number if np.isfinite(number) else None)
    return out


def series(name: str, values, tone: str = "series-1", net: bool = False) -> Series:
    return Series(name, _clean(values), tone, net)


CHART_WIDTH = 900
#: the plot's margins: room for the y ticks, for the direct end-labels (or
#: none when there are too many series to label), the x axis band
_LEFT, _RIGHT, _NO_LABELS, _TOP, _BOTTOM = 58, 150, 24, 16, 30


def _nice_ticks(low: float, high: float, count: int = 5) -> List[float]:
    """About ``count`` round ticks spanning ``low .. high`` (steps of 1, 2
    or 5 times a power of ten — never 3 ticks where 6 would read better)."""
    span = high - low or abs(low) or 1.0
    raw = span / count
    magnitude = 10 ** math.floor(math.log10(raw))
    fraction = raw / magnitude
    step = magnitude * (1 if fraction <= 1.5 else 2 if fraction <= 3 else 5 if fraction <= 7
                        else 10)
    ticks, value = [], math.ceil(low / step - 1e-9) * step
    while value <= high + step * 1e-9:
        ticks.append(0.0 if abs(value) < step * 1e-9 else value)
        value += step
    return ticks


def _tick_label(value: float, baseline: float, span: float) -> str:
    moved = value - baseline
    if not moved:
        return "0%"
    return f"{moved:+.{1 if span < 0.04 else 0}%}"


def _x_labels(labels: Sequence[str]) -> List[Tuple[int, str]]:
    """About six ticks: the first and last session in full, the ones between
    as month-day (year-month when the window crosses a year)."""
    last = len(labels) - 1
    if last < 1:
        return [(0, labels[0])] if labels else []
    picks = sorted({round(k * last / 5) for k in range(6)} | {0, last})
    dated = all(len(label) == 10 and label[4] == "-" for label in labels)
    years = {label[:4] for label in labels} if dated else set()
    out = []
    for index in picks:
        label = labels[index]
        if not dated or index in (0, last):
            out.append((index, label))
        else:
            out.append((index, label[:7] if len(years) > 1 else label[5:]))
    return out


def _fitted(item: Series, count: int) -> Series:
    """A series laid on an axis of ``count`` sessions: one that ends early
    (a benchmark file that stops before the window does) is padded with
    gaps; one with more values than sessions is a caller's mistake and is
    refused rather than silently clipped."""
    if len(item.values) > count:
        raise ValueError(f"series {item.name!r} has {len(item.values)} values for {count} "
                         "sessions")
    return item if len(item.values) == count else Series(
        item.name, [*item.values, *([None] * (count - len(item.values)))], item.tone, item.net)


def _runs(xs: Sequence[float], ys: Sequence[Optional[float]]):
    """The line as runs of consecutive valued sessions (a gap breaks it)."""
    runs, run = [], []
    for x, y in zip(xs, ys):
        if y is None:
            if run:
                runs.append(run)
            run = []
        else:
            run.append((x, y))
    if run:
        runs.append(run)
    return runs


def _line_classes(index: int, item: Series) -> str:
    """What draws a series: its tone, plus the dash that tells it from its
    neighbours when hue is unavailable (a net twin always dashes, and the
    legend key wears exactly what the line wears)."""
    if item.net:
        return f"{item.tone} net"
    if index and item.tone not in REF_TONES and index < len(SERIES_TONES):
        return f"{item.tone} dash-{index + 1}"
    return item.tone


def _polyline(points) -> str:
    x, y = points[0]
    return f"M{x:.1f},{y:.1f}" + "".join(f"L{x2:.1f},{y2:.1f}" for x2, y2 in points[1:])


def line_chart(chart_id: str, title: str, subtitle: str, labels: Sequence[str],
               lines: Sequence[Series], *, baseline: float = 1.0, area: bool = False,
               height: int = 300, anchor_top: bool = False,
               table_caption: str = "Chart data") -> str:
    """A line chart as a ``<figure>``: grid, axes and baseline rule, the
    lines, a legend (two series or more), direct end-labels for up to four,
    the crosshair layer and a table twin.

    ``baseline`` is what the values read against (1.0 for a cumulative
    curve, 0 for a drawdown) and is drawn as a rule; ``area`` washes the
    area under the first series; ``anchor_top`` pins the baseline to the
    top of the scale (a drawdown never rises above it).
    """
    labels = [str(label) for label in labels]
    shown = [_fitted(item, len(labels)) for item in lines]
    shown = [item for item in shown if item.finite()]
    heading = (f'<figcaption>{_esc(title)}</figcaption><p class="sub">{_esc(subtitle)}</p>')
    if not shown or not labels:
        return f'<figure class="chart">{heading}<p class="muted">No data.</p></figure>'
    left, top, bottom = _LEFT, _TOP, _BOTTOM
    right = _RIGHT if len(shown) <= _DIRECT_LABELS else _NO_LABELS
    width = CHART_WIDTH
    plot_w, plot_h = width - left - right, height - top - bottom
    count = len(labels)
    step = plot_w / (count - 1) if count > 1 else 0.0
    xs = [left + step * i for i in range(count)] if count > 1 else [left + plot_w / 2]
    finite = [value for item in shown for value in item.values if value is not None]
    low = min(finite + [baseline])
    high = baseline if anchor_top else max(finite + [baseline])
    pad = (high - low) * 0.05 or abs(low) * 0.02 or 0.01
    low, high = low - pad, (high if anchor_top else high + pad)
    span = high - low

    def y_of(value: float) -> float:
        return top + (high - value) / span * plot_h

    parts = [f'<svg class="plot" width="{width}" height="{height}" '
             f'viewBox="0 0 {width} {height}" role="img" aria-label="{_esc(title)}">',
             '<g class="grid">']
    for tick in _nice_ticks(low, high):
        y = y_of(tick)
        parts.append(f'<line class="gridline" x1="{left}" y1="{y:.1f}" x2="{left + plot_w}" '
                     f'y2="{y:.1f}"/>')
        parts.append(f'<text x="{left - 8}" y="{y + 4:.1f}" text-anchor="end">'
                     f'{_esc(_tick_label(tick, baseline, span))}</text>')
    def ys(item: Series) -> List[Optional[float]]:
        """The series' values at their pixel heights (a gap stays a gap)."""
        return [None if value is None else y_of(value) for value in item.values]

    bottom_y = top + plot_h
    parts.append(f'<line class="axis" x1="{left}" y1="{bottom_y}" x2="{left + plot_w}" '
                 f'y2="{bottom_y}"/>')
    parts.append(f'<line class="baseline" x1="{left}" y1="{y_of(baseline):.1f}" '
                 f'x2="{left + plot_w}" y2="{y_of(baseline):.1f}"/>')
    for index, label in _x_labels(labels):
        anchor = "start" if index == 0 else "end" if index == count - 1 else "middle"
        parts.append(f'<text x="{xs[index]:.1f}" y="{bottom_y + 18:.1f}" '
                     f'text-anchor="{anchor}">{_esc(label)}</text>')
    parts.append("</g><g class=\"series\">")
    for index, item in enumerate(shown):
        classes = "line " + _line_classes(index, item)
        runs = _runs(xs, ys(item))
        parts.append(f'<g data-key="{item.key}">')
        if area and index == 0:
            wash = " ".join(
                f"M{run[0][0]:.1f},{y_of(baseline):.1f}L{run[0][0]:.1f},{run[0][1]:.1f}"
                + "".join(f"L{x:.1f},{y:.1f}" for x, y in run[1:])
                + f"L{run[-1][0]:.1f},{y_of(baseline):.1f}Z" for run in runs)
            parts.append(f'<path class="area {item.tone}" d="{wash}"/>')
        for run in runs:
            if len(run) > 1:
                parts.append(f'<path class="{classes}" d="{_polyline(run)}"/>')
            else:                      # one lone session between gaps still shows
                parts.append(f'<circle class="dot {item.tone}" cx="{run[0][0]:.1f}" '
                             f'cy="{run[0][1]:.1f}" r="2"/>')
        parts.append("</g>")
    parts.append("</g>")                          # the series group
    parts.append('<g class="cursor" hidden="hidden"><line class="xhair axis" x1="0" x2="0" '
                 f'y1="{top}" y2="{bottom_y}"/>'
                 + "".join(f'<circle class="dot {item.tone}" cx="0" cy="0" r="4" '
                           'visibility="hidden"/>' for item in shown)
                 + "</g>")
    parts.append(_end_labels(shown, xs, y_of, baseline, top, plot_h))
    parts.append(f'<rect class="hit" x="{left}" y="{top}" width="{plot_w}" height="{plot_h}" '
                 'tabindex="0" aria-label="Chart readout: use the left and right arrow keys"/>')
    parts.append("</svg>")
    legend = ""
    if len(shown) >= 2:
        items = "".join(
            f'<li data-key="{item.key}"><button type="button"><svg width="18" height="8" '
            f'viewBox="0 0 18 8" aria-hidden="true"><line class="key {_line_classes(index, item)}"'
            f' x1="1" y1="4" x2="17" y2="4"/></svg>{_esc(item.name)}</button></li>'
            for index, item in enumerate(shown))
        legend = f'<ul class="legend">{items}</ul>'
    rows = []
    for at, label in enumerate(labels):
        rows.append([label] + [Cell(_esc(_pct(item.values[at] - baseline, 1)),
                                    _sort_key(item.values[at] - baseline))
                               if item.values[at] is not None else dashed() for item in shown])
    table = data_table([("session", ""), *[(item.name, "num") for item in shown]], rows,
                       f"{table_caption} — {count} sessions", sortable=False)
    payload = {"labels": labels, "width": width, "left": left, "top": top, "plotH": plot_h,
               "step": step, "lo": low, "hi": high, "baseline": baseline,
               "series": [{"key": item.key, "name": item.name, "values": item.values}
                          for item in shown]}
    return (f'<figure class="chart">{heading}{legend}<div class="chart-body">{"".join(parts)}'
            f'<div class="tip" hidden></div><script type="application/json" '
            f'id="{_esc(chart_id)}-data">{_json(payload)}</script></div>'
            f'<details class="chart-data"><summary>{_esc(table_caption)}</summary>{table}'
            "</details></figure>")


def _end_labels(shown: List[Series], xs, y_of, baseline: float, top: float,
                plot_h: float) -> str:
    """Direct labels at the right edge for up to ``_DIRECT_LABELS`` series:
    the value, then the name, after a line key in the series colour.
    Labels that would overlap are moved apart and joined to their line by
    a leader; past the limit the legend, the readout and the table carry
    identity instead."""
    if len(shown) > _DIRECT_LABELS:
        return ""
    ends = []
    for item in shown:
        last = max((at for at, value in enumerate(item.values) if value is not None),
                   default=None)
        if last is not None:
            ends.append({"item": item, "x": xs[last], "y": y_of(item.values[last])})
    if not ends:
        return ""
    ends.sort(key=lambda end: end["y"])
    for index, end in enumerate(ends):            # no two labels closer than a line
        end["ly"] = max(end["y"], ends[index - 1]["ly"] + 16) if index else end["y"]
    overflow = ends[-1]["ly"] - (top + plot_h - 4)
    if overflow > 0:
        for end in ends:
            end["ly"] -= overflow
    out = ['<g class="ends">']
    for end in ends:
        item, x, y, ly = end["item"], end["x"], end["y"], end["ly"]
        last = max(at for at, value in enumerate(item.values) if value is not None)
        value = _pct(item.values[last] - baseline, 1)      # signed, like the axis
        leader = (f'<line class="leader {item.tone}" x1="{x + 2:.1f}" y1="{y:.1f}" '
                  f'x2="{x + 6:.1f}" y2="{ly - 4:.1f}"/>') if abs(ly - y) > 2 else ""
        out.append(f'{leader}<line class="key {item.tone}" x1="{x + 8:.1f}" y1="{ly - 4:.1f}" '
                   f'x2="{x + 22:.1f}" y2="{ly - 4:.1f}"/>'
                   f'<text class="end-label" x="{x + 26:.1f}" y="{ly:.1f}">'
                   f'{_esc(value)} <tspan class="name">{_esc(item.name)}</tspan></text>')
    out.append("</g>")
    return "".join(out)


def bar_chart(title: str, subtitle: str, rows: Sequence[Tuple[str, float]], *, total: float,
              width: int = 440, limit: int = 8) -> str:
    """A ranked horizontal bar chart in one hue: bars 16px thick with a 4px
    rounded far end (square at the baseline), the count and its share at
    each tip.  ``rows`` are ``(label, value)`` largest first; a label of
    ``未分类`` (no classification) is drawn last, in grey.  Past ``limit``
    rows the tail folds into ``其他``; the table twin keeps every row."""
    loose = [row for row in rows if str(row[0]) == UNCLASSIFIED]
    named = [row for row in rows if str(row[0]) != UNCLASSIFIED]
    if len(named) > limit:
        tail = sum(value for _label, value in named[limit - 1:])
        named = named[:limit - 1] + [(OTHER, tail)]
    ordered = named + loose
    row_h, bar_h, label_w, value_w = 28, 16, 92, 78
    left = label_w + 8
    plot_w = width - left - value_w
    biggest = max((value for _label, value in ordered), default=0.0) or 1.0
    height = _TOP + row_h * len(ordered) + 4
    parts = [f'<svg class="plot" width="{width}" height="{height}" '
             f'viewBox="0 0 {width} {height}" role="img" aria-label="{_esc(title)}">']
    for index, (label, value) in enumerate(ordered):
        y = _TOP + index * row_h
        tone = "ref-1" if str(label) == UNCLASSIFIED else "series-1"
        length = plot_w * value / biggest
        top = y + (row_h - bar_h) / 2
        if length >= 4:                       # square at the baseline, round at the tip
            shape = (f"M{left},{top:.1f}h{length - 4:.1f}a4,4 0 0 1 4,4v{bar_h - 8}"
                     f"a4,4 0 0 1 -4,4h-{length - 4:.1f}z")
        else:
            shape = f"M{left},{top:.1f}h{length:.1f}v{bar_h}h-{length:.1f}z"
        share = value / total if total else float("nan")
        shown = f"{int(value)} ({share:.0%})" if share == share else f"{int(value)}"
        short = label if len(label) <= 6 else label[:5] + "\u2026"
        parts.append(
            f'<g class="bar-row" data-name="{_esc(label)}" data-value="{_esc(shown)}">'
            f'<text x="{left - 8}" y="{y + row_h / 2 + 4:.1f}" text-anchor="end">'
            f'{_esc(short)}</text>'
            f'<path class="bar {tone}" d="{shape}"/>'
            f'<text x="{left + length + 8:.1f}" y="{y + row_h / 2 + 4:.1f}">{_esc(shown)}</text>'
            f'<rect class="hit" x="0" y="{y}" width="{width}" height="{row_h}" tabindex="0">'
            f"<title>{_esc(label)}: {_esc(shown)}</title></rect></g>")
    parts.append("</svg>")
    table = data_table([("industry", ""), ("members", "num")],
                       [[label, cell(value)] for label, value in rows],
                       f"{title} — every industry", sortable=True)
    return (f'<figure class="chart"><figcaption>{_esc(title)}</figcaption>'
            f'<p class="sub">{_esc(subtitle)}</p><div class="chart-body">{"".join(parts)}'
            f'<div class="tip" hidden></div></div><details class="chart-data">'
            f"<summary>Every industry</summary>{table}</details></figure>")


# ------------------------------------------------------------------ pages
def _version() -> str:
    from qf_screen import __version__

    return __version__


def _page(title: str, meta: str, body: str, *, search: bool = True) -> str:
    """The document around a report body: the header with its controls (the
    filter box only where tables hold symbols to find), the body, a footer
    and the page's script."""
    controls = ""
    if any(word in body for word in ('class="plot"', 'class="searchable')):
        box = ('<label class="search"><span>Filter</span><input type="search" id="filter" '
               'placeholder="symbol, name or industry" autocomplete="off"></label>'
               '<span id="filter-status" aria-live="polite"></span>') if search else ""
        lines = ('<button type="button" id="patterns" aria-pressed="false">Line patterns'
                 "</button>") if 'class="plot"' in body else ""
        controls = f'<div class="tools">{box}{lines}</div>'
    return ("<!doctype html>\n<html lang=\"en\">\n<head>\n<meta charset=\"utf-8\">\n"
            "<meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">\n"
            f"<title>{_esc(title)}</title>\n<style>{_CSS}</style>\n</head>\n<body>\n"
            f"<header class=\"top\"><div class=\"wrap\"><p class=\"eyebrow\">qf-screen</p>"
            f"<h1>{_esc(title)}</h1><p class=\"meta\">{meta}</p>{controls}</div></header>"
            f"<main class=\"wrap\">{body}</main>"
            "<footer class=\"wrap foot\">Generated by qf-screen " + _esc(_version())
            + " · one self-contained file: no external resource is loaded.</footer>"
            f"<script>{_SCRIPT}</script>\n</body>\n</html>\n")


_INDEX_NAMES = {"csi300": "CSI 300", "csi500": "CSI 500", "csi1000": "CSI 1000",
                "sse": "SSE Composite", "szse": "SZSE Component"}


def _notes(notes: Sequence[str], title: str = "Notes") -> str:
    notes = [str(note) for note in notes if note]
    if not notes:
        return ""
    return (f"<h3>{_esc(title)}</h3><ul class=\"notes\">"
            + "".join(f"<li>{_esc(note)}</li>" for note in notes) + "</ul>")


def _drawdown(values: Sequence[Optional[float]]) -> List[Optional[float]]:
    """``value / running peak - 1`` session by session (a gap stays a gap)."""
    out, peak = [], None
    for value in values:
        if value is None:
            out.append(None)
            continue
        peak = value if peak is None else max(peak, value)
        out.append(value / peak - 1.0)
    return out


def _blank(value) -> str:
    """A text cell's content: missing / NaN text is empty, never ``nan``."""
    if value is None:
        return ""
    if isinstance(value, float) and not np.isfinite(value):
        return ""
    return str(value).strip()


# -- the daily briefing -----------------------------------------------------
def _member_rows(item: Dict) -> List[List[Cell]]:
    """The member table: rank, the change marker, symbol, name, industry,
    score and the preset's extra features."""
    before = item.get("rank_prev") or {}
    new = set(item["entered"]["symbol"]) if item.get("prev") else set()
    extra = [str(column) for column in item["picks"].columns[6:]]
    rows = []
    for values in item["picks"].to_dict(orient="records"):
        code = str(values["symbol"])
        if code in new:
            mark, key = badge_new(), "-1000000"            # new names sort first
        else:
            moved = before[code] - int(values["rank"]) if code in before else 0
            mark, key = rank_pill(moved), str(-moved) if moved else ""
        rows.append([cell(values["rank"]), Cell(mark, key), symbol(code),
                     text(_blank(values.get("name"))), text(_blank(values.get("industry_l1"))),
                     cell(values.get("score"), "score")]
                    + [cell(values.get(column), column) for column in extra])
    return rows


def _preset_section(index: int, item: Dict) -> str:
    """One preset: its thesis, the industry chart beside the change lists,
    and the member table with its badges."""
    mix = item["mix"]
    total = float(mix.sum())
    chart = bar_chart("Industry concentration",
                      f"{int(total)} members by first-level industry (today's classification)",
                      list(mix.items()), total=total)
    if item.get("prev"):
        changes = (f'<h3>Entered (+{len(item["entered"])}) vs {item["prev"]}</h3>'
                   + _names(item["entered"], badge_new)
                   + f'<h3>Exited (\u2212{len(item["exited"])})</h3>'
                   + _names(item["exited"], badge_out))
    else:
        changes = ("<h3>Changes</h3><p class=\"lede\">No earlier session in the panel to "
                   "compare with, so none is reported.</p>")
    picks = item["picks"]
    shown = (f"Top {len(picks)} of {item['members']}" if len(picks) < item["members"]
             else f"All {item['members']}")
    caption = (f"{shown} members, best first · {badge_new()} joined since the last session · "
               "a pill is a rank move (\u2191 up)")
    description = " ".join(str(item.get("description") or "").split())
    return section(
        f"p{index}", f'<code>{_esc(item["name"])}</code> — {item["members"]} members',
        (f'<p class="lede">{_esc(description)}</p>' if description else "")
        + f'<div class="grid2">{chart}<div>{changes}</div></div>'
        + data_table([("rank", "num"), ("change", ""), ("symbol", ""), ("name", ""),
                      ("industry", ""), ("score", "num")]
                     + [(str(column), "num") for column in picks.columns[6:]],
                     _member_rows(item), caption, searchable=True, scopes=True,
                     empty="No members today"))


def _overview_rows(presets: Sequence[Dict]) -> List[List[Cell]]:
    rows = []
    for index, item in enumerate(presets):
        kind = ' <span class="muted">user</span>' if item.get("user") else ""
        link = Cell(f'<a href="#p{index}"><code>{_esc(item["name"])}</code></a>{kind}')
        mix = item["mix"]
        classified = [(label, value) for label, value in mix.items() if label != UNCLASSIFIED]
        if classified:
            label, value = classified[0]
            lead = f"{label} {int(value)} ({value / max(mix.sum(), 1):.0%})"
        else:
            lead = "no members"
        if item.get("prev"):
            kept, had = item["retained"], item["previous_members"]
            share = kept / had if had else float("nan")
            rows.append([link, cell(item["members"]), cell(len(item["entered"])),
                         cell(len(item["exited"])),
                         Cell(f"{kept} <span class=\"muted\">({_plain_pct(share)})</span>",
                              _sort_key(share)), text(lead)])
        else:
            rows.append([link, cell(item["members"]), dashed(), dashed(), dashed(), text(lead)])
    return rows


def daily_report_html(context: Dict) -> str:
    """The daily briefing as a page: headline tiles, the overview, one
    section per preset (industry chart, the entered / exited lists with
    their badges, the member table), the data warnings.

    ``context`` is what :func:`qf_screen.ui.briefing.daily_report` computed:
    ``asof``, ``last_session``, ``live``, ``loaded``, ``warnings`` and,
    per preset, ``name``, ``description``, ``user``, ``members`` (count),
    ``picks`` (the member table), ``mix`` (industry -> members),
    ``entered`` / ``exited`` (frames), ``prev`` (the earlier date),
    ``rank_prev`` (symbol -> previous rank), ``retained`` and
    ``previous_members``.
    """
    presets = context["presets"]
    warnings = [str(warning) for warning in context.get("warnings") or []]
    members = sum(int(item["members"]) for item in presets)
    changed = [item for item in presets if item.get("prev")]
    entered = sum(len(item["entered"]) for item in changed)
    exited = sum(len(item["exited"]) for item in changed)
    live = f", a provisional live row {context['live']}" if context.get("live") else ""
    meta = (f"Decided on the {context['asof']} close · last session "
            f"{context['last_session']}{live} · {context['loaded']} names loaded · "
            + (f"{len(warnings)} data warning(s)" if warnings else "no data warnings"))
    against = f"against {changed[0]['prev']}" if changed else "no earlier session"
    body = [stat_tiles([
        ("Presets", str(len(presets)), ""),
        ("Members", f"{members:,}", "across the presets below"),
        ("Entered", f"+{entered}", against),
        ("Exited", f"\u2212{exited}", against),
        ("Data warnings", str(len(warnings)), "")])]
    links = "".join(f'<a href="#p{index}"><code>{_esc(item["name"])}</code></a>'
                    for index, item in enumerate(presets))
    body.append('<nav class="toc" aria-label="Sections"><a href="#overview">Overview</a>'
                + links + ('<a href="#warnings">Data warnings</a>' if warnings else "")
                + "</nav>")
    body.append(section(
        "overview", "Overview",
        '<p class="lede">One line per preset: the members it holds today and what changed '
        "since the session before. Click any column to sort; the filter box above searches "
        "every member table on the page.</p>"
        + data_table([("preset", ""), ("members", "num"), ("entered", "num"),
                      ("exited", "num"), ("retained", "num"), ("largest industry", "")],
                     _overview_rows(presets), "Presets")))
    body += [_preset_section(index, item) for index, item in enumerate(presets)]
    if warnings:
        body.append(section("warnings", f"Data warnings ({len(warnings)})",
                            "<ul class=\"notes\">"
                            + "".join(f"<li>{_esc(warning)}</li>" for warning in warnings)
                            + "</ul>"))
    return _page(f"qf-screen daily briefing — {context['asof']}", meta, "".join(body))


# -- a validation -----------------------------------------------------------
_FREQ_WORDS = {"M": "monthly", "W": "weekly", "Q": "quarterly"}


def _per_date_table(frame: pd.DataFrame, caption: str) -> str:
    """The per-rebalance table: one row per schedule date, every column of
    ``ValidationReport.per_date`` under a label and in its own unit."""
    names = {"portfolio": "book", "portfolio_net": "book, net", "universe": "pool"}

    def spec(name: str) -> Tuple[str, Callable]:
        if name in ("members", "baseline", "skipped"):
            return {"members": "members", "baseline": "pool",
                    "skipped": "skipped (limit-up)"}[name], _count
        if name == "portfolio":
            return "period return", _pct
        if name == "cost":
            return "cost paid", _plain_pct
        if name == "turnover":
            return "turnover", _plain_pct
        matched = re.fullmatch(r"(members|baseline)_(\d+)d", name)
        if matched:
            return f"{names.get(matched[1], matched[1])} {matched[2]}d", _pct
        matched = re.fullmatch(r"(.+)_cum", name)
        if matched:
            label = names.get(matched[1], _INDEX_NAMES.get(matched[1], matched[1]))
            return f"{label} value", _move
        return name, _num

    columns, formatters = [("date", "")], {"date": lambda value: text(str(value)[:10])}
    for name in frame.columns:
        if name == "date":
            continue
        label, fmt = spec(str(name))
        columns.append((label, "num"))
        formatters[name] = lambda value, fmt=fmt: Cell(_esc(fmt(value)), _sort_key(value), "num")
    rows = [[formatters[str(name)](row[name]) for name in frame.columns]
            for _index, row in frame.iterrows()]
    return data_table(columns, rows, caption)


def _path_series(curve: pd.DataFrame, preset: str) -> List[Series]:
    """The lines of one validation's cumulative chart: the book in slot 1
    (its net twin dashed beside it), the pool and the indexes in greys."""
    tones = {"portfolio": ("series-1", False), "portfolio_net": ("series-1", True),
             "universe": ("ref-1", False)}
    names = {"portfolio": preset, "portfolio_net": f"{preset} (net)", "universe": "universe"}
    out = []
    for index, column in enumerate(curve.columns):
        tone, net = tones.get(str(column), (REF_TONES[1 + index % 2], False))
        label = names.get(str(column), _INDEX_NAMES.get(str(column), str(column)))
        out.append(series(label, curve[column].to_numpy(), tone, net))
    return out


def validate_html(report, *, preset: str, start: str, end: str, freq: str, pool: str,
                  execution: str, warnings: Sequence[str]) -> str:
    """One validation as a page: headline tiles, the cumulative curve and
    the drawdown (book vs pool vs indexes), the statistics and
    forward-return tables, the rebalance-by-rebalance table, the notes.

    ``report`` is the :class:`~qf_screen.engine.ValidationReport`;
    ``execution`` says what the path was priced with ("gross", or the
    costs and the limit-up guard), ``warnings`` the store's data caveats.
    """
    book = report.stats.get("portfolio", {})
    net = report.stats.get("portfolio_net")
    after = net or {}
    turnover = report.turnover
    tiles = stat_tiles([
        ("Annualized return", _pct(book.get("ann_return")),
         f"net {_pct(after.get('ann_return'))}" if net else "before costs"),
        ("Annualized volatility", _plain_pct(book.get("ann_vol"), 1), ""),
        ("Sharpe ratio", _num(book.get("sharpe")),
         f"net {_num(after.get('sharpe'))}" if net else ""),
        ("Max drawdown", _pct(book.get("max_drawdown")),
         f"net {_pct(after.get('max_drawdown'))}" if net else ""),
        ("Cumulative return", _pct(book.get("cum_return")),
         f"{_count(book.get('days'))} sessions"),
        ("Turnover per rebalance", _plain_pct(turnover.get("mean")),
         f"{_count(turnover.get('rebalances'))} rebalances of "
         f"{_num(turnover.get('members_mean'), 1)} names"),
    ], tones=["auto", "", "", "auto", "auto", ""])

    figures = ""
    curve = report.curve
    if curve is not None and len(curve):
        labels = [pd.Timestamp(day).date().isoformat() for day in curve.index]
        path = _path_series(curve, preset)
        how = (f"An equal-weight book of the schedule's names, rebalanced {_FREQ_WORDS.get(freq, freq)}"
               f" and held in between; the value of 1.0 at the start · {execution}")
        figures = (
            line_chart("curve", f"Cumulative value: {preset} against its pool and the indexes",
                       how, labels, path)
            + line_chart(
                "drawdown", "Drawdown from the running peak",
                "The same books: the fall from their highest close so far", labels,
                [series(item.name, _drawdown(item.values), item.tone, item.net)
                 for item in path],
                baseline=0.0, area=True, anchor_top=True, height=220))
    body = [tiles, section("path", "The path of the book", figures)]

    rows = [[text(label)] + [_shown(stats.get(metric), fmt) for metric, fmt in (
        ("ann_return", _pct), ("ann_vol", lambda v: _plain_pct(v, 1)), ("sharpe", _num),
        ("max_drawdown", _pct), ("cum_return", _pct), ("days", _count))]
        for label, stats in report.stats.items()]
    rows2 = [[text(f"{horizon}d")] + [_shown(values.get(metric), fmt) for metric, fmt in (
        ("members_mean", _pct), ("members_median", _pct), ("baseline_mean", _pct),
        ("spread_mean", _pct), ("spread_t", _num), ("hit_rate", _plain_pct), ("dates", _count))]
        for horizon, values in sorted(report.horizons.items())]
    body.append(section(
        "statistics", "Statistics over the same sessions",
        data_table([("series", ""), ("ann. return", "num"), ("ann. vol", "num"),
                    ("Sharpe", "num"), ("max drawdown", "num"), ("cum. return", "num"),
                    ("sessions", "num")], rows,
                   "Gross, and net of costs where the book has a net twin · the pool and the "
                   "indexes are always gross")
        + "<h3>Forward returns by horizon</h3>"
        + data_table([("horizon", ""), ("members mean", "num"), ("members median", "num"),
                      ("pool mean", "num"), ("spread", "num"), ("t", "num"), ("hit rate", "num"),
                      ("dates", "num")], rows2,
                     "Members bought at each rebalance close and held N sessions, against every "
                     "name passing the universe constraints the session before")))
    body.append(section(
        "rebalance", "Rebalance by rebalance",
        _per_date_table(report.per_date,
                        "Each schedule date: what was held, what the period returned, what the "
                        "rebalance cost and traded, each book's value at that close")))
    notes = list(report.notes)
    if report.skipped:
        notes.append(f"{int(report.skipped['skipped'])} of {int(report.skipped['entries'])} "
                     "entries closed at the daily limit-up on their entry day and were not "
                     "bought (their share of the book stayed in cash)")
    body.append(section("notes", "Notes", _notes(notes) + _notes(
        [str(warning) for warning in warnings], "Data warnings") or "<p>No notes.</p>"))
    meta = (f"Schedule {start}..{end} · {_FREQ_WORDS.get(freq, freq)} rebalances · {pool} · "
            f"{execution}")
    return _page(f"qf-screen validation — {preset}", meta, "".join(body), search=False)


_METRIC_LABELS = {"members": "Members (mean)", "turnover": "Turnover / rebalance",
                  "rebalances": "Rebalances", "ann_return": "Annualized return",
                  "ann_vol": "Annualized volatility", "sharpe": "Sharpe ratio",
                  "max_drawdown": "Max drawdown", "cum_return": "Cumulative return",
                  "excess_ann": "Excess over own pool / yr", "information_ratio": "Information ratio"}


def _metric_label(metric: str) -> str:
    if metric in _METRIC_LABELS:
        return _METRIC_LABELS[metric]
    if metric.endswith("_net"):
        return _metric_label(metric[:-4]) + " (net)"
    matched = re.fullmatch(r"(spread|hit)_(\d+)d", metric)
    if matched:
        return f"{'Spread' if matched[1] == 'spread' else 'Hit rate'} {matched[2]}d"
    return metric


def compare_html(table: pd.DataFrame, reports: Dict, *, start: str, end: str, freq: str,
                 execution: str, notes: Sequence[str], warnings: Sequence[str]) -> str:
    """Several validations side by side: the headline tiles, one cumulative
    chart and one drawdown chart (each preset a line in its fixed slot,
    the whole market and the indexes in greys), and the metrics-by-preset
    table.

    ``table`` / ``reports`` are what :meth:`ScreenEngine.compare
    <qf_screen.engine.ScreenEngine.compare>` returned.
    """
    from qf_screen.ui.briefing import compare_cells

    presets = [str(name) for name in table.columns
               if str(name) in reports and str(name) != "universe"]
    market = reports.get("universe")
    curves = {name: reports[name].curve for name in presets
              if reports[name].curve is not None and "portfolio" in reports[name].curve}
    days = None
    for curve in [*curves.values()] + ([market.curve] if market is not None
                                       and market.curve is not None else []):
        days = curve.index if days is None else days.union(curve.index)
    days = days if days is not None else pd.DatetimeIndex([])
    labels = [pd.Timestamp(day).date().isoformat() for day in days]

    def as_lines(values, label: str, tone: str) -> Tuple[Series, Series]:
        falls = pd.Series(_drawdown(_clean(values.to_numpy())), index=values.index)
        return (series(label, values.reindex(days), tone),
                series(label, falls.reindex(days), tone))

    path, falls, rest = [], [], []
    for name in presets:
        if name not in curves:
            continue
        if len(path) >= len(SERIES_TONES):        # never a ninth colour
            rest.append(name)
            continue
        line, fall = as_lines(curves[name]["portfolio"], name, SERIES_TONES[len(path)])
        path.append(line)
        falls.append(fall)
    reference = 0
    if market is not None and market.curve is not None:
        # the benchmark's "universe" column is its own pool -- itself: one line
        for column in [str(name) for name in market.curve.columns if str(name) != "universe"]:
            is_market = column == "portfolio"
            label = "universe" if is_market else _INDEX_NAMES.get(column, column)
            tone = "ref-1" if is_market else REF_TONES[1 + reference % 2]
            reference += 0 if is_market else 1
            line, fall = as_lines(market.curve[column], label, tone)
            path.append(line)
            falls.append(fall)

    returns = table.loc["ann_return"].dropna() if "ann_return" in table.index \
        else pd.Series(dtype=float)
    ranked = returns.sort_values(ascending=False)
    tiles = stat_tiles([
        ("Presets compared", str(len(presets)),
         f"{_FREQ_WORDS.get(freq, freq)} rebalances"),
        ("Highest return", _pct(ranked.iloc[0]) if len(ranked) else "-",
         _blank(ranked.index[0]) if len(ranked) else ""),
        ("Lowest return", _pct(ranked.iloc[-1]) if len(ranked) else "-",
         _blank(ranked.index[-1]) if len(ranked) else ""),
        ("Whole market", _pct(table.loc["ann_return", "universe"])
         if "ann_return" in table.index and "universe" in table.columns else "-",
         "equal-weight universe")], tones=["", "auto", "auto", "auto"])
    figures = ""
    if path and len(labels):
        how = (f"Each preset's equal-weight book, rebalanced {_FREQ_WORDS.get(freq, freq)} and "
               f"held in between; the value of 1.0 at the start · {execution}")
        figures = (line_chart("curve", "Cumulative value: the presets against the whole market",
                              how, labels, path)
                   + line_chart("drawdown", "Drawdown from the running peak",
                                "The same books: the fall from their highest close so far",
                                labels, falls, baseline=0.0, area=False, anchor_top=True,
                                height=220))
    cells = compare_cells(table).set_index("metric")
    rows = []
    for metric in table.index:
        row = [text(_metric_label(str(metric)))]
        for name in table.columns:
            value = table.loc[metric, name]
            try:
                missing = value is None or not np.isfinite(float(value))
            except (TypeError, ValueError):
                missing = True
            row.append(dashed() if missing else Cell(
                _esc(cells.loc[metric, name]), _sort_key(value), "num"))
        rows.append(row)
    remarks = list(notes)
    if rest:
        remarks.append("only the first eight presets are charted; " + ", ".join(rest)
                       + " are in the table alone (a chart never carries a ninth colour)")
    body = [tiles, section("path", "The path of each book", figures), section(
        "table", "Metrics by preset",
        '<p class="lede">Click a preset to rank the metrics by it; click the first column to '
        "put them back in order. Each preset's excess is against its own candidate pool.</p>"
        + data_table([("metric", ""), *[(str(name), "num") for name in table.columns]], rows,
                     "Gross, and net of costs where a column has net rows", restore_first=True))]
    if remarks or warnings:
        body.append(section("notes", "Notes", _notes(remarks) + _notes(
            [str(warning) for warning in warnings], "Data warnings")))
    meta = (f"{start}..{end} · {_FREQ_WORDS.get(freq, freq)} rebalances · {len(presets)} "
            f"presets · {execution}")
    return _page("qf-screen comparison", meta, "".join(body), search=False)


def _names(frame: pd.DataFrame, badge) -> str:
    """A change list — badge (``badge()`` makes it), the rank the name
    holds or held, symbol, name and industry — one line per name."""
    if not len(frame):
        return '<p class="lede">None.</p>'
    items = []
    for values in frame.to_dict(orient="records"):
        rank = f"#{values['rank']} " if "rank" in values else ""
        where = " · ".join(part for part in (_blank(values.get("name")),
                                             _blank(values.get("industry_l1"))) if part)
        items.append(f'<li data-symbol="{_esc(values["symbol"])}">{badge()}'
                     f'<span class="who">{rank}{_esc(values["symbol"])}</span>'
                     f'<span class="where">{_esc(where)}</span></li>')
    return f'<ul class="changes">{"".join(items)}</ul>'
