"""
Node-level checks — Rules #1, #2/12/14/17, #3/#31, #5/#15, #16, from the
Pre checks validation blueprint. Each check function returns a plain dict
row shaped for direct use in the PDF's node-level table section:

    {'rule': '#5/#15', 'node': 'SCL05020', 'status': 'MISMATCH'|'MATCH'|'INFO'|'SKIPPED',
     'pre': ..., 'ciq': ..., 'edp': ..., 'rfds': ..., 'note': str}

'status' values:
    MATCH     - all available sources agree
    MISMATCH  - sources disagree (this is what the report should highlight)
    INFO      - display-only row, no pass/fail (e.g. SW version table)
    SKIPPED   - check intentionally not run (e.g. new node with no Pre data)
"""
import re

import pre_extract as pe
import ciq_edp_reader as cer


def check_sw_version(node_id, log_text):
    """Rule #1 - display-only table row."""
    sw = pe.extract_sw_version(log_text)
    if not sw:
        return {'rule': '#1', 'node': node_id, 'status': 'INFO',
                'sw_version': 'NOT FOUND', 'sw_package': 'NOT FOUND', 'note': ''}
    return {'rule': '#1', 'node': node_id, 'status': 'INFO',
            'sw_version': sw['sw_version'], 'sw_package': sw['sw_package'], 'note': ''}


def check_identity(node_id, parsed, mm_row, has_pre_log):
    """Rule #2/12/14/17 (collapsed) - eNBId/gNBId/eNodeB Name/gNodeB Name,
    Pre vs CIQ Mixed Mode Info. Only runs for nodes actually present in the
    Pre kget-all input (pre-existing); new-build nodes are SKIPPED, since
    'presence in Pre kget-all' is the sole signal for pre-existing (per
    confirmed decision)."""
    if not has_pre_log:
        return {'rule': '#2/12/14/17', 'node': node_id, 'status': 'SKIPPED',
                'note': 'No Pre kget-all log for this node — treated as new build, not checked.'}
    if mm_row is None:
        return {'rule': '#2/12/14/17', 'node': node_id, 'status': 'MISMATCH',
                'note': 'Node present in Pre kget-all but not found in CIQ Mixed Mode Info at all.'}

    pre_id = pe.extract_identity(parsed)
    mismatches = []

    ciq_enb = str(mm_row.get('eNBId') or '').strip()
    ciq_enb_name = str(mm_row.get('eNodeB Name') or '').strip()
    ciq_gnb = str(mm_row.get('gNBId') or '').strip()
    ciq_gnb_name = str(mm_row.get('gNodeB Name') or '').strip()

    if pre_id['eNBId'] and ciq_enb and pre_id['eNBId'] != ciq_enb:
        mismatches.append(f"eNBId Pre={pre_id['eNBId']} vs CIQ={ciq_enb}")
    if pre_id['gNBId'] and ciq_gnb and pre_id['gNBId'] != ciq_gnb:
        mismatches.append(f"gNBId Pre={pre_id['gNBId']} vs CIQ={ciq_gnb}")

    status = 'MISMATCH' if mismatches else 'MATCH'
    return {'rule': '#2/12/14/17', 'node': node_id, 'status': status,
            'pre_eNBId': pre_id['eNBId'], 'ciq_eNBId': ciq_enb or None,
            'pre_gNBId': pre_id['gNBId'], 'ciq_gNBId': ciq_gnb or None,
            'ciq_eNodeB_Name': ciq_enb_name or None, 'ciq_gNodeB_Name': ciq_gnb_name or None,
            'note': '; '.join(mismatches) if mismatches else 'Identity confirmed.'}


def build_site_details(ciq_wb, rfds_pages=None):
    """Report-header site details. FA Code and USID come from the CIQ's
    '5G Info' tab, which carries them as proper named columns - far more
    reliable than scraping the RFDS 'Site Details' page, whose OCR is
    column-major-scrambled (the FA Code value lands several lines above its
    own header, so only a positional-anchor regex could find it there).

    Site ID / ATOLL name still come from RFDS when available, since the CIQ
    has no equivalent column - those two are matched off a header line that
    IS stable in the OCR output. Falls back to the CIQ's node name when no
    RFDS is provided."""
    out = {}
    fiveg_rows = cer.sheet_rows_as_dicts(ciq_wb['5G Info']) if '5G Info' in ciq_wb.sheetnames else []
    for r in fiveg_rows:
        if r.get('FA Code'):
            out['fa_code'] = str(r['FA Code']).strip()
        if r.get('USID'):
            out['usid'] = str(r['USID']).strip()
        if out.get('fa_code') and out.get('usid'):
            break

    if rfds_pages is not None:
        import rfds_extract as rf
        rfds_details = rf.extract_site_details(rfds_pages)
        for key in ('site_id', 'atoll_site_name'):
            if rfds_details.get(key):
                out[key] = rfds_details[key]
        # Cross-check: RFDS agrees with the CIQ on FA Code / USID? Disagreement
        # is worth surfacing rather than silently preferring one source.
        # rfds_fa_code/rfds_usid are kept as their OWN keys (not merged into
        # fa_code/usid) so a caller can compare CIQ vs RFDS explicitly —
        # confirmed real bug this fixes: rrnrbl_checklist._fa_code_status()
        # previously read site_details['fa_code'] expecting the RFDS value,
        # but that key is always CIQ-sourced (see loop above), so it was
        # comparing the CIQ FA Code against itself under an RFDS label and
        # never actually consulting RFDS at all.
        for key in ('fa_code', 'usid'):
            rv = rfds_details.get(key)
            if rv:
                out[f'rfds_{key}'] = rv
            if rv and out.get(key) and rv != out[key]:
                out.setdefault('conflicts', []).append(f'{key}: CIQ={out[key]} vs RFDS={rv}')

    if not out.get('atoll_site_name'):
        mm = cer.mixed_mode_rows(ciq_wb)
        if mm:
            out['atoll_site_name'] = str(mm[0].get('Node to be built as') or '').strip()
    return out


def _combined_node_label(primary, secondary, bbu_mode=None):
    if secondary and str(secondary).strip().upper() not in ('', 'N/A'):
        suffix = f"({bbu_mode})" if bbu_mode and str(bbu_mode).strip().upper() not in ('', 'N/A') else ''
        return f"{primary}(P)/{secondary}(S){suffix}"
    return f"{primary}(P)"


def check_primary_secondary(node_id, edp_rows, mm_row, rfds_pages=None, rfds_bytes=None):
    """Rule #3/#31 - Primary/Secondary agreement, CIQ vs EDP vs RFDS. Each
    column shows the combined 'Primary(P)/Secondary(S)(mode)' label per its
    own source, per the confirmed format (a plain role string isn't enough -
    the label itself is the comparable unit here)."""
    if mm_row is None:
        return {'rule': '#3/#31', 'node': node_id, 'status': 'MISMATCH',
                'ciq': None, 'edp': None, 'rfds': None, 'note': 'Node not found in CIQ Mixed Mode Info.'}

    primary = str(mm_row.get('Node to be built as') or '').strip()
    e_name, g_name = mm_row.get('eNodeB Name'), mm_row.get('gNodeB Name')
    secondary = g_name if str(primary).strip().upper() == str(e_name or '').strip().upper() else e_name
    bbu_mode = mm_row.get('BBU Mode')
    ciq_label = _combined_node_label(primary, secondary, bbu_mode)

    # EDP: primary = whichever of e_name/g_name has SIAD_PORT_FACING_BBU populated
    edp_role_map = cer.edp_primary_secondary(edp_rows, [n for n in (e_name, g_name) if n])
    edp_primary = next((n for n in (e_name, g_name) if n and edp_role_map.get(n) == 'PRIMARY'), None)
    edp_secondary = next((n for n in (e_name, g_name) if n and n != edp_primary and edp_role_map.get(n)), None)
    if edp_primary:
        edp_label = _combined_node_label(edp_primary, edp_secondary, bbu_mode)
    else:
        edp_label = 'NOT FOUND IN EDP'

    rfds_label = 'NOT CHECKED'
    if rfds_pages is not None:
        import rfds_extract as rf
        present = rf.check_nodes_present_together(rfds_pages, primary, secondary, rfds_bytes) if secondary else None
        if secondary:
            # present is True/False/None (inconclusive - every extraction
            # path missed but the table read real data, confirmed real
            # false-negative case in check_nodes_present_together's own
            # docstring). Only a confirmed False is a real RFDS miss;
            # None must not read as one.
            if present is True:
                rfds_label = ciq_label
            elif present is False:
                rfds_label = 'NOT FOUND IN RFDS'
            else:
                rfds_label = 'NOT CHECKED'
        else:
            rfds_label = ciq_label  # single-identity node - nothing to cross-confirm pairing on

    match = (edp_label == ciq_label) and (rfds_label in (ciq_label, 'NOT CHECKED'))
    status = 'MATCH' if match else 'MISMATCH'
    notes = []
    if edp_label != ciq_label:
        notes.append(f'EDP ({edp_label}) differs from CIQ ({ciq_label}).')
    if rfds_label not in (ciq_label, 'NOT CHECKED'):
        notes.append(f'RFDS ({rfds_label}) differs from CIQ ({ciq_label}).')

    return {'rule': '#3/#31', 'node': node_id, 'status': status,
            'ciq': ciq_label, 'edp': edp_label, 'rfds': rfds_label,
            'note': ' '.join(notes) if notes else 'Confirmed.'}


def check_board_type(node_id, mm_row, enb_row, ciq_wb, edp_rows, rfds_pages=None):
    """Rule #5/#15/#13 (merged, per confirmed format) - Board Type:
    CIQ (eNB/gNB Info DU type) vs EDP (NODE_MODEL, current/deployed
    hardware) vs RFDS (Non RF Inventory Model text). No Pre column - EDP
    plays that role here per confirmed decision."""
    ciq_du = None
    if enb_row is not None:
        ciq_du = str(enb_row.get('DU type') or enb_row.get('1st DU type') or '').strip() or None
    if not ciq_du and mm_row is not None and 'gNB Info' in ciq_wb.sheetnames:
        g_name = str(mm_row.get('gNodeB Name') or '').strip().upper()
        for r in cer.sheet_rows_as_dicts(ciq_wb['gNB Info']):
            if str(r.get('gNodeB Name', '')).strip().upper() == g_name:
                ciq_du = str(r.get('DU type') or '').strip() or None
                break

    edp_site_rows = cer.edp_rows_for_site(edp_rows, node_id)
    edp_model = None
    if edp_site_rows:
        node_model = str(edp_site_rows[0].get('NODE_MODEL', '')).strip()
        m = re.search(r'(\d{4,5})\s*$', node_model) or re.search(r'(\d{4,5})', node_model)
        edp_model = m.group(1) if m else (node_model or None)

    rfds_model_text = None
    rfds_agrees = None
    if rfds_pages is not None:
        import rfds_extract as rf
        rfds_model_text = rf.find_pages_by_heading(rfds_pages, 'Non RF Inventory Details (Final)')
        rfds_model_text = re.sub(r'\s+', '', rfds_model_text) if rfds_model_text else None
        if ciq_du and rfds_model_text:
            rfds_agrees = ciq_du in rfds_model_text

    notes = []
    if rfds_agrees is False:
        status = 'MISMATCH'
        notes.append(f'CIQ DU type {ciq_du} not found in RFDS — CIQ and RFDS disagree.')
    elif not ciq_du:
        status = 'MISMATCH'
        notes.append('CIQ DU type not found.')
    elif edp_model and edp_model != ciq_du:
        # EDP (current/deployed) differs from CIQ target: the planned board
        # swap, expected scope of work - confirmed by RFDS agreeing with CIQ.
        status = 'EXPECTED' if rfds_agrees else 'MISMATCH'
        notes.append(f'Board swap: EDP={edp_model} → CIQ target={ciq_du}.'
                     + (' RFDS agrees with CIQ — planned change.' if rfds_agrees else ' RFDS not checked.'))
    else:
        status = 'MATCH'

    return {'rule': '#5/#15/#13', 'node': node_id, 'status': status,
            'ciq_du_type': ciq_du, 'edp_model': edp_model or 'NOT FOUND',
            'rfds_agrees': rfds_agrees,
            'note': ' '.join(notes) if notes else 'Confirmed.'}


def check_tac(node_id, log_text, enb_row, has_pre_log):
    """Rule #16 - LTE TAC, Pre kget-all vs CIQ eNB Info tac column.

    LTE-only: a node with no eNB Info row has no LTE eNodeB at all (e.g. a
    pure-5G/AAS node like an N077-only build) - there is no LTE TAC to
    check, so this must be SKIPPED, not flagged as 'not found'. 5G TAC is
    a separate field (nRTAC) checked by check_nr_tac, not this rule."""
    if not enb_row:
        return {'rule': '#16', 'node': node_id, 'status': 'SKIPPED',
                'note': 'No eNB Info row for this node — not an LTE node, LTE TAC does not apply.'}
    if not has_pre_log:
        return {'rule': '#16', 'node': node_id, 'status': 'SKIPPED',
                'note': 'No Pre kget-all log — new build (RMAP not available; not checked).'}

    pre_tacs = pe.extract_tac(log_text)
    distinct_pre = set(pre_tacs.values())
    ciq_tac = str(enb_row.get('tac')).strip() if enb_row and enb_row.get('tac') is not None else None

    notes = []
    status = 'MATCH'
    if len(distinct_pre) > 1:
        status = 'MISMATCH'
        notes.append(f'Pre-side TAC inconsistent across cells: {sorted(distinct_pre)}')
    pre_tac = next(iter(distinct_pre)) if len(distinct_pre) == 1 else None
    if pre_tac and ciq_tac and pre_tac != ciq_tac:
        status = 'MISMATCH'
        notes.append(f'Pre TAC={pre_tac} vs CIQ TAC={ciq_tac}')
    elif not ciq_tac:
        status = 'MISMATCH'
        notes.append('CIQ tac not found.')

    return {'rule': '#16', 'node': node_id, 'status': status,
            'pre_tac': pre_tac, 'ciq_tac': ciq_tac,
            'note': ' '.join(notes) if notes else 'TAC confirmed.'}


def check_xmu_rfds_vs_ciq(node_id, enb_row, gnb_row, rfds_pages, rfds_bytes=None):
    """Rule #27 - XMU Validation vs RFDS: XMU presence must agree between
    CIQ (eNB/gNB Info '1st XMU'/'2nd XMU' = YES) and RFDS's Non RF Inventory
    Details (Final) page.

    Tries node-specific attribution first (rfds_extract.extract_xmu_by_node,
    genuine-PDF RFDS only, via pdfplumber table extraction) - falls back to
    the page-wide check when that's unavailable (zip-bundle/OCR format,
    where the page's scrambled ordering makes per-node attribution
    unreliable - see rfds_extract.py's module docstring)."""
    ciq_has_xmu = False
    for row in (enb_row, gnb_row):
        if row and (str(row.get('1st XMU', '')).strip().upper() == 'YES' or str(row.get('2nd XMU', '')).strip().upper() == 'YES'):
            ciq_has_xmu = True

    if rfds_pages is None:
        return {'rule': '#27', 'node': node_id, 'status': 'SKIPPED', 'ciq_xmu': ciq_has_xmu,
                'note': 'No RFDS provided.'}

    import rfds_extract as rf

    if rfds_bytes is not None:
        by_node = rf.extract_xmu_by_node(rfds_bytes)
        if by_node is not None:
            if node_id in by_node:
                rfds_has_xmu = by_node[node_id]
                match = ciq_has_xmu == rfds_has_xmu
                note = 'Confirmed (node-specific).' if match else \
                    f'CIQ XMU present={ciq_has_xmu}, RFDS XMU present={rfds_has_xmu} (node-specific).'
                return {'rule': '#27', 'node': node_id, 'status': 'MATCH' if match else 'MISMATCH',
                        'ciq_xmu': ciq_has_xmu, 'rfds_xmu': rfds_has_xmu, 'note': note}
            # Node parsed successfully but isn't listed in the RFDS TABLE at
            # all. Does NOT fall through to a page-wide 'XMU' search here:
            # another node's XMU on the same page would be reported as this
            # node's, which is exactly backwards (confirmed on a real site -
            # OKL00082 is absent from the RFDS entirely, yet inherited
            # OKTN000082's XMU and was flagged as a mismatch against a
            # correct CIQ). _xmu_present_via_text avoids that by anchoring
            # to THIS node's own cell mentions first and returning None
            # (not False) when even those aren't found, so an absent node
            # still falls through to SKIPPED below rather than borrowing a
            # neighbor's answer.
            text_result = rf._xmu_present_via_text(rfds_pages, node_id)
            if text_result is not None:
                match = ciq_has_xmu == text_result
                note = 'Confirmed (text fallback — table extraction missed this node\'s row).' if match else \
                    f'CIQ XMU present={ciq_has_xmu}, RFDS XMU present={text_result} (text fallback).'
                return {'rule': '#27', 'node': node_id, 'status': 'MATCH' if match else 'MISMATCH',
                        'ciq_xmu': ciq_has_xmu, 'rfds_xmu': text_result, 'note': note}
            return {'rule': '#27', 'node': node_id, 'status': 'SKIPPED',
                    'ciq_xmu': ciq_has_xmu, 'rfds_xmu': None,
                    'note': 'Node not listed in RFDS Non RF Inventory — nothing to compare.'}

    text = rf.find_pages_by_heading(rfds_pages, 'Non RF Inventory Details (Final)')
    collapsed = re.sub(r'\s+', '', text) if text else ''
    rfds_has_xmu = 'XMU' in collapsed

    match = ciq_has_xmu == rfds_has_xmu
    note = 'Confirmed (page-wide check, not node-specific — see rule note).' if match else \
        f'CIQ XMU present={ciq_has_xmu}, RFDS XMU mentioned={rfds_has_xmu} (page-wide check, not node-specific).'
    return {'rule': '#27', 'node': node_id, 'status': 'MATCH' if match else 'MISMATCH',
            'ciq_xmu': ciq_has_xmu, 'rfds_xmu': rfds_has_xmu, 'note': note}


def run_node_checks(node_id, log_text, ciq_wb, edp_rows, rfds_pages=None, rfds_bytes=None):
    """Runs all node-level checks for one node and returns a list of result
    rows, ready to feed into the PDF's node-level section."""
    import log_parser as lp
    parsed = lp.parse_log(log_text) if log_text else []
    has_pre_log = bool(log_text)

    mm_rows = cer.mixed_mode_rows(ciq_wb)
    enb_rows = cer.enb_info_rows(ciq_wb)
    mm_row = cer.find_mm_row(mm_rows, node_id)
    enb_row = cer.find_enb_row(enb_rows, node_id)

    results = [check_sw_version(node_id, log_text) if has_pre_log else
               {'rule': '#1', 'node': node_id, 'status': 'SKIPPED', 'note': 'No Pre kget-all log.'}]
    results.append(check_identity(node_id, parsed, mm_row, has_pre_log))
    results.append(check_primary_secondary(node_id, edp_rows, mm_row, rfds_pages, rfds_bytes))
    results.append(check_board_type(node_id, mm_row, enb_row, ciq_wb, edp_rows, rfds_pages))

    gnb_row = None
    if mm_row is not None and 'gNB Info' in ciq_wb.sheetnames:
        g_name = str(mm_row.get('gNodeB Name') or '').strip().upper()
        if g_name:
            for r in cer.sheet_rows_as_dicts(ciq_wb['gNB Info']):
                if str(r.get('gNodeB Name', '')).strip().upper() == g_name:
                    gnb_row = r
                    break
    results.append(check_xmu_rfds_vs_ciq(node_id, enb_row, gnb_row, rfds_pages, rfds_bytes))
    results.append(check_tac(node_id, log_text, enb_row, has_pre_log))
    return results


def check_ptp_matrix(node_id, log_text, edp_rows, is_new_node=False):
    """Blueprint #30 — the A-G PTP decision matrix:

        A) Pre PTP Enabled   + EDP no PTP  -> PTP should be created
        B) Pre PTP Enabled   + EDP has PTP -> PTP to be created
        C) Pre PTP Disabled  + EDP no PTP  -> PTP should not be created
        D) Pre PTP Disabled  + EDP has PTP -> PTP should be created
        E) Pre No PTP        + EDP no PTP  -> No PTP Needed
        F) New Node          + EDP no PTP  -> No PTP Needed
        G) New Node          + EDP has PTP -> PTP to be created

    Pre state comes from amos_view.ptp_status() (Enabled/Disabled/Not
    Present), EDP state from SIAD_PTP_VLAN_ID. run_validation.py previously
    listed this in unavailable_notes as 'no PTP signal found in Pre kget-all
    logs' — that was wrong; the Transport=1,Ptp=1 MO carries it and was
    confirmed against real logs (HXL00147/HXL04147 Enabled, HXIN090147F
    genuinely absent)."""
    import amos_view as av

    edp_rec = None
    for r in (edp_rows or []):
        if str(r.get('SITE_NAME') or '').strip().upper() == str(node_id).strip().upper():
            edp_rec = r
            break
    edp_has_ptp = bool(edp_rec and str(edp_rec.get('SIAD_PTP_VLAN_ID') or '').strip())

    if is_new_node or not log_text:
        pre_state = 'New Node'
    else:
        pre_state = av.ptp_status(log_text)  # Enabled / Disabled / Not Present

    if pre_state == 'New Node':
        case, verdict = ('G', 'PTP to be created') if edp_has_ptp else ('F', 'No PTP Needed')
    elif pre_state == 'Enabled':
        case, verdict = ('B', 'PTP to be created') if edp_has_ptp else ('A', 'PTP should be created')
    elif pre_state == 'Disabled':
        case, verdict = ('D', 'PTP should be created') if edp_has_ptp else ('C', 'PTP should not be created')
    else:  # Not Present
        case, verdict = ('G', 'PTP to be created') if edp_has_ptp else ('E', 'No PTP Needed')

    status = 'INFO' if verdict in ('No PTP Needed', 'PTP should not be created') else 'MISMATCH'
    return [{'rule': '#30', 'node': node_id, 'cell': '-', 'status': status,
             'ptp': verdict,
             'note': f'Case {case}: Pre PTP = {pre_state}; EDP PTP = '
                     f'{"present" if edp_has_ptp else "absent"} -> {verdict}'}]
