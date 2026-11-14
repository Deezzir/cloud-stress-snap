"""Self-contained HTML report with tables and offline interactive graphs."""

import base64
import html as html_lib
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import cast

from plotly.offline import get_plotlyjs

from campaign.model.profiles import format_duration
from campaign.model.state import run_dir
from campaign.report import plots
from campaign.report.build import _jsonl
from campaign.report.text import _dict, _fmt, _list, summary_text

Row = dict[str, object]

STYLE = """
:root { color-scheme: light; font-family: system-ui, sans-serif; color: #172b3a;
  background: #f3f5f7; line-height: 1.5; }
* { box-sizing: border-box; }
body { margin: 0; }
main { max-width: 1160px; margin: auto; padding: 32px 24px 56px; }
h1, h2, h3, p { margin: 0; }
h1 { font-size: 1.8rem; letter-spacing: -.03em; }
h2 { font-size: 1.25rem; }
h3 { font-size: 1rem; }
a { color: #075b91; text-underline-offset: 3px; }
header, section { display: grid; gap: 16px; }
header { padding-bottom: 24px; }
section { margin-top: 24px; padding: 24px; background: white;
  border: 1px solid #dce2e7; border-radius: 10px; scroll-margin-top: 16px; }
section > * { min-width: 0; }
nav, .badges { display: flex; flex-wrap: wrap; gap: 8px 20px; }
.eyebrow, .note, footer { color: #526473; }
.eyebrow { font-size: .8rem; text-transform: uppercase; letter-spacing: .08em; }
.run-id { font-size: .9rem; overflow-wrap: anywhere; }
.badge { display: inline-block; padding: 4px 10px; border-radius: 5px;
  background: #e8eef3; font-size: .85rem; font-weight: 600; }
.good { background: #e0f2e8; color: #175537; }
.attention { background: #fff0d6; color: #744300; }
.facts { display: grid; grid-template-columns: repeat(auto-fit, minmax(170px, 1fr));
  gap: 16px; margin: 0; }
dt { color: #526473; font-size: .85rem; }
dd { margin: 4px 0 0; font-weight: 600; overflow-wrap: anywhere; }
.table-scroll { overflow-x: auto; }
table { width: 100%; border-collapse: collapse; font-size: .875rem;
  font-variant-numeric: tabular-nums; }
caption { text-align: left; font-weight: 600; padding-bottom: 12px; }
th, td { padding: 10px 12px; text-align: left; border-bottom: 1px solid #e3e8ed; }
th { background: #f1f5f8; font-weight: 600; }
td { white-space: nowrap; }
tbody tr:nth-child(even) { background: #fafbfd; }
.note { font-size: .875rem; }
figure { margin: 0; padding-top: 16px; border-top: 1px solid #e3e8ed;
  overflow-x: auto; }
figure img { display: block; width: 100%; min-width: 720px; height: auto; }
.plot-controls { display: flex; flex-wrap: wrap; align-items: center;
  gap: 8px; padding-top: 12px; font-size: .875rem; }
.plot-controls label { display: flex; flex-wrap: wrap; align-items: center; gap: 8px; }
.plot-controls input, .plot-controls button { font: inherit; padding: 6px 10px;
  border: 1px solid #b7c4ce; border-radius: 5px; background: white; color: inherit; }
.plot-controls input { width: 250px; max-width: 100%; }
.plot-controls button { cursor: pointer; background: #f1f5f8; }
.plot-controls :focus-visible { outline: 2px solid #075b91; outline-offset: 2px; }
.plot-count { color: #526473; }
.interactive-figure { overflow: visible; }
details { border-top: 1px solid #e3e8ed; padding-top: 16px; }
summary { cursor: pointer; font-weight: 600; }
details[open] > summary { margin-bottom: 16px; }
ul { margin: 0; padding-left: 24px; }
li { padding: 3px 0; overflow-wrap: anywhere; }
.gap-list { max-height: 320px; overflow-y: auto; }
pre { margin: 0; padding: 16px; overflow: auto; background: #f3f5f7;
  font-size: .8rem; line-height: 1.6; }
code { font-family: ui-monospace, monospace; }
footer { margin-top: 24px; font-size: .85rem; overflow-wrap: anywhere; }
@media (max-width: 640px) {
  main { padding: 20px 12px; }
  section { padding: 16px; }
  h1 { font-size: 1.5rem; }
  .facts { grid-template-columns: repeat(2, minmax(0, 1fr)); }
}
@media print {
  :root { background: white; }
  main { max-width: none; padding: 0; }
  nav, .plot-controls, .modebar { display: none; }
  section { border: 0; padding: 16px 0; }
  figure, tr { break-inside: avoid; }
  .table-scroll { overflow: visible; }
  td { white-space: normal; }
  figure img { min-width: 0; }
}
"""

SCRIPT = """
document.querySelectorAll(".interactive-figure").forEach(figure => {
  const plot = figure.querySelector(".plotly-graph-div");
  const input = figure.querySelector("input");
  const count = figure.querySelector(".plot-count");
  const updateCount = () => {
    const visible = plot.data.filter(t => t.visible !== false &&
      t.visible !== "legendonly").length;
    count.textContent = `${visible} / ${plot.data.length} series shown`;
  };
  let pending;
  const update = (hide = false) => {
    clearTimeout(pending);
    const terms = input.value.toLowerCase().split(",")
      .map(s => s.trim()).filter(Boolean);
    const matches = plot.data.map(t => !terms.length ||
      terms.some(term => t.name.toLowerCase().includes(term)));
    Plotly.restyle(plot, {
      visible: matches.map(match => match ? (hide ? "legendonly" : true) : false),
      showlegend: matches
    });
  };
  input.addEventListener("input", () => {
    clearTimeout(pending);
    pending = setTimeout(() => update(), 150);
  });
  figure.querySelectorAll("button").forEach(button => {
    button.addEventListener("click", () => {
      input.value = "";
      update(button.dataset.action === "hide");
    });
  });
  plot.on("plotly_restyle", updateCount);
  updateCount();
});
"""


def _e(value: object) -> str:
    """Format and escape a value at the HTML boundary."""
    return html_lib.escape(_fmt(value) if value is not None else "—")


def _table(title: str, headers: list[str], rows: list[list[object]]) -> str:
    """Scrollable, semantic table; every cell is plain text."""
    if not rows:
        return '<p class="note">No measurements available.</p>'
    head = "".join(f'<th scope="col">{_e(h)}</th>' for h in headers)
    body = "".join(
        "<tr>" + "".join(f"<td>{_e(v)}</td>" for v in row) + "</tr>" for row in rows
    )
    return (
        '<div class="table-scroll" tabindex="0" role="region" '
        f'aria-label="{_e(title)}"><table><caption>{_e(title)}</caption>'
        f"<thead><tr>{head}</tr></thead><tbody>{body}</tbody></table></div>"
    )


def _spread(value: object, scale: float = 1.0) -> str | None:
    """A range of per-VM or per-job values, never a fleet total."""
    values = _dict(value)
    if not values:
        return None
    return " / ".join(
        _fmt(v * scale) if isinstance(v := values.get(k), int | float) else "—"
        for k in ("min", "median", "max")
    )


def _window(item: Row) -> str:
    return f"{_fmt(item.get('start_s'))}–{_fmt(item.get('end_s'))} s"


def _network(result: Row) -> str:
    network = _dict(result.get("network"))
    rows: list[list[object]] = []
    for item in _list(network.get("profiles")):
        for side in ("tx", "rx"):
            values = _dict(item.get(side))
            rates = {k: values.get(f"pps_{k}") for k in ("min", "median", "max")}
            rows.append(
                [
                    int(str(item["profile"])) + 1,
                    _window(item),
                    item.get("pps_per_sender"),
                    side.upper(),
                    f"{_fmt(values.get('reporting'))}/{_fmt(values.get('of'))}",
                    _spread(rates),
                ]
            )
    return (
        f'<p class="note">{_e(network.get("mode"))} · '
        + (f"Focus: {_e(network.get('focus'))} · " if network.get("focus") else "")
        + f"{_e(network.get('senders'))} senders / "
        f"{_e(network.get('receivers'))} receivers</p>"
        + _table(
            "Per-VM packet rates",
            ["Profile", "Window", "Target pps/sender", "Direction", "VMs", "PPS"],
            rows,
        )
        + '<p class="note">PPS: minimum / median / maximum per VM. '
        "VMs: reporting / expected. TX counts TRex frame bytes; RX counts IP bytes, "
        "so their byte rates are not directly comparable.</p>"
    )


def _storage(result: Row) -> str:
    storage = _dict(result.get("storage"))
    rows: list[list[object]] = []
    for item in _list(storage.get("profiles")):
        directions = [d for d in ("read", "write") if _dict(item.get(d))]
        for direction in directions or ["unavailable"]:
            values = _dict(item.get(direction))
            rows.append(
                [
                    item.get("name"),
                    _window(item),
                    direction,
                    f"{values.get('jobs', 0)}/{_fmt(item.get('jobs_expected'))}",
                    _spread(values.get("iops")),
                    _spread(values.get("bw_kib_s"), 1 / 1024),
                    _spread(values.get("p99_ms")),
                ]
            )
    return (
        _table(
            "Per-job fio results",
            ["Profile", "Window", "Direction", "Jobs", "IOPS", "MiB/s", "p99 (ms)"],
            rows,
        )
        + '<p class="note">Values: minimum / median / maximum across jobs, '
        "each over its own runtime. Jobs: reporting / expected. "
        "Missing results are shown as —. Graphs use each job’s logged intervals; "
        "p99 is not a pooled fleet percentile.</p>"
    )


def _figures(result: Row, bundle: Path, group: str, charts: dict[str, str]) -> str:
    figures = []
    for graph in _list(result.get("graphs")):
        name = str(graph.get("file", ""))
        category = next(
            (g for g in ("network", "storage") if name.startswith(f"{g}-")),
            "infrastructure",
        )
        if category != group:
            continue
        title = _e(graph.get("title"))
        path = bundle / "graphs" / name
        if path.is_file():
            data = base64.b64encode(path.read_bytes()).decode()
            image = f'<img alt="{title}" src="data:image/png;base64,{data}">'
        else:
            image = f'<p class="note">{title}: graph file unavailable.</p>'
        if name in charts:
            figures.append(
                f'<figure class="interactive-figure" aria-label="{title}">'
                f"<figcaption><h3>{title}</h3></figcaption>"
                '<div class="plot-controls">'
                '<label>Filter series <input type="search" '
                'placeholder="e.g. vm001, vm002" aria-label="Filter series"></label>'
                '<button type="button" data-action="show">Show all</button>'
                '<button type="button" data-action="hide">Hide all</button>'
                '<span class="plot-count" role="status"></span></div>'
                + charts[name]
                + f"<noscript>{image}</noscript></figure>"
            )
        else:
            figures.append(
                f'<figure tabindex="0" aria-label="{title}">{image}'
                '<figcaption class="note">Static graph; interactive charts need '
                "the run’s metrics/ files.</figcaption></figure>"
            )
    return "".join(figures) or '<p class="note">No time-series graphs available.</p>'


def render_html(run_id: str) -> str | None:
    """The run's HTML report, if the run has a result."""
    bundle = run_dir(run_id)
    result_file = bundle / "report.json"
    if not result_file.is_file():
        return None
    result = json.loads(result_file.read_text())
    run = _dict(result.get("run"))
    fleet = _dict(result.get("fleet"))
    timeline = _dict(result.get("timeline"))
    gaps = list(dict.fromkeys(str(g) for g in result.get("gaps") or []))
    metrics = {
        path.stem: _jsonl(bundle, f"metrics/{path.name}", gaps)
        for path in sorted((bundle / "metrics").glob("*.jsonl"))
    }
    charts = {
        name: fig.to_html(
            full_html=False,
            include_plotlyjs=False,
            div_id=f"plot-{Path(name).stem}",
            default_height="520px",
            config={
                "responsive": True,
                "displaylogo": False,
                "displayModeBar": True,
                "modeBarButtonsToRemove": ["select2d", "lasso2d"],
            },
        )
        for name, fig in plots.interactive(result, metrics).items()
    }
    errors = list(
        dict.fromkeys(
            str(e)
            for e in [
                *cast(list[object], run.get("causes") or []),
                *(result.get("errors") or []),
            ]
        )
    )
    duration = run.get("duration_s")
    start = timeline.get("measured_start")
    when = (
        datetime.fromtimestamp(start, UTC).strftime("%Y-%m-%d %H:%M:%S UTC")
        if isinstance(start, int | float)
        else "Not started"
    )
    facts = [
        ("Execution", run.get("execution")),
        ("Exit code", run.get("code")),
        ("Cleanup", run.get("finalization")),
        (
            "Planned measurement",
            format_duration(duration) if isinstance(duration, int) else None,
        ),
        ("Participants", len(_list(fleet.get("vms")))),
        ("Scheduled start", when),
    ]
    fact_html = "".join(
        f"<div><dt>{_e(k)}</dt><dd>{_e(v)}</dd></div>" for k, v in facts
    )
    outcome = "good" if run.get("code") == 0 else "attention"
    coverage = "attention" if gaps else ""
    sections = []
    workloads = _dict(_dict(result.get("config")).get("workloads"))
    for name, title, summary in (
        ("network", "Network", _network),
        ("storage", "Storage", _storage),
    ):
        enabled = workloads.get("trex" if name == "network" else "fio") is not None
        if name in result or enabled:
            sections.append(
                (name, title, summary(result) + _figures(result, bundle, name, charts))
            )
    sections.append(
        (
            "infrastructure",
            "Hosts & Ceph",
            '<p class="note">Time is relative to the scheduled measurement start. '
            "Dashed markers show profile changes. Each drop line is an individual "
            "counter; counters are not added together.</p>"
            + _figures(result, bundle, "infrastructure", charts),
        )
    )
    notices = ""
    if errors:
        notices += (
            "<h3>Execution errors</h3><ul>"
            + "".join(f"<li>{_e(e)}</li>" for e in errors)
            + "</ul>"
        )
    cleanup = _list(result.get("cleanup"))
    if cleanup:
        notices += "<h3>Cleanup pending</h3>" + _table(
            "Outstanding resources",
            ["Kind", "Name", "Action", "Location"],
            [[r.get(k) for k in ("kind", "name", "action", "where")] for r in cleanup],
        )
    if gaps:
        notices += (
            f"<details><summary>Show all {len(gaps)} data gaps</summary>"
            '<ul class="gap-list">'
            + "".join(f"<li>{_e(g)}</li>" for g in gaps)
            + "</ul></details>"
        )
    sections.insert(
        0 if notices else len(sections),
        ("coverage", "Data coverage", notices or "<p>No data gaps reported.</p>"),
    )
    sections.append(
        (
            "details",
            "Run details",
            "<details><summary>Full text summary & fleet</summary><pre>"
            + html_lib.escape(summary_text(result, full=True))
            + "</pre></details>"
            + "<details><summary>Resolved configuration</summary><pre>"
            + html_lib.escape(json.dumps(result.get("config"), indent=2))
            + "</pre></details>",
        )
    )
    nav = "".join(f'<a href="#{name}">{title}</a>' for name, title, _ in sections)
    body = "".join(
        f'<section id="{name}"><h2>{title}</h2>{content}</section>'
        for name, title, content in sections
    )
    guidance = (
        '<p class="note">Hover for values; drag to zoom; double-click the plot '
        "to reset. Click a legend entry to hide it, or double-click to isolate it. "
        "Filter by VM, job, host or counter; commas match multiple names.</p>"
        if charts
        else ""
    )
    script = f"<script>{SCRIPT}</script>" if charts else ""
    library = f"<script>{get_plotlyjs()}</script>" if charts else ""
    return (
        '<!DOCTYPE html><html lang="en"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width, initial-scale=1">'
        f"<title>Run {_e(run.get('id'))} · Cloud stress</title><style>{STYLE}</style>"
        f"</head><body>{library}<main><header>"
        '<p class="eyebrow">Cloud stress · Run report</p>'
        "<h1>Workload measurements</h1>"
        f'<p class="run-id"><code>{_e(run.get("id"))}</code></p>'
        f'<div class="badges"><span class="badge {outcome}">'
        f"{_e(run.get('execution'))}</span>"
        f'<a class="badge {coverage}" href="#coverage">{len(gaps)} data gaps</a></div>'
        f'<dl class="facts">{fact_html}</dl>'
        f'<nav aria-label="Report sections">{nav}</nav>'
        f"{guidance}</header>{body}<footer>Raw outputs, metric series and graphs: "
        f"<code>{_e(str(bundle))}</code></footer></main>{script}</body></html>\n"
    )
