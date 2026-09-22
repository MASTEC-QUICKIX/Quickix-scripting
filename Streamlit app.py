"""
Streamlit app.py — QUICKIX Pre-Script Validation (Streamlit port)

Single input page (CIQ + EDP required, RFDS PDF + Pre kget-all logs
optional) -> "Run Validation" runs the full pipeline ONCE and stores it in
session_state -> tabbed results view (RFDS Validation / Audit / EDP
Validator / Consolidated Report), every tab reads
from that one stored run. "New Validation Run" clears state and returns to
the input page. This matches QUICKIX_Pre-Script_Validation.html's own
flow: inputs are on the first page only, "Run Validation" swaps to the
tabbed results, and there is no way back to the inputs except starting a
new run.

Nothing here invents new validation logic — every check is an existing,
already-confirmed function from checks_node.py / checks_sector.py /
rfds_extract.py / pre_extract.py / ciq_edp_reader.py / rrnrbl_checklist.py /
ciq_view.py / amos_view.py / antenna_resolve.py. Fixes/integrations versus
the prior version of this file:

  - run_validation.run() returns 11 values; this file now unpacks all 11
    (was silently truncated to 7, which crashed the Consolidated Report
    and Checklist buttons the moment they were used).
  - RET Antenna Checklist and the RRNRBL Checklist were conflated onto one
    tab. RRNRBL now lives only inside Consolidated Report (matching the
    HTML tool's layout); the RET Antenna Checklist tab has since been
    removed entirely.
  - ciq_view.py and amos_view.py (present in the repo, never imported
    anywhere) now drive the CIQ Checks / Pre checks (AMOS) tables — they
    are the purpose-built table builders for exactly this, replacing
    cruder inline table assembly that duplicated their job.
  - edp_checks.py is intentionally NOT used: it is an earlier, superseded
    EDP Validator with cross-node/"unexpected nodes" sections that were
    explicitly dropped from scope; rrnrbl_checklist.py's simpler per-check
    functions are the current design and are what's wired in everywhere.
  - Every comparison table is now rendered with a coloured, bordered HTML
    table (MATCH/PASS green, MISMATCH/FAIL red, INFO blue, MANUAL amber,
    SKIPPED/unknown grey) instead of a plain st.dataframe — same palette
    the PDF report and the RRNRBL checklist already use, so the look is
    consistent across every surface this tool produces.
"""
import os
import re
import html
import tempfile

import streamlit as st

import ciq_edp_reader as cer
import checks_sector as cs
import rfds_extract as rf
import pre_extract as pe
import run_validation as rv
import rrnrbl_checklist as rc
import antenna_resolve as ar
import warnings_text as wt
import ciq_view as cv
import amos_view as av
from rfds_verification_summary import build_rfds_verification_summary
from engineer_comments import build_engineer_comments, extract_bands_from_comments, extract_nodes_from_audit
from cr_description import build_cr_description, build_radio_ret_email

st.set_page_config(page_title="QUICK IX", layout="wide", page_icon="📡")

# ══════════════════════════════════════════════════════════════════════
# Styling — ported from the QUICKIX report-feature branch's own CSS
# (sticky navy topbar with MAS/TEC logo + credit, gradient buttons, white
# bordered cards for st.container(border=True)) so both features share one
# visual language ahead of being combined, plus this file's own bordered/
# colour-coded HTML table renderer for every comparison table. Table
# colours match the RRNRBL checklist's palette and the PDF report's header
# banner (navy #101F90 / #dde3f7), so a status reads the same everywhere.
# ══════════════════════════════════════════════════════════════════════
st.markdown("""
<style>
.stApp { background: linear-gradient(180deg, #eef3fa 0%, #f7f9fc 100%); }
.block-container { padding-top: 1rem; padding-left: 2rem; padding-right: 2rem; max-width: 100%; }
.qkx-topbar {
  position: sticky; top: 0; z-index: 999;
  display: flex; justify-content: space-between; align-items: center;
  padding: 0.9rem 1.75rem; margin: -1rem -1rem 1.5rem -1rem;
  background: linear-gradient(90deg, #011b36 0%, #012a4e 100%);
  border-bottom: 1px solid rgba(255,91,36,0.55);
  box-shadow: 0 4px 18px rgba(0,0,0,0.2);
}
.qkx-topbar .qkx-logo { font-size: 1.3rem; font-weight: 900; color: #ffffff; letter-spacing: 1px; }
.qkx-topbar .qkx-logo span { color: #ffffff; }
.qkx-topbar .qkx-title { font-size: 0.95rem; color: #cfe0f5; margin-left: 14px; font-weight: 600; }
.qkx-topbar .qkx-credit { font-size: 0.78rem; color: #cfe0f5; text-align: right; line-height: 1.3; }
div[data-testid="stButton"] button {
  border-radius: 10px; font-weight: 700; border: 1.5px solid #013a6b;
  background: linear-gradient(135deg, #024ea4, #013a6b); color: #ffffff;
  box-shadow: 0 3px 8px rgba(1,42,78,0.25);
  transition: transform 0.15s ease, box-shadow 0.15s ease, border-color 0.15s ease;
}
div[data-testid="stButton"] button:hover {
  border-color: #ff5b24; color: #ffffff; transform: translateY(-1px);
  box-shadow: 0 6px 14px rgba(255,91,36,0.35);
}
div[data-testid="stButton"] button:active { transform: translateY(0); }
div[data-testid="stVerticalBlockBorderWrapper"] {
  background: #ffffff !important; border: 1px solid #dde5ef !important;
  border-radius: 12px !important; box-shadow: 0 2px 10px rgba(1,42,78,0.06);
  padding: 4px 2px;
}
/* Compact mode: Streamlit's default ~1rem gap between stacked elements
   (section title, table, next bordered container, ...) added up fast on
   tabs with several stacked container(border=True) blocks (RFDS
   Validation had ~6 in a row) — tighten the gap and the blocks' own
   margins so the same content takes noticeably less vertical space. */
div[data-testid="stVerticalBlock"] { gap: 0.5rem; }
div[data-testid="stVerticalBlockBorderWrapper"] > div { gap: 0.35rem; }
.stTabs [data-baseweb="tab-list"] {
  gap: 4px; border-bottom: 2px solid #dde5ef; padding-bottom: 0;
}
.stTabs [data-baseweb="tab"] {
  font-weight: 700; font-size: 13.5px; color:#475569;
  padding: 8px 16px; border-radius: 8px 8px 0 0;
}
.stTabs [aria-selected="true"] {
  color:#101F90 !important; background:#eef1fb;
  box-shadow: inset 0 -3px 0 #101F90;
}
div[data-testid="stExpander"] details {
  border:1px solid #dde5ef !important; border-radius:10px !important;
  background:#fff; box-shadow:0 2px 8px rgba(1,42,78,.05); margin-bottom:10px;
}
div[data-testid="stExpander"] summary {
  font-weight:700 !important; font-size:13.5px !important; color:#101F90 !important;
  padding:11px 14px !important;
}
div[data-testid="stExpander"] summary:hover { background:#f4f7fc; border-radius:10px; }
.qkx-stat {
  text-align:center; border:1px solid #dde5ef; border-radius:10px; padding:10px 8px;
  background:#fff; box-shadow:0 2px 8px rgba(1,42,78,.05); font-size:12.5px;
}
.qkx-stat b { color:#64748b; font-size:10.5px; text-transform:uppercase; letter-spacing:.05em; }
.qkx-table-wrap {
  overflow-x:auto; border:1px solid #dde5ef; border-radius:0 0 10px 10px;
  margin: 0 0 8px 0; border-top:none; box-shadow:0 2px 8px rgba(1,42,78,.05);
}
.qkx-table { width:100%; border-collapse:collapse; font-size:12.8px; line-height:1.35; }
.qkx-table th {
  background:#101F90; color:#ffffff; font-weight:700; text-align:left;
  padding:6px 10px; border:none; border-right:1px solid rgba(255,255,255,.14);
  white-space:nowrap; font-size:11.5px; letter-spacing:.03em; text-transform:uppercase;
  position:sticky; top:0;
}
.qkx-table td { padding:5px 10px; border-bottom:1px solid #eef1f6; vertical-align:middle; }
.qkx-table tbody tr:hover td { background:rgba(16,31,144,.04); }
/* Mismatch rows are called out the same way in EVERY table/tab: red tint,
   red text and a red left marker bar, so a failure reads identically
   wherever it appears rather than depending on each table's own styling. */
.qkx-table tbody tr.qkx-row-bad td { background:#fdeaea !important; color:#9f1d1d; font-weight:600; }
.qkx-table tbody tr.qkx-row-bad td:first-child { box-shadow: inset 3px 0 0 #dc2626; }
.qkx-table tbody tr.qkx-row-bad:hover td { background:#fbdcdc !important; }
.qkx-table tbody tr.qkx-row-good td { background:#e8f7ef !important; }
.qkx-table tbody tr.qkx-row-good:hover td { background:#dcf2e6 !important; }
.qkx-table.qkx-zebra tbody tr:nth-child(even) td { background:#f8fafc; }
.qkx-table.qkx-zebra tbody tr:hover td { background:rgba(16,31,144,.06); }
.qkx-table td.qkx-group-start, .qkx-table th.qkx-group-start { border-left:2px solid #94a3b8; }
.qkx-empty {
  padding:16px; color:#64748b; font-style:italic; font-size:13px;
  background:#fff; border:1px dashed #cbd5e1; border-radius:10px; text-align:center;
}
.qkx-section-title {
  font-weight:700; font-size:13.5px; color:#fff; margin: 6px 0 0 0;
  padding:8px 14px; border:none;
  border-radius:10px 10px 0 0;
  background: linear-gradient(90deg, #101F90 0%, #1e3a8a 100%);
  box-shadow:0 2px 6px rgba(16,31,144,.16);
}
.qkx-warn-line {
  padding:8px 12px; margin-bottom:6px; border-radius:8px;
  background:#fff5f5; color:#991b1b; font-size:12.5px;
  border:1px solid #fecaca; border-left:3px solid #dc2626;
}
.qkx-cat-banner {
  background: linear-gradient(90deg, #101F90 0%, #1e3a8a 100%); color:#fff;
  font-weight:700; font-size:13px; letter-spacing:.02em;
  padding:9px 14px; border-radius:8px 8px 0 0; margin-top:22px;
  display:flex; justify-content:space-between; align-items:center;
  box-shadow:0 2px 6px rgba(16,31,144,.18);
}
.qkx-cat-counts { display:flex; gap:6px; align-items:center; }
.qkx-cat-count {
  font-size:10.5px; font-weight:700; padding:2px 8px; border-radius:999px;
  background:rgba(255,255,255,.16); color:#fff; white-space:nowrap;
}
.qkx-cat-count.ok   { background:#059669; }
.qkx-cat-count.bad  { background:#dc2626; }
.qkx-cat-count.man  { background:#d97706; }
.qkx-cat-count.na   { background:rgba(255,255,255,.22); }

/* Status chip — every check row carries one, so a status is readable at a
   glance instead of relying on a pale row background alone. */
.qkx-chip {
  display:inline-block; font-size:10px; font-weight:800; letter-spacing:.05em;
  padding:3px 9px; border-radius:999px; text-transform:uppercase;
  white-space:nowrap; border:1px solid transparent;
}
.qkx-chip.match    { background:#d1fae5; color:#065f46; border-color:#6ee7b7; }
.qkx-chip.mismatch { background:#fee2e2; color:#991b1b; border-color:#fca5a5; }
.qkx-chip.manual   { background:#fef3c7; color:#92400e; border-color:#fcd34d; }
.qkx-chip.info     { background:#dbeafe; color:#1d4ed8; border-color:#93c5fd; }
.qkx-chip.unknown  { background:#f1f5f9; color:#64748b; border-color:#cbd5e1; }

.qkx-sec-sub {
  font-size:13px; font-weight:700; color:#1e3a5f;
  margin:16px 0 6px 0; padding-bottom:4px;
  border-bottom:1px solid #dde5ef;
}
.qkx-count-pill {
  font-size:11.5px; color:#334155; margin-right:6px;
  background:#fff; border:1px solid #dde5ef; border-radius:999px;
  padding:4px 11px; display:inline-block; margin-bottom:4px;
}
.qkx-title-badge {
  background:#101F90; color:#fff; font-weight:700; font-size:11px;
  padding:3px 10px; border-radius:999px; white-space:nowrap;
}
.qkx-manual-label {
  font-size:13px; font-weight:600; color:#0f1720; margin-bottom:6px;
  display:flex; align-items:center; gap:8px;
}
.qkx-manual-tag {
  background:#fef3c7; color:#92400e; font-size:9.5px; font-weight:800;
  padding:2px 8px; border-radius:999px; letter-spacing:.05em;
  border:1px solid #fcd34d;
}
.qkx-manual-item {
  font-size:12.8px; font-weight:600; color:#0f1720;
  padding:2px 0 6px 0;
}
.qkx-manual-detail { font-size:11.5px; color:#64748b; font-weight:400; }
.qkx-sub-header {
  font-size:12px; font-weight:800; color:#1e3a8a; text-transform:uppercase;
  letter-spacing:.06em; margin:14px 0 6px 0;
  border-left:3px solid #101F90; padding:3px 0 3px 9px;
  background:linear-gradient(90deg,#eef1fb 0%,rgba(238,241,251,0) 100%);
}
/* Spreadsheet-style grid for the RRNRBL checklist: real vertical column
   borders on every cell (qkx-table's default only has horizontal row
   borders), so merged Category/Sub-section cells (via rowspan) read as
   genuine grouped spreadsheet cells rather than a plain list. */
.qkx-grid td, .qkx-grid th {
  border-right:1px solid #dde5ef;
}
.qkx-grid td:first-child, .qkx-grid td:nth-child(2) {
  border-right:2px solid #cbd5e1;
}
.qkx-grid-wrap { margin-bottom:16px; }
</style>
<div class="qkx-topbar">
  <div><span class="qkx-logo">MAS<span>TEC</span></span><span class="qkx-title">QUICK IX — Pre-Script Validation</span></div>
  <div class="qkx-credit">Made by <b>AKSHATHA KALLUR</b><br>Powered by <b>MASTEC</b></div>
</div>
""", unsafe_allow_html=True)

STATUS_COLORS = {
    "MATCH": ("#065f46", "#d1fae5"), "match": ("#065f46", "#d1fae5"), "PASS": ("#065f46", "#d1fae5"),
    "MISMATCH": ("#991b1b", "#fee2e2"), "mismatch": ("#991b1b", "#fee2e2"), "FAIL": ("#991b1b", "#fee2e2"),
    "SKIPPED": ("#64748b", "#f1f5f9"), "unknown": ("#64748b", "#f1f5f9"),
    "na": ("#64748b", "#f1f5f9"), "N/A": ("#64748b", "#f1f5f9"),
    "INFO": ("#1d4ed8", "#dbeafe"), "info": ("#1d4ed8", "#dbeafe"),
    "manual": ("#92400e", "#fef3c7"), "MANUAL": ("#92400e", "#fef3c7"), "EXPECTED": ("#92400e", "#fef3c7"),
}
DEFAULT_COLOR = ("#334155", "#ffffff")
STATUS_LABEL = {"match": "match", "mismatch": "mismatch", "manual": "manual",
                "unknown": "no data", "na": "n/a", "info": "info"}


def esc(v):
    return html.escape("" if v is None else str(v))


def _row_cls(status_value):
    """Uniform row emphasis across EVERY table in every tab: anything that
    failed gets the same red treatment, anything that passed the same green,
    regardless of which of the several status spellings a given check
    happens to emit (MISMATCH/mismatch/FAIL, MATCH/match/PASS)."""
    v = str(status_value or "").strip().upper()
    if v in ("MISMATCH", "FAIL", "WARN"):
        return "qkx-row-bad"
    if v in ("MATCH", "PASS"):
        return "qkx-row-good"
    return ""


def render_table(rows, columns=None, status_key="status", empty_msg="No data.", html_cols=None):
    """rows: list[dict]. Bordered HTML table, each row's background/text
    colour driven by rows[i][status_key]. columns: optional [(key,label),
    ...] order; defaults to the first row's own key order. status_key=None
    disables colouring (plain bordered table). html_cols: optional set of
    column keys whose value is already-safe inline HTML (e.g. a coloured
    <span>) built by the caller and should be emitted as-is instead of
    HTML-escaped — every other column keeps the normal esc() treatment."""
    if not rows:
        return f'<div class="qkx-empty">{esc(empty_msg)}</div>'
    if columns is None:
        columns = [(k, k.replace("_", " ").title()) for k in rows[0].keys()]
    html_cols = html_cols or set()
    head = "".join(f"<th>{esc(label)}</th>" for _, label in columns)
    body = []
    for r in rows:
        sv = str(r.get(status_key, "")) if status_key else ""
        color, bg = STATUS_COLORS.get(sv, DEFAULT_COLOR) if status_key else DEFAULT_COLOR
        cells = "".join(
            f"<td>{r.get(k, '') if k in html_cols else esc(r.get(k, ''))}</td>" for k, _ in columns
        )
        body.append(f'<tr class="{_row_cls(sv)}" style="background:{bg};color:{color};">{cells}</tr>')
    # Zebra striping only on uncoloured tables: a `td` background paints over
    # the row's inline `tr` background, so applying it globally would wash out
    # every status colour.
    zebra = " qkx-zebra" if not status_key else ""
    return (f'<div class="qkx-table-wrap"><table class="qkx-table{zebra}"><thead><tr>{head}</tr></thead>'
            f'<tbody>{"".join(body)}</tbody></table></div>')


def render_table_with_comments(rows, columns, status_key="status", note_key="note", bad_value="MISMATCH"):
    """Row background/text colour driven by rows[i][status_key] (same
    green/red/etc. palette as every other table), ending in one 'Comments'
    column holding the note text — no separate Status/Note columns."""
    if not rows:
        return '<div class="qkx-empty">No data.</div>'
    head = "".join(f"<th>{esc(label)}</th>" for _, label in columns) + '<th style="min-width:170px;">Comments</th>'
    body = []
    for r in rows:
        sv = str(r.get(status_key, ""))
        color, bg = STATUS_COLORS.get(sv, DEFAULT_COLOR)
        cells = "".join(f"<td>{esc(r.get(k, ''))}</td>" for k, _ in columns)
        cells += f'<td style="min-width:170px;">{esc(r.get(note_key, ""))}</td>'
        body.append(f'<tr class="{_row_cls(sv)}" style="background:{bg};color:{color};">{cells}</tr>')
    return (f'<div class="qkx-table-wrap"><table class="qkx-table"><thead><tr>{head}</tr></thead>'
            f'<tbody>{"".join(body)}</tbody></table></div>')


# ── Pre vs Post row-type palette — mirrors QUICKIX HTML's .new/.delete/
# .change/.nochange row classes exactly (background colours match the
# screenshots: pale green=new, pale red=delete, pale amber=change/moved,
# plain white=nochange). ──
PRE_POST_ROW_COLORS = {
    "new": "#d1fae5", "delete": "#fee2e2", "change": "#fef3c7", "nochange": "#ffffff",
}


def render_node_pre_post_table(rows):
    """Node / Status / PTP, whole-row background from row['type']."""
    if not rows:
        return '<div class="qkx-empty">Run validation with Pre logs and a CIQ to populate this.</div>'
    head = "".join(f"<th>{h}</th>" for h in ("Node", "Status", "PTP"))
    body = []
    for r in rows:
        bg = PRE_POST_ROW_COLORS.get(r["type"], "#ffffff")
        status_color = "#b45309" if r["type"] == "change" else "#0f1720"
        ptp_color = "#991b1b" if r.get("_ptp_flag") else "#0f1720"
        body.append(
            f'<tr style="background:{bg};"><td>{esc(r["node"])}</td>'
            f'<td style="color:{status_color};font-weight:600;">{esc(r["status"])}</td>'
            f'<td style="color:{ptp_color};font-weight:600;">{esc(r["ptp"])}</td></tr>'
        )
    return (f'<div class="qkx-table-wrap"><table class="qkx-table"><thead><tr>{head}</tr></thead>'
            f'<tbody>{"".join(body)}</tbody></table></div>')


def _pre_post_summary_pills(summary):
    """New/Deleted/Moved/No Change count pills, same labels+order as the
    HTML's own badge row above each LTE/5G Pre vs Post table."""
    items = [("new", "New", "#059669"), ("deleted", "Deleted", "#dc2626"),
             ("moved", "Moved", "#b45309"), ("nochange", "No Change", "#334155")]
    return "".join(
        f'<span class="qkx-count-pill"><b style="color:{color}">{summary.get(key, 0)}</b> {label}</span>'
        for key, label, color in items
    )


def render_cell_pre_post_table(rows, field_columns):
    """field_columns: list of (value_key, ok_key, label) for the PRE|POST
    comparison columns (e.g. ('sc','_sc_ok','SC')) — each cell coloured
    green/red from its own ok flag (None -> plain, used for Deleted rows
    where there's nothing to compare). Row background still follows
    row_type like the node table, but lighter, since the HTML also tints
    New/Deleted/Moved rows while still colouring individual mismatched
    fields red within them."""
    if not rows:
        return '<div class="qkx-empty">No data.</div>'
    head = "".join(f"<th>{h}</th>" for h in ("Node", "Cell")) \
        + "".join(f"<th>{esc(label)}</th>" for _, _, label in field_columns) \
        + "".join(f"<th>{h}</th>" for h in ("Link", "Comments"))
    body = []
    for r in rows:
        row_bg = PRE_POST_ROW_COLORS.get(r["row_type"], "#ffffff")
        cells = f"<td>{esc(r['node'])}</td><td><b>{esc(r['cell'])}</b></td>"
        for val_key, ok_key, _ in field_columns:
            ok = r.get(ok_key)
            if ok is None:
                cells += f"<td>{esc(r.get(val_key, '-'))}</td>"
            else:
                color = "#059669" if ok else "#dc2626"
                cells += f'<td style="color:{color};font-weight:700;">{esc(r.get(val_key, "-"))}</td>'
        link_ok = r.get("_link_ok")
        if link_ok is None:
            link_cell = f"<td>{esc(r.get('link', '-'))}</td>"
        else:
            link_color = "#059669" if link_ok else "#dc2626"
            link_cell = f'<td style="color:{link_color};font-weight:700;">{esc(r.get("link", "-"))}</td>'
        cells += link_cell + f"<td>{esc(r.get('comment', ''))}</td>"
        body.append(f'<tr style="background:{row_bg};">{cells}</tr>')
    return (f'<div class="qkx-table-wrap"><table class="qkx-table"><thead><tr>{head}</tr></thead>'
            f'<tbody>{"".join(body)}</tbody></table></div>')


def _sheet_mentions_cell(ciq_wb, sheet_name, cell_id):
    """True if any cell in the given CIQ sheet contains cell_id as a
    substring anywhere — mirrors the HTML's own Antenna Info / Losses &
    Delays presence check (antenna.some(r => Object.values(r).some(v =>
    String(v).includes(cellId)))), not an RFDS lookup."""
    if not cell_id or sheet_name not in ciq_wb.sheetnames:
        return False
    for row in cer.sheet_rows_as_dicts(ciq_wb[sheet_name]):
        for v in row.values():
            if v is not None and cell_id in str(v):
                return True
    return False


def build_rfds_grouped_rows(results, ciq_wb, rfds_pages, rfds_bytes=None):
    """One row per cell, merging Cell verification / RRU verification /
    Antenna verification / Cell id / Antenna info / Losses & Delays /
    Warning — matches the confirmed HTML grouped-header table, including
    its exact pass/fail criteria: RRH match, antenna match-or-N/A, cell ID
    match, Antenna Info found (CIQ 'Antenna Information' sheet mentions
    the cell), and Losses & Delays found (CIQ 'Losses and Delays' sheet
    mentions the cell) unless the antenna is AIR-series, where loss data
    isn't mandatory."""
    cell_map = {}
    for r in results.get("cells_vs_rfds", []):
        cell_map.setdefault(r["cell"], {})["cv"] = r
    for r in results.get("radio_type", []):
        cell_map.setdefault(r["cell"], {})["rt"] = r
    for r in results.get("cell_id_vs_rfds_rcn", []):
        cell_map.setdefault(r["cell"], {})["ci"] = r

    # Antenna model: 'RF Inventory Details (Final)' filtered to
    # EquipmentType=='ANTENNA' — the authoritative per-antenna record
    # (Model + Linked Cells explicitly), not inferred from Sec-Pos position
    # in 'Port Level Details'. AIR-series radios have no separate antenna
    # row here (antenna is integrated into the radio) — those cells simply
    # get no RFDS antenna entry, which is correct, not a gap.
    rf_antennas = rf.extract_rf_inventory_antennas(rfds_pages, rfds_bytes) if rfds_pages is not None else {}
    ant_by_cell = {}
    # LTE cells: 'eUtran Parameters' / 'antenna model'. 5G cells: '5G Info' /
    # 'Antenna Type' — the same field under a different column name on a
    # different sheet. Without the 5G leg, every NR cell fell through to the
    # 'no antenna row' branch and rendered as '—' in BOTH the RFDS and CIQ
    # antenna columns, even though extract_rf_inventory_antennas() had
    # already found their antenna (confirmed: HXON001791_N002A_1 etc. are
    # present in the RF Inventory 'Linked Cells' list, and were being
    # discarded here rather than never extracted).
    for sheet, cell_col, ant_col in (("eUtran Parameters", "EutranCellFDDId", "antenna model"),
                                      ("5G Info", "NRCellDU", "Antenna Type")):
        if sheet not in ciq_wb.sheetnames:
            continue
        for r in cer.sheet_rows_as_dicts(ciq_wb[sheet]):
            cell = r.get(cell_col)
            if not cell or cell in ant_by_cell:
                continue
            ciq_ant = r.get(ant_col)
            ant_row = rf_antennas.get(cell)
            tier, detail = ar.resolve_antenna(ciq_ant, ant_row["model"] if ant_row else None)
            ant_by_cell[cell] = {"ciq": ciq_ant or "—", "rfds": (ant_row or {}).get("model", "NOT FOUND"),
                                  "tier": tier, "found": ant_row is not None}

    rows = []
    for cell, parts in cell_map.items():
        cv, rt, ci = parts.get("cv", {}), parts.get("rt", {}), parts.get("ci", {})
        an = ant_by_cell.get(cell, {})
        cell_status = cv.get("status", "SKIPPED")
        rru_status = rt.get("status", "SKIPPED")
        # Cell id here is CIQ vs RFDS ONLY — now check_cell_id_vs_rfds_rcn's
        # own dedicated result (checks_sector.py), computed the same way
        # this table always needed it (ciq == rfds_rcn, no Pre involved).
        # An earlier version reused check_cell_id_vs_rfds's THREE-way
        # verdict (match = (ciq==rfds) and (pre=='NA' or pre==ciq)), which
        # dragged the Pre-vs-CIQ comparison in and flagged rows red while
        # showing two IDENTICAL numbers (confirmed: FCON094120_N005B_1/
        # N005C_1, RFDS 52 / CIQ 52, red) — recomputing inline fixed the
        # display but left two logic paths that could silently disagree;
        # both now read from the one canonical RCN check.
        ci_status = ci.get("status")
        cellid_status = ci_status if ci_status in ("MATCH", "MISMATCH") else "SKIPPED"
        ant_tier = an.get("tier")
        if not an:
            ant_status = "SKIPPED"
        elif not an.get("found"):
            ant_status = "MANUAL"  # amber — RFDS has no antenna data at all ("N/A", not a real mismatch)
        elif ant_tier in ("EXACT", "NORMALIZED", "SUFFIX", "TRUNCATED"):
            ant_status = "MATCH"
        else:
            ant_status = "MISMATCH"

        # Once the cell itself doesn't exist on one side (MISMATCH here means
        # "not found in CIQ" / "not found in RFDS", not a real field mismatch),
        # every downstream sub-check (RRU, Antenna, Cell ID, Antenna Info,
        # Losses & Delays) is meaningless for a cell that isn't there — skip
        # them all and report only "Cell mismatch" (per confirmed design rule).
        if cell_status == "MISMATCH":
            rru_status = "SKIPPED"
            ant_status = "SKIPPED"
            cellid_status = "SKIPPED"
            ant_info_found = None
            loss_found = None
            loss_mandatory = False
            loss_status = "SKIPPED"
            loss_display = "N/A"
            warnings = ["Cell mismatch"]
            fail = True
        else:
            is_air = str(an.get("rfds") or "").upper().startswith("AIR") or str(an.get("ciq") or "").upper().startswith("AIR")
            ant_info_found = _sheet_mentions_cell(ciq_wb, "Antenna Information", cell)
            loss_found = _sheet_mentions_cell(ciq_wb, "Losses and Delays", cell)
            loss_mandatory = not is_air
            loss_status = "MATCH" if loss_found else ("MANUAL" if not loss_mandatory else "MISMATCH")
            loss_display = "FOUND" if loss_found else ("N/A" if not loss_mandatory else "NOT FOUND")

            warnings = []
            if rru_status == "MISMATCH":
                warnings.append("RRU mismatch")
            if ant_status == "MISMATCH":
                warnings.append("Antenna mismatch")
            if cellid_status == "MISMATCH":
                warnings.append("Cell ID mismatch (CIQ vs RFDS)")
            if not ant_info_found:
                warnings.append("Antenna Info missing")
            if loss_mandatory and not loss_found:
                warnings.append("Losses & Delays missing")

            fail = any(s == "MISMATCH" for s in (rru_status, ant_status, cellid_status)) \
                or not ant_info_found or (loss_mandatory and not loss_found)

        rows.append({
            "node": cv.get("node") or rt.get("node") or ci.get("node") or "",
            "cell_rfds": cv.get("rfds_cell", "—"), "cell_ciq": cv.get("ciq_cell", "—") or cell, "cell_status": cell_status,
            "rru_rfds": rt.get("rfds", "—"), "rru_ciq": rt.get("ciq", "—"), "rru_status": rru_status,
            "ant_rfds": an.get("rfds", "—"), "ant_ciq": an.get("ciq", "—"), "ant_status": ant_status,
            "cellid_rfds": ci.get("rfds_rcn", "—"), "cellid_ciq": ci.get("ciq", "—"), "cellid_status": cellid_status,
            "ant_info": "N/A" if ant_info_found is None else ("FOUND" if ant_info_found else "NOT FOUND"),
            "ant_info_status": "SKIPPED" if ant_info_found is None else ("MATCH" if ant_info_found else "MISMATCH"),
            "losses_delays": loss_display, "losses_status": loss_status,
            "warning": "; ".join(warnings) if warnings else "—",
            "overall": "FAIL" if fail else "PASS",
        })
    rows.sort(key=lambda r: r["cell_ciq"] or "")
    return rows


def render_rfds_grouped_table(rows):
    if not rows:
        return '<div class="qkx-empty">No data.</div>'

    def gcell(val, status, group_start=False):
        color, bg = STATUS_COLORS.get(status, DEFAULT_COLOR)
        cls = ' class="qkx-group-start"' if group_start else ""
        return f'<td{cls} style="background:{bg};color:{color};">{esc(val)}</td>'

    def scell(val, status, group_start=False):
        color, bg = STATUS_COLORS.get(status, DEFAULT_COLOR)
        cls = ' class="qkx-group-start"' if group_start else ""
        return f'<td{cls} style="background:{bg};color:{color};font-weight:600;">{esc(val)}</td>'

    head1 = ('<th colspan="2">Cell verification</th><th colspan="2" class="qkx-group-start">RRU verification</th>'
             '<th colspan="2" class="qkx-group-start">Antenna verification</th>'
             '<th colspan="2" class="qkx-group-start">Cell id</th>'
             '<th rowspan="2" class="qkx-group-start">Antenna info</th><th rowspan="2">Losses &amp; Delays</th>'
             '<th rowspan="2" style="min-width:160px;">Warning</th>')
    head2 = '<th>RFDS</th><th>CIQ</th>' * 4
    body = []
    for r in rows:
        warn_cell = (f'<td style="min-width:160px;color:#991b1b;font-weight:700;">{esc(r["warning"])}</td>'
                     if r["warning"] != "—" else '<td style="min-width:160px;">—</td>')
        tds = (
            gcell(r["cell_rfds"], r["cell_status"]) + gcell(r["cell_ciq"], r["cell_status"])
            + gcell(r["rru_rfds"], r["rru_status"], True) + gcell(r["rru_ciq"], r["rru_status"])
            + gcell(r["ant_rfds"], r["ant_status"], True) + gcell(r["ant_ciq"], r["ant_status"])
            + gcell(r["cellid_rfds"], r["cellid_status"], True) + gcell(r["cellid_ciq"], r["cellid_status"])
            + scell(r["ant_info"], r["ant_info_status"], True) + scell(r["losses_delays"], r["losses_status"]) + warn_cell
        )
        body.append(f"<tr>{tds}</tr>")
    return (f'<div class="qkx-table-wrap"><table class="qkx-table">'
            f'<thead><tr>{head1}</tr><tr>{head2}</tr></thead>'
            f'<tbody>{"".join(body)}</tbody></table></div>')


def render_pre_vs_edp_pivot_table(rows):
    """Node ID + one 2-col (pre | EDP) group per Bearer/OAM field — matches
    the wide screenshot layout. rows come from
    rrnrbl_checklist.build_pre_vs_edp_pivot_rows()."""
    if not rows:
        return '<div class="qkx-empty">No data.</div>'
    groups = [("SIAD Port Size", "siad_port_size"),
              ("Bearer VLAN", "bearer_vlan"), ("Bearer IPv6", "bearer_ipv6"),
              ("Bearer Default Router", "bearer_router"), ("OAM VLAN", "oam_vlan"),
              ("OAM IPv6", "oam_ipv6"), ("OAM Default Router", "oam_router")]
    head1 = '<th rowspan="2">Node ID</th>' + "".join(
        f'<th colspan="2" class="qkx-group-start">{esc(label)}</th>' for label, _ in groups)
    head2 = "".join('<th class="qkx-group-start">pre</th><th>EDP</th>' for _ in groups)
    body = []
    for r in rows:
        cells = f"<td>{esc(r['label'])}</td>"
        for _, key in groups:
            # Each pre/EDP pair is tinted by its OWN verdict (computed in
            # build_pre_vs_edp_pivot_rows), so a single bad field stands out
            # instead of the whole table rendering flat. Previously these
            # were plain <td>s with no status at all — nothing ever
            # highlighted here, which is what made mismatches invisible.
            st_ = r.get(key + "_status", "unknown")
            tint = {"mismatch": "background:#fdecea;color:#9f1d1d;font-weight:700;",
                    "match": "background:#eafaf1;"}.get(st_, "")
            cells += (f'<td class="qkx-group-start" style="{tint}">{esc(r.get(key + "_pre", ""))}</td>'
                      f'<td style="{tint}">{esc(r.get(key + "_edp", ""))}</td>')
        body.append(f"<tr>{cells}</tr>")
    return (f'<div class="qkx-table-wrap"><table class="qkx-table">'
            f'<thead><tr>{head1}</tr><tr>{head2}</tr></thead>'
            f'<tbody>{"".join(body)}</tbody></table></div>')


def section_title(text, badge=None):
    """badge: optional right-aligned pill (e.g. '18 CELLS'), matching
    QUICKIX HTML's card-header count badge."""
    badge_html = f'<span class="qkx-title-badge">{esc(badge)}</span>' if badge else ''
    st.markdown(f'<div class="qkx-section-title" style="display:flex;justify-content:space-between;align-items:center;">'
                f'<span>{esc(text)}</span>{badge_html}</div>', unsafe_allow_html=True)


def count_caption(rows, status_key="status", bad_value="MISMATCH", noun="row"):
    n_bad = sum(1 for r in rows if r.get(status_key) == bad_value)
    st.caption(f"{len(rows)} {noun}(s) checked — {n_bad} mismatch(es).")


# ══════════════════════════════════════════════════════════════════════
# RRNRBL Checklist renderer — flat category banners (no nesting; Streamlit
# expanders can't nest and the HTML tool's own renderer doesn't collapse
# per-category either), auto rows batched into one coloured table per
# run, manual rows get a real checkbox + comment box so the value survives
# reruns and feeds the downloadable xlsx.
# ══════════════════════════════════════════════════════════════════════
def _chip(status):
    """Status chip markup — every checklist row carries one so a status is
    readable on its own, not only via a pale row background."""
    cls = {"match": "match", "MATCH": "match", "PASS": "match",
           "mismatch": "mismatch", "MISMATCH": "mismatch", "FAIL": "mismatch",
           "manual": "manual", "MANUAL": "manual", "EXPECTED": "manual",
           "info": "info", "INFO": "info",
           }.get(status, "unknown")
    return f'<span class="qkx-chip {cls}">{esc(STATUS_LABEL.get(status, status))}</span>'


def render_checklist_grid(rows, manual_values):
    """One continuous spreadsheet-style grid for the WHOLE checklist:
    Category | Sub-section | Item | Detail | Status, covering every row
    (auto AND manual) in reading order. Category/Sub-section cells use
    rowspan to merge consecutive identical values — the actual spreadsheet
    "grouped cell" look, rather than repeating the same category name on
    every row or breaking the table into one fragment per category (the
    old render_rrnrbl_checklist() approach).

    manual_values: {row_number: {"done": bool, "comment": str}} — the
    CURRENTLY SAVED values for manual items (read from session_state by the
    caller), so a manual row's Detail column shows what's actually been
    entered so far instead of always looking blank. Manual rows remain
    read-only in this grid; actually entering a comment still happens in
    the separate fill-in section below (a raw HTML table cannot host a
    live checkbox/text-input widget)."""
    if not rows:
        return '<div class="qkx-empty">Run validation to populate the checklist.</div>'

    # Precompute rowspans: for each row, how many rows below it (inclusive)
    # share the same (cat) or (cat, sub) — 0 means "this row is covered by
    # an earlier rowspan, emit no <td> for this column at all".
    n = len(rows)
    cat_span = [0] * n
    sub_span = [0] * n
    i = 0
    while i < n:
        j = i
        while j < n and rows[j]["cat"] == rows[i]["cat"]:
            j += 1
        cat_span[i] = j - i
        i = j
    i = 0
    while i < n:
        j = i
        while j < n and rows[j]["cat"] == rows[i]["cat"] and rows[j].get("sub") == rows[i].get("sub"):
            j += 1
        sub_span[i] = j - i
        i = j

    head = ('<th style="width:15%;">Category</th><th style="width:15%;">Sub-section</th>'
            '<th style="width:24%;">Item</th><th>Detail</th><th style="width:96px;">Status</th>')
    body = []
    for idx, r in enumerate(rows):
        status = r["status"]
        color, bg = STATUS_COLORS.get(status, DEFAULT_COLOR)
        if status == "manual":
            mv = manual_values.get(r["row"], {})
            detail = mv.get("comment") or "—"
            if mv.get("done"):
                detail = f"\u2713 {detail}" if detail != "—" else "\u2713 Marked done"
        else:
            detail = r.get("detail", "")

        cells = ""
        if cat_span[idx] > 0:
            cells += f'<td rowspan="{cat_span[idx]}" style="font-weight:700;vertical-align:top;background:#f8fafc;">{esc(r["cat"])}</td>'
        if sub_span[idx] > 0:
            sub_text = esc(r.get("sub")) if r.get("sub") else "\u2014"
            cells += f'<td rowspan="{sub_span[idx]}" style="vertical-align:top;color:#475569;">{sub_text}</td>'
        cells += (f'<td style="color:{color};font-weight:600;">{esc(r["item"])}</td>'
                  f'<td style="color:{color};">{esc(detail)}</td>'
                  f'<td style="width:96px;">{_chip(status)}</td>')
        body.append(f'<tr style="background:{bg};">{cells}</tr>')

    return (f'<div class="qkx-table-wrap qkx-grid-wrap"><table class="qkx-table qkx-grid">'
            f'<thead><tr>{head}</tr></thead><tbody>{"".join(body)}</tbody></table></div>')


def render_rrnrbl_checklist(rows):
    """Checklist grid: one qkx-cat-banner per category, each followed by a
    small st.dataframe of just that category's Check/Tick/Scope/Remarks
    rows — category shown once, not repeated per row (matches the
    Checklist_RRNRBL.xlsx flat layout: category banner row, then item
    rows).

    Confirmed against Streamlit's own docs and a currently-open platform
    issue (streamlit/streamlit#10953): st.data_editor does NOT apply
    background styling to its EDITABLE columns — only disabled ones. A
    single editable grid with real whole-row colour is therefore not
    possible on this platform, not a bug in this code. Every previous CSS
    attempt to fake it by targeting internal DOM nodes was fighting a
    losing battle against Streamlit's own layout updates, which is why
    alignment kept breaking.

    So the two jobs are split, each using the API actually built for it:
      - DISPLAY: st.dataframe/Styler-driven background+color tint on the
        disabled columns (Check, Scope) — real whole-row-ish colour via a
        first-class documented Streamlit feature, guaranteed column
        alignment (glide-data-grid), bold headers, fixed row_height.
      - EDIT: Tick and Remarks are both live-editable cells in this same
        grid (st.data_editor) — no separate review section. Editable
        cells can't carry the Styler tint (streamlit#10953), which is why
        Check carries a status icon + bold color instead, so status still
        reads even with a plain white Tick/Remarks cell next to it.
    """
    import pandas as pd
    from itertools import groupby

    if not rows:
        st.markdown('<div class="qkx-empty">Run validation to populate the checklist.</div>',
                    unsafe_allow_html=True)
        return

    counts = {}
    for r in rows:
        counts[r["status"]] = counts.get(r["status"], 0) + 1
    order = ["mismatch", "manual", "unknown", "info", "na", "match"]
    pills = " ".join(
        f'<span class="qkx-count-pill"><b style="color:{STATUS_COLORS.get(k, DEFAULT_COLOR)[0]}">{counts[k]}</b> '
        f'{esc(STATUS_LABEL.get(k, k))}</span>'
        for k in sorted(counts, key=lambda x: (order.index(x) if x in order else 99, x))
    )
    st.markdown(f'<div style="margin:2px 0 10px 0;">{pills}</div>', unsafe_allow_html=True)

    overrides = st.session_state.get("rrnrbl_overrides", {})

    def _checked_for(r):
        ov = overrides.get(r["row"])
        return ov["checked"] if ov is not None else (r["status"] != "manual")

    def _remarks_for(r):
        ov = overrides.get(r["row"])
        if ov is not None and ov.get("comment"):
            return ov["comment"]
        detail = r.get("detail") or ""
        # A manual row's detail is blanked ONLY when it's the generic
        # placeholder from a genuinely-unautomated item (check=None in
        # build_checklist) — some rows (e.g. Script Generation 104-109)
        # are deliberately kept manual (still unticked, still the pencil
        # icon) while their check() computes a REAL informational comment
        # to show here; that real text must not be swallowed by the same
        # blanking rule that hides the generic placeholder.
        if r["status"] == "manual" and detail == "No automated check exists for this item.":
            return ""
        return detail

    def _tint_row(styler_row, cat_df):
        # styler_row is the visible-column row Styler passes in for THIS
        # category's own df (Tick is disabled here, so it's still tinted
        # too — only truly editable columns lose background per
        # streamlit/streamlit#10953); status is looked up from that same
        # cat_df, so the index always lines up even though every
        # category's df restarts at 0. Foreground color is applied too
        # (not just background) so status still reads at a glance even
        # on the Tick column, which can't carry any color at all since
        # it's the one editable cell.
        status = cat_df.loc[styler_row.name, "_status"]
        color, bg = STATUS_COLORS.get(status, DEFAULT_COLOR)
        return [f"background-color:{bg};color:{color};font-weight:600"] * len(styler_row)

    STATUS_ICON = {"match": "\u2713 ", "mismatch": "\u2717 ", "manual": "\u270e ",
                   "unknown": "\u2013 ", "info": "\u2139 ", "na": "\u2013 "}

    new_overrides = dict(overrides)
    for cat_idx, (cat, group) in enumerate(groupby(rows, key=lambda r: r["cat"])):
        group = list(group)
        st.markdown(f'<div class="qkx-cat-banner"><span>{esc(cat)}</span></div>',
                    unsafe_allow_html=True)

        cat_df = pd.DataFrame([{
            "Check": STATUS_ICON.get(r["status"], "") + r["item"],
            "Tick": _checked_for(r),
            "Scope": r.get("tag", ""),
            "Remarks": _remarks_for(r),
            "_row": r["row"],
            "_status": r["status"],
        } for r in group])

        styled = cat_df.drop(columns=["_row", "_status"]).style.apply(
            lambda row: _tint_row(row, cat_df), axis=1
        )

        edited = st.data_editor(
            styled,
            hide_index=True,
            use_container_width=True,
            row_height=34,
            height=len(cat_df) * 34 + 38,
            column_order=["Check", "Tick", "Scope", "Remarks"],
            column_config={
                "Check": st.column_config.TextColumn("Check", width="large"),
                "Tick": st.column_config.CheckboxColumn("Tick", width=56),
                "Scope": st.column_config.TextColumn("Scope", width="small"),
                "Remarks": st.column_config.TextColumn("Remarks", width="large"),
            },
            disabled=["Check", "Scope"],
            key=f"rrnrbl_grid_{cat_idx}",
        )

        for i in range(len(cat_df)):
            rid = int(cat_df.loc[i, "_row"])
            new_checked = bool(edited.loc[i, "Tick"])
            new_comment = str(edited.loc[i, "Remarks"] or "")
            new_overrides[rid] = {"checked": new_checked, "comment": new_comment}

    st.session_state["rrnrbl_overrides"] = new_overrides



def collect_manual_overrides(checklist):
    """What the user has set directly in the checklist grid (Tick/Remarks),
    keyed by checklist row. Untouched rows (never edited, or match/skip
    rows nobody touched) fall back to the same default the display grid
    itself shows, so the export can never disagree with what was on screen."""
    overrides = st.session_state.get("rrnrbl_overrides", {})
    out = {}
    for row in checklist:
        ov = overrides.get(row["row"])
        if ov is not None:
            out[row["row"]] = {"checked": bool(ov.get("checked")), "comment": (ov.get("comment") or "").strip()}
        else:
            out[row["row"]] = {
                "checked": row["status"] != "manual",
                "comment": "" if row["status"] == "manual" else (row.get("detail") or ""),
            }
    return out



# ══════════════════════════════════════════════════════════════════════
# Session state / one-shot validation run
# ══════════════════════════════════════════════════════════════════════
st.session_state.setdefault("has_run", False)


def strip_ansi(text):
    """Removes terminal control sequences from a raw log capture. Some Pre
    kget-all logs are captured via a terminal client (e.g. PuTTY) with
    color/bold formatting enabled, which wraps the node-id prompt in ANSI
    codes: 'FCL04120> lt all' becomes '\\x1b[1mFCL04120\\x1b[0m> lt all'.
    Every regex in this project that matches a prompt line expects it to
    start with the bare node id, so without this the ANSI codes make
    node_id_from_log()/split_commands() fail silently — the whole log then
    parses to nothing, and node identification falls back to the uploaded
    FILENAME (confirmed: a real PuTTY-captured log showed Node ID as
    'FCL04120.txt', SW Version, BB Type, and all cell tables empty).
    Applied once here, at the single point every uploaded log's raw bytes
    are first decoded to text, so every downstream function (which all
    receive already-decoded text) is unaffected regardless of capture tool."""
    text = re.sub(r'\x1b\[[0-9;]*[A-Za-z]', '', text)  # CSI: colors, bold, cursor movement
    text = re.sub(r'\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)', '', text)  # OSC: window title/icon name
    text = re.sub(r'\x1b.', '', text)  # any remaining lone ESC + one char
    return text


def _tmp_path(data, suffix):
    tf = tempfile.NamedTemporaryFile(suffix=suffix, delete=False)
    tf.write(data)
    tf.close()
    return tf.name


def run_full_validation(ciq_bytes, edp_bytes, edp_ext, rfds_bytes, node_logs_text):
    ciq_path = _tmp_path(ciq_bytes, ".xlsx")
    edp_path = _tmp_path(edp_bytes, edp_ext or ".xls")
    rfds_path = _tmp_path(rfds_bytes, ".pdf") if rfds_bytes else None

    (_, results, site_details, ciq_wb, edp_rows, checked_nodes, rfds_pages,
     pre_text, post_text, scope_lines, sow) = rv.run(ciq_path, edp_path, rfds_path, node_logs_text, None)

    checklist = rc.build_checklist(results, site_details, ciq_wb, edp_rows, checked_nodes, rfds_pages, node_logs_text)
    site_id_fa = " / ".join(v for v in (site_details.get("site_id"), site_details.get("fa_code")) if v)

    # Computed once here rather than inline in each tab: those call sites ran on
    # EVERY Streamlit rerun (any widget interaction anywhere in the app reruns
    # the whole script), so a checkbox click in an unrelated tab was silently
    # re-parsing every uploaded Pre log again. Tabs now just read these back.
    node_role_list = rc.build_primary_secondary_node_list(ciq_wb)
    edp_field_rows = rc.build_edp_field_table(edp_rows, node_role_list)
    pre_edp_pivot_rows = rc.build_pre_vs_edp_pivot_rows(node_logs_text, node_role_list, edp_rows, ciq_wb) if node_logs_text else []
    amos_summary_rows, amos_lte_rows, amos_nr_rows = av.build_amos_tables(node_logs_text) if node_logs_text else ([], [], [])

    return dict(
        results=results, site_details=site_details, ciq_wb=ciq_wb, edp_rows=edp_rows,
        checked_nodes=checked_nodes, rfds_pages=rfds_pages, rfds_bytes=rfds_bytes,
        pre_text=pre_text, post_text=post_text,
        scope_lines=scope_lines, sow=sow, checklist=checklist, site_id_fa=site_id_fa,
        node_logs_text=node_logs_text,
        node_role_list=node_role_list, edp_field_rows=edp_field_rows,
        pre_edp_pivot_rows=pre_edp_pivot_rows,
        amos_summary_rows=amos_summary_rows, amos_lte_rows=amos_lte_rows, amos_nr_rows=amos_nr_rows,
    )


# ══════════════════════════════════════════════════════════════════════
# INPUT PAGE — shown only until "Run Validation" succeeds. Everything the
# tool needs is uploaded here once; no tab has its own uploader anymore.
# ══════════════════════════════════════════════════════════════════════
if not st.session_state["has_run"]:
    st.markdown("#### Load site data")
    c1, c2 = st.columns(2)
    with c1:
        ciq_up = st.file_uploader("CIQ workbook — required", type=["xlsx"], key="in_ciq")
        edp_up = st.file_uploader("EDP workbook — required (.xls or .xlsx)", type=["xls", "xlsx"], key="in_edp")
    with c2:
        rfds_up = st.file_uploader("RFDS PDF — optional", type=["pdf"], key="in_rfds")
        log_ups = st.file_uploader("Pre kget-all / hget logs — optional, one per node", type=["txt", "log"],
                                    accept_multiple_files=True, key="in_logs")

    ready = bool(ciq_up and edp_up)
    if st.button("▶ Run Validation", type="primary", disabled=not ready, use_container_width=True):
        node_logs_text = {}
        for u in (log_ups or []):
            text = strip_ansi(u.getvalue().decode("utf-8", errors="ignore"))
            nid = pe.node_id_from_log(text) or u.name
            node_logs_text[nid] = text
        with st.spinner("Running full validation…"):
            try:
                state = run_full_validation(
                    ciq_up.getvalue(), edp_up.getvalue(), os.path.splitext(edp_up.name)[1],
                    rfds_up.getvalue() if rfds_up else None, node_logs_text,
                )
            except Exception as e:
                st.error(f"Validation failed: {e}")
                st.stop()
        st.session_state["state"] = state
        st.session_state["_memo"] = {}   # new run ⇒ drop all derived caches
        st.session_state["has_run"] = True
        st.rerun()
    elif not ready:
        st.caption("CIQ and EDP are both required to run validation. RFDS PDF and Pre logs are optional but enable more checks.")
    st.stop()

# ══════════════════════════════════════════════════════════════════════
# RESULTS — one validation run, five tabs, all reading the same state.
# ══════════════════════════════════════════════════════════════════════
state = st.session_state["state"]


# ── Consolidated mismatch views ────────────────────────────────────────
# The consolidated report shows only what needs ACTION: mismatches, broken
# out to the individual PARAMETER that disagrees, rather than dumping every
# checked row (passes included) as a wide table.
_MM_NA = {"", "NA", "NOT AVAILABLE", "NOT FOUND", "NOT CHECKED", "NOT IN CIQ", "-", "\u2014", "NONE"}

# Internal field name -> the label an engineer reads on the report.
_MM_PARAM_LABEL = {
    "earfcndl": "EARFCNDL", "earfcnul": "EARFCNUL",
    "arfcnDL": "ARFCNDL", "arfcnUL": "ARFCNUL",
    "dlChannelBandwidth": "BW DL", "ulChannelBandwidth": "BW UL",
    "bSChannelBwDL": "BW DL", "bSChannelBwUL": "BW UL",
    "ssbfrequency": "SSB Frequency",
    "sec_id": "Sector Carrier", "power": "Power",
}


# CIQ-side validation rules, and the label each one reports under.
_MM_CIQ_CHECKS = [
    ("identity", "eNBId/gNBId (ENM vs CIQ)"),
    ("gnb_identity", "gNB Identity (Mixed Mode vs gNB/5G Info)"),
    ("enb_identity", "eNB Identity (Mixed Mode vs eNB Info/eUtran)"),
    ("gnb_du_type", "DU type (gNB Info vs 5G Info BBU Type)"),
    ("nrcelldu_nrcellcu", "NRCellDU/NRCellCU naming"),
    ("arfcn_bw_5g", "ARFCN/Bandwidth (5G, ENM vs CIQ)"),
    ("ssb_5g", "SSB Frequency/Offset/Duration"),
    ("dss", "Pre-existing DSS"),
    ("antenna_type_rfds", "Antenna Type/Model vs RFDS"),
    ("rbb_tx_isdlonly_4g", "RBB Type/TX-RX/ISDLONLY (4G)"),
    ("rilink_vs_rbb_4g", "RILink vs RBB Type (4G)"),
    ("electrical_tilt_type", "Electrical Tilt not an integer"),
    ("cellid_uniqueness_4g", "Cell ID uniqueness (4G)"),
    ("pci_4g", "PCI clash (LTE)"),
    ("pci_5g", "PCI clash (5G)"),
    ("antenna", "Antenna uniqueness"),
    ("port_uniqueness", "Port clash"),
    ("xmu_port_overlap", "XMU port overlap"),
    ("sef_fru", "SEF / FRU"),
    ("radio_sharing", "Sharing radio"),
    ("radio_port_conflict", "Radio port conflict"),
    ("nbiot", "NBIoT"),
    ("sector_id_4890", "SectorID (4890)"),
    ("rfbranch_per_aug", "RfBranch per AUG"),
    ("losses_vs_antenna", "Losses vs Antenna Info"),
    ("tilt", "Tilt not an integer"),
    ("mmwave_rach", "mmWave RACH"),
    ("carrier_progression", "Carrier progression"),
    ("ptp_matrix", "PTP configuration"),
]


def _mm_is_na(v):
    return str(v).strip().upper() in _MM_NA


def _mm_row(cell, source, param, left_label, left, right):
    return {"cell": cell, "source": source, "param": param,
            "comments": f"{left_label} - {left} | {'EDP' if source.endswith('EDP') else 'CIQ'} - {right}"}


def build_consolidated_mismatches(grouped_rows, results, pre_edp_rows=None, edp_rows=None, edp_node_ids=None,
                                    ciq_wb=None, node_logs_text=None, node_role_list=None, site_details=None):
    """Flat, parameter-level mismatch list for the consolidated report.

    Three comparison families, all reduced to the same four columns
    (Cell name / Mismatch on / Parameter / Comments):

      'RFDS vs CIQ'  - RRU, Antenna, Cell ID, cell missing from RFDS, and
                       cells missing from the CIQ's Antenna Information /
                       Losses and Delays sheets.
      'KGET vs CIQ'  - Sector Carrier, Cell ID, TAC, EARFCNDL/UL,
                       ARFCNDL/UL, BW, Power, TX, RX, RRU and RILink
                       (single/double).
      'KGET vs EDP'  - Bearer VLAN / IPv6 / Default Router and the OAM
                       equivalents, plus a CIQ node (primary or secondary)
                       having no EDP row published at all (Checklist row
                       20's own check, reused rather than re-derived here).

    A field whose Pre/KGET side is NA or NOT AVAILABLE is not a mismatch -
    there is nothing to compare it against - which mirrors how the
    underlying checks decide their own status."""
    rows = []

    # ── FA Code, RFDS vs CIQ (Checklist rows 17/34) ────────────────────
    if site_details:
        rfds_fa = site_details.get("rfds_fa_code")
        ciq_fa = site_details.get("fa_code")
        if rfds_fa and ciq_fa and rfds_fa != ciq_fa:
            rows.append({"cell": "site", "source": "RFDS vs CIQ", "param": "FA Code",
                         "comments": f"RFDS FA Code {rfds_fa} vs CIQ FA Code {ciq_fa}"})

    # ── EDP not published for a CIQ node (Checklist row 20) ────────────
    if edp_node_ids:
        for node in rc.edp_missing_nodes(edp_rows or [], edp_node_ids):
            rows.append({"cell": node, "source": "KGET vs EDP", "param": "EDP Published",
                         "comments": f"EDP is not published for {node}"})

        # ── cabinet (Checklist row 21) ─────────────────────────────────
        for b in rc.edp_cabinet_mismatches(edp_rows or [], edp_node_ids):
            rows.append({"cell": b["node"], "source": "KGET vs EDP", "param": "Cabinet",
                         "comments": b["note"]})

        # ── CIQ board type vs EDP NODE_MODEL (Checklist row 22) ────────
        for b in rc.bbu_type_vs_node_model_mismatches(ciq_wb, edp_rows or [], edp_node_ids):
            rows.append({"cell": b["node"], "source": "KGET vs EDP", "param": "BBU Type (NODE_MODEL)",
                         "comments": b["note"]})

        # ── CIQ BBU Mode vs EDP BBU_TYPE (Checklist row 23) ────────────
        _r23_bad, _ = rc.node_model_vs_bbu_type_mismatches(ciq_wb, edp_rows or [], edp_node_ids)
        for b in _r23_bad:
            rows.append({"cell": b["node"], "source": "KGET vs EDP", "param": "BBU Mode (BBU_TYPE)",
                         "comments": b["note"]})

        # ── Board Type, CIQ vs EDP vs RFDS (Checklist rows 56/67, #5/#15/#13) ──
        # Two independent disagreements possible per node; each routed to
        # its own section rather than lumped into one merged verdict.
        for b in results.get("board_type", []):
            if b.get("edp_mismatch"):
                rows.append({"cell": b["node"], "source": "KGET vs EDP", "param": "Board Type (DU type)",
                             "comments": f"CIQ - {b.get('ciq_du_type')} | EDP - {b.get('edp_model')}"})
            if b.get("rfds_mismatch"):
                rows.append({"cell": b["node"], "source": "RFDS vs CIQ", "param": "Board Type (DU type)",
                             "comments": f"CIQ DU type {b.get('ciq_du_type')} not found in RFDS"})

        # ── XMU, CIQ vs RFDS (Checklist row 86, #27) ───────────────────
        for x in results.get("xmu", []):
            if x.get("status") == "MISMATCH":
                rows.append({"cell": x["node"], "source": "RFDS vs CIQ", "param": "XMU",
                             "comments": x.get("note", "")})

        # ── siad_port_size_bbu (Checklist row 24) ──────────────────────
        for b in rc.siad_port_size_mismatches(node_logs_text, ciq_wb, edp_rows or [], edp_node_ids):
            rows.append({"cell": b["node"], "source": "KGET vs EDP", "param": "SIAD_PORT_SIZE_BBU",
                         "comments": b["note"]})

        # ── siad_port_facing_bbu (Checklist row 25) ────────────────────
        for b in rc.edp_port_facing_mismatches(edp_rows or [], edp_node_ids):
            rows.append({"cell": b["node"], "source": "KGET vs EDP", "param": "SIAD_PORT_FACING_BBU",
                         "comments": b["note"]})

    # ── RFDS vs CIQ ────────────────────────────────────────────────────
    for r in grouped_rows or []:
        # Prefer whichever side actually carries the cell identifier: on a
        # "not found" row one side holds the literal 'NOT FOUND', and that
        # must not become the Cell name.
        cell = next((v for v in (r.get("cell_ciq"), r.get("cell_rfds"))
                     if v and not _mm_is_na(v)), "\u2014")
        if r.get("cell_status") == "MISMATCH":
            rows.append(_mm_row(cell, "RFDS vs CIQ", "Cell Not Found", "RFDS",
                                r.get("cell_rfds", "\u2014"), r.get("cell_ciq", "\u2014")))
        if r.get("rru_status") == "MISMATCH":
            rows.append(_mm_row(cell, "RFDS vs CIQ", "RRU", "RFDS",
                                r.get("rru_rfds", "\u2014"), r.get("rru_ciq", "\u2014")))
        if r.get("ant_status") == "MISMATCH":
            rows.append(_mm_row(cell, "RFDS vs CIQ", "Antenna", "RFDS",
                                r.get("ant_rfds", "\u2014"), r.get("ant_ciq", "\u2014")))
        if r.get("cellid_status") == "MISMATCH":
            rows.append(_mm_row(cell, "RFDS vs CIQ", "CellID", "RFDS",
                                r.get("cellid_rfds", "\u2014"), r.get("cellid_ciq", "\u2014")))
        # Presence on the two CIQ sheets — a cell the RFDS designs but the
        # CIQ never lists is a real gap, reported per sheet.
        if r.get("ant_info_status") == "MISMATCH":
            rows.append({"cell": cell, "source": "RFDS vs CIQ", "param": "Missing in Antenna Info",
                         "comments": "Cell not listed on the CIQ 'Antenna Information' sheet"})
        if r.get("losses_status") == "MISMATCH":
            rows.append({"cell": cell, "source": "RFDS vs CIQ", "param": "Missing in Losses and Delays",
                         "comments": "Cell not listed on the CIQ 'Losses and Delays' sheet"})

    # ── Primary/Secondary identity (Rule #3/#31) ──────────────────────
    # Spans THREE sources (CIQ vs EDP vs RFDS), unlike every other check
    # in this function which only ever compares two — so it can't sit in
    # SW version (rule #1) never carries MISMATCH on its own raw entries
    # (only INFO/SKIPPED per node) - checklist row 13's "Major showstopper"
    # verdict comes from a cross-node comparison done only inside the
    # checklist builder (_sw_status_v2). Reused here so this same finding
    # isn't invisible everywhere except the checklist.
    #
    # Must be FAMILY-aware (rc._group_sw_by_family / rc._sw_package_family),
    # not a flat set-of-all-versions compare - confirmed real false positive
    # this exact form used to produce: a routine mixed-hardware/CRAN site
    # (G2 boards on 'RCG123.8', G3/G4 boards on '26.Q1' for the SAME
    # quarterly release - different board generations never share a
    # sw_version STRING) showed a green MATCH on the checklist (which
    # already went family-aware) while this Consolidated Report mismatch
    # list still flagged 'SW versions disagree across nodes' for the exact
    # same site, from the flat len(_sw_versions) > 1 check. Only a real
    # disagreement WITHIN one board family is an actual finding now.
    _sw_checked = [r for r in results.get("sw_version", []) if r.get("status") != "SKIPPED"]
    _sw_missing = [r.get("node") for r in _sw_checked if r.get("sw_version") in (None, "NOT FOUND")]
    if _sw_missing:
        rows.append({"cell": ", ".join(_sw_missing), "source": "CIQ check", "param": "SW Version",
                     "comments": f"No SW version detected for: {', '.join(_sw_missing)}"})
    _sw_have = [r for r in _sw_checked if r.get("sw_version") not in (None, "NOT FOUND")]
    _sw_by_family = rc._group_sw_by_family(_sw_have)
    _sw_mixed = {fam: vers for fam, vers in _sw_by_family.items() if len(vers) > 1}
    if _sw_mixed:
        detail = "; ".join(f"{r.get('node')}={r.get('sw_version')}" for r in _sw_have)
        rows.append({"cell": "site", "source": "CIQ check", "param": "SW Version",
                     "comments": f"Mixed SW versions WITHIN the same board family: {detail}"})

    # one fixed bucket. Routed to whichever source(s) actually disagree
    # with CIQ, same comparison check_primary_secondary itself already
    # made, so "missing in EDP" shows under EDP Checks and "missing in
    # RFDS" shows under RFDS vs CIQ, rather than every case landing under
    # a single generic CIQ-check heading regardless of which side the
    # real gap is on.
    for r in results.get("primary_secondary", []):
        if str(r.get("status", "")).upper() != "MISMATCH":
            continue
        node = r.get("node") or "\u2014"
        ciq_label, edp_label, rfds_label = r.get("ciq"), r.get("edp"), r.get("rfds")
        routed = False
        if edp_label is not None and edp_label != ciq_label:
            rows.append({"cell": node, "source": "KGET vs EDP", "param": "Primary/Secondary ID",
                         "comments": f"CIQ - {ciq_label} | EDP - {edp_label}"})
            routed = True
        if rfds_label is not None and rfds_label not in (ciq_label, "NOT CHECKED"):
            rows.append({"cell": node, "source": "RFDS vs CIQ", "param": "Primary/Secondary ID",
                         "comments": f"CIQ - {ciq_label} | RFDS - {rfds_label}"})
            routed = True
        if not routed:
            # Flagged MISMATCH for some reason other than a direct EDP/RFDS
            # value disagreement (e.g. CIQ itself missing from Mixed Mode
            # Info entirely) - still surface it rather than dropping it.
            rows.append({"cell": node, "source": "CIQ check", "param": "Primary/Secondary ID",
                         "comments": r.get("note", "") or "\u2014"})

    # ── KGET vs CIQ ────────────────────────────────────────────────────
    for key in ("params_4g", "params_5g", "sector_swap"):
        for r in results.get(key, []):
            if str(r.get("status", "")).upper() != "MISMATCH":
                continue
            cell = r.get("cell") or "\u2014"
            for k, v in r.items():
                if k in ("rule", "node", "cell", "status", "note") or not isinstance(v, str) or " | " not in v:
                    continue
                pre, _, ciq = v.partition(" | ")
                pre, ciq = pre.strip(), ciq.strip()
                if not _mm_is_na(pre) and pre != ciq:
                    rows.append(_mm_row(cell, "KGET vs CIQ", _MM_PARAM_LABEL.get(k, k), "KGET", pre, ciq))
            # sector_swap: pre_X / ciq_X pairs. TX/RX is stored as one
            # 'AxB' string but reads better split into its own TX and RX
            # rows; RILink (Single/Double) rides in the same field on
            # standalone-5G rows, where it is a link-type not an AxB count.
            for k in list(r):
                if not k.startswith("pre_"):
                    continue
                b = k[4:]
                ciq_key = "ciq_" + b if "ciq_" + b in r else (b if b in r else None)
                if not ciq_key:
                    continue
                pre, ciq = str(r[k]).strip(), str(r[ciq_key]).strip()
                if _mm_is_na(pre) or pre == ciq:
                    continue
                if b == "txrx":
                    pre_m = re.fullmatch(r"(\d+)x(\d+)", pre)
                    ciq_m = re.fullmatch(r"(\d+)x(\d+)", ciq)
                    if pre_m and ciq_m:
                        for idx, lbl in ((1, "TX"), (2, "RX")):
                            if pre_m.group(idx) != ciq_m.group(idx):
                                rows.append(_mm_row(cell, "KGET vs CIQ", lbl, "KGET",
                                                    pre_m.group(idx), ciq_m.group(idx)))
                        continue
                    if pre in ("Single", "Double") or "Single" in ciq or "Double" in ciq:
                        rows.append(_mm_row(cell, "KGET vs CIQ", "Link", "KGET", pre, ciq))
                        continue
                rows.append(_mm_row(cell, "KGET vs CIQ", _MM_PARAM_LABEL.get(b, b), "KGET", pre, ciq))

    # ── Pre vs Post cell-level audit (the Audit tab's own "Pre vs Post"
    # table, compare_lte_cell_level/compare_nr_cell_level) — computed
    # separately from params_4g/params_5g/sector_swap above (different
    # matching logic: by cell SUFFIX so a moved sector is still paired
    # with its Pre self) and was never wired here at all. ────────────────
    if node_logs_text and ciq_wb is not None:
        import pre_post_audit as ppa
        _LTE_PP_FIELDS = [("sc", "_sc_ok", "Sector Carrier"), ("cellid", "_cellid_ok", "Cell ID"),
                          ("tac", "_tac_ok", "TAC"), ("bw", "_bw_ok", "BW"), ("dl", "_dl_ok", "EARFCN DL"),
                          ("ul", "_ul_ok", "EARFCN UL"), ("power", "_power_ok", "Power"),
                          ("tx", "_tx_ok", "TX"), ("rx", "_rx_ok", "RX"), ("rru", "_rru_ok", "RRU"),
                          ("cellrange", "_cellrange_ok", "Cell Range"), ("dss", "_dss_ok", "DSS")]
        for r in ppa.compare_lte_cell_level(node_logs_text, ciq_wb):
            if r.get("row_type") == "new":
                continue  # no Pre match at all - nothing to compare, not a mismatch
            for key, ok_key, label in _LTE_PP_FIELDS:
                if r.get(ok_key) is False:
                    pre, _, ciq = str(r.get(key, "")).partition(" | ")
                    rows.append(_mm_row(r.get("cell") or "\u2014", "KGET vs CIQ", label, "KGET",
                                         pre.strip(), ciq.strip()))
        _NR_PP_FIELDS = [("cellid", "_cellid_ok", "Cell ID"), ("dl", "_dl_ok", "ARFCN DL"),
                         ("ul", "_ul_ok", "ARFCN UL"), ("bw_dl", "_bw_dl_ok", "BW DL"),
                         ("bw_ul", "_bw_ul_ok", "BW UL"), ("power", "_power_ok", "Power"),
                         ("ssb", "_ssb_ok", "SSB"), ("rru", "_rru_ok", "RRU"),
                         ("cellrange", "_cellrange_ok", "Cell Range"), ("dss", "_dss_ok", "DSS")]
        for r in ppa.compare_nr_cell_level(node_logs_text, ciq_wb):
            if r.get("row_type") == "new":
                continue
            for key, ok_key, label in _NR_PP_FIELDS:
                if r.get(ok_key) is False:
                    pre, _, ciq = str(r.get(key, "")).partition(" | ")
                    rows.append(_mm_row(r.get("cell") or "\u2014", "KGET vs CIQ", label, "KGET",
                                         pre.strip(), ciq.strip()))

    for key, label in (("cell_id_vs_rfds", "CellID"), ("radio_type", "RRU")):
        for r in results.get(key, []):
            if str(r.get("status", "")).upper() != "MISMATCH":
                continue
            pre, ciq = str(r.get("pre", "")).strip(), str(r.get("ciq", "")).strip()
            if not _mm_is_na(pre) and pre != ciq:
                rows.append(_mm_row(r.get("cell") or "\u2014", "KGET vs CIQ", label, "KGET", pre, ciq))

    # TAC: NR is per-cell (pre_nrtac/ciq_nrtac); LTE is one node-level row
    # whose values live only in its note, so the note is carried as-is.
    for r in results.get("nr_tac", []):
        if str(r.get("status", "")).upper() != "MISMATCH":
            continue
        pre, ciq = str(r.get("pre_nrtac") or "").strip(), str(r.get("ciq_nrtac") or "").strip()
        if not _mm_is_na(pre) and pre != ciq:
            rows.append(_mm_row(r.get("cell") or "\u2014", "KGET vs CIQ", "TAC", "KGET", pre, ciq))
    for r in results.get("tac", []):
        if str(r.get("status", "")).upper() == "MISMATCH":
            rows.append({"cell": r.get("node") or "\u2014", "source": "KGET vs CIQ",
                         "param": "TAC", "comments": r.get("note", "")})

    # ── CIQ checks (sanity / uniqueness rules on the CIQ itself) ───────
    for key, label in _MM_CIQ_CHECKS:
        for r in results.get(key, []):
            if str(r.get("status", "")).upper() not in ("MISMATCH", "FAIL", "WARN"):
                continue
            cell = r.get("cell")
            if not cell or _mm_is_na(cell):
                cell = r.get("node") or "\u2014"
            rows.append({"cell": cell, "source": "CIQ check", "param": label,
                         "comments": r.get("note", "") or "\u2014"})

    # ── KGET vs EDP ────────────────────────────────────────────────────
    for r in pre_edp_rows or []:
        if str(r.get("status", "")).lower() != "mismatch":
            continue
        rows.append({"cell": r.get("node") or "\u2014", "source": "KGET vs EDP",
                     "param": r.get("field", "\u2014"),
                     "comments": f"KGET - {r.get('pre_value', '\u2014')} | EDP - {r.get('edp_value', '\u2014')}"})

    seen, unique = set(), []
    for r in rows:
        sig = (r["cell"], r["source"], r["param"], r["comments"])
        if sig not in seen:
            seen.add(sig)
            unique.append(r)
    unique.sort(key=lambda r: (r["source"], r["cell"], r["param"]))
    return unique


# ── Per-validation-run memo ────────────────────────────────────────────
# Streamlit re-executes the WHOLE script on every widget interaction, and
# st.tabs renders every tab's body regardless of which one is on screen.
# So ticking one checkbox in the RRNRBL checklist re-ran all the derived
# work for all five tabs before the next tick could register.
# build_rfds_grouped_rows() was the worst of it: it calls
# extract_rf_inventory_antennas(), whose genuine-PDF path runs pdfplumber
# table extraction (~2.7s on a real RFDS), and it was being called TWICE
# per rerun — once for the RFDS tab and again for the consolidated report
# — so roughly 5s of pure recompute per keystroke/tick.
# None of these inputs change between reruns; they only change when
# 'Run Validation' produces a new state. Memoising against that run
# (cache is reset in the run handler) makes the second and later reruns
# effectively free.
def _memo(key, fn, sig=()):
    # One slot per key: a new sig replaces the old entry rather than adding
    # to it, so the checklist workbook cache can't grow a copy per tick.
    cache = st.session_state.setdefault("_memo", {})
    hit = cache.get(key)
    if hit is not None and hit[0] == sig:
        return hit[1]
    val = fn()
    cache[key] = (sig, val)
    return val
results = state["results"]
ciq_wb = state["ciq_wb"]
site_details = state["site_details"]
edp_rows = state["edp_rows"]
checked_nodes = state["checked_nodes"]
rfds_pages = state["rfds_pages"]
node_logs_text = state["node_logs_text"]
sow = state["sow"]

@st.dialog("Revision History", width="large")
def _show_revision_history_dialog(ciq_wb):
    sheet_name, rows = cer.read_revision_history(ciq_wb)
    if not rows:
        st.caption("No Revision History sheet found in this CIQ.")
        return
    # The sheet stacks TWO mini-tables with different headers (Version/
    # Description/Updated Date/Updated By, then Date/Confirmations
    # Received) — a header row is any row whose first two cells are both
    # non-numeric-looking text, same detection QUICKIX's own renderer uses
    # rather than assuming a fixed row count for the first table.
    def _looks_like_header(row):
        a, b = str(row[0]).strip(), str(row[1]).strip()
        return bool(a) and bool(b) and not any(ch.isdigit() for ch in a[:1])

    blocks, current = [], None
    for row in rows:
        if _looks_like_header(row):
            current = {"header": row, "rows": []}
            blocks.append(current)
        elif current is not None:
            current["rows"].append(row)
        else:
            current = {"header": ["", "", "", "", ""], "rows": [row]}
            blocks.append(current)

    for block in blocks:
        # Keep every header column that has a label OR that any data row in
        # this block actually uses (drops the sheet's trailing blank 5th
        # column when nothing in the block ever fills it, without hiding a
        # legitimately blank-labelled column that does have data).
        n = len(block["header"])
        used = [bool(str(block["header"][i]).strip()) or any(str(r[i]).strip() for r in block["rows"] if i < len(r))
                for i in range(n)]
        columns = [(i, str(block["header"][i]) or f"Col {i+1}") for i in range(n) if used[i]]
        if not columns:
            continue
        st.markdown(render_table(
            [dict(zip(range(n), r)) for r in block["rows"]],
            columns=columns, status_key=None,
        ), unsafe_allow_html=True)


top_l, top_m, top_r = st.columns([1, 1, 4])
with top_l:
    if st.button("🔄 New Validation Run", use_container_width=True):
        st.session_state.clear()
        st.rerun()
with top_m:
    if st.button("📜 Revision History", use_container_width=True):
        _show_revision_history_dialog(ciq_wb)
with top_r:
    bits = [f"Site ID: `{site_details.get('site_id') or '—'}`", f"FA Code: `{site_details.get('fa_code') or '—'}`",
            f"USID: `{site_details.get('usid') or '—'}`", f"Nodes: `{', '.join(checked_nodes) or '—'}`"]
    st.caption(" &nbsp;·&nbsp; ".join(bits), unsafe_allow_html=True)

tab_rfds, tab_pre, tab_ciq, tab_auditpvc, tab_crdesc, tab_edp, tab_consolidated = st.tabs(
    ["RFDS Validation", "Pre checks (AMOS)", "CIQ Checks", "Audit (Pre vs CIQ)", "CR Desc",
     "EDP Validator", "Consolidated Report"]
)

# ══════════════════════════════════════════════════════════════════════
# TAB 1 — RFDS Validation: every RFDS-vs-CIQ(-vs-Pre) comparison the run
# already computed (Primary/Secondary, Board type, XMU, Cells, Cell ID,
# Radio type) — all colour-coded, bordered tables.
# ══════════════════════════════════════════════════════════════════════
with tab_rfds:
    st.subheader("RFDS Validation")
    m1, m2, m3, m4 = st.columns(4)
    m1.markdown(f'<div class="qkx-stat"><b>FA Code</b><br>{esc(site_details.get("fa_code") or "—")}</div>', unsafe_allow_html=True)
    m2.markdown(f'<div class="qkx-stat"><b>USID</b><br>{esc(site_details.get("usid") or "—")}</div>', unsafe_allow_html=True)
    m3.markdown(f'<div class="qkx-stat"><b>Site ID</b><br>{esc(site_details.get("site_id") or "—")}</div>', unsafe_allow_html=True)
    m4.markdown(f'<div class="qkx-stat"><b>Atoll Name</b><br>{esc(site_details.get("atoll_site_name") or "—")}</div>', unsafe_allow_html=True)

    if rfds_pages is None:
        st.info("No RFDS PDF was loaded for this run — RFDS-dependent comparisons below are skipped.")

    _rfds_inventory_text = None
    if rfds_pages is not None:
        _t = rf.find_pages_by_heading(rfds_pages, "Non RF Inventory Details (Final)")
        _rfds_inventory_text = re.sub(r"\s+", "", _t) if _t else None

    def _board_rfds_display(r):
        ciq_du = r.get("ciq_du_type") or ""
        if r.get("rfds_agrees") is True:
            return ciq_du or "FOUND"
        if _rfds_inventory_text:
            candidates = sorted(set(re.findall(r"\d{4,5}", _rfds_inventory_text)) - {ciq_du})
            return "/".join(candidates[:3]) if candidates else "NOT FOUND"
        return "NOT CHECKED"

    def _ciq_xmu_count(node_id, ciq_wb):
        mm = next((m for m in cer.mixed_mode_rows(ciq_wb)
                   if str(m.get("Node to be built as") or m.get("eNodeB Name") or "").strip() == node_id), None)
        if mm is None:
            return None
        e_name, g_name = mm.get("eNodeB Name"), mm.get("gNodeB Name")
        row = None
        if e_name and "eNB Info" in ciq_wb.sheetnames:
            row = next((r for r in cer.sheet_rows_as_dicts(ciq_wb["eNB Info"])
                        if str(r.get("eNodeB Name", "")).strip().upper() == str(e_name).strip().upper()), None)
        if row is None and g_name and "gNB Info" in ciq_wb.sheetnames:
            row = next((r for r in cer.sheet_rows_as_dicts(ciq_wb["gNB Info"])
                        if str(r.get("gNodeB Name", "")).strip().upper() == str(g_name).strip().upper()), None)
        if row is None:
            return None
        return sum(1 for k in ("1st XMU", "2nd XMU", "3rd XMU") if str(row.get(k, "")).strip().upper() == "YES")

    with st.container(border=True):
        section_title("Primary & Secondary Node")
        rows = results.get("primary_secondary", [])
        display_rows = [
            dict(r, comments="Match" if r.get("status") != "MISMATCH" else
                 f"Mismatch found on {wt.primary_secondary_mismatched_role(r)} id on {r.get('node')}.")
            for r in rows
        ]
        st.markdown(render_table_with_comments(display_rows, columns=[("node", "Node"), ("ciq", "CIQ"),
                                                                        ("edp", "EDP"), ("rfds", "RFDS")],
                                                note_key="comments"),
                    unsafe_allow_html=True)

    with st.container(border=True):
        section_title("Board Type")
        rows = results.get("board_type", [])
        display_rows = [
            dict(r, rfds=_board_rfds_display(r),
                 comments=("Match" if r.get("status") == "MATCH" else
                           f"Board swap (expected) on {r.get('node')}." if r.get("status") == "EXPECTED" else
                           f"Board type mismatch found on the {r.get('node')}."))
            for r in rows
        ]
        st.markdown(render_table_with_comments(display_rows, columns=[("node", "Node"), ("ciq_du_type", "CIQ DU Type"),
                                                                        ("edp_model", "EDP Model"), ("rfds", "RFDS")],
                                                note_key="comments"),
                    unsafe_allow_html=True)

    with st.container(border=True):
        section_title("XMU Validation")
        rows = results.get("xmu", [])
        display_rows = []
        for r in rows:
            n = _ciq_xmu_count(r.get("node"), ciq_wb)
            ciq_label = f"{n} XMU" if n else ("0 XMU" if n == 0 else "—")
            rfds_val = r.get("rfds_xmu")
            rfds_label = "XMU Found" if rfds_val is True else ("XMU Not Found" if rfds_val is False else "NOT CHECKED")
            status = r.get("status")
            if status == "MATCH":
                comments = "Match"
            elif status == "MISMATCH":
                comments = f"XMU mismatch found on the {r.get('node')}."
            else:
                comments = r.get("note") or "Not checked."
            display_rows.append(dict(r, ciq_xmu=ciq_label, rfds_xmu=rfds_label, comments=comments))
        st.markdown(render_table_with_comments(display_rows, columns=[("node", "Node"), ("ciq_xmu", "CIQ XMU"),
                                                                        ("rfds_xmu", "RFDS XMU")],
                                                note_key="comments"),
                    unsafe_allow_html=True)

    with st.container(border=True):
        grouped_rows = _memo("grouped_rows", lambda: build_rfds_grouped_rows(
            results, ciq_wb, rfds_pages, state.get("rfds_bytes")))
        n_fail = sum(1 for r in grouped_rows if r["overall"] == "FAIL")
        n_pass = len(grouped_rows) - n_fail
        st.markdown(
            f'<div style="text-align:right;font-weight:700;margin:0 0 8px;">'
            f'Total: {len(grouped_rows)} &nbsp;|&nbsp; '
            f'<span style="color:#065f46;">PASS: {n_pass}</span> &nbsp;|&nbsp; '
            f'<span style="color:#991b1b;">FAIL: {n_fail}</span></div>',
            unsafe_allow_html=True,
        )
        st.markdown(render_rfds_grouped_table(grouped_rows), unsafe_allow_html=True)

# ══════════════════════════════════════════════════════════════════════
# TAB 2 — Audit: Pre checks (AMOS) / CIQ Checks / Audit (Pre vs CIQ) / CR Desc
# ══════════════════════════════════════════════════════════════════════

with tab_pre:
    if not node_logs_text:
        st.info("No Pre kget-all logs were loaded for this run.")
    else:
        summary_rows, lte_rows, nr_rows = state["amos_summary_rows"], state["amos_lte_rows"], state["amos_nr_rows"]

        section_title("Node Summary", badge=f"{len(summary_rows)} NODE(S)")
        st.markdown(render_table(summary_rows, status_key=None, columns=[
            ("node", "Node ID"), ("sw_package", "BB Type"), ("sw_version", "SW Version"),
            ("type", "Mode"), ("ptp_status", "PTP Status"), ("sa_nsa_status", "SA/NSA Status"),
            ("vonr_status", "VoNR Status"),
        ]), unsafe_allow_html=True)

        section_title(f"LTE Cells — {', '.join(summary_rows and [r['node'] for r in summary_rows] or sorted(node_logs_text))}",
                      badge=f"{len(lte_rows)} CELLS")
        st.markdown(render_table(lte_rows, status_key=None, columns=[
            ("node", "Node"), ("cell", "Cell"), ("sector_carrier", "Sector Carries"), ("rru", "RRUs"),
            ("radio_type", "Radio Type"), ("sharing_radio", "Sharing Radio"), ("tx", "TX"), ("rx", "RX"),
            ("rfbranch_tx_ref", "RFBRANCHTXREF"), ("rfbranch_rx_ref", "RFBRANCHRXREF"),
            ("sef_rfbranches", "SEF RFBRANCHES"), ("pre_existing_dss", "Pre Existing DSS"),
            ("rilink_id", "RiLink ID"), ("rilink_port", "RiLink Port"), ("rilink_type", "RiLink"),
            ("air_if_load_profile", "AirIfLoadProfile"),
            ("eutranfreqcheck", "EutranFreqCheck"),
        ], html_cols={"eutranfreqcheck"}), unsafe_allow_html=True)

        section_title("5G NR Cells", badge=f"{len(nr_rows)} CELLS")
        st.markdown(render_table(nr_rows, status_key=None, columns=[
            ("node", "Node"), ("cell", "Cell"), ("rru", "RRUs"), ("tx", "TX"), ("rx", "RX"),
            ("sef_rfbranches", "SEF RFBRANCHES"),
            ("rilink_id", "RiLink ID"), ("rilink_port", "RiLink Port"), ("rilink_type", "RiLink"),
        ]), unsafe_allow_html=True)

with tab_ciq:
    import ciq_checks as cc

    controller_rows = cv.build_controller_info(ciq_wb)
    if controller_rows:
        col1, col2 = st.columns([2, 1])
        with col1:
            section_title("Node Integration")
            st.markdown(render_table(cv.build_node_integration(ciq_wb), status_key=None, columns=[
                ("node", "Node"), ("eNBId", "ENBID"), ("eNodeB", "ENODEB"), ("gNBId", "GNBID"), ("gNodeB", "GNODEB"),
                ("mode", "Mode"), ("bb_type", "BB Type"), ("mme_region", "MME Region"), ("enm", "ENM"),
                ("xmu", "XMU"), ("ports", "Ports"),
            ]), unsafe_allow_html=True)
        with col2:
            section_title("Controller Info")
            st.markdown(render_table(controller_rows, status_key=None, columns=[
                ("usid", "USID"), ("controller", "Controller"), ("id", "ID"),
            ]), unsafe_allow_html=True)
    else:
        section_title("Node Integration")
        st.markdown(render_table(cv.build_node_integration(ciq_wb), status_key=None, columns=[
            ("node", "Node"), ("eNBId", "ENBID"), ("eNodeB", "ENODEB"), ("gNBId", "GNBID"), ("gNodeB", "GNODEB"),
            ("mode", "Mode"), ("bb_type", "BB Type"), ("mme_region", "MME Region"), ("enm", "ENM"),
            ("xmu", "XMU"), ("ports", "Ports"),
        ]), unsafe_allow_html=True)

    ciq_lte_rows = cc.build_lte_ciq_rows(ciq_wb, rbb_results=results.get("rbb_tx_isdlonly_4g", []))
    ciq_nr_rows = cc.build_nr_ciq_rows(ciq_wb)
    cc.apply_link_and_sharing(ciq_lte_rows, ciq_nr_rows)

    section_title("LTE E-UTRAN Parameters", badge=f"{len(ciq_lte_rows)}")
    st.markdown(render_table(ciq_lte_rows, status_key="status", columns=[
        ("node", "Node"), ("cell", "Cell"), ("pci", "PCI"), ("cell_id", "Cell ID"),
        ("electrical_tilt", "Electrical Tilt"),
        ("rbb_type", "RBB Type Verification"), ("tx", "TX"), ("rx", "RX"),
        ("riport", "RIPORT"), ("sharing_radio", "Sharing Radio"),
        ("link", "Link (Single/Doublelink)"), ("comments_html", "Comments/Warning"),
    ]), unsafe_allow_html=True)

    section_title("5G NR Parameters", badge=f"{len(ciq_nr_rows)}")
    st.markdown(render_table(ciq_nr_rows, status_key="status", columns=[
        ("node", "Node"), ("cell", "Cell"), ("sef", "SEF"), ("fru", "FRU"), ("nr_pci", "NR PCI"),
        ("cell_id", "Cell ID"),
        ("electrical_tilt", "Electrical Tilt"), ("rbb_type", "RBB Type Verification"), ("riport", "RIPORT"),
        ("sharing_radio", "Sharing Radio"), ("link", "Link (Single/Doublelink)"), ("comments_html", "Comments/Warning"),
    ]), unsafe_allow_html=True)

    antenna_rows = cs.check_antenna_uniqueness(node_id="", ciq_wb=ciq_wb)
    section_title("Antenna Uniqueness", badge=f"{len(antenna_rows)}")
    st.markdown(render_table(antenna_rows, status_key="status", columns=[
        ("cell", "Cells"), ("aug_au_asu_1", "AUG/AU/ASU (1)"), ("aug_au_asu_2", "AUG/AU/ASU (2)"),
        ("verdict", "Status"),
    ]), unsafe_allow_html=True)

with tab_auditpvc:
    import pre_post_audit as ppa

    section_title("Pre vs Post")
    pre_summary_rows = state["amos_summary_rows"]
    ciq_node_rows = cv.build_node_integration(ciq_wb)
    node_pre_post_rows = ppa.build_node_pre_post(pre_summary_rows, ciq_node_rows, node_logs_text, edp_rows, ciq_wb=ciq_wb)
    st.markdown(render_node_pre_post_table(node_pre_post_rows), unsafe_allow_html=True)

    if node_logs_text:
        lte_pp_rows = ppa.compare_lte_cell_level(node_logs_text, ciq_wb)
        lte_pp_summary = ppa.summarize_rows(lte_pp_rows)
        section_title("LTE: Pre vs Post")
        st.markdown(_pre_post_summary_pills(lte_pp_summary), unsafe_allow_html=True)
        st.caption("Green = Match  Red = Mismatch  Format: PRE | POST")
        st.markdown(render_cell_pre_post_table(lte_pp_rows, [
            ("sc", "_sc_ok", "Sec Carrier"), ("cellid", "_cellid_ok", "Cell ID"), ("tac", "_tac_ok", "TAC"),
            ("bw", "_bw_ok", "BW"), ("dl", "_dl_ok", "EARFCN DL"), ("ul", "_ul_ok", "EARFCN UL"),
            ("power", "_power_ok", "Power"), ("tx", "_tx_ok", "TX"), ("rx", "_rx_ok", "RX"),
            ("rru", "_rru_ok", "RRU Model"), ("cellrange", "_cellrange_ok", "Cell Range"),
            ("dss", "_dss_ok", "DSS"),
        ]), unsafe_allow_html=True)

        nr_pp_rows = ppa.compare_nr_cell_level(node_logs_text, ciq_wb)
        nr_pp_summary = ppa.summarize_rows(nr_pp_rows)
        section_title("5G: Pre vs Post")
        st.markdown(_pre_post_summary_pills(nr_pp_summary), unsafe_allow_html=True)
        st.caption("Green = Match  Red = Mismatch  Format: PRE | POST")
        st.markdown(render_cell_pre_post_table(nr_pp_rows, [
            ("cellid", "_cellid_ok", "Cell ID"), ("dl", "_dl_ok", "ARFCN DL"), ("ul", "_ul_ok", "ARFCN UL"),
            ("bw_dl", "_bw_dl_ok", "BW DL"), ("bw_ul", "_bw_ul_ok", "BW UL"), ("power", "_power_ok", "TX Power"),
            ("ssb", "_ssb_ok", "SSB Frequency"), ("rru", "_rru_ok", "RRU Model"),
            ("cellrange", "_cellrange_ok", "Cell Range"), ("dss", "_dss_ok", "DSS"),
        ]), unsafe_allow_html=True)
    else:
        st.caption("Upload Pre kget-all logs to see the LTE/5G cell-level Pre vs Post tables.")

    # Engineer Comments is computed silently here (not displayed in this
    # tab) purely so CR Desc's auto-detected Nodes/Bands still populate —
    # CR Desc reads state["engineer_comments"] via extract_bands_from_comments().
    amos_lte_rows = state["amos_lte_rows"] if node_logs_text else None
    amos_nr_rows = state["amos_nr_rows"] if node_logs_text else None
    ciq_lte_rows = cv.build_param_table(ciq_wb, "eUtran Parameters", ["EutranCellFDDId", "RRU type"])
    ciq_nr_rows = cv.build_param_table(ciq_wb, "5G Info", ["NRCellDU", "RRU Type"])
    state["engineer_comments"] = build_engineer_comments(
        sow, results, checked_nodes,
        amos_lte_rows=amos_lte_rows, amos_nr_rows=amos_nr_rows,
        ciq_lte_rows=ciq_lte_rows, ciq_nr_rows=ciq_nr_rows,
        node_logs_text=node_logs_text,
    )

with tab_crdesc:
    section_title("CR Description")
    engineer_comments = state.get("engineer_comments", [])
    all_nodes, deleted_nodes_cr, regular_nodes_cr = extract_nodes_from_audit(sow, checked_nodes)
    bands_cr = extract_bands_from_comments(engineer_comments)

    c1, c2, c3 = st.columns(3)
    with c1:
        mic_mca = st.selectbox("MIC DESC", ["MIC - MCA", "MCA - CRAN"], key="cr_mic_mca")
    with c2:
        site_name_in = st.text_input("Site Name", placeholder="e.g. DOWNTOWN_EAST", key="cr_site_name")
    with c3:
        # Auto-fetched from the CIQ's own 5G Info 'FA Code' column
        # (site_details['fa_code'] is always CIQ-sourced - see
        # checks_node.build_site_details()) - still editable, since the
        # user may need to override it.
        fa_number_in = st.text_input("FA Number", value=site_details.get("fa_code") or "",
                                      placeholder="e.g. 1034567", key="cr_fa_number")
    c4, c5 = st.columns(2)
    with c4:
        sw_version_in = st.text_input("Sw Version", placeholder="e.g. 25.Q4", key="cr_sw_version")
    with c5:
        link_in = st.text_input("Link", placeholder="link to CIQ / ticket / script", key="cr_link")

    rfds_fa = site_details.get("rfds_fa_code")
    if rfds_fa:
        ciq_fa = site_details.get("fa_code")
        if ciq_fa and rfds_fa != ciq_fa:
            st.warning(f"FA Code mismatch — CIQ: `{ciq_fa}` vs RFDS: `{rfds_fa}`. "
                       f"The field above uses the CIQ value; verify which is correct before sending.")
        else:
            st.caption(f"FA Code confirmed — CIQ and RFDS both report `{ciq_fa}`.")
    elif rfds_pages is not None:
        st.caption("RFDS was provided but no FA Code was found on it — CIQ value used, not cross-checked.")

    n1, n2 = st.columns(2)
    with n1:
        st.markdown("**Nodes (from Audit)** — auto-detected")
        st.markdown(", ".join(all_nodes) if all_nodes else "_Run validation to auto-populate…_")
    with n2:
        st.markdown("**Bands (from Audit)** — auto-detected")
        st.markdown(" / ".join(bands_cr) if bands_cr else "_Run validation to auto-populate…_")

    if st.button("Generate CR Description", type="primary", key="btn_gen_cr"):
        cr_text, breakdown = build_cr_description(mic_mca, site_name_in, fa_number_in, all_nodes, bands_cr)
        if cr_text is None:
            st.error("Please enter Site Name and FA Number (and make sure a validation run has produced node data).")
        else:
            st.session_state["cr_output"] = cr_text
            st.session_state["cr_breakdown"] = breakdown

    if st.session_state.get("cr_output"):
        st.text_area("Generated CR description", value=st.session_state["cr_output"], height=80, key="cr_output_area")
        st.markdown(render_table(
            [{"field": k, "value": v} for k, v in (st.session_state.get("cr_breakdown") or [])],
            columns=[("field", "Field"), ("value", "Value")], status_key=None,
        ), unsafe_allow_html=True)

    st.divider()
    email_text = build_radio_ret_email(sw_version_in, fa_number_in, link_in, engineer_comments)
    st.text_area("Radio/RET Comments Email", value=email_text, height=260, key="cr_email_area")

# ══════════════════════════════════════════════════════════════════════
# TAB 3 — EDP Validator
# ══════════════════════════════════════════════════════════════════════
with tab_edp:
    st.subheader("EDP Validator")
    node_role_list = state["node_role_list"]

    section_title("EDP Field Values — Primary & Secondary Nodes")
    edp_field_rows = state["edp_field_rows"]
    st.markdown(render_table(edp_field_rows, status_key=None, columns=[
        ("node", "Node"), ("role", "Role"), ("SITE_NAME", "SITE_NAME"), ("CABINET", "CABINET"),
        ("BBU_TYPE", "BBU_TYPE"), ("NODE_MODEL", "NODE_MODEL"), ("SIAD_PORT_SIZE_BBU", "SIAD_PORT_SIZE_BBU"),
        ("SIAD_PORT_FACING_BBU", "SIAD_PORT_FACING_BBU"), ("BEARER_ENODEB_SB_VLAN_ID", "BEARER_ENODEB_SB_VLAN_ID"),
        ("IPV6_SIAD_BEARER_IP_DEF_ROUTER", "IPV6_SIAD_BEARER_IP_DEF_ROUTER"),
        ("IPV6_ENODEB_BEARER_IP", "IPV6_ENODEB_BEARER_IP"),
        ("OAM_ENODEB_SIAD_OAM_VLAN", "OAM_ENODEB_SIAD_OAM_VLAN"),
        ("IPV6_SIAD_OAM_IP_DEF_ROUTER", "IPV6_SIAD_OAM_IP_DEF_ROUTER"),
        ("IPV6_ENODEB_OAM_IP", "IPV6_ENODEB_OAM_IP"),
    ]), unsafe_allow_html=True)

    section_title("Pre vs EDP — Bearer & OAM IPv6/VLAN")
    if not node_logs_text:
        st.caption("Upload Pre kget-all logs to compare these fields against EDP.")
    else:
        pivot_rows = state["pre_edp_pivot_rows"]
        if not pivot_rows:
            st.caption("No Pre log matched any Primary/Secondary node for this run.")
        else:
            st.markdown(render_pre_vs_edp_pivot_table(pivot_rows), unsafe_allow_html=True)

# ══════════════════════════════════════════════════════════════════════
# TAB 4 — Consolidated Report: Pre/Post Config → SOW Summary → Warnings &
# Comments (always visible) → RRNRBL Checklist → RFDS vs CIQ & Pre vs CIQ
# → CIQ Sanity Check → EDP Checks (each collapsible) → PDF/xlsx downloads.
# ══════════════════════════════════════════════════════════════════════
with tab_consolidated:
    st.subheader("Consolidated Report")

    section_title("Pre / Post Configuration")
    pc1, pc2 = st.columns(2)
    pc1.markdown(f'<div class="qkx-stat" style="text-align:left;"><b>Pre</b><br>{esc(state["pre_text"] or "(none — new build / no Pre log)")}</div>', unsafe_allow_html=True)
    pc2.markdown(f'<div class="qkx-stat" style="text-align:left;"><b>Post</b><br>{esc(state["post_text"] or "—")}</div>', unsafe_allow_html=True)

    section_title("SOW Summary")
    scope_lines = state["scope_lines"]
    if scope_lines:
        st.markdown("\n".join(f"- {esc(l)}" for l in scope_lines))
    else:
        st.caption("Nothing to report.")

    # Isolated in its own fragment: Streamlit reruns the WHOLE script (all
    # 5 tabs, every dataframe/table on screen) on ANY widget interaction,
    # even just editing one Remarks cell here - _memo already stops the
    # expensive derived-data RECOMPUTE on that rerun, but it never stopped
    # the RE-RENDER of everything else on the page, which is real cost too
    # (confirmed real symptom: ~5s per comment edit). st.fragment scopes a
    # widget interaction INSIDE it to rerunning only this function, not the
    # surrounding script - editing a Tick/Remarks cell here no longer
    # touches the other 4 tabs at all. The download button/xlsx build are
    # inside the SAME fragment (not left outside it) so the downloaded file
    # stays live-synced with the grid instead of needing one more, separate
    # interaction to pick up the latest edit.
    @st.fragment
    def _render_rrnrbl_section():
        with st.expander("RRNRBL Checklist", expanded=False):
            render_rrnrbl_checklist(state["checklist"])

        st.divider()
        manual_overrides = collect_manual_overrides(state["checklist"])
        # Keyed on the overrides themselves: reruns that don't touch a tick
        # or a remark reuse the built workbook instead of rebuilding it.
        _ov_sig = tuple(sorted((r, bool(v.get("checked")), str(v.get("comment") or ""))
                                for r, v in manual_overrides.items()))
        checklist_xlsx = _memo("checklist_xlsx",
                               lambda: rc.fill_checklist_xlsx(state["checklist"], state["site_id_fa"],
                                                              manual_overrides=manual_overrides),
                               _ov_sig)
        st.download_button("⬇️ Download filled RRNRBL Checklist (.xlsx)", data=checklist_xlsx,
                            file_name="Checklist_RRNRBL_filled.xlsx",
                            mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                            use_container_width=True, key="cr_checklist_dl")
        st.caption("Edits above are baked into the download automatically.")

    _render_rrnrbl_section()

    # ── Every mismatch in one place, grouped by comparison family ──────
    # One section (not three separate expanders to hunt through), but the
    # rows stay categorised under the three headings below rather than
    # being flattened into an undifferentiated list.
    section_title("Mismatches")
    mm_rows = _memo("mm_rows", lambda: build_consolidated_mismatches(
        _memo("grouped_rows", lambda: build_rfds_grouped_rows(
            results, ciq_wb, rfds_pages, state.get("rfds_bytes"))),
        results,
        rc.build_pre_vs_edp_ipv6_table(node_logs_text, state["node_role_list"], edp_rows)
        if node_logs_text else [],
        edp_rows=edp_rows,
        edp_node_ids=([n["node"] for n in state["node_role_list"]] or checked_nodes),
        ciq_wb=ciq_wb,
        node_logs_text=node_logs_text,
        node_role_list=state["node_role_list"],
        site_details=site_details))

    # category heading -> which "source" values belong under it
    MM_GROUPS = [
        ("Mismatches \u2014 RFDS vs CIQ & KGET vs CIQ", ("RFDS vs CIQ", "KGET vs CIQ")),
        ("CIQ Sanity Check", ("CIQ check",)),
        ("EDP Checks \u2014 KGET vs EDP", ("KGET vs EDP",)),
    ]

    if not mm_rows:
        st.success("No mismatches found.")
    else:
        st.caption(f"**{len(mm_rows)}** mismatch(es) found across {len(MM_GROUPS)} categories.")
        for heading, sources in MM_GROUPS:
            group = [r for r in mm_rows if r["source"] in sources]
            st.markdown(f'<div class="qkx-sec-sub">{esc(heading)} '
                        f'<span class="qkx-count-pill"><b>{len(group)}</b></span></div>',
                        unsafe_allow_html=True)
            if not group:
                st.caption("No mismatches in this category.")
                continue
            st.markdown(render_table(group, status_key=None, columns=[
                ("cell", "Cell / Node"), ("source", "Mismatch on"),
                ("param", "Parameter"), ("comments", "Comments"),
            ]), unsafe_allow_html=True)
