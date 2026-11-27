"""What qf_screen shows: every view of a result, for every audience.

All of it is reached through the command line (``qf-screen <command>
--format table|json|markdown|html``): by a person at a terminal, by a cron
job writing ``--out``, by a webhook, or by the qf_tools website app, which
runs that command and shows what it prints.  Nothing here decides what a
screen is -- that is the engine's; this package turns results into output.

- :mod:`qf_screen.ui.render` -- the units: one feature value in its human
  unit (``5.7亿``, ``24.0%``), and text / Markdown tables.  The one place a
  number's display unit is decided; the pages below format through it.
- :mod:`qf_screen.ui.briefing` -- the text / JSON / Markdown views of
  ``run``, ``diff``, ``inspect``, ``report`` and ``compare``, and the
  payloads the other formats are built from.
- :mod:`qf_screen.ui.html_report` -- the self-contained HTML pages of
  ``report`` / ``validate`` / ``compare``: inline SVG charts, sortable and
  filterable tables, a crosshair readout; no external resource is loaded.
- :mod:`qf_screen.ui.watch` -- the watchlist: its rules and watchlist
  files, the alert computation behind ``qf-screen watch``, and that report
  as text, Markdown or JSON.

``qf_screen.cli`` -- the command line itself -- stays at the package root:
it is the program's entry point, which the installed ``qf-screen`` command
imports by name.

Nothing is imported here: ``import qf_screen`` stays cheap, and the package
root re-exports the public names lazily (``qf_screen.watch``,
``qf_screen.validate_html`` ...).
"""
