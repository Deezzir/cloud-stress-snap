"""Self-contained HTML report: the summary text and the run's PNG graphs."""

import base64
import html as html_lib
import json

from campaign.model.state import run_dir
from campaign.report.text import summary_text


def render_html(run_id: str) -> str | None:
    """The run's HTML report, if the run has a result."""
    bundle = run_dir(run_id)
    result_file = bundle / "report.json"
    if not result_file.is_file():
        return None
    result = json.loads(result_file.read_text())
    figures = []
    for graph in result.get("graphs") or []:
        path = bundle / "graphs" / str(graph.get("file"))
        if not path.is_file():
            continue
        data = base64.b64encode(path.read_bytes()).decode()
        title = html_lib.escape(str(graph.get("title")))
        figures.append(
            f"<figure><img alt='{title}' src='data:image/png;base64,{data}'></figure>"
        )
    run = result.get("run") or {}
    title = html_lib.escape(f"Run {run.get('id')}")
    return (
        "<!DOCTYPE html><html><head><meta charset='utf-8'>"
        f"<title>{title}</title><style>"
        "body{font-family:sans-serif;margin:2em;max-width:1100px}"
        "pre{background:#f6f6f6;padding:1em;overflow-x:auto}"
        "figure{margin:1em 0}img{max-width:100%}</style></head><body>"
        f"<pre>{html_lib.escape(summary_text(result, full=True))}</pre>"
        f"{''.join(figures)}"
        f"<p>Raw outputs, metric series and graphs: {html_lib.escape(str(bundle))}</p>"
        "</body></html>\n"
    )
