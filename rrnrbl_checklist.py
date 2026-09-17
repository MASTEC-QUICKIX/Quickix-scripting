"""
rrnrbl_checklist.py

Maps the results dict produced by run_validation.run() onto the 63-item
"Legacy - N2e Engineer Checklist" sheet (Checklist_RRNRBL.xlsx) and can
write a filled copy of that exact template (Site ID/FA + Date filled in,
each row's checkbox + Comments column set from the validation results).

Design note on honesty: every row below is wired to a REAL existing check
where one exists (by 'rule' tag - see checks_node.py / checks_sector.py),
a newly-added check where the data was clearly available (EDP field rules,
MME Region, NR_SA tab, FA Code CIQ-vs-RFDS), or left 'manual' when no
reliable signal exists. Nothing here fabricates a pass.
"""
import datetime
import io
import os
import re

import openpyxl

import ciq_edp_reader as cer
from band_labels import SECTOR_ORDER, is_5g_cell

TEMPLATE_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "Combined_NRBL-RR Checklist - V3.xlsx")


def _log_text_for(entry, node_logs_text):
    """The Pre log text for a node_role_list entry - a Secondary's own
    name never appears as a log filename/AMOS prompt, so log_alias (set
    to the Primary on the same Mixed Mode Info row) is used instead when
    present."""
    return (node_logs_text or {}).get(entry.get("log_alias") or entry["node"])


def _bearer_pre_value(pre_vals, pre_key, entry):
    """Pick the right side of a bearer_vlan/bearer_ip/bearer_router_ip
    value out of pre_extract.extract_bearer_oam_ipv6()'s result, using the
    entry's tech (LTE/NR) when set - REQUIRED on a TMBB node, where both
    identities' bearer values live in the same dict and the flat
    (untagged) key only ever holds the LTE side. Falls back to the flat
    key for OAM fields (no _lte/_nr split - confirmed shared) and for any
    entry with no tech (non-TMBB, single-technology log).

    NO fallback to the flat key when tech IS set and its own value is
    missing — confirmed real bug: a genuinely new Secondary (gNodeB not
    added to this node's Pre config yet, only appearing in the Post/CIQ
    design — confirmed real case, TNL01216/TNMN001216, log has no
    InterfaceIPv6=NR at all) got the Primary's own LTE value silently
    substituted in, since the flat key is `bearer_vlan_lte or
    bearer_vlan_nr` and the old `or pre_vals.get(pre_key)` fallback
    reached for it whenever the NR side was None. A missing tech-specific
    value must surface as no-data, never as the other technology's value.

    A Secondary identity has NO OAM of its own — OAM belongs to the
    physical node as a whole and is reported once, under the Primary
    only (confirmed: EDP itself never publishes a separate OAM target
    for a Secondary). So oam_* fields return None here for a Secondary
    entry rather than the log's single shared OAM value, which would
    otherwise look like it belongs to the Secondary too."""
    if entry.get("role") == "Secondary" and pre_key.startswith("oam_"):
        return None
    tech = entry.get("tech")
    if tech and pre_key in ("bearer_vlan", "bearer_ip", "bearer_router_ip"):
        return pre_vals.get(f"{pre_key}_{tech.lower()}")
    return pre_vals.get(pre_key)

STATUS_META = {
    "match": ("PASS", True),
    "mismatch": ("FAIL", False),
    "manual": ("MANUAL", False),
    "unknown": ("NO DATA", False),
    "na": ("N/A", False),
    "info": ("INFO", False),
}


# ══════════════════════════════════════════════════════════════════════
# Generic aggregation helpers over the existing checks_node/checks_sector
# result lists (every item in those lists already carries a 'status' of
# MATCH / MISMATCH / SKIPPED / INFO - see checks_sector.py).
# ══════════════════════════════════════════════════════════════════════

def _agg_row66(cell_id_results, uniqueness_results):
    """Row 66: combines the Pre-vs-CIQ cellId comparison (cell_id_vs_rfds,
    shared with rows 40/59/74) with the NEW cellId uniqueness check —
    unlike PCI, cellId must be unique across ALL bands on one node, not
    scoped per-band. Same grouping convention as the other rows."""
    all_results = cell_id_results + uniqueness_results
    if not all_results:
        return "unknown", "No data (check did not run for this site)."
    real = [r for r in all_results if r.get("status") not in (None, "SKIPPED")]
    bad = [r for r in real if r.get("status") == "MISMATCH"]
    if not bad:
        if real:
            return "match", f"{len(real)} checked, no mismatch."
        skipped_notes = {r.get("note") for r in all_results if r.get("note")}
        return "unknown", "; ".join(sorted(skipped_notes)) or "Skipped for every node (no Pre log / no RFDS)."

    def _reason(r):
        if r.get("rule") == "#66U":
            return "Cell ID uniqueness clash"
        return "Cell ID mismatch"

    return "mismatch", _group_bad_by_node_reason(bad, real, _reason)


def _agg_carrier_progression(carrier_results):
    """Row 64: same as _agg, except the pass message is
    check_carrier_progression's own 'Each carrier maps to a single
    band.' instead of the generic 'N checked, no mismatch.' — the
    function runs once per node and returns one lone MATCH row when
    clean, so _agg's count (number of nodes, not cells) was misleading."""
    if not carrier_results:
        return "unknown", "No data (check did not run for this site)."
    real = [r for r in carrier_results if r.get("status") not in (None, "SKIPPED")]
    bad = [r for r in real if r.get("status") == "MISMATCH"]
    if bad:
        parts = []
        for r in bad[:6]:
            bits = [str(r.get(f)) for f in ("node", "cell", "note") if r.get(f)]
            parts.append(": ".join(bits) if bits else str(r))
        more = f" (+{len(bad)-6} more)" if len(bad) > 6 else ""
        return "mismatch", "; ".join(parts) + more
    if real:
        return "match", "Each carrier maps to a single band."
    skipped_notes = {r.get("note") for r in carrier_results if r.get("note")}
    return "unknown", "; ".join(sorted(skipped_notes)) or "Skipped for every node (no Pre log / no RFDS)."


def _agg_port_uniqueness(port_results):
    """Rows 51/67 (Riport uniqueness): same as _agg, except the pass
    message is 'No port clash, all RIports unique.' instead of the
    generic 'N checked, no mismatch.' A mismatch still names exactly
    which port is reused and by which cells, same as _agg's default
    itemization."""
    if not port_results:
        return "unknown", "No data (check did not run for this site)."
    real = [r for r in port_results if r.get("status") not in (None, "SKIPPED")]
    bad = [r for r in real if r.get("status") == "MISMATCH"]
    if bad:
        parts = []
        for r in bad[:6]:
            bits = [str(r.get(f)) for f in ("node", "cell", "note") if r.get(f)]
            parts.append(": ".join(bits) if bits else str(r))
        more = f" (+{len(bad)-6} more)" if len(bad) > 6 else ""
        return "mismatch", "; ".join(parts) + more
    if real:
        return "match", "No port clash, all RIports unique."
    skipped_notes = {r.get("note") for r in port_results if r.get("note")}
    return "unknown", "; ".join(sorted(skipped_notes)) or "Skipped for every node (no Pre log / no RFDS)."


def _agg(results_list, note_fields=("node", "cell", "note")):
    """Any MISMATCH -> mismatch. Only MATCH/INFO seen -> match. Nothing but
    SKIPPED (or empty) -> unknown (no data to judge, not a pass)."""
    if not results_list:
        return "unknown", "No data (check did not run for this site)."
    real = [r for r in results_list if r.get("status") not in (None, "SKIPPED")]
    bad = [r for r in real if r.get("status") == "MISMATCH"]
    if bad:
        parts = []
        for r in bad[:6]:
            bits = [str(r.get(f)) for f in note_fields if r.get(f)]
            parts.append(": ".join(bits) if bits else str(r))
        more = f" (+{len(bad)-6} more)" if len(bad) > 6 else ""
        return "mismatch", "; ".join(parts) + more
    if real:
        return "match", f"{len(real)} checked, no mismatch."
    skipped_notes = {r.get("note") for r in results_list if r.get("note")}
    return "unknown", "; ".join(sorted(skipped_notes)) or "Skipped for every node (no Pre log / no RFDS)."


def _group_bad_by_node_reason(bad, real, reason_of):
    """Shared core for the 'not found in RFDS' / RRU / Cell ID grouping:
    when 2+ cells on the SAME node fail for the SAME reason, produce one
    summary line (count + involved bands) instead of listing each cell's
    full detail. A LONE mismatch on a node keeps its full per-cell detail
    (Pre/CIQ/RFDS values, etc.) — a single discrepancy is worth seeing in
    full, not compressed. reason_of(r) returns the grouping key's human
    label (e.g. 'not found in RFDS').

    Within a grouped line, each band shows its SECTOR letters too when
    only PART of that band failed — confirmed real need: 'AWS_1 Alpha,
    Beta' failing while Gamma passes must stay visible even once grouped,
    not collapse to a bare band name that reads as if all sectors failed.
    'real' (every checked result, match + mismatch) is the denominator
    used to tell whole-band-failed (just the band name) from
    partial-band-failed (band name + the specific sector letters) —
    using 'bad' alone can't make that distinction, since it has no record
    of which sectors PASSED."""
    checked_sectors = {}
    for r in real:
        key = (r.get("node"), r.get("label"))
        checked_sectors.setdefault(key, set()).add(r.get("sector"))

    by_node_reason = {}
    for r in bad:
        key = (r.get("node"), reason_of(r))
        by_node_reason.setdefault(key, []).append(r)

    parts = []
    for (node, reason), entries in by_node_reason.items():
        if len(entries) == 1:
            r = entries[0]
            bits = [str(r.get(f)) for f in ("node", "cell", "note") if r.get(f)]
            parts.append(": ".join(bits) if bits else str(r))
            continue
        failed_sectors_by_label = {}
        for r in entries:
            failed_sectors_by_label.setdefault(r.get("label") or "unknown band", set()).add(r.get("sector"))
        label_parts = []
        for label, failed in sorted(failed_sectors_by_label.items()):
            total = checked_sectors.get((node, label), set())
            if total and failed >= total:
                label_parts.append(label)
            else:
                ordered = [s for s in SECTOR_ORDER if s in failed] or sorted(s for s in failed if s)
                label_parts.append(f"{label} ({', '.join(ordered)})" if ordered else label)
        parts.append(f"{node}: {len(entries)} sector(s) with {reason} ({', '.join(label_parts)}).")
    more = f" (+{len(parts)-6} more)" if len(parts) > 6 else ""
    return "; ".join(parts[:6]) + more


def _agg_row60(cells_results):
    """Row 60: EutranCellFDDId presence, CIQ vs RFDS — filtered to LTE
    cells only from cells_vs_rfds (which combines LTE+5G), using
    is_5g_cell() to tell them apart. beamDirection has NO RFDS extraction
    anywhere (same gap as row 47 — that data lives on RFDS's still-unbuilt
    'AntennaPositionDetails' page), so it's always flagged for manual
    verification regardless of the automated portion's outcome. A clean
    automated pass is 'info' (blue), not 'match' — partially checked, not
    a full pass."""
    lte_results = [r for r in cells_results if r.get("cell") and not is_5g_cell(r["cell"])]
    manual_note = "Verify the beamDirection manually."
    if not lte_results:
        return "manual", manual_note
    real = [r for r in lte_results if r.get("status") not in (None, "SKIPPED")]
    bad = [r for r in real if r.get("status") == "MISMATCH"]
    if bad:
        def _reason(r):
            return "not found in RFDS" if r.get("note") == "Not found in RFDS." else "found in RFDS but not in CIQ"
        return "mismatch", _group_bad_by_node_reason(bad, real, _reason) + " " + manual_note
    if real:
        return "info", f"{len(real)} checked, no mismatch. {manual_note}"
    skipped_notes = {r.get("note") for r in lte_results if r.get("note")}
    base = "; ".join(sorted(skipped_notes)) or "Skipped for every node (no Pre log / no RFDS)."
    return "manual", f"{base} {manual_note}"


def _agg_row47(cells_results, cell_id_results, radio_results, nrcelldu_results, antenna_results):
    """Row 47: automates NRCellDU/NRCellCU (internal consistency),
    cellLocalId, RRU Type, and Antenna Type against RFDS/CIQ — same
    grouping as row 31 (_agg_cell_details). Electrical Tilt and
    BeamDirection have NO RFDS extraction at all (that data lives on
    RFDS's 'AntennaPositionDetails' page, which is row 32's own
    still-unbuilt placeholder) — always flagged for manual verification
    regardless of the automated portion's outcome, since this row can
    never be a full pass on its own.

    Status: a genuine automated MISMATCH stays 'mismatch' (red) with the
    manual-verify note appended, not overridden — an automated failure is
    still a failure. Only a clean automated pass becomes 'info' (blue):
    partially checked, not a full match, since Tilt/BeamDirection were
    never actually verified."""
    all_results = cells_results + cell_id_results + radio_results + nrcelldu_results + antenna_results
    manual_note = "Verify the Electrical Tilt, BeamDirection manually."
    if not all_results:
        return "manual", manual_note
    real = [r for r in all_results if r.get("status") not in (None, "SKIPPED")]
    bad = [r for r in real if r.get("status") == "MISMATCH"]
    if bad:
        def _reason(r):
            rule = r.get("rule")
            if rule == "#6/#18":
                return "not found in RFDS" if r.get("note") == "Not found in RFDS." else "found in RFDS but not in CIQ"
            if rule == "#6/#24":
                return "Cell ID mismatch"
            if rule == "#6":
                return "RRU type mismatch"
            if rule == "#39":
                return "NRCellDU/NRCellCU mismatch"
            if rule == "#47":
                return "Antenna Type mismatch"
            return "mismatch"
        return "mismatch", _group_bad_by_node_reason(bad, real, _reason) + " " + manual_note
    if real:
        return "info", f"{len(real)} checked, no mismatch. {manual_note}"
    skipped_notes = {r.get("note") for r in all_results if r.get("note")}
    base = "; ".join(sorted(skipped_notes)) or "Skipped for every node (no Pre log / no RFDS)."
    return "manual", f"{base} {manual_note}"


def _agg_row63(rbb_results, rilink_results):
    """Row 63 ('TxRx / RBB Type Need to be checked with - Single / Double
    RILink - RRU type & RBB type'): combines the SAME RBB Type/TX-RX/
    Radio Port validation as row 58 (check_rbb_tx_isdlonly_4g) with the
    Pre-vs-CIQ RILink comparison (check_rilink_vs_rbb_4g) — same grouping
    convention as the other rows."""
    all_results = rbb_results + rilink_results
    if not all_results:
        return "unknown", "No data (check did not run for this site)."
    real = [r for r in all_results if r.get("status") not in (None, "SKIPPED")]
    bad = [r for r in real if r.get("status") == "MISMATCH"]
    if not bad:
        if real:
            return "match", f"{len(real)} checked, no mismatch."
        skipped_notes = {r.get("note") for r in all_results if r.get("note")}
        return "unknown", "; ".join(sorted(skipped_notes)) or "Skipped for every node (no Pre log / no RFDS)."

    def _reason(r):
        note = r.get("note") or ""
        if "does not match the expected RBB" in note:
            return "unparseable RBB type"
        if "ISDLONLY" in note:
            return "ISDLONLY mismatch"
        if "implies TX/RX" in note:
            return "RBB/TX-RX mismatch"
        if "implies" in note and "link but Radio Port" in note:
            return "Radio Port link mismatch"
        if r.get("rule") == "#63":
            return "RILink Pre vs CIQ mismatch"
        return "mismatch"

    return "mismatch", _group_bad_by_node_reason(bad, real, _reason)


def _agg_electrical_tilt_type(results_tilt):
    """Row 61 ('electricalAntennaTilt should be integer value not
    character'): same grouping treatment as the other rows — whole-band
    collapses to just the band name, partial names the specific sectors;
    a lone mismatch keeps its full detail."""
    if not results_tilt:
        return "unknown", "No data (check did not run for this site)."
    real = [r for r in results_tilt if r.get("status") not in (None, "SKIPPED")]
    bad = [r for r in real if r.get("status") == "MISMATCH"]
    if not bad:
        if real:
            return "match", f"{len(real)} checked, no mismatch."
        skipped_notes = {r.get("note") for r in results_tilt if r.get("note")}
        return "unknown", "; ".join(sorted(skipped_notes)) or "Skipped for every node (no Pre log / no RFDS)."
    return "mismatch", _group_bad_by_node_reason(bad, real, lambda r: "electricalAntennaTilt stored as character")


def _agg_rbb_tx_isdlonly_4g(results_4g):
    """Row 58 ('RBB type/noOfTx/noOfRx / Identify ISDLONLY carrier'): same
    grouping treatment as the 5G rows — whole-band collapses to just the
    band name, partial names the specific sectors; a lone mismatch keeps
    its full detail."""
    if not results_4g:
        return "unknown", "No data (check did not run for this site)."
    real = [r for r in results_4g if r.get("status") not in (None, "SKIPPED")]
    bad = [r for r in real if r.get("status") == "MISMATCH"]
    if not bad:
        if real:
            return "match", f"{len(real)} checked, no mismatch."
        skipped_notes = {r.get("note") for r in results_4g if r.get("note")}
        return "unknown", "; ".join(sorted(skipped_notes)) or "Skipped for every node (no Pre log / no RFDS)."

    def _reason(r):
        note = r.get("note") or ""
        if "does not match the expected RBB" in note:
            return "unparseable RBB type"
        if "ISDLONLY" in note:
            return "ISDLONLY mismatch"
        if "implies TX/RX" in note:
            return "RBB/TX-RX mismatch"
        return "mismatch"

    return "mismatch", _group_bad_by_node_reason(bad, real, _reason)


def _agg_params_4g(params_results):
    """Row 57 ('earfcnDl/dlChannelBandwidth ENM vs CIQ' — actually covers
    all four LTE fields check_rf_params_4g checks: earfcnDl/earfcnUl/
    dlChannelBandwidth/ulChannelBandwidth). Same grouping treatment as
    the 5G rows — whole-band collapses to just the band name, partial
    names the specific sectors; a lone mismatch keeps its full detail."""
    if not params_results:
        return "unknown", "No data (check did not run for this site)."
    real = [r for r in params_results if r.get("status") not in (None, "SKIPPED")]
    bad = [r for r in real if r.get("status") == "MISMATCH"]
    if not bad:
        if real:
            return "match", f"{len(real)} checked, no mismatch."
        skipped_notes = {r.get("note") for r in params_results if r.get("note")}
        return "unknown", "; ".join(sorted(skipped_notes)) or "Skipped for every node (no Pre log / no RFDS)."
    return "mismatch", _group_bad_by_node_reason(bad, real, lambda r: "earfcn/bandwidth mismatch")


def _agg_cell_details(cells_results, cell_id_results, radio_results):
    """Row 31 ('CellDetails(Final) -- CellID / RCN / RRH'): same as _agg,
    except cells failing for the SAME reason on the SAME node are grouped
    into one summary line (see _group_bad_by_node_reason) instead of
    listed cell-by-cell — covers cell presence BOTH directions ('not
    found in RFDS' / 'found in RFDS but not in CIQ'), Cell ID mismatch,
    and RRU type mismatch — matching this row's own title. Applies to any
    band/sector, not just DOD_BWE (N77 carrier '_3') — confirmed: the
    grouping is about the failure reason itself repeating, not which band
    it happens to be.

    Reason is dispatched by each result's 'rule' tag (#6/#18 = cell
    presence, #6/#24 = Cell ID, #6 = RRU), not by matching note text —
    Cell ID's mismatch note embeds live Pre/CIQ/RFDS values with no fixed
    string to match on, unlike the other two."""
    all_results = cells_results + cell_id_results + radio_results
    if not all_results:
        return "unknown", "No data (check did not run for this site)."
    real = [r for r in all_results if r.get("status") not in (None, "SKIPPED")]
    bad = [r for r in real if r.get("status") == "MISMATCH"]
    if not bad:
        if real:
            return "match", f"{len(real)} checked, no mismatch."
        skipped_notes = {r.get("note") for r in all_results if r.get("note")}
        return "unknown", "; ".join(sorted(skipped_notes)) or "Skipped for every node (no Pre log / no RFDS)."

    def _reason(r):
        rule = r.get("rule")
        if rule == "#6/#18":
            return "not found in RFDS" if r.get("note") == "Not found in RFDS." else "found in RFDS but not in CIQ"
        if rule == "#6/#24":
            return "Cell ID mismatch"
        if rule == "#6":
            return "RRU type mismatch"
        return "mismatch"

    return "mismatch", _group_bad_by_node_reason(bad, real, _reason)


def _agg_rbb_5g(results_5g):
    """Row 42 ('RBB Type vs no.ofrx and tx from ENM'): same grouping
    treatment as _agg_cell_id — 2+ cells on one node failing for the SAME
    reason (RBB Type unparseable / RILink mismatch / TX-RX mismatch)
    summarize to one line with the involved bands; a lone mismatch keeps
    its full detail."""
    if not results_5g:
        return "unknown", "No data (check did not run for this site)."
    real = [r for r in results_5g if r.get("status") not in (None, "SKIPPED")]
    bad = [r for r in real if r.get("status") == "MISMATCH"]
    if not bad:
        if real:
            return "match", f"{len(real)} checked, no mismatch."
        skipped_notes = {r.get("note") for r in results_5g if r.get("note")}
        return "unknown", "; ".join(sorted(skipped_notes)) or "Skipped for every node (no Pre log / no RFDS)."

    def _reason(r):
        note = r.get("note") or ""
        if "does not match the expected RBB" in note:
            return "unparseable RBB Type"
        if note.startswith("RILink"):
            return "RILink mismatch"
        if "TX/RX" in note:
            return "TX/RX mismatch"
        return "mismatch"

    return "mismatch", _group_bad_by_node_reason(bad, real, _reason)


def _agg_ssb_5g(ssb_results):
    """Row 44 ('ssbFrequency/ssbOffset/ssbDuration'): same grouping
    treatment as _agg_cell_id — 2+ cells on one node with a mismatch
    summarize to one line naming just the band when EVERY sector of that
    band failed, or '<band> (<sectors>)' when only some did; a lone
    mismatch keeps its full Pre/CIQ field-level detail."""
    if not ssb_results:
        return "unknown", "No data (check did not run for this site)."
    real = [r for r in ssb_results if r.get("status") not in (None, "SKIPPED")]
    bad = [r for r in real if r.get("status") == "MISMATCH"]
    if not bad:
        if real:
            return "match", f"{len(real)} checked, no mismatch."
        skipped_notes = {r.get("note") for r in ssb_results if r.get("note")}
        return "unknown", "; ".join(sorted(skipped_notes)) or "Skipped for every node (no Pre log / no RFDS)."
    return "mismatch", _group_bad_by_node_reason(bad, real, lambda r: "ssbFrequency/ssbOffset/ssbDuration mismatch")


def _agg_cell_id(cell_id_results):
    """Cell ID checks (rows 40/59/66/74, all reading the same
    cell_id_vs_rfds results): same grouping treatment as
    _agg_cell_details — 2+ cells on one node with a Cell ID mismatch
    summarize to one line with the involved bands (and specific sector
    letters when only part of a band failed); a lone mismatch keeps its
    full Pre/CIQ/RFDS detail."""
    if not cell_id_results:
        return "unknown", "No data (check did not run for this site)."
    real = [r for r in cell_id_results if r.get("status") not in (None, "SKIPPED")]
    bad = [r for r in real if r.get("status") == "MISMATCH"]
    if not bad:
        if real:
            return "match", f"{len(real)} checked, no mismatch."
        skipped_notes = {r.get("note") for r in cell_id_results if r.get("note")}
        return "unknown", "; ".join(sorted(skipped_notes)) or "Skipped for every node (no Pre log / no RFDS)."
    return "mismatch", _group_bad_by_node_reason(bad, real, lambda r: "Cell ID mismatch")


def _worst_status(statuses):
    """Roll several (status, note) verdicts into one, worst-first:
    mismatch > manual > unknown > match. Notes from every contributing
    verdict at that severity are joined, so the row says which field(s)
    actually failed rather than just that something did."""
    order = ["mismatch", "manual", "unknown", "match"]
    pairs = [s for s in statuses if s]
    if not pairs:
        return "unknown", "No data (check did not run for this site)."
    for level in order:
        hits = [n for s, n in pairs if s == level]
        if hits:
            seen, notes = set(), []
            for n in hits:
                if n and n not in seen:
                    seen.add(n)
                    notes.append(n)
            return level, "; ".join(notes)
    return "unknown", "No data (check did not run for this site)."


def _pre_detected_status(node_logs_text, what):
    """'Detected in the Pre kget log' checks (Radio Ports / RfBranch /
    Sharing Radio).

    These three are presence checks, not comparisons: the Pre log either
    exposes the data or it doesn't. Detected on at least one node -> match.
    Logs uploaded but the data is absent everywhere -> mismatch (the Pre
    capture is incomplete, which is the thing worth flagging). No logs at
    all -> unknown, never a pass."""
    import pre_extract as pe
    if not node_logs_text:
        return "unknown", "No Pre kget logs uploaded — nothing to detect."

    found, missing, any_radio_data = [], [], False
    for nid, text in node_logs_text.items():
        if not text:
            continue
        if what == "ports":
            fru = pe.extract_cell_to_fru(text)
            n = len(pe.extract_cell_to_rilink_detail(text, fru))
            label = "RiLink/RiPort entries"
        elif what == "rfbranch":
            refs = pe.extract_rf_branch_refs(text)
            n = sum(1 for v in refs.values() if v.get("sef_branches") or v.get("tx_ref"))
            label = "cells with RfBranch refs"
        elif what == "sharing":
            import band_labels as bl
            fru = pe.extract_cell_to_fru(text)
            # Radio sharing = two or more sectors OF THE SAME BAND landing
            # on one physical radio.
            #
            # Both qualifiers matter, and getting either wrong produced a
            # false positive on a real site:
            #   - several cells on one radio is NOT sharing (RRU-7 carries
            #     2A_1, 2A_3, 9A_1, N002A_1 — ordinary multi-carrier), and
            #   - several BANDS on one radio is NOT sharing either (that
            #     same RRU-7 carries AWS, PCS and 5G_PCS, all sector Alpha).
            # Only a repeated SECTOR within one band on one radio counts.
            #
            # band_label() returns (band_with_carrier, sector) — the
            # carrier index is stripped so AWS_1 and AWS_3 compare as one
            # band, and its sector name is used rather than re-parsing the
            # cell name.
            sectors_by_radio_band = {}
            for cell, f in fru.items():
                if not f or f == "-":
                    continue
                label, sector = bl.band_label(cell)
                if not label or not sector:
                    continue
                band = re.sub(r'_\d+$', '', str(label))
                sectors_by_radio_band.setdefault((f, band), set()).add(sector)
            shared = {k: v for k, v in sectors_by_radio_band.items() if len(v) > 1}
            n = len(shared)
            label = "radio/band combination(s) carrying 2+ sectors"
            if shared:
                detail = "; ".join(f"{f} {b}: {', '.join(sorted(secs))}"
                                   for (f, b), secs in sorted(shared.items()))
                found.append(f"{nid}: {detail}")
                continue
            # 'No sharing' is a legitimate design, but only knowable if
            # radio data was actually read — track that so it can be told
            # apart from 'nothing parsed'.
            if sectors_by_radio_band:
                any_radio_data = True
        else:
            return "unknown", f"Unknown detection target '{what}'."
        (found if n else missing).append(f"{nid}: {n} {label}")

    if found:
        return "match", "; ".join(found)
    if what == "sharing" and any_radio_data:
        # Radios WERE read and none is shared — a legitimate site design.
        return "match", "No shared radios on this site (each cell on its own radio)."
    return "mismatch", "Not detected in any Pre log — " + ("; ".join(missing) or "no usable log text.")


def _filter(results_list, rule_prefix):
    return [r for r in results_list if str(r.get("rule", "")).strip() == rule_prefix]


# ══════════════════════════════════════════════════════════════════════
# EDP field-level checks (cabinet naming / port size / port facing /
# bearer VLAN clash / IPv6 bearer+OAM groups). run_validation.py's own
# pipeline never built these - they only exist today in the separate
# HTML tool's EDP Validator - so this ports that exact logic here,
# reading straight off edp_rows (ciq_edp_reader.build_edp_index output).
# ══════════════════════════════════════════════════════════════════════

def _norm(v):
    v = "" if v is None else str(v).strip()
    return "" if v.lower() in ("none", "nan") else v


def _edp_role(edp_row):
    return "PRIMARY" if _norm(edp_row.get("SIAD_PORT_FACING_BBU")) else "SECONDARY"


def _edp_node_rows(edp_rows, node_ids):
    """{node_id: edp_row_or_None} for every node we're checking."""
    out = {}
    for nid in node_ids:
        rows = cer.edp_rows_for_site(edp_rows, nid)
        out[nid] = rows[0] if rows else None
    return out


def edp_missing_nodes(edp_rows, node_ids):
    """Public wrapper around _edp_node_rows for callers outside this module
    (Consolidated Report) that need the same 'node has no EDP row at all'
    list Row 20 (_edp_found_status) already computes — one detection, two
    surfaces, instead of two independently-maintained EDP-presence checks."""
    rows = _edp_node_rows(edp_rows, node_ids)
    return [n for n, r in rows.items() if r is None]


def _edp_found_status(edp_rows, node_ids):
    missing = edp_missing_nodes(edp_rows, node_ids)
    if not node_ids:
        return "unknown", "No nodes to check."
    if missing:
        return "mismatch", "; ".join(f"EDP is not published for {n}" for n in missing)
    return "match", f"{len(node_ids)} node(s) all found in EDP."


def _edp_cabinet_status(edp_rows, node_ids):
    """Rule: Primary node cabinet is BBUXX; Secondary is the SAME number
    suffixed V (BBUXXV). Both directions enforced — a Secondary missing
    the V, AND a Primary wrongly carrying one, both fail."""
    rows = _edp_node_rows(edp_rows, node_ids)
    bad, checked = [], 0
    for nid, r in rows.items():
        if r is None:
            continue
        checked += 1
        cab = _norm(r.get("CABINET"))
        role = _edp_role(r)
        base_ok = bool(re.match(r"^BBU\s*\d+V?$", cab, re.I)) if cab else False
        ends_v = cab.upper().endswith("V") if cab else False
        ok = base_ok and (ends_v if role == "SECONDARY" else not ends_v)
        if not ok:
            bad.append(f"{nid}: cabinet '{cab or '(blank)'}' ({role})")
    if not checked:
        return "unknown", "No EDP rows to check."
    if bad:
        return "mismatch", "; ".join(bad[:6])
    return "match", f"{checked} node(s) checked, all pass."


def _edp_port_size_status(edp_rows, node_ids, mm_rows_by_node):
    rows = _edp_node_rows(edp_rows, node_ids)
    bad, checked = [], 0
    for nid, r in rows.items():
        if r is None:
            continue
        mode = _norm(mm_rows_by_node.get(nid, {}).get("BBU Mode")).upper()
        size = _norm(r.get("SIAD_PORT_SIZE_BBU")).upper()
        if not size:
            continue
        checked += 1
        if mode in ("TMBB", "MMBB") and size != "10GE":
            bad.append(f"{nid}: {mode} node shows port size '{size}', expected 10GE")
    if not checked:
        return "unknown", "No SIAD_PORT_SIZE_BBU values to check."
    if bad:
        return "mismatch", "; ".join(bad[:6])
    return "match", f"{checked} node(s) checked, all pass."


def _edp_port_facing_status(edp_rows, node_ids):
    rows = _edp_node_rows(edp_rows, node_ids)
    bad, checked = [], 0
    for nid, r in rows.items():
        if r is None:
            continue
        checked += 1
        role = _edp_role(r)
        facing = _norm(r.get("SIAD_PORT_FACING_BBU"))
        if role == "PRIMARY" and not facing:
            bad.append(f"{nid}: Primary but SIAD_PORT_FACING_BBU is blank")
        if role == "SECONDARY" and facing:
            bad.append(f"{nid}: Secondary but SIAD_PORT_FACING_BBU is populated ('{facing}')")
    if not checked:
        return "unknown", "No EDP rows to check."
    if bad:
        return "mismatch", "; ".join(bad[:6])
    return "match", f"{checked} node(s) checked, all pass."


def _edp_bearer_vlan_status(edp_rows, node_ids):
    rows = _edp_node_rows(edp_rows, node_ids)
    vlans = [(_norm(r.get("BEARER_ENODEB_SB_VLAN_ID")), nid) for nid, r in rows.items() if r]
    vlans = [(v, n) for v, n in vlans if v]
    if not vlans:
        return "unknown", "No BEARER_ENODEB_SB_VLAN_ID values to check."
    seen = {}
    clashes = []
    for v, n in vlans:
        if v in seen and seen[v] != n:
            clashes.append(f"VLAN {v} shared by {seen[v]} and {n}")
        seen[v] = n
    if clashes:
        return "mismatch", "; ".join(clashes[:6])
    return "match", f"{len(vlans)} node(s), no bearer VLAN clash."


def _edp_group_status(edp_rows, node_ids, fields, label):
    rows = _edp_node_rows(edp_rows, node_ids)
    bad, checked = [], 0
    for nid, r in rows.items():
        if r is None:
            continue
        checked += 1
        missing = [f for f in fields if not _norm(r.get(f))]
        if missing:
            bad.append(f"{nid}: missing {', '.join(missing)}")
    if not checked:
        return "unknown", "No EDP rows to check."
    if bad:
        return "mismatch", "; ".join(bad[:6])
    return "match", f"{checked} node(s) checked, all {label} fields present."


IPV6_BEARER_FIELDS = ["IPV6_ENODEB_BEARER_SUBNET_61", "IPV6_ENODEB_SIAD_BEARER_SUB_64",
                       "IPV6_SIAD_BEARER_IP_DEF_ROUTER", "IPV6_ENODEB_BEARER_IP"]
IPV6_OAM_FIELDS = ["OAM_ENODEB_SIAD_OAM_VLAN", "IPV6_ENODEB_OAM_SUBNET_61",
                    "IPV6_ENODEB_SIAD_OAM_SUB_64", "IPV6_SIAD_OAM_IP_DEF_ROUTER", "IPV6_ENODEB_OAM_IP"]


# ══════════════════════════════════════════════════════════════════════
# New checks that had no home anywhere yet: SW-version consistency across
# nodes, MME Region (N2E), NR_SA tab + TAC digit rule, FA Code CIQ-vs-RFDS.
# ══════════════════════════════════════════════════════════════════════

def _sw_consistency_status(sw_version_results):
    versions = {r.get("sw_version") for r in sw_version_results if r.get("sw_version") not in (None, "NOT FOUND")}
    if not versions:
        return "unknown", "No SW version captured from any Pre kget-all log."
    if len(versions) > 1:
        detail = "; ".join(f"{r.get('node')}={r.get('sw_version')}" for r in sw_version_results if r.get("sw_version") not in (None, "NOT FOUND"))
        return "mismatch", f"Mixed SW versions across Pre nodes: {detail}"
    return "match", f"All Pre nodes on {versions.pop()}."


def _sw_status_v2(sw_version_results):
    """Confirmed to do BOTH signals, not just one: (1) every node that has a
    Pre log actually shows a detected SW version, AND (2) every detected
    version agrees across nodes. Either failing is a mismatch.

    SKIPPED entries (no Pre log at all for this node — confirmed real case:
    a genuinely new node being added in this build, e.g. Pre has 2 nodes
    and CIQ adds a 3rd) are excluded from 'missing' entirely — that node
    was never expected to have a Pre log, so its absence isn't a real
    version-mismatch finding. Only a node that HAD a log but still
    couldn't yield a version (status not SKIPPED, sw_version still None/
    'NOT FOUND') counts as missing."""
    if not sw_version_results:
        return "unknown", "No Pre kget-all logs loaded."
    checked = [r for r in sw_version_results if r.get("status") != "SKIPPED"]
    missing = [r.get("node") for r in checked if r.get("sw_version") in (None, "NOT FOUND")]
    versions = {r.get("sw_version") for r in checked if r.get("sw_version") not in (None, "NOT FOUND")}
    bad = []
    if missing:
        bad.append(f"No SW version detected for: {', '.join(missing)}")
    if len(versions) > 1:
        detail = "; ".join(f"{r.get('node')}={r.get('sw_version')}" for r in checked if r.get("sw_version") not in (None, "NOT FOUND"))
        bad.append(f"Mixed SW versions across Pre nodes: {detail}")
    if bad:
        return "mismatch", " | ".join(bad)
    if versions:
        return "match", f"All Pre nodes show a SW version, all on {versions.pop()}."
    return "unknown", "No SW version captured from any Pre kget-all log."


def _nsa_sa_status(nr_tac_results):
    """Row 45 ('NSA/SA'): a per-SITE summary, not per-cell pass/fail — a
    node counts as SA in Pre if ANY of its cells report a 7-digit Pre
    nRTAC (check_nr_tac's own SA signal). Distinct from row 48 (Pre vs
    CIQ nRTAC value agreement), which still uses the per-cell result."""
    if not nr_tac_results:
        return "unknown", "No data (check did not run for this site)."
    sa_nodes = sorted({r.get("node") for r in nr_tac_results
                        if r.get("pre_nrtac") and str(r.get("pre_nrtac")).isdigit()
                        and len(str(r.get("pre_nrtac"))) == 7})
    if sa_nodes:
        return "info", f"{', '.join(sa_nodes)} {'is' if len(sa_nodes) == 1 else 'are'} SA config in pre."
    return "match", "All Nodes are NSA in pre."


def _mme_region_status(ciq_wb):
    """N2E-ness is a SITE-level fact, not per-node: presence of any real cell
    row in the CIQ's Nokia_Info tab (the source-Nokia cell being migrated
    off) means this is an N2E site — confirmed real CIQ structure has a
    'Nokia Cell Id' column there, non-empty only for actual N2E migrations
    (every non-N2E CIQ checked has Nokia_Info present but entirely empty).
    For an N2E site, EVERY node's MME Region (Mixed Mode Info tab) must be
    N-RAN; E-RAN is flagged so it can be raised as a PI to the design team."""
    if "Mixed Mode Info" not in ciq_wb.sheetnames:
        return "unknown", "No Mixed Mode Info sheet."
    is_n2e = False
    if "Nokia_Info" in ciq_wb.sheetnames:
        nokia_rows = cer.sheet_rows_as_dicts(ciq_wb["Nokia_Info"])
        is_n2e = any(_norm(r.get("Nokia Cell Id")) for r in nokia_rows)
    if not is_n2e:
        return "match", "No cells in Nokia_Info — not an N2E site, MME Region rule does not apply."
    rows = cer.sheet_rows_as_dicts(ciq_wb["Mixed Mode Info"])
    bad = [r for r in rows if re.search(r"E-?RAN", _norm(r.get("MME Region")), re.I)
           and not re.search(r"N-?RAN", _norm(r.get("MME Region")), re.I)]
    if bad:
        return "mismatch", "; ".join(
            f"{_norm(r.get('eNodeB Name'))}: MME Region '{_norm(r.get('MME Region'))}' — should be N-RAN, raise PI to design team"
            for r in bad
        )
    return "match", f"N2E site — {len(rows)} node(s), MME Region correctly N-RAN."


def _n2e_detection_status(ciq_wb):
    """Row 91 ('Nokia info present means N2E site else NSB'): same
    Nokia_Info-tab-presence signal _mme_region_status uses to decide
    N2E-ness (real non-empty 'Nokia Cell Id' cell = N2E), surfaced here as
    its own informational row rather than folded into the MME Region
    check."""
    if not ciq_wb or "Nokia_Info" not in ciq_wb.sheetnames:
        return "unknown", "No Nokia_Info tab in CIQ."
    nokia_rows = cer.sheet_rows_as_dicts(ciq_wb["Nokia_Info"])
    is_n2e = any(_norm(r.get("Nokia Cell Id")) for r in nokia_rows)
    return ("info", "Nokia_Info has cell data — N2E site.") if is_n2e else \
           ("info", "Nokia_Info empty — NSB site.")


def _nr_sa_tac_status(ciq_wb):
    """NR_SA tab declares, per NODE, the exact nRTAC value expected if that
    node is SA-converted ('Node Name' + 'nrTAC' columns, confirmed real
    CIQ structure). Checked at CELL level — each 5G Info row's own nRTAC
    is compared individually against its node's expected value, not
    collapsed into a per-node set first (a single divergent cell must be
    named, not just inferred from the node showing more than one value)."""
    has_nr_sa = "NR_SA" in ciq_wb.sheetnames
    if not has_nr_sa:
        return "na", "No NR_SA tab in this CIQ — SA-carrier TAC rule does not apply."
    if "5G Info" not in ciq_wb.sheetnames:
        return "unknown", "NR_SA tab present but no 5G Info sheet found."

    sa_tac_by_node = {_norm(r.get("Node Name")).upper(): _norm(r.get("nrTAC"))
                      for r in cer.sheet_rows_as_dicts(ciq_wb["NR_SA"]) if _norm(r.get("Node Name"))}

    fiveg_rows = [r for r in cer.sheet_rows_as_dicts(ciq_wb["5G Info"]) if _norm(r.get("gNB Name"))]
    if not fiveg_rows:
        return "unknown", "NR_SA tab present but no nRTAC values read from 5G Info."

    bad = []
    node_tacs = {}
    for r in fiveg_rows:
        node = _norm(r.get("gNB Name")).upper()
        cell = _norm(r.get("NRCellDU")) or node
        tac = _norm(r.get("nRTAC"))
        expected = sa_tac_by_node.get(node)
        if expected is not None:
            if tac != expected:
                bad.append(f"{cell}: nRTAC='{tac}' does not match NR_SA value '{expected}' for {node}")
            else:
                node_tacs.setdefault(node, ("sa", expected))
        else:
            if tac != "0":
                bad.append(f"{cell}: nRTAC='{tac}' expected 0 ({node} not in NR_SA tab)")
            else:
                node_tacs.setdefault(node, ("nsa", "0"))

    if bad:
        return "mismatch", "; ".join(bad[:6])
    sa_lines = [f"NR TAC: {tac}: {node}" for node, (kind, tac) in node_tacs.items() if kind == "sa"]
    nsa_nodes = sorted(node for node, (kind, _) in node_tacs.items() if kind == "nsa")
    lines = list(sa_lines)
    if nsa_nodes:
        lines.append(f"NR TAC: 0: {', '.join(nsa_nodes)}")
    return "match", " | ".join(lines) if lines else "No 5G nodes to check."


def _fa_code_status(site_details, ciq_wb):
    """Compares the CIQ's own FA Code (site_details['fa_code'], always
    CIQ-sourced per build_site_details()) against the RFDS-sourced value
    (site_details['rfds_fa_code']) - NOT against itself. An earlier version
    of this function read site_details.get('fa_code') and called it
    'rfds_fa', but that key has never held the RFDS value (RFDS's own value
    is never merged into 'fa_code' - see build_site_details()), so this was
    silently comparing the CIQ FA Code against itself and never actually
    checked RFDS at all."""
    rfds_fa = _norm(site_details.get("rfds_fa_code"))
    if "5G Info" not in ciq_wb.sheetnames:
        return "unknown", "No 5G Info sheet (LTE-only build) to compare."
    rows = cer.sheet_rows_as_dicts(ciq_wb["5G Info"])
    ciq_fas = sorted({_norm(r.get("FA Code")) for r in rows if _norm(r.get("FA Code"))})
    if not rfds_fa:
        return "unknown", "No FA Code found on the RFDS Site Details page - not checked."
    if not ciq_fas:
        return "unknown", "No FA Code on the CIQ 5G Info sheet."
    bad = [f for f in ciq_fas if f != rfds_fa]
    if bad:
        return "mismatch", f"RFDS FA Code {rfds_fa} vs CIQ FA Code(s) {', '.join(bad)}"
    return "match", f"RFDS and CIQ FA Code both {rfds_fa}."


def _xmu_vs_rfds_status(enb_rows_all, node_ids, rfds_pages):
    has_xmu_nodes = []
    for nid in node_ids:
        row = cer.find_enb_row(enb_rows_all, nid)
        if not row:
            continue
        x1, x2 = _norm(row.get("1st XMU")).upper(), _norm(row.get("2nd XMU")).upper()
        if x1 not in ("", "NO", "N/A", "NOT USED") or x2 not in ("", "NO", "N/A", "NOT USED"):
            has_xmu_nodes.append(nid)
    if not has_xmu_nodes:
        return "match", "No node shows a 1st/2nd XMU in CIQ eNB Info."
    if not rfds_pages:
        return "unknown", f"{', '.join(has_xmu_nodes)} show XMU in CIQ, but no RFDS PDF was provided to check."
    full_text = " ".join(rfds_pages.values()).upper() if isinstance(rfds_pages, dict) else ""
    if "XMU" not in full_text:
        return "mismatch", f"{', '.join(has_xmu_nodes)} show XMU in CIQ eNB Info, but 'XMU' does not appear anywhere in the RFDS PDF text."
    return "match", f"{len(has_xmu_nodes)} node(s) with XMU in CIQ — RFDS PDF text also mentions XMU."


# ══════════════════════════════════════════════════════════════════════
# The 63-row checklist definition. `row` = exact Excel row in
# Checklist_RRNRBL.xlsx ("Legacy - N2e Engineer Checklist" sheet).
# `check` is a zero-arg callable returning (status, detail), or None
# for a manual item.
# ══════════════════════════════════════════════════════════════════════

def _edp_controller_status(edp_rows, controller_ids):
    """NEW - Controller/ANCEQ checks (cabinet naming/port-size/etc. above are
    Primary/Secondary-only). controller_ids: list of EDP SITE_NAME values for
    Controller rows (= the CIQ's Controller Info 'Controller ID').
    IPv6 ANCEQ fields are checked as informational only - a real, valid EDP
    row was found with every IPv6 ANCEQ_* field genuinely blank, so treating
    it as a required field would be a false mismatch."""
    if not controller_ids:
        return "na", "No Controller node in this CIQ's Controller Info sheet."
    bad, checked = [], 0
    ipv6_present = 0
    for cid in controller_ids:
        rows = cer.edp_rows_for_site(edp_rows, cid)
        if not rows:
            bad.append(f"{cid}: not published in EDP")
            continue
        r = rows[0]
        checked += 1
        missing = [f for f in ("ANCEQ_TYPE", "ANCEQ_NAME", "ANCEQ_SIAD_IP_HOST_1", "ANCEQ_SIAD_IP_HOST_2") if not _norm(r.get(f))]
        if missing:
            bad.append(f"{cid}: missing {', '.join(missing)}")
        if _norm(r.get("ANCEQ_SIAD_IPV6_HOST_1")):
            ipv6_present += 1
    if not checked:
        return "unknown", "; ".join(bad) if bad else "No EDP rows to check."
    if bad:
        return "mismatch", "; ".join(bad[:6])
    return "match", f"{checked} controller(s) checked (IPv4 required fields all present; {ipv6_present} also have IPv6)."


def _edp_ptp_status(edp_rows, node_ids):
    """NEW - EDP's own PTP fields (SIAD_PTP_VLAN_ID + the PTP_VLAN_SUBNET_30 /
    PTP_SIAD_INTERFACE_IP / PTP_CAB_INTERFACE_IP group), confirmed present on
    a real published EDP row. Separate from the kget-log-side PTP guess in
    pre_extract.extract_ptp_status() - this one reads data this backend
    definitely has."""
    rows = _edp_node_rows(edp_rows, node_ids)
    bad, checked, no_ptp = [], 0, 0
    for nid, r in rows.items():
        if r is None:
            continue
        vlan = _norm(r.get("SIAD_PTP_VLAN_ID"))
        if not vlan:
            no_ptp += 1
            continue
        checked += 1
        missing = [f for f in ("PTP_VLAN_SUBNET_30", "PTP_SIAD_INTERFACE_IP", "PTP_CAB_INTERFACE_IP") if not _norm(r.get(f))]
        if missing:
            bad.append(f"{nid}: PTP VLAN {vlan} set but missing {', '.join(missing)}")
    if bad:
        return "mismatch", "; ".join(bad[:6])
    if checked:
        return "match", f"{checked} node(s) with PTP configured, all required fields present."
    return "info", f"No node declares a PTP VLAN in EDP ({no_ptp} checked) — PTP may not be in scope for this build."


def _high_capacity_status(ciq_wb):
    """Row 73 ('High Capacity Site (Identify if its HC)'), blue-marked in
    the rule-mapping sheet: 'We can make it read from eUtran Parameters
    tab of CIQ' — a direct read of CIQ's own 'High Capacity Site' column,
    not a Pre/RFDS comparison (no other source declares HC status)."""
    if not ciq_wb or "eUtran Parameters" not in ciq_wb.sheetnames:
        return "unknown", "No eUtran Parameters sheet."
    rows = [r for r in cer.sheet_rows_as_dicts(ciq_wb["eUtran Parameters"]) if _norm(r.get("EutranCellFDDId"))]
    if not rows:
        return "unknown", "No LTE cells in eUtran Parameters."
    missing = [r.get("EutranCellFDDId") for r in rows if not _norm(r.get("High Capacity Site"))]
    hc_cells = [r.get("EutranCellFDDId") for r in rows
                if _norm(r.get("High Capacity Site")).upper() in ("TRUE", "YES", "Y")]
    if missing:
        return "manual", f"High Capacity Site blank for: {', '.join(missing)} — verify manually."
    if hc_cells:
        return "info", f"High Capacity Site = TRUE for: {', '.join(hc_cells)}."
    return "info", "No cells marked High Capacity Site."


def _cellrange_status(ciq_wb):
    """Row 74's cellrange portion, blue-marked in the rule-mapping sheet:
    'We can make it read from eUtran Parameters tab of CIQ'. qRxLevMin
    and crsgain are NOT covered — no CIQ column maps to either and no
    Pre/RFDS extraction exists for them, so they stay manual (per the
    honesty convention: display what's actually read, don't imply the
    whole row title is automated)."""
    if not ciq_wb or "eUtran Parameters" not in ciq_wb.sheetnames:
        return "unknown", "No eUtran Parameters sheet."
    rows = [r for r in cer.sheet_rows_as_dicts(ciq_wb["eUtran Parameters"]) if _norm(r.get("EutranCellFDDId"))]
    if not rows:
        return "unknown", "No LTE cells in eUtran Parameters."
    tail = " (qRxLevMin/crsgain: manual — no CIQ column mapped, no Pre/RFDS source.)"
    missing = [r.get("EutranCellFDDId") for r in rows if not _norm(r.get("cellRange"))]
    if missing:
        return "manual", f"cellRange blank for: {', '.join(missing)} — verify manually.{tail}"
    detail = "; ".join(f"{r.get('EutranCellFDDId')}={_norm(r.get('cellRange'))}" for r in rows)
    return "info", f"cellRange: {detail}.{tail}"


def build_checklist(results, site_details, ciq_wb, edp_rows, node_ids, rfds_pages=None, node_logs_text=None):
    mm_rows = cer.mixed_mode_rows(ciq_wb) if ciq_wb else []
    mm_by_node = {}
    for r in mm_rows:
        n = _norm(r.get("Node to be built as")) or _norm(r.get("eNodeB Name"))
        if n:
            mm_by_node[n] = r
    enb_rows_all = cer.enb_info_rows(ciq_wb) if ciq_wb else []

    board_type = results.get("board_type", [])
    identity = results.get("identity", [])

    # Primary AND Secondary node ids — node_ids (checked_nodes) only ever
    # holds the Primary name, so site_name/cabinet/bbu_type/node_model and
    # the 6 bearer/OAM rows below (which all need to see a Secondary that
    # EDP is missing, or a Secondary added by an SMBB->MMBB transition)
    # need this instead. Computed here rather than passed in, matching the
    # EDP Validator tab's own fix for the same gap.
    node_role_list = build_primary_secondary_node_list(ciq_wb) if ciq_wb else []
    edp_node_ids = [n["node"] for n in node_role_list] or node_ids

    def edp_field(fields, label):
        return lambda: _edp_group_status(edp_rows, node_ids, fields, label)

    # Row numbers below match the V3 combined NRBL-RR checklist template
    # (116 rows total, replacing the old 81-row layout). New items with no
    # existing automated signal are left manual (check=None) rather than
    # reusing an unrelated check — see module docstring on honesty.
    rows = [
        (13, "Major showstopper check", None, "SW should be match with ENM", "NR/Radio",
         lambda: _sw_status_v2(results.get("sw_version", []))),
        (14, "Major showstopper check", None, "Software in the ENM vs Software upgradation tracker ", "NR/Radio", None),

        (17, "ENM check", "Pre Vs RTS Sheet in QWEST", "Please validate the FA code with latest RTS sheet in qwest", "Radio", lambda: _fa_code_status(site_details, ciq_wb)),

        (20, "EDP check", "EDP vs Site", "site_name", "NR/Radio", lambda: _edp_found_status(edp_rows, edp_node_ids)),
        (21, "EDP check", "EDP vs Site", "cabinet", "Radio", lambda: _cabinet_pairing_status(ciq_wb, edp_rows, edp_node_ids)),
        (22, "EDP check", "EDP vs Site", "bbu_type", "Radio", lambda: _bbu_type_vs_node_model_status(ciq_wb, edp_rows, edp_node_ids)),
        (23, "EDP check", "EDP vs Site", "node_model", "Radio", lambda: _node_model_vs_bbu_type_status(ciq_wb, edp_rows, edp_node_ids)),
        (24, "EDP check", "EDP vs Site", "siad_port_size_bbu", "Radio", lambda: _siad_port_size_pre_status(node_logs_text, ciq_wb, edp_rows, edp_node_ids)),
        (25, "EDP check", "EDP vs Site", "siad_port_facing_bbu", "Radio", lambda: _edp_port_facing_status(edp_rows, edp_node_ids)),
        (26, "EDP check", "EDP vs Site", "bearer_enodeb_sb_vlan_id", "Radio",
         lambda: _pre_vs_edp_field_status(node_logs_text, node_role_list, edp_rows, "bearer_vlan", "BEARER_ENODEB_SB_VLAN_ID")),
        (27, "EDP check", "EDP vs Site", "ipv6_siad_bearer_ip_def_router", "Radio",
         lambda: _pre_vs_edp_field_status(node_logs_text, node_role_list, edp_rows, "bearer_router_ip", "IPV6_SIAD_BEARER_IP_DEF_ROUTER", is_ipv6=True)),
        (28, "EDP check", "EDP vs Site", "ipv6_enodeb_bearer_ip", "Radio",
         lambda: _pre_vs_edp_field_status(node_logs_text, node_role_list, edp_rows, "bearer_ip", "IPV6_ENODEB_BEARER_IP", is_ipv6=True)),
        (29, "EDP check", "EDP vs Site", "oam_enodeb_siad_oam_vlan", "Radio",
         lambda: _pre_vs_edp_field_status(node_logs_text, node_role_list, edp_rows, "oam_vlan", "OAM_ENODEB_SIAD_OAM_VLAN")),
        (30, "EDP check", "EDP vs Site", "ipv6_siad_oam_ip_def_router", "Radio",
         lambda: _pre_vs_edp_field_status(node_logs_text, node_role_list, edp_rows, "oam_router_ip", "IPV6_SIAD_OAM_IP_DEF_ROUTER", is_ipv6=True)),
        (31, "EDP check", "EDP vs Site", "ipv6_enodeb_oam_ip", "Radio",
         lambda: _pre_vs_edp_field_status(node_logs_text, node_role_list, edp_rows, "oam_ip", "IPV6_ENODEB_OAM_IP", is_ipv6=True)),

        (34, "RFDS Checks", "Pre Vs RTS Sheet in QWEST", "FACode", "Radio", lambda: _fa_code_status(site_details, ciq_wb)),
        (35, "RFDS Checks", None, "JobDetail", "Radio", None),
        (36, "RFDS Checks", None, "NonRFInventoryDetails(Final)", "Radio", None),
        (37, "RFDS Checks", None, "CellDetails(Final) -- CellID / RCN /RRH", "Radio",
         lambda: _agg_cell_details(results.get("cells_vs_rfds", []), results.get("cell_id_vs_rfds", []), results.get("radio_type", []))),
        (38, "RFDS Checks", None, "AntennaPositionDetails -- Model / LinkedCells / Azimuth(Design)  / Total Postions", "Radio", None),
        (39, "RFDS Checks", None, "Plumbing Diagram -- TxRx / TMA / Radio - RET Controller / Total Postions", "Radio", None),

        (43, "CIQ tabs checks", "Revision History", "All Confirmation checks", "NR/Radio", None),
        (44, "CIQ tabs checks", "Mixed Mode Info Tab", "eNBId and gNBId ENM vs CIQ", "NR/Radio", lambda: _agg(identity)),
        (45, "CIQ tabs checks", "Mixed Mode Info Tab", "MME Region [N2E site MME Regionn should be with N-RAN,if its E-RAN,raise PI to design team]", "NR/Radio", lambda: _mme_region_status(ciq_wb)),
        (46, "CIQ tabs checks", "Mixed Mode Info Tab", "Note: Make sure Primary & secondary node is matching with RFDS-Non RF Inventory Details (Final)", "Radio", lambda: _agg(results.get("primary_secondary", []))),

        (47, "CIQ tabs checks", "NBIoT Parameters", "All columns - ENM vs CIQ", "NR", None),

        (48, "CIQ tabs checks", "5g info", "NRCellDU/ NRCellCU  ENM vs CIQ ", "NR/Radio", lambda: _agg(results.get("nrcelldu_nrcellcu", []))),
        (49, "CIQ tabs checks", "5g info", "nRTAC/ cellLocalId ENM Vs CIQ", "NR/Radio", lambda: _agg_cell_id(results.get("cell_id_vs_rfds", []))),
        (50, "CIQ tabs checks", "5g info", "arfcnDL/ arfcnUL and bSChannelBwDL/ bSChannelBwDL\nENM Vs CIQ", "NR/Radio", lambda: _agg(results.get("arfcn_bw_5g", []))),
        (51, "CIQ tabs checks", "5g info", "RBB Type vs no.ofrx and tx from ENM", "Radio",
         lambda: _agg_rbb_5g([r for r in results.get("sector_swap", []) if r.get("kind") == "5g"])),
        (52, "CIQ tabs checks", "5g info", "DSS check", "NR/Radio", lambda: _agg(results.get("dss", []))),
        (53, "CIQ tabs checks", "5g info", "ssbFrequency /ssbOffset/ ssbDuration ", "NR/Radio", lambda: _agg_ssb_5g(results.get("ssb_5g", []))),
        (54, "CIQ tabs checks", "5g info", "NSA/SA", "NR/Radio", lambda: _nsa_sa_status(results.get("nr_tac", []))),
        (55, "CIQ tabs checks", "5g info", "VoNR", "NR/Radio", None),
        (56, "CIQ tabs checks", "5g info", "Make sure  BBU Type should match with RFDS and CIQ - BBU Type", "NR/Radio", lambda: _agg(board_type)),
        (57, "CIQ tabs checks", "5g info", "NRCellDU/NRCellCU/cellLocalId/RRU Type/ BeamDirection (Azimuth) /Antenna Type /Electrical Tilt must same as RFDS ", "Radio",
         lambda: _agg_row47(results.get("cells_vs_rfds", []), results.get("cell_id_vs_rfds", []), results.get("radio_type", []),
                             results.get("nrcelldu_nrcellcu", []), [r for r in results.get("antenna_type_rfds", []) if r.get("rule") == "#47"])),
        (58, "CIQ tabs checks", "5g info", "NR TAC - Existing sectors - ENM", "NR/Radio", lambda: _nsa_sa_status(results.get("nr_tac", []))),
        (59, "CIQ tabs checks", "5g info", " NR TAC   - For newly added Carriers-  NSA= 0 & SA =7 digit value", "NR/Radio", lambda: _nr_sa_tac_status(ciq_wb)),
        (60, "CIQ tabs checks", "5g info", "6472 / AIR-6449 - C Band / AIR6419 - DOD - Check for the SEF/FRU -- Check for the SEF/FRU", "Radio", lambda: _agg(results.get("sef_fru", []))),
        (61, "CIQ tabs checks", "5g info", "MMwave - Rach Should not Exceed 137 - PCI/RACH limitation", "Radio", None),
        (62, "CIQ tabs checks", "5g info", "Unique Port for 5G and LTE incase of Separate Radio - Ports and data ports ", "Radio", lambda: _agg_port_uniqueness(results.get("port_uniqueness", []))),
        (63, "CIQ tabs checks", "5g info", "Additional Check:  SOW – For 5G addition on an existing node, the snssaiList should match the existing 5G configuration.", "NR", None),

        (64, "CIQ tabs checks", "gNB Info", "gNBId/gNodeB Name must should with  Mixed Mode Info tab ", "NR/Radio", lambda: _agg(results.get("gnb_identity", []))),
        (65, "CIQ tabs checks", "gNB Info", "DU type should be same as 5G Info tab - BBU Type", "NR/Radio", lambda: _agg(results.get("gnb_du_type", []))),

        (66, "CIQ tabs checks", "eNB Info", "eNBId/eNodeB Name should match with Mixed Mode Info tab - eNBId/eNodeB", "NR/Radio", lambda: _agg(results.get("enb_identity", []))),
        (67, "CIQ tabs checks", "eNB Info", "BBU Type should match with RFDS - BBU Type", "Radio", lambda: _agg(board_type)),
        (68, "CIQ tabs checks", "eNB Info", "need to check TAC is updated or not", "NR/Radio", lambda: _agg(results.get("tac", []))),
        (69, "CIQ tabs checks", "eNB Info", "Pre-existing node tac as per ENM & N2E/NSB site tac as per RMAP - tac value", "Radio", None),

        (70, "CIQ tabs checks", "eUtran Parameters Tab", "earfcnDl/ dlChannelBandwidth ENM vs CIQ", "NR/Radio", lambda: _agg_params_4g(results.get("params_4g", []))),
        (71, "CIQ tabs checks", "eUtran Parameters Tab", "RBB type/ noOfTx/noOfRx\nIdentify  ISDLONLY carrier", "NR/Radio", lambda: _agg_rbb_tx_isdlonly_4g(results.get("rbb_tx_isdlonly_4g", []))),
        (72, "CIQ tabs checks", "eUtran Parameters Tab", "cellId ENM vs CIQ \nIdentify cellid change SOW", "NR/Radio", lambda: _agg_cell_id(results.get("cell_id_vs_rfds", []))),
        (73, "CIQ tabs checks", "eUtran Parameters Tab", "High Capacity Site \n(Identify if its HC)", "NR", lambda: _high_capacity_status(ciq_wb)),
        (74, "CIQ tabs checks", "eUtran Parameters Tab", "qRxLevMin | cellrange | crsgain ENM Vs CIQ", "NR", lambda: _cellrange_status(ciq_wb)),
        (75, "CIQ tabs checks", "eUtran Parameters Tab", "EutranCellFDDId/beamDirection should match with RFDS - EutranCell", "Radio", lambda: _agg_row60(results.get("cells_vs_rfds", []))),
        (76, "CIQ tabs checks", "eUtran Parameters Tab", "electricalAntennaTilt should be integer value not character - Tilt", "Radio", lambda: _agg_electrical_tilt_type(results.get("electrical_tilt_type", []))),
        (77, "CIQ tabs checks", "eUtran Parameters Tab", "configuredOutputPower depends on RRU type (Ericsson 4490, 4890, or 4472 radios (e.g., NSB or Allagi projects, New Carrier Adds, Radio Swaps) will be Configured with maximum allowed power of 160W.) - configuredOutputPower", "Radio", None),
        (78, "CIQ tabs checks", "eUtran Parameters Tab", "TxRx / RBB Type Need to be checked with - Single / Double RILink - RRU type & RBB type", "Radio",
         lambda: _agg_row63(results.get("rbb_tx_isdlonly_4g", []), results.get("rilink_vs_rbb_4g", []))),
        (79, "CIQ tabs checks", "eUtran Parameters Tab", "1)Compare Sectorid With Carrier Progression - sectorId / Carrier", "Radio", lambda: _agg_carrier_progression(results.get("carrier_progression", []))),
        (80, "CIQ tabs checks", "eUtran Parameters Tab", "2) Check for sectorID for  4890 Radio Type. \"_s\" should not be present", "Radio", None),
        (81, "CIQ tabs checks", "eUtran Parameters Tab", "PhysicalLayerCellIdGroup and physicalLayerSubCellId should be unique - PCI", "Radio", lambda: _agg(results.get("pci_4g", []) + results.get("pci_5g", []))),
        (82, "CIQ tabs checks", "eUtran Parameters Tab", "Pre-existing node cellId must be same as ENM & N2E/NSB site CellId should be match with RFDS - Cellid", "NR/Radio",
         lambda: _agg_row66(results.get("cell_id_vs_rfds", []), results.get("cellid_uniqueness_4g", []))),
        (83, "CIQ tabs checks", "eUtran Parameters Tab", "Riport should be unique", "Radio", lambda: _agg_port_uniqueness(results.get("port_uniqueness", []))),
        (84, "CIQ tabs checks", "eUtran Parameters Tab", "tmaType / tmaConfiguration", "Radio", None),
        (85, "CIQ tabs checks", "eUtran Parameters Tab", "antenna model", "Radio",
         lambda: _agg([r for r in results.get("antenna_type_rfds", []) if r.get("rule") == "#69"])),
        (86, "CIQ tabs checks", "eUtran Parameters Tab", " XMU Validation - Need to check with RFDS - XMU", "Radio", lambda: _agg(results.get("xmu", []))),
        (87, "CIQ tabs checks", "eUtran Parameters Tab", "ENM Validation - Need to check with site locator or ENM sheet (B2E) - ENM", "Radio", None),

        (88, "CIQ tabs checks", "Losses and delay", "Check for Losses delay matches to FDD and TxRx", "Radio", lambda: _agg(results.get("losses_vs_antenna", []))),
        (89, "CIQ tabs checks", "Antenna Information", "AntennaUnit/AntennaSubunit should unique for the band wise", "Radio", lambda: _agg(results.get("antenna", []))),
        (90, "CIQ tabs checks", "Sector Movement / Deletion sheet", "All source cells cellid/SSB/ BW matching with ENM and all target cells with eUtan tab", "NR/Radio", lambda: _agg_cell_id(results.get("cell_id_vs_rfds", []))),
        (91, "CIQ tabs checks", "Nokia Info tab", "Nokia info present means N2E site else NSB", "NR/Radio", lambda: _n2e_detection_status(ciq_wb)),

        # "IP Validation Pre Vs EDP" and "Rehoming sites" (old rows 75-76)
        # were dropped from the V3 template — not carried over.

        (94, "Pre checks", "ENM Pre-checks", "DSS and WCS Slim checks\nessscpairid | esssclocalid | AirIfLoadProfile|ailgRef", "NR", None),
        (95, "Pre checks", "ENM Pre-checks", "VoNR Check \nget . Epsfallbackoperation | get CXC4012592", "NR", None),
        (96, "Pre checks", "ENM Pre-checks", "hget EUtraNetwork=.,EUtranFrequency arfcnValueEUtranDl Limit for,\nGNBCUCPFunction=1 ---> 32\nENodeBFunction=1    ---> 24", "NR", None),
        (97, "Pre checks", "ENM Pre-checks", "Verfiy maxfreqcheck ", "NR", None),
        (98, "Pre checks", "ENM Pre-checks", "RIPORT", "Radio", lambda: _pre_detected_status(node_logs_text, "ports")),
        # RADIO PORT is new and distinct from RIPORT in this template — no
        # confirmed signal separates them, so left manual rather than
        # reusing the RIPORT check under a different name.
        (99, "Pre checks", "ENM Pre-checks", "RADIO PORT", "Radio", None),
        (100, "Pre checks", "ENM Pre-checks", "RfBrach", "Radio", lambda: _pre_detected_status(node_logs_text, "rfbranch")),

        # Script Generation and Additional check are new sections in V3 —
        # pre-generation input/checkbox reminders and post-generation
        # manual verifications, none of which this tool has a data signal
        # for (they're about script-generation-time choices, not
        # CIQ/EDP/RFDS/Pre content this app validates).
        (103, "Script Generation", "QWEST Selections", "Required input - CIQ|EDP|Pre-Kgetall|\nInternal Parameters File (Pre-Mom) | SCG File |", "NR", None),
        (104, "Script Generation", "QWEST Selections", "Input - Site Logs (Kget all)", "NR", None),
        (105, "Script Generation", "QWEST Selections", "Input - Deleted Site Logs (Kget all)", "NR", None),
        (106, "Script Generation", "QWEST Selections", "Check box - Nokia swap", "NR", None),
        (107, "Script Generation", "QWEST Selections", "Check box - DSS", "NR", None),
        (108, "Script Generation", "QWEST Selections", "Check box - Hi-Cap", "NR", None),
        (109, "Script Generation", "QWEST Selections", "Check box - SA Conversion", "NR", None),

        (112, "Additional check", "Additional Manual checks", "Need to check whether radios are shared between two sectors.", "Radio", lambda: _pre_detected_status(node_logs_text, "sharing")),
        (113, "Additional check", "Additional Manual checks", "Please validate the sector Id and Riport. It should be unique", "Radio", lambda: _agg_port_uniqueness(results.get("port_uniqueness", []))),
        (114, "Additional check", "Additional Manual checks", "ECDD Radio: The radio numbers should be sequential, with a maximum range from 1 to 12. For 5G, the radio naming should follow formats like ECDD-4/ECDD-5/ECDD-6 (any unique sequence). Do not use N005,N002 naming.", "Radio", None),
        (115, "Additional check", "Additional Manual checks", "If the 5G bandwidth is less than 50 MHz, ensure that PdcchSymbConfig is set to MINIMUM before removing the NR sector carrier reference, and make sure to include the specified command at the top of the delete scripts.", "Radio", None),
        (116, "Additional check", "Additional Manual checks", "Please verify the TX/RX after script generation; it should match the CIQ.", "Radio", None),
    ]

    out = []
    for row, cat, sub, item, tag, check in rows:
        if check is None:
            status, detail = "manual", "No automated check exists for this item."
        else:
            try:
                status, detail = check()
            except Exception as e:  # never let one bad check take down the whole checklist
                status, detail = "unknown", f"Check raised an error: {e}"
        out.append({"row": row, "cat": cat, "sub": sub, "item": item, "tag": tag,
                    "status": status, "detail": detail})
    return out


# ══════════════════════════════════════════════════════════════════════
# Fill the real template.
# ══════════════════════════════════════════════════════════════════════

_FPB_PART = "xl/featurePropertyBag/featurePropertyBag.xml"
_FPB_CONTENT_TYPE = "application/vnd.ms-excel.featurepropertybag+xml"
_FPB_REL_TYPE = "http://schemas.microsoft.com/office/2022/11/relationships/FeaturePropertyBag"


def _template_checkbox_xf(template_path):
    """(index, extLst_xml) of the cellXfs <xf> in the template that carries
    the checkbox xfComplement extension, or (None, None)."""
    import zipfile
    with zipfile.ZipFile(template_path) as tz:
        if "xl/styles.xml" not in tz.namelist():
            return None, None
        st = tz.read("xl/styles.xml").decode("utf-8")
    m = re.search(r"<cellXfs[^>]*>(.*?)</cellXfs>", st, re.S)
    if not m:
        return None, None
    for i, xf in enumerate(re.findall(r"<xf [^>]*/>|<xf .*?</xf>", m.group(1), re.S)):
        ext = re.search(r"<extLst>.*?</extLst>", xf, re.S)
        if ext and "xfComplement" in ext.group(0):
            return i, ext.group(0)
    return None, None


def _inject_xf_complement(styles_bytes, xf_index, ext_xml):
    """Put ext_xml back onto cellXfs entry #xf_index of a saved styles.xml,
    converting a self-closing <xf .../> into an open/close pair so the
    extension can live inside it."""
    if xf_index is None or not ext_xml:
        return styles_bytes
    st = styles_bytes.decode("utf-8")
    m = re.search(r"(<cellXfs[^>]*>)(.*?)(</cellXfs>)", st, re.S)
    if not m:
        return styles_bytes
    xfs = re.findall(r"<xf [^>]*/>|<xf .*?</xf>", m.group(2), re.S)
    if xf_index >= len(xfs):
        return styles_bytes
    target = xfs[xf_index]
    if "xfComplement" in target:
        return styles_bytes
    if target.endswith("/>"):
        rebuilt = target[:-2] + ">" + ext_xml + "</xf>"
    else:
        rebuilt = target[: target.rindex("</xf>")] + ext_xml + "</xf>"
    xfs[xf_index] = rebuilt
    return (st[: m.start(2)] + "".join(xfs) + st[m.end(2):]).encode("utf-8")


def _restore_native_checkboxes(filled_bytes, template_path):
    """openpyxl's save() silently drops xl/featurePropertyBag/featurePropertyBag.xml
    - the part that marks C-column cells as Excel's native interactive
    Checkbox control (confirmed by a real load->set value->save round-trip:
    the part vanishes even though the underlying boolean cell value is
    preserved). Without it, Excel still shows the right TRUE/FALSE value but
    the checkbox widget itself is gone. This copies that part (and its two
    small registration entries) from the original template's zip into the
    filled workbook's zip after openpyxl is done, so the checkboxes stay
    exactly as clickable as they were in the template you uploaded."""
    import zipfile

    with zipfile.ZipFile(template_path) as tz:
        if _FPB_PART not in tz.namelist():
            return filled_bytes  # template has no native checkboxes to restore
        fpb_xml = tz.read(_FPB_PART)

    # The bag alone is NOT what renders a checkbox. The binding lives in
    # xl/styles.xml: the checkbox cells use a specific <xf> that carries
    #   <extLst><ext uri="{C7286773-...}"><xfpb:xfComplement i="0"/></ext></extLst>
    # openpyxl rewrites styles.xml from its own object model and drops that
    # extension (confirmed by a real round-trip: the template has 2 <extLst>
    # blocks, the saved copy none) — which is why the download showed bare
    # TRUE/FALSE.
    #
    # The extension is re-injected into the xf that the checkbox cells
    # actually use, rather than copying the template's styles.xml wholesale:
    # openpyxl APPENDS style entries when it saves, so the saved sheet
    # references xf indices beyond the template's table and swapping the
    # whole part produces a workbook Excel/openpyxl cannot open
    # (IndexError: list index out of range — verified).
    ck_xf_idx, ck_ext = _template_checkbox_xf(template_path)

    out = io.BytesIO()
    with zipfile.ZipFile(io.BytesIO(filled_bytes)) as src, zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as dst:
        for item in src.infolist():
            data = src.read(item.filename)
            if item.filename == "xl/styles.xml" and ck_ext is not None:
                data = _inject_xf_complement(data, ck_xf_idx, ck_ext)
            elif item.filename == "[Content_Types].xml":
                text = data.decode("utf-8")
                if _FPB_PART.split("xl/")[1] not in text and "featurePropertyBag" not in text:
                    text = text.replace(
                        "</Types>",
                        f'<Override PartName="/{_FPB_PART}" ContentType="{_FPB_CONTENT_TYPE}"/></Types>',
                    )
                data = text.encode("utf-8")
            elif item.filename == "xl/_rels/workbook.xml.rels":
                text = data.decode("utf-8")
                if _FPB_REL_TYPE not in text:
                    existing_ids = [int(rid) for rid in re.findall(r'Id="rId(\d+)"', text)]
                    new_id = f"rId{max(existing_ids, default=0) + 1}"
                    text = text.replace(
                        "</Relationships>",
                        f'<Relationship Id="{new_id}" Type="{_FPB_REL_TYPE}" '
                        f'Target="featurePropertyBag/featurePropertyBag.xml"/></Relationships>',
                    )
                data = text.encode("utf-8")
            dst.writestr(item, data)
        if _FPB_PART not in src.namelist():
            dst.writestr(_FPB_PART, fpb_xml)
    out.seek(0)
    return out.read()


def fill_checklist_xlsx(checklist, site_id_fa, engineer_name=None, sow=None, date_str=None,
                         template_path=TEMPLATE_PATH, manual_overrides=None):
    """manual_overrides: optional {row_number: {'done': bool, 'comment': str}}
    for rows whose status is 'manual' - lets a person's own checkbox/comment
    (entered in the Streamlit UI) override the generic 'no automated check'
    placeholder text before this gets written out."""
    manual_overrides = manual_overrides or {}
    wb = openpyxl.load_workbook(template_path)
    ws = wb["Legacy - N2e Engineer Checklist"]

    ws["B8"] = site_id_fa or ""
    ws["B9"] = date_str or datetime.date.today().strftime("%m/%d/%Y")
    if engineer_name:
        ws["B7"] = engineer_name
    if sow:
        ws["B10"] = sow

    for entry in checklist:
        r = entry["row"]
        override = manual_overrides.get(r) or {}
        # The UI widget writes 'checked'; older callers passed 'done'. Accept
        # both — a key mismatch here is why edits made in the app never
        # reached the downloaded file.
        user_checked = override.get("checked", override.get("done"))
        user_comment = (override.get("comment") or "").strip()

        # The tick means "this check was carried out", NOT "it passed" — so
        # an automated row is ticked even when the check found a mismatch
        # (the finding itself is reported in the Comments column).
        #
        # MANUAL rows are the exception: nothing was verified
        # automatically, so they default to UNTICKED and only the engineer
        # can tick them, in the UI. Ticking them here would assert a review
        # that never happened.
        #
        # An explicit choice from the UI always wins, either way.
        ws[f"C{r}"] = (bool(user_checked) if user_checked is not None
                       else entry["status"] != "manual")

        label, _ = STATUS_META.get(entry["status"], ("", False))
        if user_comment:
            # User's own words win, but keep the status label so a failure
            # is never silently downgraded to a clean-looking row.
            ws[f"E{r}"] = f"[{label}] {user_comment}" if label else user_comment
        elif entry["status"] == "manual":
            ws[f"E{r}"] = ("[MANUAL — marked done, no comment]" if user_checked
                           else "[MANUAL] Not yet reviewed.")
        else:
            comment = entry["detail"] or ""
            ws[f"E{r}"] = f"[{label}] {comment}" if label else comment

    buf = io.BytesIO()
    wb.save(buf)
    buf.seek(0)
    return _restore_native_checkboxes(buf.read(), template_path)


EDP_FIELD_TABLE_COLUMNS = [
    "SITE_NAME", "CABINET", "BBU_TYPE", "NODE_MODEL", "SIAD_PORT_SIZE_BBU",
    "SIAD_PORT_FACING_BBU", "BEARER_ENODEB_SB_VLAN_ID", "IPV6_SIAD_BEARER_IP_DEF_ROUTER",
    "IPV6_ENODEB_BEARER_IP", "OAM_ENODEB_SIAD_OAM_VLAN", "IPV6_SIAD_OAM_IP_DEF_ROUTER",
    "IPV6_ENODEB_OAM_IP",
]


def build_primary_secondary_node_list(ciq_wb):
    """One {node, role, tech, log_alias} entry per identity declared in
    Mixed Mode Info — both the Primary (whichever of eNodeB/gNodeB Name
    matches 'Node to be built as') and the Secondary (the other one),
    when both exist.

    Each Mixed Mode Info ROW stands alone: 'Node to be built as' is
    always the real log/AMOS node id for that row, and the row's OTHER
    identity (Secondary) lives inside that SAME log — a Secondary is
    never a separately uploaded log. log_alias on a Secondary entry
    names which key to use against node_logs_text (always the Primary on
    the same row).

    tech is 'LTE' if that entry came from eNodeB Name, 'NR' if from
    gNodeB Name — this is a TECHNOLOGY tag, not a role tag. On a TMBB
    node both identities' bearer VLAN/IP/default-router live under the
    same log's 'Router=LTE', split only by an InterfaceIPv6/NextHop
    suffix ('1' for LTE-tech, 'NR' for NR-tech) — see
    pre_extract.extract_bearer_oam_ipv6. WHICH identity (LTE or NR) is
    Primary varies by site — confirmed opposite on two real sites
    (FCL04120: eNodeB/LTE is Primary; OKTN000082: gNodeB/NR is Primary)
    — so tech must be read off the actual identity, never assumed from
    role.

    This does NOT reuse checked_nodes (run_validation.py's own node list):
    checked_nodes only ever holds the PRIMARY name ('Node to be built as'),
    so every existing EDP check in this module (_edp_found_status etc.,
    all called with checked_nodes) has only ever looked up the primary
    node's own EDP row — a real gap confirmed on a real dual-tech site:
    HXL04147 (primary) and HXIN010147 (secondary) are two separate EDP
    rows under different SITE_NAME values, and HXIN010147's row was never
    looked up anywhere. This function is additive: it does not change
    checked_nodes or any existing check, it only supplies both node names
    for the field-value display table below."""
    out = []
    mm_rows_all = cer.mixed_mode_rows(ciq_wb)
    for m in mm_rows_all:
        build_as = _norm(m.get("Node to be built as")).upper()
        e_name = _norm(m.get("eNodeB Name"))
        # g_name straight off this row can be blank (Name field wiped,
        # gNBId wiped, or both) while a real Secondary still sits in EDP/
        # RFDS/gNB Info/5G Info — same recovery cer.resolve_g_name already
        # does for the run_validation.py pipeline, reused here so this
        # table (and, via edp_node_ids below, the checklist's own EDP-check
        # rows) don't silently drop that Secondary the same way. rfds_pages/
        # rfds_bytes aren't available in this call chain, so only the
        # gNBId and sole-candidate-elimination tiers apply here.
        g_name = _norm(m.get("gNodeB Name")) or _norm(cer.resolve_g_name(ciq_wb, m, e_name, None, None, mm_rows_all) or "")
        bbu_mode = _norm(m.get("BBU Mode")).upper()
        if e_name and e_name.upper() == build_as:
            primary, secondary = e_name, g_name
        elif g_name and g_name.upper() == build_as:
            primary, secondary = g_name, e_name
        else:
            primary, secondary = (e_name or g_name), (g_name if e_name else "")
        if primary:
            primary_tech = "LTE" if primary == e_name else ("NR" if primary == g_name else None)
            out.append({"node": primary, "role": "Primary", "tech": primary_tech})
        if secondary and bbu_mode != "SMBB":
            secondary_tech = "NR" if secondary == g_name else ("LTE" if secondary == e_name else None)
            out.append({"node": secondary, "role": "Secondary", "tech": secondary_tech, "log_alias": primary})
    return out



def build_edp_field_table(edp_rows, node_role_list):
    """One row per (node, role) in node_role_list, with the raw EDP field
    values requested for a side-by-side view: SITE_NAME/CABINET/BBU_TYPE/
    NODE_MODEL/SIAD_PORT_SIZE_BBU/SIAD_PORT_FACING_BBU/
    BEARER_ENODEB_SB_VLAN_ID/IPV6_SIAD_BEARER_IP_DEF_ROUTER/
    IPV6_ENODEB_BEARER_IP/OAM_ENODEB_SIAD_OAM_VLAN/
    IPV6_SIAD_OAM_IP_DEF_ROUTER/IPV6_ENODEB_OAM_IP.

    Uses the same per-node EDP row lookup (cer.edp_rows_for_site) every
    other EDP check in this module uses, via node_role_list from
    build_primary_secondary_node_list() so both Primary and Secondary
    physical nodes get their OWN row looked up (see that function's
    docstring for why this differs from every existing check's node list).
    This is a raw-value DISPLAY table, not a new check."""
    out = []
    for entry in node_role_list:
        nid = entry["node"]
        rows = cer.edp_rows_for_site(edp_rows, nid)
        rec = rows[0] if rows else None
        row = {"node": nid, "role": entry["role"]}
        for col in EDP_FIELD_TABLE_COLUMNS:
            row[col] = _norm(rec.get(col)) if rec else "NOT FOUND"
        out.append(row)
    return out


def build_pre_vs_edp_ipv6_table(node_logs_text, node_role_list, edp_rows):
    """Pre (from Pre kget-all logs, pre_extract.extract_bearer_oam_ipv6())
    vs EDP (the same field, read directly off the site's own EDP row) for
    the 6 bearer/OAM fields — one row per node in node_role_list that has
    a Pre log available. A node with no uploaded Pre log is skipped (there
    is nothing to compare, not a MISMATCH)."""
    import ipaddress
    import pre_extract as pe

    def _ipv6_equal(a, b):
        """Two IPv6 address strings are the SAME address even when written
        differently — confirmed real case: Pre reports '...6:954:2' and EDP
        reports '...6:0954:2' for the identical address (a zero-padded
        hextet). A plain string compare after stripping '/64' called that a
        mismatch; this parses both through ipaddress.IPv6Address so
        zero-padding, letter case, and '::' compression differences are all
        normalised before comparing. Falls back to the stripped-string
        compare if either side fails to parse (e.g. a genuinely malformed
        value), so a parse failure surfaces as its own mismatch rather than
        silently passing."""
        try:
            return ipaddress.IPv6Address(a.split("/")[0]) == ipaddress.IPv6Address(b.split("/")[0])
        except ValueError:
            return a.split("/")[0] == b.split("/")[0]

    field_map = [
        ("bearer_vlan", "BEARER_ENODEB_SB_VLAN_ID", "Bearer VLAN", False),
        ("bearer_ip", "IPV6_ENODEB_BEARER_IP", "Bearer IPv6", True),
        ("bearer_router_ip", "IPV6_SIAD_BEARER_IP_DEF_ROUTER", "Bearer Default Router", True),
        ("oam_vlan", "OAM_ENODEB_SIAD_OAM_VLAN", "OAM VLAN", False),
        ("oam_ip", "IPV6_ENODEB_OAM_IP", "OAM IPv6", True),
        ("oam_router_ip", "IPV6_SIAD_OAM_IP_DEF_ROUTER", "OAM Default Router", True),
    ]

    out = []
    for entry in node_role_list:
        nid = entry["node"]
        log_text = _log_text_for(entry, node_logs_text)
        if not log_text:
            continue
        pre_vals = pe.extract_bearer_oam_ipv6(log_text)
        rows = cer.edp_rows_for_site(edp_rows, nid)
        edp_rec = rows[0] if rows else None
        for pre_key, edp_key, label, is_ipv6 in field_map:
            pre_v = _bearer_pre_value(pre_vals, pre_key, entry)
            edp_v = _norm(edp_rec.get(edp_key)) if edp_rec else None
            if pre_v is None and not edp_v:
                continue  # neither side has data - nothing to show
            if not pre_v or not edp_v:
                status = "unknown"
            elif is_ipv6:
                status = "match" if _ipv6_equal(pre_v, edp_v) else "mismatch"
            else:
                status = "match" if pre_v == edp_v else "mismatch"
            out.append({
                "node": nid, "role": entry["role"], "field": label,
                "pre_value": pre_v or "Not found in Pre log",
                "edp_value": edp_v or "Not found in EDP",
                "status": status,
            })
    return out


def _same_ipv6(a, b):
    """Compare two IPv6 values ignoring cosmetic differences: the '/prefix'
    suffix, zero-padding and '::' compression. Same rule the long-form
    Pre-vs-EDP check uses (_pre_vs_edp_field_status._ipv6_eq) — shared here
    so the pivot table and that check can never disagree on what counts as
    a mismatch. Falls back to a plain string compare if a value isn't a
    parseable address."""
    import ipaddress
    a, b = str(a or "").strip(), str(b or "").strip()
    try:
        return ipaddress.IPv6Address(a.split("/")[0]) == ipaddress.IPv6Address(b.split("/")[0])
    except Exception:
        return a.split("/")[0] == b.split("/")[0]


def build_pre_vs_edp_pivot_rows(node_logs_text, node_role_list, edp_rows, ciq_wb=None):
    """One row per (node, role): Bearer/OAM VLAN, IPv6, Default Router,
    pre + EDP side by side — wide layout (Node ID + 2-col-per-field),
    replacing the long one-row-per-field format from
    build_pre_vs_edp_ipv6_table() above. A node with no uploaded Pre log
    still gets a row (pre columns show '—'), so the Node ID list is
    complete regardless of which logs were uploaded this run."""
    import pre_extract as pe

    field_map = [
        ("bearer_vlan", "BEARER_ENODEB_SB_VLAN_ID", "bearer_vlan"),
        ("bearer_ip", "IPV6_ENODEB_BEARER_IP", "bearer_ipv6"),
        ("bearer_router_ip", "IPV6_SIAD_BEARER_IP_DEF_ROUTER", "bearer_router"),
        ("oam_vlan", "OAM_ENODEB_SIAD_OAM_VLAN", "oam_vlan"),
        ("oam_ip", "IPV6_ENODEB_OAM_IP", "oam_ipv6"),
        ("oam_router_ip", "IPV6_SIAD_OAM_IP_DEF_ROUTER", "oam_router"),
    ]
    role_short = {"Primary": "P", "Secondary": "S"}
    du_type = _du_type_by_node(ciq_wb) if ciq_wb is not None else {}

    out = []
    for entry in node_role_list:
        nid = entry["node"]
        log_text = _log_text_for(entry, node_logs_text)
        pre_vals = pe.extract_bearer_oam_ipv6(log_text) if log_text else {}
        rows = cer.edp_rows_for_site(edp_rows, nid)
        edp_rec = rows[0] if rows else None
        row = {"label": f"{nid} ({role_short.get(entry['role'], entry['role'][:1])})"}
        for pre_key, edp_key, out_key in field_map:
            row[f"{out_key}_pre"] = _bearer_pre_value(pre_vals, pre_key, entry) or "—"
            row[f"{out_key}_edp"] = _norm(edp_rec.get(edp_key)) if edp_rec else "—"

        # SIAD port size: Pre side is the transport EthernetPort's
        # admOperatingMode ('10G_FULL'/'1G_FULL' -> 10GE/1GE). Which port
        # holds it depends on the board generation, so the DU type is read
        # from the CIQ first — same source _siad_port_size_pre_status uses,
        # so the pivot and that check can't disagree. node_role_list
        # entries carry only {node, role}, no board model.
        # SIAD port size is a physical-node property, same as OAM — a
        # Secondary identity has no port of its own (confirmed: it's the
        # SAME physical transport port the Primary already reports),
        # so it's suppressed here rather than repeating the Primary's
        # own port size under the Secondary's row.
        if entry.get("role") == "Secondary":
            board, pre_size = None, None
        else:
            board = du_type.get(nid) if ciq_wb is not None else None
            _, pre_size = pe.extract_transport_port_mode(log_text, board) if (log_text and board) else (None, None)
        row["siad_port_size_pre"] = pre_size or "—"
        row["siad_port_size_edp"] = _norm(edp_rec.get("SIAD_PORT_SIZE_BBU")) if edp_rec else "—"

        # Per-field verdict, so the UI can colour each pair independently.
        # IPv6 is normalised before comparing (zero-padding / '::'
        # compression are cosmetic, not mismatches); a '—' on either side
        # means "not captured", which is unknown, never a mismatch.
        for out_key in [k for _, _, k in field_map] + ["siad_port_size"]:
            pv, ev = row[f"{out_key}_pre"], row[f"{out_key}_edp"]
            if pv in ("—", "") or ev in ("—", ""):
                row[f"{out_key}_status"] = "unknown"
            elif "ipv6" in out_key or "router" in out_key:
                row[f"{out_key}_status"] = "match" if _same_ipv6(pv, ev) else "mismatch"
            else:
                row[f"{out_key}_status"] = "match" if _norm(pv).upper() == _norm(ev).upper() else "mismatch"
        out.append(row)
    return out


# ── Unified Pre/CIQ vs Post(EDP) checklist — the 12-field spec confirmed
# against the screenshot table. Two fields (the Default Router pair) are
# intentionally excluded from mismatch-highlighting per that spec (still
# shown, status forced to 'info' so they never render red/green) — routers
# are shared infra, not something a build error would typically shift.
#
# Per-field source of the "Pre/CIQ" side:
#   - the 6 Bearer/OAM network fields  -> Pre kget-all log (extract_bearer_oam_ipv6)
#   - node_model                        -> CIQ, via the SAME results['board_type']
#                                          check already computed elsewhere (CIQ DU
#                                          Type vs EDP Model) — not Pre-log based,
#                                          matching "Node model should match the CIQ"
#   - cabinet                           -> derived, not read from any log: a
#                                          Secondary's cabinet is checked against
#                                          its OWN paired Primary's cabinet + 'V'
#                                          (e.g. Primary BBU01 -> Secondary BBU01V),
#                                          not just format-checked independently
#                                          (the older _edp_cabinet_status above only
#                                          checks the regex/'V' suffix in isolation,
#                                          never that the NUMBER actually matches its
#                                          own Primary — two unrelated nodes named
#                                          BBU01/BBU02V would previously pass)
#   - site_name/bbu_type/siad_port_size_bbu/siad_port_facing_bbu -> EDP value only,
#                                          no Pre/CIQ counterpart in this pipeline
#
# A node with NO uploaded Pre log (new node — same convention run_validation.py
# already uses for is_new_node=not has_pre) gets 'unknown' (grey, no highlight)
# on every Pre-sourced field instead of 'mismatch': this is what makes an
# SMBB(Pre)->MMBB(Post) transition safe — the newly-appearing Secondary has no
# Pre history by definition, and that absence must not be flagged. The Primary's
# own row is built and compared exactly as it always is, unaffected by whether
# a Secondary exists at all.
CHECKLIST_FIELD_SPEC = [
    ("SITE_NAME", "site_name", True),
    ("CABINET", "cabinet", True),
    ("BBU_TYPE", "bbu_type", True),
    ("NODE_MODEL", "node_model", True),
    ("SIAD_PORT_SIZE_BBU", "siad_port_size_bbu", True),
    ("SIAD_PORT_FACING_BBU", "siad_port_facing_bbu", True),
    ("BEARER_ENODEB_SB_VLAN_ID", "bearer_enodeb_sb_vlan_id", True),
    ("IPV6_SIAD_BEARER_IP_DEF_ROUTER", "ipv6_siad_bearer_ip_def_router", False),
    ("IPV6_ENODEB_BEARER_IP", "ipv6_enodeb_bearer_ip", True),
    ("OAM_ENODEB_SIAD_OAM_VLAN", "oam_enodeb_siad_oam_vlan", True),
    ("IPV6_SIAD_OAM_IP_DEF_ROUTER", "ipv6_siad_oam_ip_def_router", False),
    ("IPV6_ENODEB_OAM_IP", "ipv6_enodeb_oam_ip", True),
]

_PRE_NETWORK_FIELD_MAP = {
    "BEARER_ENODEB_SB_VLAN_ID": "bearer_vlan",
    "IPV6_ENODEB_BEARER_IP": "bearer_ip",
    "IPV6_SIAD_BEARER_IP_DEF_ROUTER": "bearer_router_ip",
    "OAM_ENODEB_SIAD_OAM_VLAN": "oam_vlan",
    "IPV6_ENODEB_OAM_IP": "oam_ip",
    "IPV6_SIAD_OAM_IP_DEF_ROUTER": "oam_router_ip",
}


def _cabinet_pairing_map(ciq_wb, edp_rows):
    """{secondary_node_id: expected_cabinet} from the SAME Mixed Mode Info
    pairing build_primary_secondary_node_list() uses — recomputed here
    (rather than reverse-engineered from its flat output) so a Secondary
    is always checked against its OWN Primary, never just row order."""
    expected = {}
    for m in cer.mixed_mode_rows(ciq_wb):
        build_as = _norm(m.get("Node to be built as")).upper()
        e_name, g_name = _norm(m.get("eNodeB Name")), _norm(m.get("gNodeB Name"))
        bbu_mode = _norm(m.get("BBU Mode")).upper()
        if e_name and e_name.upper() == build_as:
            primary, secondary = e_name, g_name
        elif g_name and g_name.upper() == build_as:
            primary, secondary = g_name, e_name
        else:
            primary, secondary = (e_name or g_name), (g_name if e_name else "")
        if not (secondary and primary and bbu_mode != "SMBB"):
            continue
        prim_rows = cer.edp_rows_for_site(edp_rows, primary)
        prim_cab = _norm(prim_rows[0].get("CABINET")) if prim_rows else ""
        expected[secondary] = f"{prim_cab}V" if prim_cab else None
    return expected


def _cabinet_pairing_status(ciq_wb, edp_rows, node_ids):
    """Combines the existing format-only check (well-formed 'BBUxx'/'BBUxxV')
    with the real cross-node pairing check confirmed in this conversation:
    a Secondary's cabinet number must match its OWN Primary's, not just
    look like a valid cabinet string in isolation."""
    fmt_status, fmt_detail = _edp_cabinet_status(edp_rows, node_ids)
    expected = _cabinet_pairing_map(ciq_wb, edp_rows)
    bad, checked = [], 0
    for nid, exp in expected.items():
        rows = cer.edp_rows_for_site(edp_rows, nid)
        actual = _norm(rows[0].get("CABINET")) if rows else ""
        if not exp or not actual:
            continue
        checked += 1
        if actual.upper() != exp.upper():
            bad.append(f"{nid}: expected cabinet '{exp}' (from its own Primary), EDP shows '{actual}'")
    if bad or fmt_status == "mismatch":
        parts = ([fmt_detail] if fmt_status == "mismatch" else []) + bad
        return "mismatch", "; ".join(parts[:6])
    if checked:
        return "match", f"{fmt_detail} {checked} Secondary/Primary pair(s) also checked, all pass."
    return fmt_status, fmt_detail


def _du_type_by_node(ciq_wb):
    """{node_id: hardware model number} from eNB/gNB Info 'DU type' — the
    CIQ-side counterpart to EDP's NODE_MODEL string (e.g. 'RAN PROCESSOR
    6672' contains this same '6672')."""
    out = {}
    for r in (cer.enb_info_rows(ciq_wb) if ciq_wb else []):
        n = _norm(r.get("eNodeB Name"))
        if n:
            out[n] = _norm(r.get("DU type"))
    if ciq_wb and "gNB Info" in ciq_wb.sheetnames:
        for r in cer.sheet_rows_as_dicts(ciq_wb["gNB Info"]):
            n = _norm(r.get("gNodeB Name"))
            if n and n not in out:
                out[n] = _norm(r.get("DU type"))
    return out


def _bbu_type_vs_node_model_status(ciq_wb, edp_rows, node_ids):
    """CIQ hardware board number (5G Info/eNB/gNB Info 'DU type'/'BBU Type')
    vs EDP NODE_MODEL. Confirmed against real EDP data in this conversation:
    the EDP column named BBU_TYPE actually holds the mode string
    ('MIXED MODE'/'TRIPLE MODE'), and NODE_MODEL holds the hardware string
    ('RAN PROCESSOR 6672', 'BASEBAND 6630') — the reverse of what the
    column names suggest. This check is deliberately wired to NODE_MODEL,
    not BBU_TYPE, for that reason."""
    du_type = _du_type_by_node(ciq_wb)
    bad, checked = [], 0
    for nid in node_ids:
        board = du_type.get(nid)
        rows = cer.edp_rows_for_site(edp_rows, nid)
        edp_model = _norm(rows[0].get("NODE_MODEL")) if rows else ""
        if not board or not edp_model:
            continue
        checked += 1
        if board not in edp_model:
            bad.append(f"{nid}: CIQ board '{board}' not found in EDP NODE_MODEL '{edp_model}'")
    if not checked:
        return "unknown", "No CIQ board type / EDP NODE_MODEL data to check."
    if bad:
        return "mismatch", "; ".join(bad[:6])
    return "match", f"{checked} node(s) checked, all pass."


# MMBB/TMBB map to a fixed EDP BBU_TYPE string, confirmed against real data.
# SMBB does NOT — confirmed real value for an SMBB (LTE-only) node was
# '4G LTE Macro', not 'SINGLE MODE' as originally assumed — so SMBB is
# flagged 'manual' rather than compared against a guessed string.
_BBU_MODE_TO_EDP_TYPE = {"MMBB": "MIXED MODE", "TMBB": "TRIPLE MODE"}


def _node_model_vs_bbu_type_status(ciq_wb, edp_rows, node_ids):
    """CIQ Mixed Mode Info 'BBU Mode' (MMBB/SMBB/TMBB) vs EDP BBU_TYPE."""
    mm_rows = cer.mixed_mode_rows(ciq_wb) if ciq_wb else []
    mode_by_node = {}
    for r in mm_rows:
        n = _norm(r.get("Node to be built as")) or _norm(r.get("eNodeB Name")) or _norm(r.get("gNodeB Name"))
        if n:
            mode_by_node[n] = _norm(r.get("BBU Mode")).upper()

    bad, checked, manual = [], 0, []
    for nid in node_ids:
        mode = mode_by_node.get(nid)
        rows = cer.edp_rows_for_site(edp_rows, nid)
        edp_type = _norm(rows[0].get("BBU_TYPE")) if rows else ""
        if not mode or not edp_type:
            continue
        expected = _BBU_MODE_TO_EDP_TYPE.get(mode)
        if expected is None:
            manual.append(f"{nid}: SMBB — EDP BBU_TYPE is '{edp_type}', no fixed expected string confirmed for SMBB yet")
            continue
        checked += 1
        if edp_type.upper() != expected:
            bad.append(f"{nid}: CIQ {mode} expects EDP BBU_TYPE '{expected}', got '{edp_type}'")
    if bad:
        return "mismatch", "; ".join(bad[:6])
    if checked:
        note = f"{checked} node(s) checked, all pass."
        if manual:
            note += f" ({len(manual)} SMBB node(s) need manual check — see note)"
        return "match", note
    if manual:
        return "manual", "; ".join(manual[:6])
    return "unknown", "No CIQ BBU Mode / EDP BBU_TYPE data to check."


def _pre_vs_edp_field_status(node_logs_text, node_role_list, edp_rows, pre_key, edp_col, is_ipv6=False):
    """One EDP field, Pre vs EDP, per (node, role) in node_role_list. A node
    with no uploaded Pre log at all is treated as 'no history to compare'
    (unknown, not mismatch) — this is what makes an SMBB(Pre)->MMBB(Post)
    transition safe: the newly-appearing Secondary has no Pre log by
    definition, and that must not be flagged. Confirmed: highlight ALL 6
    bearer/OAM fields equally, including both Default Router fields."""
    import pre_extract as pe
    import ipaddress

    def _ipv6_eq(a, b):
        try:
            return ipaddress.IPv6Address(a.split("/")[0]) == ipaddress.IPv6Address(b.split("/")[0])
        except ValueError:
            return a.split("/")[0] == b.split("/")[0]

    bad, checked, no_pre = [], 0, []
    for entry in node_role_list:
        nid = entry["node"]
        log_text = _log_text_for(entry, node_logs_text)
        rows = cer.edp_rows_for_site(edp_rows, nid)
        edp_v = _norm(rows[0].get(edp_col)) if rows else ""
        if not log_text:
            no_pre.append(nid)
            continue
        pre_v = _bearer_pre_value(pe.extract_bearer_oam_ipv6(log_text), pre_key, entry) or ""
        if not pre_v or not edp_v:
            continue
        checked += 1
        same = _ipv6_eq(pre_v, edp_v) if is_ipv6 else (pre_v == edp_v)
        if not same:
            bad.append(f"{nid} ({entry['role']}): Pre={pre_v}, EDP={edp_v}")
    if bad:
        return "mismatch", "; ".join(bad[:6])
    if checked:
        note = f"{checked} node(s) checked, all pass."
        if no_pre:
            note += f" ({len(no_pre)} node(s) with no Pre log, not checked: {', '.join(no_pre[:4])})"
        return "match", note
    if no_pre:
        return "unknown", f"No Pre log for: {', '.join(no_pre[:6])}"
    return "unknown", "No Pre/EDP data to compare."


def _siad_port_size_pre_status(node_logs_text, ciq_wb, edp_rows, node_ids):
    """Pre (admOperatingMode on the board-generation-specific transport
    port — see pre_extract.extract_transport_port_mode) vs EDP
    SIAD_PORT_SIZE_BBU."""
    import pre_extract as pe
    du_type = _du_type_by_node(ciq_wb)

    bad, checked, no_port = [], 0, []
    for nid in node_ids:
        board = du_type.get(nid)
        log_text = (node_logs_text or {}).get(nid)
        if not board or not log_text:
            continue
        port, pre_size = pe.extract_transport_port_mode(log_text, board)
        rows = cer.edp_rows_for_site(edp_rows, nid)
        edp_size = _norm(rows[0].get("SIAD_PORT_SIZE_BBU")) if rows else ""
        if not pre_size:
            no_port.append(f"{nid}: no known transport port found in Pre log for board '{board}'")
            continue
        if not edp_size:
            continue
        checked += 1
        if pre_size.upper() != edp_size.upper():
            bad.append(f"{nid}: Pre {port}={pre_size}, EDP={edp_size}")
    if bad:
        return "mismatch", "; ".join(bad[:6])
    if checked:
        note = f"{checked} node(s) checked, all pass."
        if no_port:
            note += f" ({len(no_port)} skipped: {'; '.join(no_port[:3])})"
        return "match", note
    if no_port:
        return "unknown", "; ".join(no_port[:6])
    return "unknown", "No Pre log / board type data to check."


def build_checklist_field_table(node_role_list, node_logs_text, edp_rows, ciq_wb, results):
    """One row per (node, role, field) across all 12 fields in
    CHECKLIST_FIELD_SPEC — 'pre_value' is Pre-log/CIQ/derived depending on
    the field (see module comment above), 'edp_value' is always the EDP
    (Post/target) value. status is 'unknown' (no highlight) whenever
    there's nothing on the Pre/CIQ side to compare, INCLUDING every node
    with no uploaded Pre log at all — this is what keeps a newly-added
    Secondary (SMBB->MMBB) from being flagged just for lacking history."""
    import pre_extract as pe

    board_type_by_node = {r.get("node"): r for r in results.get("board_type", [])}
    cabinet_expected = _cabinet_pairing_map(ciq_wb, edp_rows)

    out = []
    for entry in node_role_list:
        nid, role = entry["node"], entry["role"]
        log_text = _log_text_for(entry, node_logs_text)
        pre_net_vals = pe.extract_bearer_oam_ipv6(log_text) if log_text else {}
        rows = cer.edp_rows_for_site(edp_rows, nid)
        edp_rec = rows[0] if rows else None

        for edp_col, label, highlight in CHECKLIST_FIELD_SPEC:
            edp_v = _norm(edp_rec.get(edp_col)) if edp_rec else ""

            if edp_col == "NODE_MODEL":
                bt = board_type_by_node.get(nid)
                pre_v = _norm(bt.get("ciq_du_type")) if bt else ""
                edp_v = _norm(bt.get("edp_model")) if bt else edp_v
                status = str(bt.get("status", "unknown")).lower() if bt else "unknown"
            elif edp_col == "CABINET":
                if role == "Secondary" and nid in cabinet_expected:
                    pre_v = cabinet_expected[nid] or ""
                    status = "unknown" if not pre_v or not edp_v else (
                        "match" if pre_v.upper() == edp_v.upper() else "mismatch")
                else:
                    pre_v = ""
                    status = "unknown"
            elif edp_col in _PRE_NETWORK_FIELD_MAP:
                pre_v = _bearer_pre_value(pre_net_vals, _PRE_NETWORK_FIELD_MAP[edp_col], entry) or ""
                status = "unknown" if not pre_v or not edp_v else (
                    "match" if pre_v == edp_v else "mismatch")
            else:
                pre_v = ""
                status = "unknown"

            if not highlight and status == "mismatch":
                status = "info"  # unchecked fields: shown, never highlighted red

            out.append({
                "node": nid, "role": role, "field": label,
                "pre_value": pre_v or "—", "edp_value": edp_v or "—",
                "status": status,
            })
    return out
