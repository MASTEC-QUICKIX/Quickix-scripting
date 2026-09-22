"""
Pre vs Post — Audit tab's node-level and cell-level diff. Python port of
QUICKIX_Pre-Script_Validation.html's runAudit() / renderNodeAudit() /
compareCellLevel() / compareNRCellLevel(), cross-checked against the
extracted HTML source function-by-function (see comments below each rule
citing the exact HTML behavior it mirrors).

Cell matching for the LTE/5G tables uses cell-name SUFFIX matching
(everything after the first '_'), same as the HTML's getSuffix()/getPrefix()
— deliberately NOT this project's own sow_analysis.classify_carriers()
signal, because a sector move changes the node PREFIX by definition, so
suffix matching is the only way to still pair a moved cell with its old Pre
values for the field-level diff. sow_analysis's CIQ-sheet-based signal
remains the right source for engineer_comments.py's narrative, which is a
different question ("what moved, per the CIQ's own bookkeeping") than this
module's ("show me every Pre value beside its Post value").

Link (Single/Double) PRE vs POST: NOT the HTML's original "Dual Link in
AMOS, Single Link in CIQ" comment addendum, which depended on a Pre-side
RadioPort (DATA1/DATA2) signal — confirmed absent from every kget-all log
this project has seen, and confirmed NOT reconstructable from a shared-FRU
heuristic either (tested against a real CIQ: two cells sharing one physical
RRU in kget-all were both CIQ RadioPort=DATA1, while a single-fed cell
elsewhere was RadioPort=DATA1/DATA2 — the opposite of what that heuristic
would predict, so it was dropped rather than shipped as a false signal).

Instead this reuses the two Single/Double Link values THIS PROJECT ALREADY
COMPUTES independently on each side, from confirmed sources:
  - Pre:  pe.extract_cell_to_rilink_detail()'s 'rilink_type' (the same
    value the Pre checks (AMOS) tab's own RiLink column shows) — RiLink
    row count per FRU, a genuinely different (and confirmed-extractable)
    physical signal from the DATA1/DATA2 one above.
  - Post: ciq_checks.apply_link_and_sharing()'s 'link' (the same value the
    CIQ Checks tab's own Link (Single/Doublelink) column shows) —
    DATA1/DATA2 RadioPort grouping.
Comparing these two existing, already-displayed-elsewhere values is a
different question from the dropped HTML feature above (which needed ONE
signal present on BOTH sides) - here each side keeps its own real source,
and this table just shows them together and flags disagreement, exactly
like every other PRE | POST field in this table.
"""
import band_labels as bl
import checks_sector as cs
import ciq_checks as cc
import ciq_edp_reader as cer
import pre_cell_inventory as pci
import pre_extract as pe


def _nz(v):
    """Same as the HTML's nz(): None/undefined -> '', otherwise unchanged —
    used before string comparison so None and "" compare equal."""
    return "" if v is None else v


def _get_suffix(cell_name):
    """getSuffix(): everything after the first '_', upper-cased — this is
    the sector+carrier part of a cell name, stable across a node rename or
    sector move (e.g. 'HXL00147_7A_1' -> '7A_1')."""
    if not cell_name:
        return ""
    parts = str(cell_name).split("_")
    return "_".join(parts[1:]).strip().upper()


def _get_prefix(cell_name):
    """getPrefix(): the node-name part before the first '_', upper-cased."""
    return str(cell_name or "").split("_")[0].upper()


def _norm(v):
    return str(v or "").strip().upper()


def _norm_bb(v):
    """normBB(): pulls the first 3-4 digit run out of a board string, so
    'CXP9024418/16_R17C21' and '5216' compare on the board NUMBER only."""
    import re
    s = str(v or "").strip().upper()
    m = re.search(r'\d{3,4}', s)
    return m.group(0) if m else s


def build_node_pre_post(pre_summary_rows, ciq_node_rows, node_logs_text, edp_rows, ciq_wb=None):
    """pre_summary_rows: amos_view.build_amos_tables()'s summary_rows (one
    dict per Pre node, with 'node' and 'sw_package').
    ciq_node_rows: ciq_view.build_node_integration()'s output (one dict per
    CIQ node, with 'node' and 'bb_type').
    node_logs_text: {node_id: raw Pre log text}, passed straight to
    checks_node.check_ptp_matrix() for the real A-G PTP verdict — this
    replaces a hardcoded '-' placeholder that predated that check.
    edp_rows: EDP sheet rows, also passed to check_ptp_matrix().
    ciq_wb: accepted for backward compatibility with existing callers;
    unused here now that DSS is shown at the cell level only (see
    compare_lte_cell_level's 'dss'/'_dss_ok' fields) rather than as a
    node-level rollup.

    Returns a list of {node, status, type, ptp, _ptp_flag} rows: type is
    one of 'change'/'nochange'/'delete'/'new', matching the HTML's row
    classes for color coding (see PRE_POST_ROW_COLORS in the caller).
    _ptp_flag is True when the PTP verdict itself represents an action
    item ('PTP should be created' / 'PTP to be created'), used to
    highlight it red."""
    import checks_node as cn

    pre_by_node = {_norm(r["node"].split(" / ")[0]): r for r in pre_summary_rows}
    ciq_by_node = {_norm(r["node"]): r for r in ciq_node_rows}

    def _ptp_for(node_id, is_new_node):
        log_text = (node_logs_text or {}).get(node_id)
        rows = cn.check_ptp_matrix(node_id, log_text, edp_rows, is_new_node=is_new_node)
        verdict = rows[0]["ptp"] if rows else "-"
        flag = verdict in ("PTP should be created", "PTP to be created")
        return verdict, flag

    result = []
    for key, p in pre_by_node.items():
        pre_bb = p.get("sw_package")
        node_id = p["node"].split(" / ")[0]
        if key in ciq_by_node:
            fin_bb = ciq_by_node[key].get("bb_type")
            changed = _norm_bb(pre_bb) != _norm_bb(fin_bb)
            status = f"Board Changed: {pre_bb} \u2192 {fin_bb}" if changed else "No Board Change"
            row_type = "change" if changed else "nochange"
            ptp, ptp_flag = _ptp_for(node_id, is_new_node=False)
        else:
            status, row_type = "Node Deleted", "delete"
            ptp, ptp_flag = "\u2014", False
        result.append({
            "node": p["node"], "status": status, "type": row_type,
            "ptp": ptp, "_ptp_flag": ptp_flag,
        })

    for key, c in ciq_by_node.items():
        if key not in pre_by_node:
            ptp, ptp_flag = _ptp_for(c["node"], is_new_node=True)
            result.append({"node": c["node"], "status": "Newly Adding Node", "type": "new",
                            "ptp": ptp, "_ptp_flag": ptp_flag})
    return result


def _cmp(pre, post, is_rru=False):
    """cmp(): returns (display_text, is_match). Callers color the cell
    green/red from is_match, same as the HTML's cmp()->tdFromCmp() pipeline,
    and append '(Swap)' to RRU-model comparisons that mismatch."""
    p, q = str(_nz(pre)).strip(), str(_nz(post)).strip()
    is_match = (p == q) or (not p and not q)
    text = f"{p or '-'} | {q or '-'}"
    if is_rru and p and q and p != q:
        text += " (Swap)"
    return text, is_match


def _cmp_sector_id(pre, post):
    """cmpSectorId(): AMOS gives a raw SectorCarrier index ('1'); CIQ gives a
    compound sectorId ('1_1'). They match when the AMOS value equals the
    part of the CIQ value before the underscore — same rule as the HTML,
    otherwise a real match would show red."""
    p, q = str(pre or "").strip(), str(post or "").strip()
    q_prefix = q.split("_")[0]
    is_match = (p == q) or (p == q_prefix) or (not p and not q)
    return f"{p or '-'} | {q or '-'}", is_match


def _amos_lte_index(node_logs_text):
    """Node logs -> flat list of Pre LTE cell dicts with the exact field
    names compareCellLevel() expects (Cell/SC/CellID/TAC/BW/EARFCN_DL/
    EARFCN_UL/Pwr/TX/RX/Model/RiLink), built from this project's own
    confirmed extraction functions rather than re-deriving them."""
    flat = []
    for node_id, text in (node_logs_text or {}).items():
        cells = [c for c in pci.extract_pre_cells_for_node(text) if not bl.is_5g_cell(c)]
        params = pe.extract_lte_sector_params(text)
        cfg = cs._extract_sector_config(text)
        radio_by_cell = pe.extract_cell_to_radio(text)
        sc_by_cell = _extract_sector_carrier_index(text)
        cell_range_by_cell = pe.extract_cell_range(text)
        dss_by_cell = pe.extract_dss_status(text)
        # Same two-step chain amos_view.build_lte_cell_rows() uses for the
        # Pre checks (AMOS) tab's own RiLink column - reused here (not
        # re-derived) so this table's Pre-side Link value can never drift
        # from what that tab already shows for the same cell.
        fru_by_cell = pe.extract_cell_to_fru(text)
        rilink_by_cell = pe.extract_cell_to_rilink_detail(text, fru_by_cell)
        for cell in cells:
            p = params.get(cell, {})
            c = cfg.get(cell, {})
            flat.append({
                "Cell": cell, "Node": node_id,
                "SC": sc_by_cell.get(cell, ""),
                "CellID": p.get("cellId", ""), "TAC": p.get("tac", ""),
                "BW": p.get("dlChannelBandwidth", ""),
                "EARFCN_DL": p.get("earfcndl", ""), "EARFCN_UL": p.get("earfcnul", ""),
                "Pwr": c.get("power", ""), "TX": c.get("tx", ""), "RX": c.get("rx", ""),
                "Model": pe._short_radio_name(radio_by_cell.get(cell)) or "",
                "CellRange": cell_range_by_cell.get(cell, ""),
                "DSS": bool(dss_by_cell.get(cell, False)),
                "RiLink": (rilink_by_cell.get(cell) or {}).get("rilink_type") or "",
            })
    return flat


def _amos_nr_index(node_logs_text):
    """Same as _amos_lte_index but for 5G — CellID/DL/UL/BW_DL/BW_UL/Pwr/
    SSB/Model/CellRange/DSS/RiLink, matching compareNRCellLevel()'s
    expected fields."""
    flat = []
    for node_id, text in (node_logs_text or {}).items():
        cells = [c for c in pci.extract_pre_cells_for_node(text) if bl.is_5g_cell(c)]
        params = pe.extract_5g_sector_params_from_text(text)
        cfg = cs._extract_sector_config_5g(text)
        used = pe.extract_nr_used_antennas(text)
        radio_by_cell = pe.extract_cell_to_radio(text)
        cell_range_by_cell = pe.extract_cell_range_5g(text)
        dss_by_cell = pe.extract_dss_status(text)
        fru_by_cell = pe.extract_cell_to_fru(text)
        rilink_by_cell = pe.extract_cell_to_rilink_detail(text, fru_by_cell)
        for cell in cells:
            p = params.get(cell, {})
            c = cfg.get(cell, {})
            u = used.get(cell, {})
            flat.append({
                "Cell": cell, "Node": node_id,
                "CellID": p.get("cellLocalId", ""),
                "DL": p.get("arfcnDL", ""), "UL": p.get("arfcnUL", ""),
                "BW_DL": p.get("bSChannelBwDL", ""), "BW_UL": p.get("bSChannelBwUL", ""),
                "Pwr": c.get("power", ""), "SSB": p.get("ssbFrequency", ""),
                "Model": pe._short_radio_name(radio_by_cell.get(cell)) or "",
                "CellRange": cell_range_by_cell.get(cell, ""),
                "DSS": bool(dss_by_cell.get(cell, False)),
                "RiLink": (rilink_by_cell.get(cell) or {}).get("rilink_type") or "",
            })
    return flat


def _ciq_link_map(ciq_wb):
    """{cell: 'Single Link'/'Double Link'} for every LTE+NR cell in the
    CIQ, from ciq_checks.apply_link_and_sharing() — the exact same
    computation the CIQ Checks tab's own Link (Single/Doublelink) column
    already shows, reused rather than re-implemented so the two can never
    silently drift apart."""
    lte_rows = cc.build_lte_ciq_rows(ciq_wb)
    nr_rows = cc.build_nr_ciq_rows(ciq_wb)
    cc.apply_link_and_sharing(lte_rows, nr_rows)
    return {r.get("cell"): r.get("link") for r in lte_rows + nr_rows if r.get("cell")}


def _extract_sector_carrier_index(text):
    """The raw SectorCarrier index (e.g. '1', '7_1') per cell — same source
    amos_view._extract_sector_carrier_numbers() already reads; duplicated
    here as a thin wrapper so this module doesn't reach into amos_view.py's
    private helper across module boundaries."""
    import amos_view as av
    return av._extract_sector_carrier_numbers(text)


def compare_lte_cell_level(node_logs_text, ciq_wb):
    """Returns a list of row dicts, one per LTE cell in CIQ. Cross-verified
    against compareCellLevel(): for every CIQ cell, find its Pre match by
    SUFFIX; if none, 'Newly Adding Cell'; if the PREFIX also changed,
    'Sector moved: X -> Y'; otherwise 'No Sector Movement'. Every field is a
    (text, is_match) pair from _cmp()/_cmp_sector_id(), ready for color
    rendering.

    Only CIQ cells are shown (per instruction) - a Pre cell with no CIQ
    counterpart is no longer appended as a synthetic 'Cell Deleted' row.

    TAC comes from the eNB Info sheet (one row per node, keyed by eNBId) -
    NOT eUtran Parameters, which has no TAC column at all (confirmed on a
    real CIQ: 'eUtran Parameters' genuinely lacks a 'tac' field, so reading
    c.get('tac') there always returned '-', regardless of what the Pre side
    reported)."""
    amos = _amos_lte_index(node_logs_text)
    ciq_rows = cer.sheet_rows_as_dicts(ciq_wb["eUtran Parameters"]) if "eUtran Parameters" in ciq_wb.sheetnames else []
    enb_tac_by_id = {str(r.get("eNBId") or "").strip(): r.get("tac") for r in cer.enb_info_rows(ciq_wb)}
    ciq_link_by_cell = _ciq_link_map(ciq_wb)

    # CIQ/Post-side DSS signal: '5G Info' tab's own 'DSS' column names the
    # LTE cell it's paired with ('NO' when not paired) — confirmed real CIQ
    # (HXIN010147_N002A_1's DSS='HXL04147_9A_1'). Built once as a dict
    # LTE cell -> its 5G partner cell name, so the per-cell check below can
    # show WHICH 5G cell it's paired with, not just yes/no — that detail is
    # already sitting right there in the column, no reason to throw it away.
    dss_post_partner = {}
    if "5G Info" in ciq_wb.sheetnames:
        for r in cer.sheet_rows_as_dicts(ciq_wb["5G Info"]):
            v = str(r.get("DSS") or "").strip()
            if v and v.upper() != "NO":
                dss_post_partner[v.upper()] = r.get("NRCellDU") or v

    result = []
    for c in ciq_rows:
        cell_full = c.get("EutranCellFDDId") or c.get("Cell") or ""
        final_pfx, final_sfx = _get_prefix(cell_full), _get_suffix(cell_full)
        match = next((a for a in amos if _get_suffix(a["Cell"]) == final_sfx), None)
        if not match:
            comment, row_type = "Newly Adding Cell", "new"
        else:
            pre_pfx = _get_prefix(match["Cell"])
            comment, row_type = ("No Sector Movement", "nochange") if pre_pfx == final_pfx \
                else (f"Sector moved: {pre_pfx} -> {final_pfx}", "change")

        ciq_tac = enb_tac_by_id.get(str(c.get("eNBId") or "").strip())
        sc_text, sc_ok = _cmp_sector_id(_nz(match["SC"]) if match else "", c.get("sectorId"))
        cellid_text, cellid_ok = _cmp(_nz(match["CellID"]) if match else "", c.get("cellId"))
        tac_text, tac_ok = _cmp(_nz(match["TAC"]) if match else "", ciq_tac)
        bw_text, bw_ok = _cmp(_nz(match["BW"]) if match else "", c.get("dlChannelBandwidth"))
        dl_text, dl_ok = _cmp(_nz(match["EARFCN_DL"]) if match else "", c.get("earfcnDl"))
        ul_text, ul_ok = _cmp(_nz(match["EARFCN_UL"]) if match else "", c.get("earfcnUl"))
        pwr_text, pwr_ok = _cmp(_nz(match["Pwr"]) if match else "", c.get("configuredOutputPower"))
        tx_text, tx_ok = _cmp(_nz(match["TX"]) if match else "", c.get("noOfTxAntennas"))
        rx_text, rx_ok = _cmp(_nz(match["RX"]) if match else "", c.get("noOfRxAntennas"))
        rru_text, rru_ok = _cmp(_nz(match["Model"]) if match else "", c.get("RRU type"), is_rru=True)
        cellrange_text, cellrange_ok = _cmp(_nz(match["CellRange"]) if match else "", c.get("cellRange"))
        if match:
            dss_pre_bool = bool(match.get("DSS"))
            partner = dss_post_partner.get(str(cell_full).strip().upper())
            dss_post_bool = partner is not None
            post_label = f"Yes ({partner})" if partner else "No"
            dss_text = f"{'Yes' if dss_pre_bool else 'No'} | {post_label}"
            dss_ok = dss_pre_bool == dss_post_bool
        else:
            dss_text, dss_ok = "-", None  # no Pre match - nothing to compare (new cell)

        # Link (Single/Double): Pre side is RiLink row count (pe.extract_
        # cell_to_rilink_detail, same value the Pre checks tab shows); Post
        # side is CIQ RadioPort grouping (ciq_checks.apply_link_and_sharing,
        # same value the CIQ Checks tab shows). Two different, independently
        # confirmed signals - see this module's docstring for why they're
        # compared as-is rather than one being re-derived from the other.
        link_text, link_ok = _cmp(_nz(match["RiLink"]) if match else "", ciq_link_by_cell.get(cell_full))

        result.append({
            "node": c.get("Node") or final_pfx, "cell": cell_full,
            "sc": sc_text, "_sc_ok": sc_ok, "cellid": cellid_text, "_cellid_ok": cellid_ok,
            "tac": tac_text, "_tac_ok": tac_ok, "bw": bw_text, "_bw_ok": bw_ok,
            "dl": dl_text, "_dl_ok": dl_ok, "ul": ul_text, "_ul_ok": ul_ok,
            "power": pwr_text, "_power_ok": pwr_ok, "tx": tx_text, "_tx_ok": tx_ok,
            "rx": rx_text, "_rx_ok": rx_ok, "rru": rru_text, "_rru_ok": rru_ok,
            "cellrange": cellrange_text, "_cellrange_ok": cellrange_ok,
            "dss": dss_text, "_dss_ok": dss_ok,
            "link": link_text, "_link_ok": link_ok, "comment": comment, "row_type": row_type,
        })
    return result


def compare_nr_cell_level(node_logs_text, ciq_wb):
    """5G equivalent of compare_lte_cell_level(), cross-verified against
    compareNRCellLevel(). NR suffix matching in the HTML normalizes via
    normNR()/getNRSuffix(); this project's NR cell names use the same
    '<prefix>_<sector-suffix>' shape as LTE ones (confirmed against real
    logs earlier in this project), so _get_suffix()/_get_prefix() apply
    unchanged rather than needing a separate NR-specific normalizer.

    Only CIQ cells are shown (per instruction) - a Pre cell with no CIQ
    counterpart is no longer appended as a synthetic 'NR Cell Deleted' row.

    Node and the 'Sector moved' comment both use the PRIMARY (LTE-paired)
    node name, not the raw gNodeB-style prefix baked into the NR cell name
    itself - confirmed real case: FSNN090877_N005B_1's own prefix is
    'FSNN090877', but its actual primary node is 'FSL00877' (from Mixed
    Mode Info's gNBId mapping). Without this substitution both the Node
    column and 'NR Sector moved: X -> Y' showed the 5G-only identity on
    both sides, even though every other part of this project (Node Summary,
    LTE Sector Movement comments) uses the primary node name."""
    amos = _amos_nr_index(node_logs_text)
    ciq_rows = cer.sheet_rows_as_dicts(ciq_wb["5G Info"]) if "5G Info" in ciq_wb.sheetnames else []
    ciq_link_by_cell = _ciq_link_map(ciq_wb)

    # Primary (LTE-paired) node name per gNBId, from Mixed Mode Info - same
    # source ciq_checks._node_name_maps() reads. A gNBId with no Mixed Mode
    # Info entry (a pure 5G-only node, no LTE pairing) falls back to the raw
    # gNodeB-style prefix, since there is no primary name to substitute.
    gnb_to_primary = {}
    for m in cer.mixed_mode_rows(ciq_wb):
        gnb = str(m.get("gNBId") or "").strip()
        node = m.get("Node to be built as")
        if gnb and node:
            gnb_to_primary[gnb] = node

    result = []
    for c in ciq_rows:
        cell_full = c.get("NRCellDU") or ""
        raw_pfx, final_sfx = _get_prefix(cell_full), _get_suffix(cell_full)
        final_pfx = gnb_to_primary.get(str(c.get("gNBId") or "").strip(), raw_pfx)
        match = next((a for a in amos if _get_suffix(a["Cell"]) == final_sfx), None)
        if not match:
            comment, row_type = "Newly Adding NR Cell", "new"
        else:
            # match["Node"] is the Pre log's own dict key (already the
            # LTE-style primary name, e.g. 'FSL02877') - NOT re-derived from
            # the matched cell's own gNodeB-style prefix.
            pre_pfx = match.get("Node") or _get_prefix(match["Cell"])
            comment, row_type = ("No Sector Movement", "nochange") if pre_pfx == final_pfx \
                else (f"NR Sector moved: {pre_pfx} -> {final_pfx}", "change")

        cellid_text, cellid_ok = _cmp(_nz(match["CellID"]) if match else "", c.get("cellLocalId"))
        dl_text, dl_ok = _cmp(_nz(match["DL"]) if match else "", c.get("arfcnDL"))
        ul_text, ul_ok = _cmp(_nz(match["UL"]) if match else "", c.get("arfcnUL"))
        bwdl_text, bwdl_ok = _cmp(_nz(match["BW_DL"]) if match else "", c.get("bSChannelBwDL"))
        bwul_text, bwul_ok = _cmp(_nz(match["BW_UL"]) if match else "", c.get("bSChannelBwUL"))
        pwr_text, pwr_ok = _cmp(_nz(match["Pwr"]) if match else "", c.get("configuredMaxTxPower"))
        ssb_text, ssb_ok = _cmp(_nz(match["SSB"]) if match else "", c.get("ssbFrequency"))
        rru_text, rru_ok = _cmp(_nz(match["Model"]) if match else "", c.get("RRU Type") or c.get("RRU type"), is_rru=True)
        cellrange_text, cellrange_ok = _cmp(_nz(match["CellRange"]) if match else "", c.get("CellRange"))
        if match:
            dss_pre_bool = bool(match.get("DSS"))
            dss_partner_raw = str(c.get("DSS") or "").strip()
            dss_post_bool = dss_partner_raw.upper() not in ("", "NO")
            post_label = f"Yes ({dss_partner_raw})" if dss_post_bool else "No"
            dss_text = f"{'Yes' if dss_pre_bool else 'No'} | {post_label}"
            dss_ok = dss_pre_bool == dss_post_bool
        else:
            dss_text, dss_ok = "-", None  # no Pre match - nothing to compare (new cell)

        link_text, link_ok = _cmp(_nz(match["RiLink"]) if match else "", ciq_link_by_cell.get(cell_full))

        result.append({
            "node": final_pfx, "cell": cell_full,
            "cellid": cellid_text, "_cellid_ok": cellid_ok, "dl": dl_text, "_dl_ok": dl_ok,
            "ul": ul_text, "_ul_ok": ul_ok, "bw_dl": bwdl_text, "_bw_dl_ok": bwdl_ok,
            "bw_ul": bwul_text, "_bw_ul_ok": bwul_ok, "power": pwr_text, "_power_ok": pwr_ok,
            "ssb": ssb_text, "_ssb_ok": ssb_ok, "rru": rru_text, "_rru_ok": rru_ok,
            "cellrange": cellrange_text, "_cellrange_ok": cellrange_ok,
            "dss": dss_text, "_dss_ok": dss_ok,
            "link": link_text, "_link_ok": link_ok, "comment": comment, "row_type": row_type,
        })
    return result


def summarize_rows(rows):
    """New/Deleted/Moved/No Change counts for the badge row, matching the
    HTML's own New/Deleted/Moved/No Change pill counts."""
    return {
        "new": sum(1 for r in rows if r["row_type"] == "new"),
        "deleted": sum(1 for r in rows if r["row_type"] == "delete"),
        "moved": sum(1 for r in rows if r["row_type"] == "change"),
        "nochange": sum(1 for r in rows if r["row_type"] == "nochange"),
    }
