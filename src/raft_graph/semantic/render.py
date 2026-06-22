"""Render an ExperimentResult as a standalone HTML contract guide.

Turns a grounding run into a self-contained .html file (own CSS, no external
dependencies) -- readable in a browser and usable directly as a blog figure.
Each method shows its contract joined with the structural provenance
(file:line) every entity carries, plus the per-method grounding verdict.

All dynamic text is HTML-escaped: Go signatures contain '<', '>', '*' (e.g.
'<-chan Ready', '[]*pb.Entry') that would otherwise break the markup.
"""

from __future__ import annotations

import html
import os

from .extractor import ExperimentResult


def _esc(value) -> str:
    return html.escape(str(value)) if value is not None else ""


def _prov(entity) -> str:
    if entity is None:
        return ""
    return f"{entity.file.split('/')[-1]}:{entity.line}"


_CSS = """
:root{--bg:#fff;--surface:#faf9f5;--text:#1c1b1a;--muted:#6b6a64;--faint:#9a988f;
--border:rgba(0,0,0,.12);--ok-bg:#e1f5ee;--ok-fg:#0f6e56;--bad-bg:#fceaea;--bad-fg:#a32d2d;
--mono:ui-monospace,SFMono-Regular,Menlo,monospace;--sans:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,Helvetica,sans-serif}
@media(prefers-color-scheme:dark){:root{--bg:#1f1e1c;--surface:#161513;--text:#ecebe6;
--muted:#a3a199;--faint:#76746c;--border:rgba(255,255,255,.14);--ok-bg:#0c3a2f;--ok-fg:#5dcaa5;
--bad-bg:#3d1414;--bad-fg:#f09595}}
*{box-sizing:border-box}
body{margin:0;background:var(--surface);color:var(--text);font-family:var(--sans);line-height:1.5;padding:24px}
.card{max-width:760px;margin:0 auto;background:var(--bg);border:.5px solid var(--border);border-radius:12px;padding:20px 24px}
.hdr{display:flex;align-items:center;justify-content:space-between;gap:12px;flex-wrap:wrap}
.name{font-size:16px;font-weight:600}
.muted{font-size:13px;color:var(--muted)}
.prov{font-size:12px;color:var(--faint);font-family:var(--mono)}
.implline{font-size:13px;color:var(--muted);margin:6px 0 4px;padding-bottom:12px;border-bottom:.5px solid var(--border)}
.mono{font-family:var(--mono)}
.pill{display:inline-flex;align-items:center;gap:5px;font-size:13px;font-weight:600;padding:4px 10px;border-radius:8px}
.chip{font-size:12px;font-weight:600;padding:2px 9px;border-radius:8px;white-space:nowrap}
.ok{background:var(--ok-bg);color:var(--ok-fg)}
.bad{background:var(--bad-bg);color:var(--bad-fg)}
.m{padding:13px 0;border-bottom:.5px solid var(--border)}
.m:last-child{border-bottom:none;padding-bottom:0}
.mhead{display:flex;align-items:center;justify-content:space-between;gap:10px}
.mname{font-size:14px;font-weight:600;font-family:var(--mono)}
.sig{font-size:12px;color:var(--faint);font-family:var(--mono);margin:3px 0 6px;word-break:break-word}
.promise{font-size:14px;color:var(--text)}
.impl{font-size:13px;color:var(--muted);margin-top:5px}
.impl .mono{color:var(--text)}
.foot{font-size:12px;color:var(--faint);margin:14px auto 0;max-width:760px;text-align:center}
"""


def render_result_html(result: ExperimentResult) -> str:
    s = result.slice_
    report = result.report
    parsed = result.parsed or {"methods": []}
    iface, impl = s.interface, s.impl

    imeth = {m.id: m for m in s.interface_methods if m is not None}
    pmeth = {m.id: m for m in s.impl_methods if m is not None}
    expected = set(report.expected_methods) if report else set(imeth)

    viol_by: dict = {}
    for v in (report.violations if report else []):
        viol_by.setdefault(v.get("interface_method_id"), []).append(v)

    grounded = bool(report and report.grounded)
    total = len(expected)
    if grounded:
        badge = f'<span class="pill ok">&#10003; grounded &middot; {total} / {total} methods</span>'
    else:
        n_iss = len(report.violations) if report else 0
        n_miss = len(report.missing_methods) if report else 0
        badge = f'<span class="pill bad">{n_iss} issue(s) &middot; {n_miss} missing</span>'

    rows = []
    for item in parsed.get("methods", []):
        im_id = item.get("interface_method_id")
        im = imeth.get(im_id)
        fb_id = item.get("fulfilled_by_id")
        fb = pmeth.get(fb_id)

        if im_id not in expected:
            chip = '<span class="chip bad">invented</span>'
        elif viol_by.get(im_id):
            chip = f'<span class="chip bad">{_esc(viol_by[im_id][0]["type"])}</span>'
        else:
            chip = '<span class="chip ok">&#10003; grounded</span>'

        rows.append(
            '<div class="m">'
            f'<div class="mhead"><span class="mname">{_esc(im.name if im else im_id)}</span>{chip}</div>'
            f'<div class="sig">{_esc(im.signature if im and im.signature else "")}</div>'
            f'<div class="promise">{_esc(item.get("semantic_contract", ""))}</div>'
            f'<div class="impl">&rarr; fulfilled by <span class="mono">{_esc(fb.name if fb else fb_id)}</span> '
            f'<span class="prov">{_esc(_prov(fb))}</span></div>'
            '</div>'
        )

    return (
        "<!DOCTYPE html><html lang=\"en\"><head><meta charset=\"utf-8\">"
        f"<meta name=\"viewport\" content=\"width=device-width,initial-scale=1\">"
        f"<title>{_esc(iface.name)} — structural grounding</title>"
        f"<style>{_CSS}</style></head><body>"
        '<div class="card">'
        '<div class="hdr">'
        f'<div><span class="name">{_esc(iface.name)}</span> '
        f'<span class="muted">interface &middot; <span class="mono">{_esc(_prov(iface))}</span></span></div>'
        f'{badge}</div>'
        f'<div class="implline">implemented by <span class="mono">{_esc(impl.name)}</span> '
        f'&middot; <span class="prov">{_esc(_prov(impl))}</span></div>'
        f'{"".join(rows)}'
        '</div>'
        '<p class="foot">contracts interpreted from signatures; "grounded" = every cited entity exists '
        'and the implementation mapping matches the Go compiler.</p>'
        "</body></html>"
    )


def write_result_html(result: ExperimentResult, out_dir: str = "out") -> str:
    os.makedirs(out_dir, exist_ok=True)
    path = os.path.join(out_dir, f"{result.slice_.interface.name}_grounding.html")
    with open(path, "w", encoding="utf-8") as f:
        f.write(render_result_html(result))
    return path
