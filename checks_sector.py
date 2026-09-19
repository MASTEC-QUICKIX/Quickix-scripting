"""
Sector-level checks - Rules #4, #7/#8, #9, #10, #11/#26/#27, #19, #29 from the
Pre checks validation blueprint. Rules #6, #18, #25 need RFDS (Non RF
Inventory / RF Inventory data) which isn't wired in yet - they're stubbed
here with the same optional-parameter pattern used in checks_node.py.

Each check function returns a list of result dicts (one row per affected
cell/sector), same status vocabulary as checks_node.py: MATCH / MISMATCH /
INFO / SKIPPED.
"""
import re

import pre_extract as pe
import ciq_edp_reader as cer
from band_labels import band_label, is_5g_cell, is_mmwave_cell, is_cband_cell, is_dod_cell


def _rows(ciq_wb, sheet_name):
    return cer.sheet_rows_as_dicts(ciq_wb[sheet_name]) if sheet_name in ciq_wb.sheetnames else []


def check_radio_type(node_id, log_text, ciq_wb, rfds_pages, e_name, g_name, node_logs=None, moved_map=None):
    """RFDS vs CIQ only (confirmed scope — this function's only consumer is
    the checklist's 'RFDS Checks' section, which is RFDS-vs-CIQ, not
    Pre-vs-CIQ). Pre is still resolved and returned in each result's 'pre'
    field for reference, but does NOT affect status/note — an earlier
    version folded a Pre-vs-CIQ 'radio swap' comparison into this same
    match/mismatch, which doesn't belong under an RFDS-scoped check.

    Pre is resolved via the full MO chain (confirmed):
        SectorCarrier -> fdd (EUtranCellFDD/NRCellDU)
        SectorCarrier -> AntennaUnitGroup,RfBranch (rfBranchRxRef/TxRef)
        AntennaUnitGroup,RfBranch -> FieldReplaceableUnit=RRU-N
        FieldReplaceableUnit=RRU-N -> productName
    (pre_extract.extract_cell_to_radio). An earlier version stopped at the
    SectorEquipmentFunction number, showing '(SEF ...)' instead of an actual
    radio model - the real link exists via RfBranch, not SEF."""
    results = []
    cell_details = {}
    if rfds_pages is not None:
        import rfds_extract as rf
        cell_details = rf.extract_cell_details(rfds_pages)
    cell_to_radio = pe.extract_cell_to_radio(log_text) if log_text else {}
    if node_logs and moved_map:
        cell_to_radio = pe.merge_moved_in_pre(cell_to_radio, node_logs, moved_map, pe.extract_cell_to_radio)

    def _pre_for(cell):
        product = cell_to_radio.get(cell)
        return pe._short_radio_name(product) if product else 'NOT AVAILABLE'

    for row in _rows(ciq_wb, 'eUtran Parameters'):
        cell = row.get('EutranCellFDDId')
        if not (cell and e_name and str(cell).startswith(e_name)):
            continue
        # A cell genuinely absent from RFDS has nothing to compare its RRU
        # type against — check_cells_vs_rfds (#6/#18) already flags the
        # absence itself. Skipping it here instead of comparing against
        # the literal string 'NOT FOUND'/'NOT CHECKED', which never
        # contains the RRU token and so always failed, producing a false
        # MISMATCH for every such cell.
        if rfds_pages is not None and cell not in cell_details:
            continue
        ciq_rru = str(row.get('RRU type', '')).strip()
        rfds_rrh = cell_details.get(cell, {}).get('rrh', 'NOT CHECKED')
        pre_val = _pre_for(cell)
        rru_token = ciq_rru.split()[-1] if ciq_rru else ''
        match = rfds_pages is None or (bool(rru_token) and rru_token in rfds_rrh)
        label, sector = band_label(cell)
        results.append({'rule': '#6', 'node': node_id, 'cell': cell, 'pre': pre_val, 'label': label, 'sector': sector,
                         'ciq': ciq_rru, 'rfds': rfds_rrh, 'status': 'MATCH' if match else 'MISMATCH',
                         'note': 'Confirmed.' if match else 'RFDS does not confirm CIQ RRU type.'})

    for row in _rows(ciq_wb, '5G Info'):
        cell = row.get('NRCellDU')
        if not (cell and g_name and str(cell).startswith(g_name)):
            continue
        if rfds_pages is not None and cell not in cell_details:
            continue
        ciq_rru = str(row.get('RRU Type', '')).strip()
        rfds_rrh = cell_details.get(cell, {}).get('rrh', 'NOT CHECKED')
        pre_val = _pre_for(cell)
        rru_token = ciq_rru.split()[-1] if ciq_rru else ''
        match = rfds_pages is None or (bool(rru_token) and rru_token in rfds_rrh)
        label, sector = band_label(cell)
        results.append({'rule': '#6', 'node': node_id, 'cell': cell, 'pre': pre_val, 'label': label, 'sector': sector,
                         'ciq': ciq_rru, 'rfds': rfds_rrh, 'status': 'MATCH' if match else 'MISMATCH',
                         'note': 'Confirmed.' if match else 'RFDS does not confirm CIQ RRU type.'})
    return results


def check_radio_sharing_pairs(node_id, ciq_wb):
    """Blueprint section 13, rule #28 - flags cell pairs sharing the same
    physical radio (same SectorEquipmentFunction / RRU FieldReplaceableUnit)
    for a plain informational note, e.g. 'FCL05583_2A_1/FCL05583_2A_2 radios
    are shared'."""
    fiveg_rows = _rows(ciq_wb, '5G Info')
    by_fru = {}
    for row in fiveg_rows:
        cell = row.get('NRCellDU')
        fru = row.get('RRU FieldReplaceableUnit')
        if cell and fru:
            by_fru.setdefault(str(fru).strip(), []).append(cell)
    results = []
    seen = set()
    for fru, cells in by_fru.items():
        if len(cells) < 2:
            continue
        for i in range(len(cells)):
            for j in range(i + 1, len(cells)):
                pair = tuple(sorted((cells[i], cells[j])))
                if pair in seen:
                    continue
                seen.add(pair)
                results.append({'rule': '#28', 'node': node_id, 'cell': '/'.join(pair),
                                 'status': 'INFO', 'note': 'radios are shared'})
    return results


def _extract_sector_config(log_text):
    """LTE cell -> {sec_id, tx, rx, power} from the single
    '^EUtranCell.DD|Sector ^earfcn|...' hget block - see
    check_sector_swap_config's docstring for the exact command/table
    structure. Returns {} if the command isn't present in this log."""
    if not log_text:
        return {}
    import log_parser as lp
    block = lp.get_command_block(log_text, '^EUtranCell.DD|Sector ^earfcn')
    if not block:
        return {}
    tables = lp.parse_tables(block)

    sc_power = {}
    for t in tables:
        if len(t['header']) > 1 and 'configuredMaxTxPower' in t['header'][1]:
            for row in t['rows']:
                mo = row.get('MO', '')
                if mo.startswith('SectorCarrier='):
                    vals = row.get('configuredMaxTxPower noOfRxAntennas noOfTxAntennas', '').split()
                    if len(vals) >= 3:
                        sc_power[mo.split('=', 1)[1]] = vals

    cell_to_sec_id = {}
    for m in re.finditer(r'^EUtranCellFDD=(\S+)\s.*?\[\d+\]\s*=\s*SectorCarrier=(\S+)', block, re.M):
        cell_to_sec_id[m.group(1)] = m.group(2)

    result = {}
    for cell, sec_id in cell_to_sec_id.items():
        pv = sc_power.get(sec_id)
        if pv:
            result[cell] = {'sec_id': sec_id, 'power': pv[0], 'tx': pv[2], 'rx': pv[1]}
    return result


def _extract_sector_config_5g(log_text):
    """NRSectorCarrier -> {power, tx, rx} from the same hget block, 5G side."""
    if not log_text:
        return {}
    import log_parser as lp
    block = lp.get_command_block(log_text, '^EUtranCell.DD|Sector ^earfcn')
    if not block:
        return {}
    result = {}
    for t in lp.parse_tables(block):
        if len(t['header']) > 1 and 'configuredMaxTxPower' in t['header'][1]:
            for row in t['rows']:
                mo = row.get('MO', '')
                if mo.startswith('NRSectorCarrier='):
                    cell = mo.split('=', 1)[1]
                    vals = row.get('configuredMaxTxPower noOfRxAntennas noOfTxAntennas', '').split()
                    if len(vals) >= 3:
                        result[cell] = {'power': vals[0], 'tx': vals[2], 'rx': vals[1]}
    return result


def check_sector_swap_config(node_id, log_text, ciq_wb, e_name, g_name=None, node_logs=None, moved_map=None):
    """Blueprint section 13, rules #21/#22/#32 - sector ID, TX/RX antenna
    count, and configured power, Pre vs CIQ (per cell).

    All three Pre values come from ONE command's output
    ('hget ^EUtranCell.DD|Sector ^earfcn|...|configuredMaxTxPower|
    noOfRxAntennas|noOfTxAntennas|sectorcarrierref') - confirmed against a
    real log, this single hget produces three tables: the LTE combo table
    (giving sectorCarrierRef, e.g. 'SectorCarrier=7_1' - the sec_id itself),
    and TWO 'configuredMaxTxPower|noOfRxAntennas|noOfTxAntennas' tables, one
    keyed by SectorCarrier=<sec_id> (LTE) and one by NRSectorCarrier=<cell>
    (5G). 'Link' (DATA1/DATA2 port designation) has no confirmed CIQ column
    and is NOT AVAILABLE rather than guessed.

    node_logs/moved_map: see check_rf_params_4g's docstring - a moved-in
    cell's real sec_id/TX-RX/Power lives on its SOURCE node's log.

    Standalone 5G (own radio, not co-located with LTE): TX/RX comes from
    'RBB Type' via parse_rbb_txrx() ('RBB44_1D' -> '4x4', confirmed real
    CIQ naming convention — the two digits right after 'RBB' ARE the
    TX/RX count; RBBAIR_* codes don't follow this pattern and correctly
    fall back to NOT FOUND rather than a guess), compared against
    fiveg_config's real Pre noOfTxAntennas/noOfRxAntennas reading. An
    earlier version extracted fiveg_config but never actually compared it
    here — only RILink Single/Double vs the RBB suffix was checked, so a
    genuine TX/RX mismatch on a standalone 5G radio went unflagged."""
    if not log_text:
        return []
    lte_config = _extract_sector_config(log_text)
    fiveg_config = _extract_sector_config_5g(log_text)
    if node_logs and moved_map:
        lte_config = pe.merge_moved_in_pre(lte_config, node_logs, moved_map, _extract_sector_config)
        fiveg_config = pe.merge_moved_in_pre(fiveg_config, node_logs, moved_map, _extract_sector_config_5g)

    results = []
    for row in _rows(ciq_wb, 'eUtran Parameters'):
        cell = row.get('EutranCellFDDId')
        if not (cell and e_name and str(cell).startswith(e_name)):
            continue
        cfg = lte_config.get(cell)
        pre_sec_id = cfg['sec_id'] if cfg else 'NOT AVAILABLE'
        pre_power = cfg['power'] if cfg else 'NOT AVAILABLE'
        pre_txrx = f"{cfg['tx']}x{cfg['rx']}" if cfg else 'NOT AVAILABLE'
        ciq_tx, ciq_rx = row.get('noOfTxAntennas'), row.get('noOfRxAntennas')
        ciq_txrx = f"{ciq_tx}x{ciq_rx}" if ciq_tx and ciq_rx else 'NOT FOUND'
        ciq_power = str(row.get('configuredOutputPower', '')).strip()
        ciq_sec_id = str(row.get('sectorId', '')).strip()
        mismatches = []
        if pre_sec_id != 'NOT AVAILABLE' and pre_sec_id != ciq_sec_id:
            mismatches.append(f'sec_id Pre={pre_sec_id} vs CIQ={ciq_sec_id}')
        if pre_txrx != 'NOT AVAILABLE' and pre_txrx != ciq_txrx:
            mismatches.append(f'TX/RX Pre={pre_txrx} vs CIQ={ciq_txrx}')
        if pre_power != 'NOT AVAILABLE' and ciq_power and pre_power != ciq_power:
            mismatches.append(f'Power Pre={pre_power} vs CIQ={ciq_power}')
        results.append({'rule': '#21/#22/#32', 'kind': 'lte', 'node': node_id, 'cell': cell,
                         'sec_id': ciq_sec_id, 'pre_sec_id': pre_sec_id,
                         'pre_txrx': pre_txrx, 'ciq_txrx': ciq_txrx,
                         'pre_power': pre_power, 'ciq_power': ciq_power,
                         'status': 'MISMATCH' if mismatches else 'MATCH',
                         'note': '; '.join(mismatches) if mismatches else 'Confirmed.'})

    if g_name:
        # 5G sectors sharing a radio with LTE (Co-Located Technology Cell)
        # are already covered by the LTE row above; only standalone 5G
        # sectors (own radio, not colocated) get their own row here.
        colo_lte_5g = set()
        for row in _rows(ciq_wb, 'eUtran Parameters'):
            for c in str(row.get('Co-Located Technology Cell', '')).split(','):
                if c.strip().startswith(g_name):
                    colo_lte_5g.add(c.strip())
        rilink = pe.extract_cell_to_rilink(log_text) if log_text else {}
        if node_logs and moved_map:
            rilink = pe.merge_moved_in_pre(rilink, node_logs, moved_map, pe.extract_cell_to_rilink)
        for row in _rows(ciq_wb, '5G Info'):
            cell = row.get('NRCellDU')
            if not (cell and str(cell).startswith(g_name)) or cell in colo_lte_5g:
                continue
            rbb = row.get('RBB Type')
            ciq_txrx = pe.parse_rbb_txrx(rbb)
            ciq_ri = pe.parse_rbb_link(rbb)
            pre_ri = rilink.get(cell, 'NA')
            cfg5g = fiveg_config.get(cell)
            pre_txrx = f"{cfg5g['tx']}x{cfg5g['rx']}" if cfg5g else 'NOT AVAILABLE'
            mismatches = []
            if ciq_txrx is None or ciq_ri is None:
                mismatches.append(f"RBB Type '{rbb}' does not match the expected RBB<TX><RX>_<link><letter> "
                                   f"pattern — cannot validate TX/RX or link count.")
            else:
                if pre_ri != 'NA' and pre_ri != ciq_ri:
                    mismatches.append(f'RILink Pre={pre_ri} vs CIQ={ciq_ri}')
                if pre_txrx != 'NOT AVAILABLE' and pre_txrx != ciq_txrx:
                    mismatches.append(f'TX/RX Pre={pre_txrx} vs CIQ={ciq_txrx} (RBB Type {rbb})')
            label, sector = band_label(cell)
            results.append({'rule': '#21/#22/#32', 'kind': '5g', 'node': node_id, 'cell': cell, 'label': label, 'sector': sector,
                             'sec_id': 'NA', 'pre_sec_id': 'NA',
                             'pre_txrx': pre_txrx, 'ciq_txrx': ciq_txrx or 'NOT FOUND',
                             'pre_power': 'NA', 'ciq_power': str(row.get('configuredMaxTxPower', '')).strip(),
                             'status': 'MISMATCH' if mismatches else 'MATCH',
                             'note': '; '.join(mismatches) if mismatches else 'RBB Type/RILink/TX-RX confirmed (standalone 5G radio).'})
    return results


def check_nbiot(node_id, log_text, ciq_wb=None):
    """Blueprint section 16 'NBIOT Cells check' (#4). nbIotCellName |
    NBIoT Cell ID [pre] | NBIoT Cell ID [post/CIQ] | Match. Matched by
    physicalLayerCellId, which both sides expose. Only triggers if NBIoT
    cells are present (per confirmed condition)."""
    pre_cells = pe.extract_nbiot_cells(__import__('log_parser').parse_log(log_text)) if log_text else []
    ciq_rows = _rows(ciq_wb, 'NBIoT Parameters') if ciq_wb is not None else []
    if not pre_cells and not ciq_rows:
        return [{'rule': '#4', 'node': node_id, 'cell': None, 'status': 'SKIPPED',
                  'note': 'No NBIoT cells present on this node - check does not trigger.'}]

    pre_by_pcid = {c.get('physicalLayerCellId'): c for c in pre_cells}
    results = []
    matched_pre = set()
    for row in ciq_rows:
        name = row.get('nbIotCellName')
        ciq_id = row.get('nbIotCellId')
        pcid = row.get('physicalLayerCellId')
        pre_c = pre_by_pcid.get(str(pcid)) if pcid is not None else None
        pre_id = pre_c.get('cellid') if pre_c else 'NA'
        if pre_c:
            matched_pre.add(pcid)
        match = pre_id in ('NA', None) or str(pre_id) == str(ciq_id)
        results.append({'rule': '#4', 'node': node_id, 'cell': name,
                         'pre_id': pre_id, 'ciq_id': ciq_id,
                         'status': 'MATCH' if match else 'MISMATCH',
                         'note': 'Confirmed.' if match else f'Pre={pre_id} vs CIQ={ciq_id}.'})
    # Confirmed scoping rule: every check is driven by the CIQ (the post
    # design). Cells that exist only in Pre are out of scope - they are not
    # what is being scripted - so no row is emitted for them. Pre is only
    # ever a comparison column against a CIQ cell, and reads 'NA' when the
    # CIQ cell is newly adding.
    return results


def check_nr_tac(node_id, log_text, ciq_wb, has_pre_log, has_amf, g_name=None, node_logs=None, moved_map=None):
    """Rule #7/#8 - NR TAC. Pre kget-all vs CIQ 5G Info nRTAC, per 5G cell.
    #8's NSA(0)/SA(7-digit) branch: cells present in NR_SA tab are expected
    7-digit (SA); others expected '0' (NSA). #7's warning: if AMF is present
    (has_amf, from 'st amf' in the Pre kget-all - caller determines this) and
    nRTAC is 7-digit in Pre, warn of SA configuration on this node.

    g_name: this node's gNodeB Name - cells are filtered to those belonging
    to it. Without this filter every node emits every 5G cell in the CIQ
    (confirmed: a 3-node site produced each cell three times, and compared
    other nodes' cells against THIS node's Pre data, producing false
    mismatches). A node with no gNodeB Name has no 5G identity at all, so
    it correctly yields no rows rather than falling through to every cell."""
    if not g_name:
        return []
    fiveg_rows = _rows(ciq_wb, '5G Info')
    nr_sa_rows = _rows(ciq_wb, 'NR_SA')
    sa_nodes = {str(r.get('Node Name', '')).strip().upper() for r in nr_sa_rows if r.get('Node Name')}

    pre_nr_tac = pe.extract_nr_tac(log_text) if (has_pre_log and log_text) else {}
    if node_logs and moved_map:
        pre_nr_tac = pe.merge_moved_in_pre(pre_nr_tac, node_logs, moved_map, pe.extract_nr_tac)

    results = []
    for row in fiveg_rows:
        cell = row.get('NRCellDU')
        if not cell:
            continue
        if not str(cell).startswith(g_name):
            continue
        ciq_nrtac = str(row.get('nRTAC', '')).strip()
        gnb_name = str(row.get('gNB Name', '')).strip().upper()
        expects_sa = gnb_name in sa_nodes
        notes = []
        status = 'MATCH'

        # NR_SA tab empty means no SA conversion is in scope at all - in that
        # case nRTAC is whatever the design says and there is no 0-vs-7-digit
        # expectation to enforce (confirmed: enforcing it on a site with an
        # empty NR_SA tab flagged every legitimately-populated nRTAC as a
        # mismatch).
        if sa_nodes:
            if expects_sa and not (ciq_nrtac.isdigit() and len(ciq_nrtac) == 7):
                status = 'MISMATCH'
                notes.append(f"Node in NR_SA (SA conversion) but nRTAC='{ciq_nrtac}' is not 7-digit.")
            elif not expects_sa and ciq_nrtac not in ('0', ''):
                status = 'MISMATCH'
                notes.append(f"Not an NR_SA node (expect NSA, nRTAC=0) but nRTAC='{ciq_nrtac}'.")

        pre_val = pre_nr_tac.get(cell)
        if pre_val is not None and pre_val != ciq_nrtac:
            status = 'MISMATCH'
            notes.append(f'Pre nRTAC={pre_val} vs CIQ nRTAC={ciq_nrtac}.')
        if has_amf and pre_val and pre_val.isdigit() and len(pre_val) == 7:
            notes.append(f'WARNING: SA configuration on : {node_id}')

        results.append({'rule': '#7/#8', 'node': node_id, 'cell': cell, 'status': status,
                         'pre_nrtac': pre_val, 'ciq_nrtac': ciq_nrtac, 'expects_sa': expects_sa,
                         'note': ' '.join(notes) if notes else 'NR TAC confirmed.'})
    return results


def check_mmwave_rach(node_id, ciq_wb):
    """Rule #10 - mmWave (N260) rachRootSequence must be strictly less
    than 137 (137 itself fails). CIQ-only check, no Pre comparison (per
    blueprint)."""
    fiveg_rows = _rows(ciq_wb, '5G Info')
    results = []
    for row in fiveg_rows:
        cell = row.get('NRCellDU')
        if not cell or not is_mmwave_cell(cell):
            continue
        rach = row.get('rachRootSequence')
        try:
            rach_val = int(str(rach).strip())
        except (TypeError, ValueError):
            results.append({'rule': '#10', 'node': node_id, 'cell': cell, 'status': 'MISMATCH',
                             'rachRootSequence': rach, 'note': 'rachRootSequence not numeric.'})
            continue
        exceeded = rach_val >= 137
        results.append({'rule': '#10', 'node': node_id, 'cell': cell,
                         'status': 'MISMATCH' if exceeded else 'MATCH',
                         'rachRootSequence': rach_val,
                         'note': f'rachRootSequence must be < 137 for the MMWave sector {cell} (found {rach_val}).' if exceeded else 'Within limit.'})
    return results


def check_sef_fru(node_id, ciq_wb):
    """Rule #9 - SEF/FRU sharing vs uniqueness, radio-type dependent:
      - 6472 (sharing radio) -> SEF must be the SAME (shared) across
        whichever of CBAND/DOD/DOD_BWE sectors use that radio, grouped by
        sector letter (a 6472 serves one sector across multiple bands,
        never across different sectors) - a DIFFERING SEF within one
        sector's 6472 cells is the bug this catches, not a passing case.
        An earlier version had this backwards (always INFO, never
        flagged a differing SEF).
      - 6419/6449 (single-band radios) -> BOTH SectorEquipmentFunction
        AND RRU FieldReplaceableUnit must be unique per CBAND|DOD cell.
        An earlier version only checked SEF, never FRU, missing a real
        FRU-only duplicate.
      - 8863/4461/4467 -> RRU FieldReplaceableUnit must start with 'RRU'."""
    fiveg_rows = _rows(ciq_wb, '5G Info')
    cband_dod_rows = [r for r in fiveg_rows if r.get('NRCellDU') and
                       (is_cband_cell(r['NRCellDU']) or is_dod_cell(r['NRCellDU']))]

    # 6472: SEF is expected to be the SAME across whichever of CBAND/DOD/
    # DOD_BWE sectors share that physical radio — grouped by SECTOR letter
    # (Alpha/Beta/Gamma...) since a 6472 radio serves one sector across
    # multiple bands, not across different sectors. A differing SEF within
    # the same sector's 6472 cells is the actual bug this rule catches.
    sef_by_sector_6472 = {}
    for row in cband_dod_rows:
        if '6472' in str(row.get('RRU Type', '')):
            _, sector = band_label(row.get('NRCellDU'))
            sef_by_sector_6472.setdefault(sector, set()).add(row.get('SectorEquipmentFunction'))

    results = []
    # group by RRU Type to determine which radio family governs each cell
    for row in cband_dod_rows:
        cell = row.get('NRCellDU')
        rru_type = str(row.get('RRU Type', '')).strip()
        sef = row.get('SectorEquipmentFunction')
        fru = row.get('RRU FieldReplaceableUnit')

        if '6472' in rru_type:
            _, sector = band_label(cell)
            shared = len(sef_by_sector_6472.get(sector, set())) <= 1
            results.append({'rule': '#9', 'node': node_id, 'cell': cell,
                             'status': 'MATCH' if shared else 'MISMATCH',
                             'rru_type': rru_type, 'sef': sef, 'fru': fru,
                             'note': '6472 radio - SEF correctly shared across CBAND/DOD/DOD_BWE on this sector.'
                                     if shared else
                                     f"6472 radio - SEF should be shared across CBAND/DOD/DOD_BWE on this sector but differs (SEF='{sef}')."})
        elif any(m in rru_type for m in ('6419', '6449')):
            dup_sef = any(other is not row and other.get('SectorEquipmentFunction') == sef
                          for other in cband_dod_rows if is_cband_cell(other.get('NRCellDU')) or is_dod_cell(other.get('NRCellDU')))
            dup_fru = any(other is not row and other.get('RRU FieldReplaceableUnit') == fru
                          for other in cband_dod_rows if is_cband_cell(other.get('NRCellDU')) or is_dod_cell(other.get('NRCellDU')))
            bad_fields = [f for f, dup in (('SEF', dup_sef), ('RRU FieldReplaceableUnit', dup_fru)) if dup]
            results.append({'rule': '#9', 'node': node_id, 'cell': cell,
                             'status': 'MISMATCH' if bad_fields else 'MATCH',
                             'rru_type': rru_type, 'sef': sef, 'fru': fru,
                             'note': f"{'/'.join(bad_fields)} must be unique for single-band radio but is shared."
                                     if bad_fields else 'Unique, as required.'})
        elif any(m in rru_type for m in ('8863', '4461', '4467')):
            ok = str(fru or '').strip().upper().startswith('RRU')
            results.append({'rule': '#9', 'node': node_id, 'cell': cell,
                             'status': 'MATCH' if ok else 'MISMATCH',
                             'rru_type': rru_type, 'sef': sef, 'fru': fru,
                             'note': 'OK.' if ok else f"RRU FieldReplaceableUnit '{fru}' does not start with 'RRU'."})
    return results


def check_port_uniqueness(node_id, ciq_wb):
    """Rule #11/#26/#27 - RI Port uniqueness within same BBU/XMU + colocated
    group (5G Info 'BB/XMU' + 'Port 1'-'Port 4'), plus XMU port exclusivity
    (if XMU is present, its ports must not be reused by any other sector on
    that node - per confirmed addition).

    Exemption: cells sharing a 6472 radio (per rule #9's confirmed
    sharing-is-expected logic) legitimately share the same physical ports too
    - e.g. a CBAND+DOD pair on one 6472 radio. Without this exemption, every
    such pair would false-positive here even though check_sef_fru() already
    confirms the sharing is correct. Cells are grouped into a shared-radio set
    by (SectorEquipmentFunction, RRU Type) when RRU Type contains 6472."""
    fiveg_rows = _rows(ciq_wb, '5G Info')
    port_cols = ['Port 1', 'Port 2', 'Port 3', 'Port 4']

    # XMU ports must be scoped PER NODE - ports on different physical nodes
    # cannot conflict. Which tab declares them depends on the node type
    # (confirmed): MMBB nodes map to both eNB Info (by eNodeB Name) and
    # gNB Info (by gNodeB Name); a 4G-standalone node only has eNB Info; a
    # 5G-only node only has gNB Info. Collecting them site-wide flagged a
    # sector on one node against another node's XMU (confirmed false
    # positive: HXL00147's XMU D/E/F vs HXIN010147's cells, which belong to
    # HXL04147).
    mm_rows = _rows(ciq_wb, 'Mixed Mode Info')
    enb_by_name = {str(r.get('eNodeB Name', '')).strip(): r for r in _rows(ciq_wb, 'eNB Info')
                   if str(r.get('eNodeB Name', '')).strip()}
    gnb_by_name = {str(r.get('gNodeB Name', '')).strip(): r for r in _rows(ciq_wb, 'gNB Info')
                   if str(r.get('gNodeB Name', '')).strip()}

    def _ports_from(row):
        found = set()
        if not row:
            return found
        for which in ('1st', '2nd'):
            if str(row.get(f'{which} XMU', '')).strip().upper() != 'YES':
                continue
            for i in (1, 2, 3):
                v = row.get(f'{which} XMU Port {i}')
                if v is not None and str(v).strip().upper() not in ('', 'N/A', 'NA', 'NOT USED'):
                    found.add(str(v).strip())
        return found

    # cell-name prefix -> that node's XMU ports. On a TMBB/MMBB node eNB
    # Info's XMU and gNB Info's XMU describe the SAME physical XMU unit
    # from the 4G/5G side respectively - identical ports in both is the
    # expected shape, not a clash; only a genuine disagreement (both
    # declared, different ports) is a real problem.
    xmu_ports_by_prefix = {}
    xmu_disagreement = {}
    for mm in mm_rows:
        e_name = str(mm.get('eNodeB Name') or '').strip()
        g_name = str(mm.get('gNodeB Name') or '').strip()
        enb_ports = _ports_from(enb_by_name.get(e_name))
        gnb_ports = _ports_from(gnb_by_name.get(g_name))
        node_ports = enb_ports | gnb_ports
        if enb_ports and gnb_ports and enb_ports != gnb_ports:
            xmu_disagreement[e_name or g_name] = (enb_ports, gnb_ports)
        if not node_ports:
            continue
        for prefix in (e_name, g_name):
            if prefix:
                xmu_ports_by_prefix[prefix] = xmu_ports_by_prefix.get(prefix, set()) | node_ports

    def _xmu_ports_for_cell(cell):
        for prefix, ports in xmu_ports_by_prefix.items():
            if cell and str(cell).startswith(prefix):
                return ports
        return set()

    xmu_ports = set().union(*xmu_ports_by_prefix.values()) if xmu_ports_by_prefix else set()

    # Node identity for scoping: SAME (node, port) is a real clash only when
    # both cells sit on the SAME node. Two or three different nodes can
    # legitimately reuse the same board TYPE and the same port letter -
    # board type/number alone must never be treated as a global namespace.
    # Reuse the Mixed Mode Info prefix map already built above (eNodeB
    # Name / gNodeB Name are the node's own identity, covering 4G+5G cells
    # of one physical node the same way _xmu_ports_for_cell() does).
    node_prefixes = sorted({p for p in xmu_ports_by_prefix} |
                            {str(mm.get('eNodeB Name') or '').strip() for mm in mm_rows} |
                            {str(mm.get('gNodeB Name') or '').strip() for mm in mm_rows},
                            key=len, reverse=True)
    node_prefixes = [p for p in node_prefixes if p]

    def _node_key_for_cell(cell):
        for prefix in node_prefixes:
            if cell and str(cell).startswith(prefix):
                return prefix
        return str(cell)  # unmatched cell -> its own key, never collides with anything else

    def _colo_set(row):
        raw = str(row.get('Co-Located Technology Cell') or '').strip().upper()
        return {c.strip() for c in raw.split(',') if c.strip() and c.strip() not in ('NA', 'N/A', 'NOT USED')}

    # Board/port is the physical RI-port namespace, but ONLY within one
    # node - a TMBB/MMBB node's 4G and 5G cells sit on the SAME DU/BBU
    # hardware (confirmed elsewhere - check_gnb_du_type_vs_5g_bbu_type
    # relies on the same board id agreeing across eNB Info 'DU type', gNB
    # Info 'DU type', and 5G Info 'BBU Type'). Each entry below is
    # (cell, board, [that cell's own port values], declared Co-Located set).
    cell_ports = [(row.get('NRCellDU'), str(row.get('BB/XMU', '')).strip(),
                   [str(row.get(pc)).strip() for pc in port_cols
                    if row.get(pc) is not None and str(row.get(pc)).strip()],
                   _colo_set(row))
                  for row in fiveg_rows]
    cell_ports += [(row.get('EutranCellFDDId'), str(row.get('DUS / XMU', '')).strip(),
                    [str(row.get(pc)).strip() for pc in ('DUS / XMU Port', 'DUS / XMU Port Expansion')
                     if row.get(pc) is not None and str(row.get(pc)).strip().upper() not in ('', 'N/A', 'NOT USED')],
                    _colo_set(row))
                   for row in _rows(ciq_wb, 'eUtran Parameters')]

    shared_radio_group = {}  # cell -> group key, for 6472-sharing cells only
    for row in fiveg_rows:
        rru_type = str(row.get('RRU Type', '')).strip()
        if '6472' in rru_type:
            cell = row.get('NRCellDU')
            sef = row.get('SectorEquipmentFunction')
            if cell and sef:
                shared_radio_group[cell] = sef

    # Primary key is (node, port) - NOT (board, port). Board is still
    # recorded per cell so the message/verdict can tell a same-board clash
    # (genuine physical port collision) apart from a cross-board reuse on
    # the same node (only OK if mutually declared Co-Located, or exempt
    # under the 6472 sharing-radio rule).
    usage = {}
    for cell, bbu, ports, colo in cell_ports:
        is_xmu = 'XMU' in bbu.upper()
        node_key = _node_key_for_cell(cell)
        for val in ports:
            key = (node_key, val)
            usage.setdefault(key, []).append((cell, bbu, colo))
            if is_xmu:
                xmu_ports.add(val)

    # Blueprint's RI port table has TWO port columns: LTE cells use one RI
    # port (second shows NA), 5G cells can use two. Collect each cell's full
    # port list so the second can be rendered.
    ports_by_cell = {cell: ports for cell, bbu, ports, colo in cell_ports if cell}

    # XMU ports are keyed separately in `usage` (by their own BB/XMU value), so
    # a sector reusing an XMU port never collides there and its own row would
    # read Unique. Collect the conflicting (cell, port) pairs up front so the
    # sector's real row can be marked - previously this only appended an extra
    # MISMATCH row afterwards, leaving the sector's original row saying Unique
    # and the same cell appearing twice with contradictory verdicts. Runs
    # over 4G cells too now, same reason as the `usage` map above - a 4G
    # cell landing on an XMU's declared port is just as real a conflict.
    xmu_conflicts = {}
    for cell, bbu, ports, colo in cell_ports:
        if 'XMU' in bbu.upper():
            continue
        own_xmu_ports = _xmu_ports_for_cell(cell)
        for val in ports:
            if val in own_xmu_ports:
                xmu_conflicts[(cell, val)] = bbu

    results = []
    for (node_key, port), entries in usage.items():
        cells = [c for c, bbu, colo in entries]
        boards = {bbu for c, bbu, colo in entries}
        same_board = len(boards) == 1
        # 6472 sharing-radio exemption only ever applies within one board.
        groups = {shared_radio_group.get(c) for c in cells}
        radio_exempt = len(cells) > 1 and len(groups) == 1 and None not in groups
        # Cross-board reuse on the same node is only OK if EVERY cell in the
        # group declares every other cell in its own Co-Located field -
        # a one-sided or missing declaration is not a confirmed pairing.
        colo_exempt = (not same_board and len(cells) > 1 and
                       all(all(other == c or other.upper() in colo for other in cells)
                           for c, bbu, colo in entries))
        exempt = radio_exempt or colo_exempt
        for cell, bbu, colo in entries:
            _pl = [p for p in ports_by_cell.get(cell, []) if p != port]
            xmu_clash = (cell, port) in xmu_conflicts
            if len(cells) > 1 and not exempt and same_board:
                status = 'MISMATCH'
                note = f"Port {port} on {bbu} shared by multiple sectors: {cells}"
            elif len(cells) > 1 and not exempt and not same_board:
                status = 'MISMATCH'
                note = (f"Port {port} reused across different boards on this node "
                        f"({', '.join(sorted(boards))}) without a mutual Co-Located "
                        f"Technology Cell declaration: {cells}")
            elif xmu_clash:
                status = 'MISMATCH'
                note = f"Port {port} is assigned to an XMU on this node but reused by this sector."
            elif radio_exempt:
                status, note = 'MATCH', 'Port shared as expected (6472 sharing radio, per rule #9).'
            elif colo_exempt:
                status, note = 'MATCH', f'Port shared across boards ({", ".join(sorted(boards))}) as declared in Co-Located Technology Cell.'
            else:
                status, note = 'MATCH', 'Port unique.'
            results.append({'rule': '#11/#26/#27', 'node': node_id, 'cell': cell, 'status': status,
                             'bbu': bbu, 'port': port, 'port2': _pl[0] if _pl else None, 'note': note})

    for node_prefix, (enb_p, gnb_p) in xmu_disagreement.items():
        results.append({'rule': '#11/#26/#27', 'node': node_id, 'cell': node_prefix, 'status': 'MISMATCH',
                         'bbu': 'XMU', 'port': ', '.join(sorted(enb_p ^ gnb_p)), 'port2': None,
                         'note': f"eNB Info's XMU ports ({sorted(enb_p)}) and gNB Info's XMU ports ({sorted(gnb_p)}) on {node_prefix} should describe the same physical XMU but disagree."})

    return results


def check_rf_params(node_id, log_text, ciq_wb, has_pre_log, retuned_cells=None):
    """Rule #19 - LTE/5G RF params, Pre vs CIQ. One row per (cell, field) so
    each parameter renders as its own comparable table row: cell | field |
    Pre value | CIQ value | Match.

    retuned_cells: optional set of cell names the SOW analysis already
    identified as deliberately retuned (from Sector Del_Movement). Per
    confirmed direction these stay HIGHLIGHTED (red) even though the change
    is expected - a frequency change is always worth an engineer's eyes -
    but the note names it as a planned retune so it reads as "verify this"
    rather than "something is broken". Differences on cells NOT in that set
    carry no SOW explanation at all and are genuine unexplained faults."""
    import log_parser as lp
    retuned_cells = retuned_cells or set()
    results = []

    def _status_for(cell, match):
        # Both planned retunes and unexplained differences highlight red -
        # the note distinguishes them.
        return 'MATCH' if match else 'MISMATCH'

    def _note_for(cell, match):
        if match:
            return 'Confirmed.'
        if cell in retuned_cells:
            return 'Planned retune (see Scope of Work) — verify values.'
        return 'No SOW context available - if unexpected, check CIQ Revision History tab.'

    def _is_planned(cell):
        return cell in retuned_cells

    eutran_rows = _rows(ciq_wb, 'eUtran Parameters')
    pre_lte = pe.extract_lte_sector_params(log_text) if (has_pre_log and log_text) else {}
    for row in eutran_rows:
        cell = row.get('EutranCellFDDId')
        if not cell:
            continue
        pre_vals = pre_lte.get(cell)
        if not pre_vals:
            continue
        for field, pre_key in (('earfcnDl', 'earfcndl'), ('earfcnUl', 'earfcnul'), ('dlChannelBandwidth', 'dlChannelBandwidth')):
            ciq_v = str(row.get(field, '')).strip()
            pre_v = pre_vals.get(pre_key)
            if not (pre_v and ciq_v):
                continue
            match = pre_v == ciq_v
            results.append({'rule': '#19', 'node': node_id, 'cell': cell, 'field': field,
                             'pre_value': pre_v, 'ciq_value': ciq_v,
                             'status': _status_for(cell, match), 'planned': _is_planned(cell) and not match,
                             'note': _note_for(cell, match)})

    fiveg_rows = _rows(ciq_wb, '5G Info')
    parsed = lp.parse_log(log_text) if (has_pre_log and log_text) else []
    pre_5g = pe.extract_5g_sector_params(parsed, log_text) if (has_pre_log and log_text) else {}
    for row in fiveg_rows:
        cell = row.get('NRCellDU')
        if not cell:
            continue
        pre_vals = pre_5g.get(cell)
        if not pre_vals:
            continue
        for field in ('arfcnDL', 'arfcnUL', 'bSChannelBwDL', 'bSChannelBwUL'):
            ciq_v = str(row.get(field, '')).strip()
            pre_v = pre_vals.get(field)
            if not (pre_v and ciq_v):
                continue
            match = pre_v == ciq_v
            results.append({'rule': '#19', 'node': node_id, 'cell': cell, 'field': field,
                             'pre_value': pre_v, 'ciq_value': ciq_v,
                             'status': _status_for(cell, match), 'planned': _is_planned(cell) and not match,
                             'note': _note_for(cell, match)})
        ciq_ssb = str(row.get('ssbFrequency', '')).strip()
        pre_ssb = pre_vals.get('ssbFrequency')
        if pre_ssb and ciq_ssb:
            match = pre_ssb == ciq_ssb
            results.append({'rule': '#19', 'node': node_id, 'cell': cell, 'field': 'ssbFrequency',
                             'pre_value': pre_ssb, 'ciq_value': ciq_ssb,
                             'status': _status_for(cell, match), 'planned': _is_planned(cell) and not match,
                             'note': _note_for(cell, match)})
    return results


def check_rfds_cell_presence(node_id, ciq_wb, rfds_pages):
    """Rule #18 - EutranCellFDDId presence match: CIQ cell exists in RFDS's
    Non RF Inventory page (per confirmed decision - presence only, no
    beamDirection/azimuth needed)."""
    if rfds_pages is None:
        return [{'rule': '#18', 'node': node_id, 'cell': None, 'status': 'SKIPPED',
                  'note': 'No RFDS provided.'}]
    import rfds_extract as rf
    rfds_cells = rf.extract_non_rf_inventory_cells(rfds_pages)
    mm_row = None
    for r in _rows(ciq_wb, 'Mixed Mode Info'):
        if str(r.get('Node to be built as', '')).strip().upper() == str(node_id).strip().upper():
            mm_row = r
            break
    e_name = str(mm_row.get('eNodeB Name', '')).strip() if mm_row else node_id

    results = []
    for row in _rows(ciq_wb, 'eUtran Parameters'):
        cell = row.get('EutranCellFDDId')
        if not cell or not str(cell).startswith(e_name):
            continue
        present = cell in rfds_cells
        results.append({'rule': '#18', 'node': node_id, 'cell': cell,
                         'status': 'MATCH' if present else 'MISMATCH',
                         'note': 'Present in RFDS.' if present else 'Cell not found in RFDS Non RF Inventory.'})
    return results


def check_rfds_cell_id(node_id, ciq_wb, rfds_pages):
    """Rule #25 - cellId (LTE eUtran Parameters / 5G's cellLocalId - see
    check_rfds_nrcelldu for the 5G side) must match RFDS 'Cell Details
    (Final)' RCN column."""
    if rfds_pages is None:
        return [{'rule': '#25', 'node': node_id, 'cell': None, 'status': 'SKIPPED',
                  'note': 'No RFDS provided.'}]
    import rfds_extract as rf
    cell_details = rf.extract_cell_details(rfds_pages)

    results = []
    for row in _rows(ciq_wb, 'eUtran Parameters'):
        cell = row.get('EutranCellFDDId')
        if not cell or cell not in cell_details:
            continue
        ciq_cell_id = str(row.get('cellId', '')).strip()
        rfds_rcn = cell_details[cell]['rcn']
        match = ciq_cell_id == rfds_rcn
        results.append({'rule': '#25', 'node': node_id, 'cell': cell,
                         'status': 'MATCH' if match else 'MISMATCH',
                         'ciq_cellId': ciq_cell_id, 'rfds_rcn': rfds_rcn,
                         'note': 'Confirmed.' if match else f'CIQ cellId={ciq_cell_id} vs RFDS RCN={rfds_rcn}.'})
    return results


def check_rfds_nrcelldu(node_id, ciq_wb, rfds_pages):
    """Rule #6 - NRCellDU/cellLocalId/RRU Type vs RFDS: cellLocalId vs RFDS
    RCN (5G side of rule #25's logic) + RRU Type vs RFDS RRH text
    (substring match, since RFDS's RRH field isn't cleanly split from
    Sector-Position - see rfds_extract.extract_cell_details's docstring)."""
    if rfds_pages is None:
        return [{'rule': '#6', 'node': node_id, 'cell': None, 'status': 'SKIPPED',
                  'note': 'No RFDS provided.'}]
    import rfds_extract as rf
    cell_details = rf.extract_cell_details(rfds_pages)

    results = []
    for row in _rows(ciq_wb, '5G Info'):
        cell = row.get('NRCellDU')
        if not cell or cell not in cell_details:
            continue
        ciq_local_id = str(row.get('cellLocalId', '')).strip()
        rfds_rcn = cell_details[cell]['rcn']
        ciq_rru = str(row.get('RRU Type', '')).strip()
        rfds_rrh = cell_details[cell]['rrh']

        mismatches = []
        if ciq_local_id != rfds_rcn:
            mismatches.append(f'cellLocalId={ciq_local_id} vs RFDS RCN={rfds_rcn}')
        # RRU Type model number (last-ish token convention, e.g. '4449') should
        # appear somewhere in RFDS's combined RRH text - substring check only,
        # per the field's known ambiguity.
        rru_token = ciq_rru.split()[-1] if ciq_rru else ''
        if rru_token and rru_token not in rfds_rrh:
            mismatches.append(f"RRU Type '{ciq_rru}' not found in RFDS RRH text '{rfds_rrh}'")

        results.append({'rule': '#6', 'node': node_id, 'cell': cell,
                         'ciq_values': f'{ciq_local_id} / {ciq_rru}',
                         'rfds_values': f'{rfds_rcn} / {rfds_rrh}',
                         'status': 'MISMATCH' if mismatches else 'MATCH',
                         'note': '; '.join(mismatches) if mismatches else 'Confirmed.'})
    return results


def check_antenna_model_vs_rfds_4g(node_id, ciq_wb, rfds_pages, e_name):
    """LTE eUtran Parameters 'antenna model' vs RFDS 'RF Inventory Details
    (Final)' antenna model — confirmed exact string match on real data
    (both read 'NNH4-85B-R6' for the same cell). Same AIR-series/missing-
    from-RFDS exemptions as the 5G version (check_antenna_type_vs_rfds).
    Distinct from CIQ's separate 'Antenna type' column (values like
    'MULTIPORT'), which is a different concept, not the physical model."""
    if rfds_pages is None:
        return [{'rule': '#69', 'node': node_id, 'cell': None, 'status': 'SKIPPED', 'note': 'No RFDS provided.'}]
    import rfds_extract as rf
    antennas = rf.extract_rf_inventory_antennas(rfds_pages)
    results = []
    for row in _rows(ciq_wb, 'eUtran Parameters'):
        cell = row.get('EutranCellFDDId')
        if not (cell and e_name and str(cell).startswith(e_name)):
            continue
        rfds_ant = antennas.get(cell, {}).get('model')
        if not rfds_ant:
            continue
        ciq_ant = str(row.get('antenna model', '')).strip()
        label, sector = band_label(cell)
        where = f"{label or 'unknown band'} {sector or 'unknown sector'}"
        match = ciq_ant == rfds_ant
        note = 'Match.' if match else f"{where}: CIQ antenna model={ciq_ant} vs RFDS={rfds_ant}."
        results.append({'rule': '#69', 'node': node_id, 'cell': cell, 'label': label, 'sector': sector,
                         'status': 'MATCH' if match else 'MISMATCH', 'note': note})
    return results


def check_antenna_type_vs_rfds(node_id, ciq_wb, rfds_pages, g_name, e_name=None):
    """Antenna model vs RFDS 'RF Inventory Details (Final)' antenna model
    — confirmed exact string match on real data, both sides (LTE and 5G):
    LTE's 'antenna model' column and RFDS both read 'NNH4-85B-R6' for the
    same cell; 5G's 'Antenna Type' column matches the same way. (LTE also
    has a separate 'Antenna type' column reading 'MULTIPORT' — a
    different concept, port configuration, not the antenna model itself;
    not compared here.)

    AIR-series radios (integrated antenna, no separate ANTENNA row in
    RFDS at all — confirmed via extract_rf_inventory_antennas's own
    docstring and real data: every RBBAIR/AIR-6472 cell checked simply
    has no entry) are skipped rather than flagged missing — that's
    expected, not a gap. A cell missing from RFDS for any OTHER reason is
    also skipped here (check_cells_vs_rfds already owns that finding)
    rather than duplicated as a second 'missing' result."""
    if rfds_pages is None:
        return [{'rule': '#47', 'node': node_id, 'cell': None, 'status': 'SKIPPED', 'note': 'No RFDS provided.'}]
    import rfds_extract as rf
    antennas = rf.extract_rf_inventory_antennas(rfds_pages)
    results = []
    for row in _rows(ciq_wb, '5G Info'):
        cell = row.get('NRCellDU')
        if not (cell and g_name and str(cell).startswith(g_name)):
            continue
        rfds_ant = antennas.get(cell, {}).get('model')
        if not rfds_ant:
            continue
        ciq_ant = str(row.get('Antenna Type', '')).strip()
        label, sector = band_label(cell)
        where = f"{label or 'unknown band'} {sector or 'unknown sector'}"
        match = ciq_ant == rfds_ant
        results.append({'rule': '#47', 'node': node_id, 'cell': cell, 'label': label, 'sector': sector,
                         'status': 'MATCH' if match else 'MISMATCH',
                         'note': 'Match.' if match else f'{where}: CIQ Antenna Type={ciq_ant} vs RFDS={rfds_ant}.'})
    for row in _rows(ciq_wb, 'eUtran Parameters'):
        cell = row.get('EutranCellFDDId')
        if not (cell and e_name and str(cell).startswith(e_name)):
            continue
        rfds_ant = antennas.get(cell, {}).get('model')
        if not rfds_ant:
            continue
        ciq_ant = str(row.get('antenna model', '')).strip()
        label, sector = band_label(cell)
        where = f"{label or 'unknown band'} {sector or 'unknown sector'}"
        match = ciq_ant == rfds_ant
        results.append({'rule': '#69', 'node': node_id, 'cell': cell, 'label': label, 'sector': sector,
                         'status': 'MATCH' if match else 'MISMATCH',
                         'note': 'Match.' if match else f'{where}: CIQ antenna model={ciq_ant} vs RFDS={rfds_ant}.'})
    return results


def check_enb_identity_consistency(node_id, ciq_wb, e_name):
    """eNBId/eNodeB Name must agree across Mixed Mode Info, eNB Info, and
    eUtran Parameters — confirmed real CIQ columns: Mixed Mode Info and
    eNB Info both have 'eNBId'/'eNodeB Name'; eUtran Parameters has its
    own per-cell 'eNBId' column but no eNodeB Name column at all, so only
    eNBId is cross-checked against it. eUtran Parameters has one row per
    cell, not per node — collapsed to its distinct set of values first, so
    a cell that disagrees with its own tab's siblings is itself part of
    the mismatch, not silently picked from one row."""
    if not e_name:
        return []
    e_upper = e_name.strip().upper()
    mm_row = next((r for r in _rows(ciq_wb, 'Mixed Mode Info')
                   if str(r.get('eNodeB Name') or '').strip().upper() == e_upper), None)
    enb_row = next((r for r in _rows(ciq_wb, 'eNB Info')
                    if str(r.get('eNodeB Name') or '').strip().upper() == e_upper), None)
    eutran_rows = [r for r in _rows(ciq_wb, 'eUtran Parameters')
                   if str(r.get('EutranCellFDDId') or '').strip().upper().startswith(e_upper)]

    def _val(v):
        return str(v).strip() if v is not None else ''

    id_sources, name_sources = {}, {}
    if mm_row:
        id_sources['Mixed Mode Info'] = _val(mm_row.get('eNBId'))
        name_sources['Mixed Mode Info'] = _val(mm_row.get('eNodeB Name'))
    if enb_row:
        id_sources['eNB Info'] = _val(enb_row.get('eNBId'))
        name_sources['eNB Info'] = _val(enb_row.get('eNodeB Name'))
    if eutran_rows:
        ids = {_val(r.get('eNBId')) for r in eutran_rows}
        id_sources['eUtran Parameters'] = '/'.join(sorted(ids))

    if len(id_sources) < 2 and len(name_sources) < 2:
        return [{'rule': '#54', 'node': node_id, 'cell': e_name, 'status': 'SKIPPED',
                 'note': f"Only found in {', '.join(set(id_sources) | set(name_sources)) or 'no tab'} - nothing to cross-check."}]

    mismatches = []
    if len({v for v in id_sources.values() if v}) > 1:
        mismatches.append('eNBId differs: ' + ', '.join(f'{k}={v}' for k, v in id_sources.items()))
    if len({v for v in name_sources.values() if v}) > 1:
        mismatches.append('eNodeB Name differs: ' + ', '.join(f'{k}={v}' for k, v in name_sources.items()))
    status = 'MISMATCH' if mismatches else 'MATCH'
    note = ('; '.join(mismatches) if mismatches else
            'eNBId/eNodeB Name consistent across Mixed Mode Info, eNB Info, eUtran Parameters.')
    return [{'rule': '#54', 'node': node_id, 'cell': e_name, 'status': status, 'note': note}]


def check_gnb_du_type_vs_5g_bbu_type(node_id, ciq_wb, g_name, e_name=None):
    """Board type must agree across gNB Info 'DU type', eNB Info 'DU type'
    (same physical BBU, TMBB/MMBB pairing), and 5G Info 'BBU Type' —
    confirmed real CIQ data, all read '6672' for the same node. Purely
    internal CIQ cross-tab consistency, no Pre/EDP/RFDS involved. eNB Info
    is only compared when e_name is given (a paired LTE identity exists
    for this node) — a pure-5G/AAS-only node with no eNB Info row at all
    correctly has nothing to cross-check there. 5G Info has one row per
    cell, not per node — collapsed to its distinct set of values first, so
    a cell that disagrees with its own tab's siblings is itself part of
    the mismatch, not silently picked from one row."""
    if not g_name:
        return []
    g_upper = g_name.strip().upper()
    gnb_row = next((r for r in _rows(ciq_wb, 'gNB Info')
                    if str(r.get('gNodeB Name') or '').strip().upper() == g_upper), None)
    fiveg_rows = [r for r in _rows(ciq_wb, '5G Info')
                  if str(r.get('gNB Name') or '').strip().upper() == g_upper]
    if not gnb_row or not fiveg_rows:
        return [{'rule': '#53', 'node': node_id, 'cell': g_name, 'status': 'SKIPPED',
                 'note': 'gNB Info row or 5G Info rows not found for this node - nothing to cross-check.'}]

    sources = {'gNB Info': str(gnb_row.get('DU type') or '').strip()}
    fiveg_bbu_types = {str(r.get('BBU Type') or '').strip() for r in fiveg_rows}
    sources['5G Info'] = '/'.join(sorted(fiveg_bbu_types))

    if e_name:
        e_upper = e_name.strip().upper()
        enb_row = next((r for r in _rows(ciq_wb, 'eNB Info')
                        if str(r.get('eNodeB Name') or '').strip().upper() == e_upper), None)
        if enb_row:
            sources['eNB Info'] = str(enb_row.get('DU type') or '').strip()

    values = {v for v in sources.values() if v}
    match = len(values) == 1 and len(fiveg_bbu_types) == 1
    note = ('Board type consistent across ' + '/'.join(sources) + '.' if match else
            '; '.join(f'{k}={v}' for k, v in sources.items()) + ' - do not all agree.')
    return [{'rule': '#53', 'node': node_id, 'cell': g_name, 'status': 'MATCH' if match else 'MISMATCH', 'note': note}]


def check_gnb_identity_consistency(node_id, ciq_wb, g_name, mm_row=None):
    """gNBId/gNodeB Name must agree across Mixed Mode Info, gNB Info, and
    5G Info — confirmed real CIQ column names: Mixed Mode Info and gNB
    Info both use 'gNBId'/'gNodeB Name'; 5G Info uses 'gNBId'/'gNB Name'
    (different column name for the name field, same value expected).
    5G Info has one row per CELL, not per node, so its own rows are
    collapsed to their distinct set of values first — a differing value
    across 5G Info's own cells is itself part of the mismatch, not
    silently picked from one row.

    mm_row: this node's OWN Mixed Mode Info row (found by node identity,
    e.g. via cer.find_mm_row), passed in directly rather than re-found by
    matching g_name against Mixed Mode Info's own gNodeB Name column.
    Confirmed real gap fixed here: when that column is blank (and g_name
    only reached this function via a fallback recovery elsewhere, e.g.
    resolve_g_name using gNBId), the old name-match search could never
    find the row it was itself looking for — Mixed Mode Info silently
    dropped out of the comparison, and the MATCH branch's note was a
    HARDCODED string claiming all three tabs agreed, even though Mixed
    Mode Info was never actually compared. A blank field sitting next to
    a real value elsewhere is exactly the kind of gap this check exists
    to catch, not something to quietly omit."""
    if not g_name:
        return []
    g_upper = g_name.strip().upper()

    if mm_row is None:
        mm_row = next((r for r in _rows(ciq_wb, 'Mixed Mode Info')
                       if str(r.get('gNodeB Name') or '').strip().upper() == g_upper), None)
    gnb_row = next((r for r in _rows(ciq_wb, 'gNB Info')
                    if str(r.get('gNodeB Name') or '').strip().upper() == g_upper), None)
    fiveg_rows = [r for r in _rows(ciq_wb, '5G Info')
                  if str(r.get('gNB Name') or '').strip().upper() == g_upper]

    def _val(v):
        return str(v).strip() if v is not None else ''

    sources = {}
    if mm_row is not None:
        sources['Mixed Mode Info'] = (_val(mm_row.get('gNBId')), _val(mm_row.get('gNodeB Name')))
    if gnb_row:
        sources['gNB Info'] = (_val(gnb_row.get('gNBId')), _val(gnb_row.get('gNodeB Name')))
    if fiveg_rows:
        ids = {_val(r.get('gNBId')) for r in fiveg_rows}
        names = {_val(r.get('gNB Name')) for r in fiveg_rows}
        sources['5G Info'] = ('/'.join(sorted(ids)), '/'.join(sorted(names)))

    if len(sources) < 2:
        return [{'rule': '#52', 'node': node_id, 'cell': g_name, 'status': 'SKIPPED',
                 'note': f"Only found in {', '.join(sources) or 'no tab'} - nothing to cross-check."}]

    def _field_mismatches(label, per_source):
        """A real disagreement is either two different non-blank values,
        OR one source blank while another has a real value — a blank
        column is a genuine gap, not an unopinionated abstention, once
        another tab shows there's a real value it should have held."""
        non_blank = {v for v in per_source.values() if v}
        blank_in = [k for k, v in per_source.items() if not v]
        if len(non_blank) > 1:
            return [f'{label} differs: ' + ', '.join(f'{k}={v or "(blank)"}' for k, v in per_source.items())]
        if len(non_blank) == 1 and blank_in:
            present_in = [k for k, v in per_source.items() if v]
            return [f'{label} blank in {", ".join(blank_in)} but {next(iter(non_blank))} in {", ".join(present_in)}.']
        return []

    ids_by_source = {k: v[0] for k, v in sources.items()}
    names_by_source = {k: v[1] for k, v in sources.items()}
    mismatches = _field_mismatches('gNBId', ids_by_source) + _field_mismatches('gNodeB Name', names_by_source)
    status = 'MISMATCH' if mismatches else 'MATCH'
    note = '; '.join(mismatches) if mismatches else f"gNBId/gNodeB Name consistent across {', '.join(sources)}."
    return [{'rule': '#52', 'node': node_id, 'cell': g_name, 'status': status, 'note': note}]


def check_nrcelldu_nrcellcu_match(node_id, ciq_wb, g_name):
    """CIQ-internal consistency check (5G Info tab, no Pre/RFDS involved):
    NRCellDU and NRCellCU must be the same value for every cell — confirmed
    against real CIQ data (always identical in practice on every real
    sample checked); a mismatch would be a genuine CIQ data-entry error,
    not a discrepancy against some other source."""
    results = []
    for row in _rows(ciq_wb, '5G Info'):
        du = str(row.get('NRCellDU') or '').strip()
        cu = str(row.get('NRCellCU') or '').strip()
        if not du or not g_name or not du.startswith(g_name):
            continue
        label, sector = band_label(du)
        match = du == cu
        results.append({'rule': '#39', 'node': node_id, 'cell': du, 'label': label, 'sector': sector,
                         'status': 'MATCH' if match else 'MISMATCH',
                         'note': 'Match.' if match else f'NRCellDU={du} vs NRCellCU={cu or "(blank)"}.'})
    return results


def check_cells_vs_rfds(node_id, ciq_wb, rfds_pages, e_name, g_name):
    """Blueprint section 8 'Cells verification' (#6, #18) - every CIQ cell
    (LTE + 5G combined) checked for presence in RFDS. CIQ | RFDS | Match.
    ALSO the reverse direction: a cell present in RFDS but never declared
    in CIQ at all — confirmed real gap: every other check here only walks
    CIQ's own rows, so an RFDS-only cell was previously invisible to the
    whole pipeline no matter how it got there (a design change RFDS never
    picked up, or a genuinely deleted-but-still-listed cell).

    Each result carries 'label' from band_labels.band_label() — confirmed
    mapping: N77-band carrier suffix '_3' is DOD_BWE (Bandwidth Expansion),
    a newly-added carrier that's genuinely expected to be absent from RFDS
    until built. Used by the checklist to group these separately from a
    real missing-cell finding rather than list each one individually."""
    if rfds_pages is None:
        return [{'rule': '#6/#18', 'node': node_id, 'cell': None, 'status': 'SKIPPED', 'note': 'No RFDS provided.'}]
    import rfds_extract as rf
    rfds_cells = rf.extract_non_rf_inventory_cells(rfds_pages)
    results = []
    ciq_cells = set()
    for row in _rows(ciq_wb, 'eUtran Parameters'):
        cell = row.get('EutranCellFDDId')
        if cell and e_name and str(cell).startswith(e_name):
            ciq_cells.add(cell)
            present = cell in rfds_cells
            label, sector = band_label(cell)
            results.append({'rule': '#6/#18', 'node': node_id, 'cell': cell, 'label': label, 'sector': sector,
                             'ciq_cell': cell, 'rfds_cell': cell if present else 'NOT FOUND',
                             'status': 'MATCH' if present else 'MISMATCH',
                             'note': 'Match.' if present else 'Not found in RFDS.'})
    for row in _rows(ciq_wb, '5G Info'):
        cell = row.get('NRCellDU')
        if cell and g_name and str(cell).startswith(g_name):
            ciq_cells.add(cell)
            present = cell in rfds_cells
            label, sector = band_label(cell)
            results.append({'rule': '#6/#18', 'node': node_id, 'cell': cell, 'label': label, 'sector': sector,
                             'ciq_cell': cell, 'rfds_cell': cell if present else 'NOT FOUND',
                             'status': 'MATCH' if present else 'MISMATCH',
                             'note': 'Match.' if present else 'Not found in RFDS.'})

    node_prefixes = [p for p in (e_name, g_name) if p]
    for rfds_cell in sorted(rfds_cells):
        if rfds_cell in ciq_cells or not any(str(rfds_cell).startswith(p) for p in node_prefixes):
            continue
        label, sector = band_label(rfds_cell)
        results.append({'rule': '#6/#18', 'node': node_id, 'cell': rfds_cell, 'label': label, 'sector': sector,
                         'ciq_cell': 'NOT IN CIQ', 'rfds_cell': rfds_cell,
                         'status': 'MISMATCH', 'note': 'Found in RFDS but not in CIQ.'})
    return results


def check_cell_id_vs_rfds(node_id, log_text, ciq_wb, rfds_pages, e_name, g_name, node_logs=None, moved_map=None):
    """Blueprint section 9 'Cell ID verification' (#6, #24) - Cells | Pre |
    CIQ [cellId/cellLocalId] | RFDS [RCN] | Match, LTE + 5G combined. If a
    cell is newly adding, Pre shows 'NA' per the blueprint's own example.

    Pass/fail is Pre vs CIQ ONLY (confirmed) — RFDS's RCN is shown in the
    note for reference on every result, match or mismatch, but does NOT
    affect status. This is deliberate and shared: checklist rows 49/72/90
    (nRTAC/cellLocalId, cellId, and sector-movement cellid checks) all read
    THIS SAME result list and their own rule-mapping text is explicitly
    Pre-vs-CIQ — changing this function's pass/fail would silently flip
    those three rows too. Row 37's own CIQ-vs-RFDS Cell ID requirement is
    handled separately by check_cell_id_vs_rfds_rcn() below, which this
    function does not feed."""
    results = []
    cell_details = {}
    if rfds_pages is not None:
        import rfds_extract as rf
        cell_details = rf.extract_cell_details(rfds_pages)

    pre_lte = pe.extract_lte_sector_params(log_text) if log_text else {}
    if node_logs and moved_map:
        pre_lte = pe.merge_moved_in_pre(pre_lte, node_logs, moved_map, pe.extract_lte_sector_params)
    for row in _rows(ciq_wb, 'eUtran Parameters'):
        cell = row.get('EutranCellFDDId')
        if not (cell and e_name and str(cell).startswith(e_name)):
            continue
        # A cell genuinely absent from RFDS has nothing to compare its Cell
        # ID against — that's rule #6/#18's job (check_cells_vs_rfds), not
        # this check's. Skipping it here instead of comparing CIQ's cellId
        # against the literal string 'NOT FOUND'/'NOT CHECKED', which
        # always fails and produced a false MISMATCH for every such cell.
        if cell not in cell_details:
            continue
        ciq_id = str(row.get('cellId', '')).strip()
        pre_id = (pre_lte.get(cell) or {}).get('cellId') or 'NA'
        rfds_rcn = cell_details[cell]['rcn']
        match = pre_id in ('NA',) or pre_id == ciq_id  # Pre vs CIQ is the pass/fail; RFDS shown for reference only
        label, sector = band_label(cell)
        where = f"{label or 'unknown band'} {sector or 'unknown sector'}"
        results.append({'rule': '#6/#24', 'node': node_id, 'cell': cell, 'label': label, 'sector': sector,
                         'pre': pre_id, 'ciq': ciq_id, 'rfds_rcn': rfds_rcn,
                         'status': 'MATCH' if match else 'MISMATCH',
                         'note': f'Match. (RFDS RCN={rfds_rcn})' if match else f'{where}: Pre={pre_id}, CIQ={ciq_id}, RFDS={rfds_rcn}.'})

    parsed = __import__('log_parser').parse_log(log_text) if log_text else []
    pre_5g = pe.extract_5g_sector_params(parsed, log_text) if log_text else {}
    if node_logs and moved_map:
        pre_5g = pe.merge_moved_in_pre(pre_5g, node_logs, moved_map, pe.extract_5g_sector_params_from_text)
    for row in _rows(ciq_wb, '5G Info'):
        cell = row.get('NRCellDU')
        if not (cell and g_name and str(cell).startswith(g_name)):
            continue
        if cell not in cell_details:
            continue
        ciq_id = str(row.get('cellLocalId', '')).strip()
        pre_id = (pre_5g.get(cell) or {}).get('cellLocalId') or 'NA'
        rfds_rcn = cell_details[cell]['rcn']
        match = pre_id in ('NA',) or pre_id == ciq_id  # Pre vs CIQ is the pass/fail; RFDS shown for reference only
        label, sector = band_label(cell)
        where = f"{label or 'unknown band'} {sector or 'unknown sector'}"
        results.append({'rule': '#6/#24', 'node': node_id, 'cell': cell, 'label': label, 'sector': sector,
                         'pre': pre_id, 'ciq': ciq_id, 'rfds_rcn': rfds_rcn,
                         'status': 'MATCH' if match else 'MISMATCH',
                         'note': f'Match. (RFDS RCN={rfds_rcn})' if match else f'{where}: Pre={pre_id}, CIQ={ciq_id}, RFDS={rfds_rcn}.'})
    return results


def check_cell_id_vs_rfds_rcn(node_id, ciq_wb, rfds_pages, e_name, g_name):
    """Row 37's own Cell ID requirement, CIQ vs RFDS ONLY — separate from
    check_cell_id_vs_rfds() above, which is Pre-vs-CIQ and feeds three
    OTHER checklist rows (49/72/90) that must not be affected by this.

    RFDS's RCN column is the numeric Cell ID, confirmed identical to CIQ's
    cellId/cellLocalId on real data (HXL00147_7A_1: CIQ cellId=15, RFDS
    RCN=15; HXIN090147F_...N077A_1: CIQ cellLocalId=25, RFDS RCN=25). A
    cell missing from RFDS entirely is skipped (check_cells_vs_rfds
    already flags that absence) rather than compared against nothing."""
    results = []
    if rfds_pages is None:
        return results
    import rfds_extract as rf
    cell_details = rf.extract_cell_details(rfds_pages)

    for row in _rows(ciq_wb, 'eUtran Parameters'):
        cell = row.get('EutranCellFDDId')
        if not (cell and e_name and str(cell).startswith(e_name)) or cell not in cell_details:
            continue
        ciq_id = str(row.get('cellId', '')).strip()
        rfds_rcn = cell_details[cell]['rcn']
        match = bool(ciq_id) and ciq_id == rfds_rcn
        label, sector = band_label(cell)
        where = f"{label or 'unknown band'} {sector or 'unknown sector'}"
        results.append({'rule': '#6/#37', 'node': node_id, 'cell': cell, 'label': label, 'sector': sector,
                         'ciq': ciq_id, 'rfds_rcn': rfds_rcn, 'status': 'MATCH' if match else 'MISMATCH',
                         'note': 'Match.' if match else f'{where}: CIQ Cell ID={ciq_id}, RFDS RCN={rfds_rcn}.'})

    for row in _rows(ciq_wb, '5G Info'):
        cell = row.get('NRCellDU')
        if not (cell and g_name and str(cell).startswith(g_name)) or cell not in cell_details:
            continue
        ciq_id = str(row.get('cellLocalId', '')).strip()
        rfds_rcn = cell_details[cell]['rcn']
        match = bool(ciq_id) and ciq_id == rfds_rcn
        label, sector = band_label(cell)
        where = f"{label or 'unknown band'} {sector or 'unknown sector'}"
        results.append({'rule': '#6/#37', 'node': node_id, 'cell': cell, 'label': label, 'sector': sector,
                         'ciq': ciq_id, 'rfds_rcn': rfds_rcn, 'status': 'MATCH' if match else 'MISMATCH',
                         'note': 'Match.' if match else f'{where}: CIQ Cell ID={ciq_id}, RFDS RCN={rfds_rcn}.'})
    return results


def check_ssb_5g(node_id, parsed, log_text, ciq_wb, has_pre_log, node_logs=None, moved_map=None):
    """Row 44 scope ONLY: ssbFrequency/ssbOffset/ssbDuration, Pre vs CIQ —
    split out from check_rf_params_5g the same way check_arfcn_bw_5g was
    for row 41. ssbOffset/ssbDuration were already extracted generically
    by pre_extract's NRCellDU table parser but never surfaced in
    extract_5g_sector_params()'s result dict until now. Mismatch note
    shows band+sector and the actual Pre/CIQ values, not just field names
    — grouping (via checklist's _group_bad_by_node_reason) collapses to
    just the band name when every sector of that band fails, and to
    '<band> (<sectors>)' when only some do."""
    pre_5g = pe.extract_5g_sector_params(parsed, log_text) if (has_pre_log and log_text) else {}
    if node_logs and moved_map:
        pre_5g = pe.merge_moved_in_pre(pre_5g, node_logs, moved_map, pe.extract_5g_sector_params_from_text)
    results = []
    for row in _rows(ciq_wb, '5G Info'):
        cell = row.get('NRCellDU')
        if not cell:
            continue
        pre_vals = pre_5g.get(cell)
        if not pre_vals:
            continue
        mismatched = []
        for field in ('ssbFrequency', 'ssbOffset', 'ssbDuration'):
            ciq_v = str(row.get(field, '')).strip()
            pre_v = pre_vals.get(field) or 'NA'
            if pre_v != 'NA' and pre_v != ciq_v:
                mismatched.append(f'{field}: {pre_v}/{ciq_v}')
        label, sector = band_label(cell)
        status = 'MATCH' if not mismatched else 'MISMATCH'
        where = f"{label or 'unknown band'} {sector or 'unknown sector'}"
        note = 'Confirmed.' if not mismatched else f"{where}: " + '; '.join(mismatched)
        results.append({'rule': '#44', 'node': node_id, 'cell': cell, 'label': label, 'sector': sector,
                         'status': status, 'note': note})
    return results


def check_arfcn_bw_5g(node_id, parsed, log_text, ciq_wb, has_pre_log, node_logs=None, moved_map=None):
    """Row 41 scope ONLY: arfcnDL/arfcnUL/bSChannelBwDL/bSChannelBwUL, Pre vs
    CIQ — split out from check_rf_params_5g's combined 5-field result
    (which also folds in ssbFrequency, row 44's own topic) because sharing
    one result list made rows 41/42/44 all show identical status/notes
    despite different titled scopes. Mismatch note shows the actual
    Pre/CIQ values per field, not just which field's name mismatched."""
    pre_5g = pe.extract_5g_sector_params(parsed, log_text) if (has_pre_log and log_text) else {}
    if node_logs and moved_map:
        pre_5g = pe.merge_moved_in_pre(pre_5g, node_logs, moved_map, pe.extract_5g_sector_params_from_text)
    results = []
    for row in _rows(ciq_wb, '5G Info'):
        cell = row.get('NRCellDU')
        if not cell:
            continue
        pre_vals = pre_5g.get(cell)
        if not pre_vals:
            continue
        mismatched = []
        for field in ('arfcnDL', 'arfcnUL', 'bSChannelBwDL', 'bSChannelBwUL'):
            ciq_v = str(row.get(field, '')).strip()
            pre_v = pre_vals.get(field) or 'NA'
            if pre_v != 'NA' and pre_v != ciq_v:
                mismatched.append(f'{field}: {pre_v}/{ciq_v}')
        label, sector = band_label(cell)
        status = 'MATCH' if not mismatched else 'MISMATCH'
        where = f"{label or 'unknown band'} {sector or 'unknown sector'}"
        note = 'Confirmed.' if not mismatched else f"{where}: " + '; '.join(mismatched)
        results.append({'rule': '#41', 'node': node_id, 'cell': cell, 'label': label, 'sector': sector,
                         'status': status, 'note': note})
    return results


def check_rilink_vs_rbb_4g(node_id, log_text, ciq_wb, e_name, node_logs=None, moved_map=None):
    """Row 63: Pre's actual RILink (Single/Double fiber) vs CIQ's RBB Type
    link suffix ('_1'=Single, '_2'=Double — pe.parse_rbb_link), LTE side.
    Same comparison check_sector_swap_config already does for standalone
    5G cells; built separately here since that function is LTE-vs-EDP/
    sector-config scoped, not this Pre-vs-CIQ RILink question."""
    if not e_name:
        return []
    rilink = pe.extract_cell_to_rilink(log_text) if log_text else {}
    if node_logs and moved_map:
        rilink = pe.merge_moved_in_pre(rilink, node_logs, moved_map, pe.extract_cell_to_rilink)
    results = []
    for row in _rows(ciq_wb, 'eUtran Parameters'):
        cell = row.get('EutranCellFDDId')
        if not (cell and str(cell).startswith(e_name)):
            continue
        pre_ri = rilink.get(cell, 'NA')
        rbb = row.get('RBB type')
        ciq_ri = pe.parse_rbb_link(rbb)
        label, sector = band_label(cell)
        where = f"{label or 'unknown band'} {sector or 'unknown sector'}"
        if pre_ri == 'NA' or ciq_ri is None:
            continue
        match = pre_ri == ciq_ri
        note = 'Confirmed.' if match else f"{where}: RILink Pre={pre_ri} vs CIQ (RBB type {rbb})={ciq_ri}."
        results.append({'rule': '#63', 'node': node_id, 'cell': cell, 'label': label, 'sector': sector,
                         'status': 'MATCH' if match else 'MISMATCH', 'note': note})
    return results


def check_electrical_tilt_type(node_id, ciq_wb, e_name):
    """Row 61: 'electricalAntennaTilt' must be stored as an integer, not a
    character/string — confirmed real bug on a real site (FCL04120_7B_1
    had '0' as a string while every sibling cell had a proper int like
    50). Checked on the raw openpyxl value type directly (str vs int/
    float), which is what actually distinguishes a text-formatted Excel
    cell from a number-formatted one — not re-parsed from a string, since
    a re-parse would accept '0' just as happily as 0 and miss the bug
    entirely."""
    if not e_name:
        return []
    results = []
    for row in _rows(ciq_wb, 'eUtran Parameters'):
        cell = row.get('EutranCellFDDId')
        if not (cell and str(cell).startswith(e_name)):
            continue
        val = row.get('electricalAntennaTilt')
        label, sector = band_label(cell)
        where = f"{label or 'unknown band'} {sector or 'unknown sector'}"
        if val is None:
            continue
        is_character = isinstance(val, str)
        status = 'MISMATCH' if is_character else 'MATCH'
        note = (f"{where}: electricalAntennaTilt='{val}' is stored as a character, not an integer."
                if is_character else 'Confirmed integer.')
        results.append({'rule': '#61', 'node': node_id, 'cell': cell, 'label': label, 'sector': sector,
                         'status': status, 'note': note})
    return results


def check_rbb_tx_isdlonly_4g(node_id, ciq_wb, e_name):
    """Row 58: LTE 'RBB type' vs CIQ's own 'noOfTxAntennas'/'noOfRxAntennas'
    and 'Radio Port' columns — CIQ-internal consistency, confirmed real
    data ('RBB44_1D' with noOfTxAntennas=4/noOfRxAntennas=4 and Radio
    Port='DATA1' — single port, matching the '_1' Single-link suffix).
    Plus: if noOfTxAntennas is 0 (no transmit antennas — a downlink-only
    carrier), ISDLONLY must be TRUE in CIQ."""
    if not e_name:
        return []
    results = []
    for row in _rows(ciq_wb, 'eUtran Parameters'):
        cell = row.get('EutranCellFDDId')
        if not (cell and str(cell).startswith(e_name)):
            continue
        rbb = row.get('RBB type')
        rbb_txrx = pe.parse_rbb_txrx(rbb)
        ciq_tx = str(row.get('noOfTxAntennas', '')).strip()
        ciq_rx = str(row.get('noOfRxAntennas', '')).strip()
        ciq_txrx = f'{ciq_tx}x{ciq_rx}' if ciq_tx and ciq_rx else None
        isdlonly = str(row.get('ISDLONLY', '')).strip().upper()
        radio_port = str(row.get('Radio Port', '')).strip()
        rbb_link = pe.parse_rbb_link(rbb)
        radio_port_link = 'Double' if '/' in radio_port else ('Single' if radio_port else None)

        label, sector = band_label(cell)
        where = f"{label or 'unknown band'} {sector or 'unknown sector'}"
        mismatches = []
        if rbb_txrx is None:
            mismatches.append(f"RBB type '{rbb}' does not match the expected RBB<TX><RX> pattern.")
        elif ciq_txrx and rbb_txrx != ciq_txrx:
            mismatches.append(f"RBB type {rbb} implies TX/RX {rbb_txrx} but noOfTxAntennas/noOfRxAntennas={ciq_txrx}.")
        if ciq_tx == '0' and isdlonly != 'TRUE':
            mismatches.append(f"noOfTxAntennas=0 but ISDLONLY='{isdlonly or 'blank'}' (expected TRUE).")
        if rbb_link and radio_port_link and rbb_link != radio_port_link:
            mismatches.append(f"RBB type {rbb} implies {rbb_link} link but Radio Port='{radio_port}' is {radio_port_link}.")

        status = 'MATCH' if not mismatches else 'MISMATCH'
        note = 'Confirmed.' if not mismatches else f"{where}: " + '; '.join(mismatches)
        results.append({'rule': '#58', 'node': node_id, 'cell': cell, 'label': label, 'sector': sector,
                         'status': status, 'note': note})
    return results


def check_rf_params_4g(node_id, log_text, ciq_wb, has_pre_log, retuned_cells=None, node_logs=None, moved_map=None):
    """Blueprint section 10 'Parameters Verification - 4G' (#19). One row
    per cell, each field shown as a single 'Pre | CIQ' string per the
    confirmed compact format. Retuned cells (from SOW) still highlight red -
    the note marks them as planned so the engineer knows why, but doesn't
    downgrade the color (per confirmed decision).

    node_logs/moved_map: when a cell is moving in from another physical node
    (Sector Del_Movement), its real Pre history sits on the SOURCE node's
    log under the source cell name, not this node's own log - confirmed on
    a real rehome, this node's own log has never seen the cell at all.
    Without the merge, every moved-in cell reported NA despite real Pre
    data being available."""
    retuned_cells = retuned_cells or set()
    pre_lte = pe.extract_lte_sector_params(log_text) if (has_pre_log and log_text) else {}
    pre_ul = pe.extract_ul_channel_bandwidth(log_text) if (has_pre_log and log_text) else {}
    if node_logs and moved_map:
        pre_lte = pe.merge_moved_in_pre(pre_lte, node_logs, moved_map, pe.extract_lte_sector_params)
        pre_ul = pe.merge_moved_in_pre(pre_ul, node_logs, moved_map, pe.extract_ul_channel_bandwidth)
    results = []
    for row in _rows(ciq_wb, 'eUtran Parameters'):
        cell = row.get('EutranCellFDDId')
        if not cell:
            continue
        pre_vals = pre_lte.get(cell)
        if not pre_vals:
            continue
        fields = {}
        mismatched = []
        for out_key, ciq_col, pre_key in (('earfcndl', 'earfcnDl', 'earfcndl'), ('earfcnul', 'earfcnUl', 'earfcnul'),
                                            ('dlChannelBandwidth', 'dlChannelBandwidth', 'dlChannelBandwidth')):
            ciq_v = str(row.get(ciq_col, '')).strip()
            pre_v = pre_vals.get(pre_key) or 'NA'
            fields[out_key] = f'{pre_v} | {ciq_v}'
            if pre_v != 'NA' and pre_v != ciq_v:
                mismatched.append(out_key)
        ciq_ul = str(row.get('ulChannelBandwidth', '')).strip()
        pre_ul_v = pre_ul.get(cell) or 'NA'
        fields['ulChannelBandwidth'] = f'{pre_ul_v} | {ciq_ul}'
        if pre_ul_v != 'NA' and pre_ul_v != ciq_ul:
            mismatched.append('ulChannelBandwidth')

        planned = bool(mismatched) and cell in retuned_cells
        status = 'MATCH' if not mismatched else 'MISMATCH'
        label, sector = band_label(cell)
        where = f"{label or 'unknown band'} {sector or 'unknown sector'}"
        note = 'Confirmed.' if not mismatched else \
            (f"{where}: Planned retune ({', '.join(mismatched)}) - verify." if planned else
             f"{where}: Mismatch on {', '.join(mismatched)} - no SOW context, check Revision History.")
        results.append({'rule': '#19', 'node': node_id, 'cell': cell, 'label': label, 'sector': sector,
                         'status': status, 'note': note, **fields})
    return results


def check_rf_params_5g(node_id, parsed, log_text, ciq_wb, has_pre_log, retuned_cells=None, node_logs=None, moved_map=None):
    """Blueprint section 11 'Parameters Verification - 5G' (#19). Same
    compact 'Pre | CIQ' format as the 4G table. See check_rf_params_4g's
    docstring for node_logs/moved_map (moved-in cell Pre lookup)."""
    retuned_cells = retuned_cells or set()
    pre_5g = pe.extract_5g_sector_params(parsed, log_text) if (has_pre_log and log_text) else {}
    if node_logs and moved_map:
        pre_5g = pe.merge_moved_in_pre(pre_5g, node_logs, moved_map, pe.extract_5g_sector_params_from_text)
    results = []
    for row in _rows(ciq_wb, '5G Info'):
        cell = row.get('NRCellDU')
        if not cell:
            continue
        pre_vals = pre_5g.get(cell)
        if not pre_vals:
            continue
        fields = {}
        mismatched = []
        for field in ('arfcnDL', 'arfcnUL', 'bSChannelBwDL', 'bSChannelBwUL'):
            ciq_v = str(row.get(field, '')).strip()
            pre_v = pre_vals.get(field) or 'NA'
            fields[field] = f'{pre_v} | {ciq_v}'
            if pre_v != 'NA' and pre_v != ciq_v:
                mismatched.append(field)
        ciq_ssb = str(row.get('ssbFrequency', '')).strip()
        pre_ssb = pre_vals.get('ssbFrequency') or 'NA'
        fields['ssbfrequency'] = f'{pre_ssb} | {ciq_ssb}'
        if pre_ssb != 'NA' and pre_ssb != ciq_ssb:
            mismatched.append('ssbfrequency')

        planned = bool(mismatched) and cell in retuned_cells
        status = 'MATCH' if not mismatched else 'MISMATCH'
        note = 'Confirmed.' if not mismatched else \
            (f"Planned retune ({', '.join(mismatched)}) - verify." if planned else
             f"Mismatch on {', '.join(mismatched)} - no SOW context, check Revision History.")
        results.append({'rule': '#19', 'node': node_id, 'cell': cell, 'status': status, 'note': note, **fields})
    return results


def check_riport_uniqueness(node_id, enb_row, gnb_row, ciq_wb, e_name=None, g_name=None):
    """CIQ-only design check (POST/target, not a Pre log check — confirmed):
    once a physical RiPort is used for one band on this node, no OTHER
    band/sector may reuse that same port UNLESS the two cells are listed
    as sharing the same radio — 'Co-Located Technology Cell' is the
    CONFIRMED field for that (verified against a real CIQ: every port
    reused there legitimately lists the other cell(s) sharing it, e.g.
    FCL04120_7A_1 <-> FCL04120_8A_1 both on port 'A', each listing the
    other; FCL09220's own A/B/C ports are a different physical node, so
    never compared against FCL04120's). Separately, confirmed rule #2: a
    port declared under this node's 1st/2nd XMU is reserved outright — no
    cell may use that port number even if otherwise legitimately
    co-located with something (this is check_xmu_port_overlap()'s own
    existing rule, folded in here so both halves of 'Riport should be
    unique' show under one checklist result instead of two disconnected
    ones).

    e_name/g_name (from Mixed Mode Info, same pairing
    build_primary_secondary_node_list() uses): each sheet is filtered by
    its OWN matching identity, not by the single node_id for both —
    confirmed real gap on a TMBB site (TNL01216/TNMN001216): NR cells are
    prefixed with the SECONDARY's name ('TNMN001216_N002A_1'), not the
    physical node_id passed in for the primary ('TNL01216'), so a single
    node_id used to filter BOTH sheets silently excluded every NR cell —
    the cross-technology co-location (port D shared between
    TNL01216_2A_1 and TNMN001216_N002A_1, confirmed legitimate) was never
    actually checked as one group. Falls back to node_id for whichever of
    e_name/g_name isn't given, so a plain single-identity node still
    works exactly as before.

    Ports are scoped to a comma-split of 'Co-Located Technology Cell' —
    a cell can be listed for more than one co-located partner (a 3-way
    combine), and the check only requires EVERY other cell sharing that
    exact port to appear somewhere in this cell's own list (not the
    reverse — a real CIQ can leave the list one-directional on one side
    of a pair)."""
    def _cells(sheet, id_col, board_col, port_cols, co_col, prefix):
        out = []
        if not prefix:
            return out
        for row in _rows(ciq_wb, sheet):
            cid = str(row.get(id_col) or "").strip()
            if not cid or not cid.upper().startswith(str(prefix).upper()):
                continue
            ports = set()
            for pc in port_cols:
                v = str(row.get(pc) or "").strip()
                if v and v.upper() not in ("", "N/A", "NA", "NOT USED"):
                    ports.add(v.upper())
            co = {c.strip().upper() for c in str(row.get(co_col) or "").split(",")
                  if c.strip() and c.strip().upper() not in ("N/A", "NA")}
            out.append({"cell": cid, "board": str(row.get(board_col) or "").strip(),
                        "ports": ports, "co_located": co})
        return out

    # Ports are per-BOARD connectors (A/B/C.../D/E/F are relabeled on every
    # DU) - two different boards reusing the same letter is normal and not
    # a clash. Confirmed real false positive this fixes: a cell on board
    # 6672 port A was flagged against unrelated cells on board 6651 port A
    # just because the letter matched, even though physically different
    # hardware can never actually collide.
    cells = (_cells("eUtran Parameters", "EutranCellFDDId", "DUS / XMU",
                     ["DUS / XMU Port", "DUS / XMU Port Expansion"], "Co-Located Technology Cell",
                     e_name or node_id)
             + _cells("5G Info", "NRCellDU", "BB/XMU", ["Port 1", "Port 2", "Port 3", "Port 4"],
                      "Co-Located Technology Cell", g_name or node_id))
    if not cells:
        return []

    port_to_cells = {}
    for c in cells:
        for p in c["ports"]:
            port_to_cells.setdefault((c["board"], p), []).append(c)

    # Rule #2: ports this node's own 1st/2nd XMU declares are reserved
    # outright, regardless of Co-Located Technology Cell. On a TMBB/MMBB
    # node eNB Info's XMU and gNB Info's XMU describe the SAME physical
    # XMU unit from the 4G side and the 5G side respectively - identical
    # ports declared in both is the EXPECTED, correct shape (confirmed),
    # not a clash. Only a genuine DISAGREEMENT between the two sides (both
    # declared 'YES' but with different ports - inconsistent data entry
    # for what should be one shared physical fact) is a real problem.
    enb_xmu, gnb_xmu = set(), set()
    for row, bucket in ((enb_row, 'enb_xmu'), (gnb_row, 'gnb_xmu')):
        if row is None:
            continue
        target = enb_xmu if bucket == 'enb_xmu' else gnb_xmu
        for which in ("1st", "2nd"):
            if str(row.get(f"{which} XMU", "")).strip().upper() != "YES":
                continue
            for i in (1, 2, 3):
                v = str(row.get(f"{which} XMU Port {i}") or "").strip()
                if v and v.upper() not in ("", "N/A", "NA", "NOT USED"):
                    target.add(v.upper())
    xmu_ports = enb_xmu | gnb_xmu
    xmu_disagreement = (enb_xmu ^ gnb_xmu) if (enb_xmu and gnb_xmu) else set()

    out = []
    if xmu_disagreement:
        out.append({"rule": "#67", "node": node_id, "cell": node_id, "status": "MISMATCH",
                    "note": f"eNB Info's XMU ports ({sorted(enb_xmu) or 'none'}) and gNB Info's XMU ports "
                            f"({sorted(gnb_xmu) or 'none'}) should describe the same physical XMU but disagree."})
    for (board, port), group in port_to_cells.items():
        if port in xmu_ports:
            out.append({"rule": "#67", "node": node_id, "cell": port, "status": "MISMATCH",
                        "note": f"Port {port} on board {board} is declared under this node's XMU but also assigned to: "
                                f"{', '.join(sorted(c['cell'] for c in group))}"})
            continue
        if len(group) < 2:
            out.append({"rule": "#67", "node": node_id, "cell": port, "status": "MATCH", "note": "Unique."})
            continue
        # CBAND/DOD/DOD_BWE (the N77 carrier tiers '_1'/'_2'/'_3') sharing a
        # port on the SAME sector is legitimate by construction — they're
        # the same physical radio's own carrier tiers, not a genuine
        # cross-band clash, and 'Co-Located Technology Cell' isn't used
        # for this case at all (confirmed real CIQ: that field reads 'NA'
        # on every one of these cells even when correctly co-located).
        # Only flags if the port is ALSO used by a cell OUTSIDE this
        # same-sector CBAND/DOD/DOD_BWE group — a genuine different-band
        # reuse of a CBAND port.
        def _sector_of(c):
            _, sector = band_label(c["cell"])
            return sector
        n77_tier_group = all(is_dod_cell(c["cell"]) or is_cband_cell(c["cell"]) for c in group)
        same_sector = len({_sector_of(c) for c in group}) == 1
        if n77_tier_group and same_sector:
            out.append({"rule": "#67", "node": node_id, "cell": port, "status": "MATCH",
                        "note": f"Port {port} on board {board} shared by CBAND/DOD/DOD_BWE carrier tiers on the same sector: "
                                f"{', '.join(sorted(c['cell'] for c in group))}"})
            continue
        bad = [c for c in group if not all(other["cell"] in c["co_located"]
                                            for other in group if other is not c)]
        if bad:
            out.append({"rule": "#67", "node": node_id, "cell": port, "status": "MISMATCH",
                        "note": f"Port {port} on board {board} reused by non-co-located cells: "
                                f"{', '.join(sorted(c['cell'] for c in group))}"})
        else:
            out.append({"rule": "#67", "node": node_id, "cell": port, "status": "MATCH",
                        "note": f"Port {port} on board {board} shared by confirmed co-located cells: "
                                f"{', '.join(sorted(c['cell'] for c in group))}"})
    return out


def check_xmu_port_overlap(node_id, enb_row, gnb_row, ciq_wb):
    """Blueprint section 14 second table: Node id | 1st DU type | 1st XMU |
    1st XMU Port 1/2/3 | Port Uniqueness - do the XMU's own designated ports
    get reused by any other sector's RI port on this node?

    Reads BOTH eNB Info and gNB Info rows, and both 1st and 2nd XMU - the
    earlier version took `enb_row or gnb_row` (silently ignoring the other
    tab) and only looked at 1st XMU, so a 2nd XMU or a gNB-side declaration
    was invisible."""
    rows = [r for r in (enb_row, gnb_row) if r is not None]
    if not rows:
        return []

    du_type = ''
    xmu_ports = set()
    enb_ports, gnb_ports = set(), set()
    declared = False
    for row in rows:
        du_type = du_type or str(row.get('DU type') or row.get('1st DU type') or '').strip()
        row_ports = set()
        for which in ('1st', '2nd'):
            if str(row.get(f'{which} XMU', '')).strip().upper() != 'YES':
                continue
            declared = True
            for i in (1, 2, 3):
                v = row.get(f'{which} XMU Port {i}')
                if v is not None and str(v).strip().upper() not in ('', 'N/A', 'NA', 'NOT USED'):
                    row_ports.add(str(v).strip())
        xmu_ports |= row_ports
        if row is enb_row:
            enb_ports |= row_ports
        if row is gnb_row:
            gnb_ports |= row_ports
    if not declared:
        return []

    # eNB Info's XMU and gNB Info's XMU describe the SAME physical XMU on
    # a TMBB/MMBB node - identical ports in both is expected, not a clash;
    # only a genuine disagreement between the two is a real problem.
    xmu_disagreement = (enb_ports ^ gnb_ports) if (enb_ports and gnb_ports) else set()

    # Only this node's own cells can conflict with this node's XMU ports -
    # a sector on a different physical node uses different hardware.
    own_prefixes = {str(r.get(k)).strip() for r in rows for k in ('eNodeB Name', 'gNodeB Name')
                    if r.get(k) and str(r.get(k)).strip()}
    own_prefixes.add(str(node_id))

    used_elsewhere = set()
    for row5g in _rows(ciq_wb, '5G Info'):
        bbu = str(row5g.get('BB/XMU', '')).strip()
        if 'XMU' in bbu.upper():
            continue
        cell = str(row5g.get('NRCellDU') or '')
        if not any(cell.startswith(p) for p in own_prefixes):
            continue
        for pc in ('Port 1', 'Port 2', 'Port 3', 'Port 4'):
            v = row5g.get(pc)
            if v is not None and str(v).strip() in xmu_ports:
                used_elsewhere.add(str(v).strip())

    # 4G cells (eUtran Parameters) sit on the SAME shared DU/BBU hardware on
    # a TMBB/MMBB node and were never checked here at all - a 4G cell
    # landing on this node's declared XMU port is exactly the same kind of
    # conflict as a 5G cell doing it.
    for row4g in _rows(ciq_wb, 'eUtran Parameters'):
        board = str(row4g.get('DUS / XMU', '')).strip()
        if 'XMU' in board.upper():
            continue
        cell = str(row4g.get('EutranCellFDDId') or '')
        if not any(cell.startswith(p) for p in own_prefixes):
            continue
        for pc in ('DUS / XMU Port', 'DUS / XMU Port Expansion'):
            v = row4g.get(pc)
            if v is not None and str(v).strip().upper() not in ('', 'N/A', 'NOT USED') and str(v).strip() in xmu_ports:
                used_elsewhere.add(str(v).strip())

    unique = not used_elsewhere and not xmu_disagreement
    note_parts = []
    if xmu_disagreement:
        note_parts.append(f"eNB Info XMU ports ({sorted(enb_ports)}) and gNB Info XMU ports ({sorted(gnb_ports)}) should describe the same physical XMU but disagree.")
    if used_elsewhere:
        note_parts.append(f'XMU ports reused elsewhere: {sorted(used_elsewhere)}')
    return [{'rule': '#11/#25', 'node': node_id, 'cell': node_id,
              'du_type': du_type, 'xmu': 'Yes', 'xmu_ports': ', '.join(sorted(xmu_ports)) or 'NOT USED',
              'status': 'MATCH' if unique else 'MISMATCH',
              'note': 'Unique.' if unique else ' '.join(note_parts)}]


def check_radio_port_conflict(node_id, ciq_wb):
    """NEW - not part of the confirmed Blueprint rule set. General Radio Map
    port-conflict check (the HTML tool's 'Port Conflict (Multiple Radios)'):
    does the same (DUS/XMU board, DUS/XMU Port) get declared for cells
    belonging to more than one distinct RRU type? Broader than
    check_xmu_port_overlap(), which only looks at a node's own declared
    1st/2nd XMU ports - this looks at every LTE cell's port assignment
    regardless of XMU declaration. Tested against real CIQs; not yet run
    against enough real *conflicting* sites to carry the 'confirmed' bar."""
    port_to_rrus = {}
    port_to_cells = {}
    for row in _rows(ciq_wb, 'eUtran Parameters'):
        cell = row.get('EutranCellFDDId')
        if not cell or not (node_id and str(cell).startswith(node_id)):
            continue
        board = str(row.get('DUS / XMU') or '').strip()
        port = str(row.get('DUS / XMU Port') or '').strip()
        rru = str(row.get('RRU type') or '').strip()
        if not (board and port):
            continue
        key = (board, port)
        port_to_rrus.setdefault(key, set()).add(rru)
        port_to_cells.setdefault(key, set()).add(cell)

    results = []
    for key, rrus in port_to_rrus.items():
        board, port = key
        cells = sorted(port_to_cells[key])
        if len(rrus) > 1:
            results.append({'rule': 'NEW', 'node': node_id, 'cell': ', '.join(cells),
                             'board': board, 'port': port, 'status': 'MISMATCH',
                             'note': f"Port {port} on board {board} used by {len(rrus)} different RRU types: {', '.join(sorted(rrus))}"})
        else:
            results.append({'rule': 'NEW', 'node': node_id, 'cell': ', '.join(cells),
                             'board': board, 'port': port, 'status': 'MATCH',
                             'note': 'Single RRU type on this port.'})
    return results


def check_cellid_uniqueness_4g(node_id, ciq_wb, e_name):
    """LTE cellId must be unique across ALL bands on the same node — unlike
    PCI (scoped per-band), a cellId clash between two different bands on
    ONE node is still a real conflict. A different physical node reusing
    the same cellId is fine (confirmed: no cross-node scoping needed)."""
    if not e_name:
        return []
    counts = {}
    rows_by_cell = {}
    for row in _rows(ciq_wb, 'eUtran Parameters'):
        cell = row.get('EutranCellFDDId')
        if not (cell and str(cell).startswith(e_name)):
            continue
        cid = str(row.get('cellId', '')).strip()
        if not cid:
            continue
        counts.setdefault(cid, []).append(cell)
        rows_by_cell[cell] = cid

    results = []
    for cell, cid in rows_by_cell.items():
        dup = len(counts[cid]) > 1
        label, sector = band_label(cell)
        where = f"{label or 'unknown band'} {sector or 'unknown sector'}"
        note = (f"{where}: Cell ID clash: cellId {cid} shared with {[x for x in counts[cid] if x != cell]}"
                if dup else 'Unique.')
        results.append({'rule': '#66U', 'node': node_id, 'cell': cell, 'label': label, 'sector': sector,
                         'status': 'MISMATCH' if dup else 'MATCH', 'note': note})
    return results


def check_pci_uniqueness(node_id, ciq_wb, e_name=None):
    """Rule #23 - PCI uniqueness within same band. PCI = PhysicalLayerCellIdGroup*3
    + physicalLayerSubCellId (verified against CIQ's own 'PCI' column); flags
    any two same-band-labeled sectors on this node sharing a PCI value.
    e_name: eNodeB Name prefix for this node's cells (defaults to node_id)."""
    prefix = e_name or node_id
    eutran_rows = _rows(ciq_wb, 'eUtran Parameters')
    results = []
    by_band = {}
    for row in eutran_rows:
        cell = row.get('EutranCellFDDId')
        if not cell or not str(cell).startswith(prefix):
            continue
        try:
            group = int(str(row.get('PhysicalLayerCellIdGroup', '')).strip())
            sub = int(str(row.get('physicalLayerSubCellId', '')).strip())
        except (TypeError, ValueError):
            continue
        pci = group * 3 + sub
        ciq_pci = str(row.get('PCI', '')).strip()
        label, sector = band_label(cell)
        by_band.setdefault(label, []).append({'cell': cell, 'group': group, 'sub': sub, 'pci': pci, 'ciq_pci': ciq_pci, 'sector': sector})

    for label, cells in by_band.items():
        pci_counts = {}
        for c in cells:
            pci_counts.setdefault(c['pci'], []).append(c['cell'])
        for c in cells:
            dup = len(pci_counts[c['pci']]) > 1
            mismatch_calc = str(c['pci']) != c['ciq_pci']
            status = 'MISMATCH' if (dup or mismatch_calc) else 'MATCH'
            where = f"{label or 'unknown band'} {c['sector'] or 'unknown sector'}"
            notes = []
            if dup:
                notes.append(f"PCI clash: PCI {c['pci']} shared with {[x for x in pci_counts[c['pci']] if x != c['cell']]}")
            if mismatch_calc:
                notes.append(f"Computed PCI {c['pci']} != CIQ PCI {c['ciq_pci']}")
            results.append({'rule': '#23', 'node': node_id, 'cell': c['cell'], 'label': label, 'sector': c['sector'],
                             'group': c['group'], 'sub': c['sub'], 'pci': c['pci'],
                             'status': status, 'note': f"{where}: " + '; '.join(notes) if notes else 'Unique.'})
    return results


def check_nr_pci_uniqueness(node_id, ciq_wb, g_name=None):
    """Rule #23 (5G side) - nRPCI uniqueness within same band on this node.
    g_name: gNodeB Name prefix for this node's 5G cells (skips if None -
    node has no 5G identity)."""
    if not g_name:
        return []
    fiveg_rows = _rows(ciq_wb, '5G Info')
    results = []
    by_band = {}
    for row in fiveg_rows:
        cell = row.get('NRCellDU')
        if not cell or not str(cell).startswith(g_name):
            continue
        nrpci = row.get('nRPCI')
        if nrpci is None or str(nrpci).strip() == '':
            continue
        label, sector = band_label(cell)
        by_band.setdefault(label, []).append({'cell': cell, 'nrpci': str(nrpci).strip(), 'sector': sector})

    for label, cells in by_band.items():
        counts = {}
        for c in cells:
            counts.setdefault(c['nrpci'], []).append(c['cell'])
        for c in cells:
            dup = len(counts[c['nrpci']]) > 1
            where = f"{label or 'unknown band'} {c['sector'] or 'unknown sector'}"
            results.append({'rule': '#23', 'node': node_id, 'cell': c['cell'], 'label': label, 'sector': c['sector'], 'nrpci': c['nrpci'],
                             'status': 'MISMATCH' if dup else 'MATCH',
                             'note': f"{where}: PCI clash: nRPCI {c['nrpci']} shared with {[x for x in counts[c['nrpci']] if x != c['cell']]}" if dup else 'Unique.'})
    return results


def check_antenna_uniqueness(node_id, ciq_wb):
    """Blueprint section 15 'Antenna Uniqueness' (#27, #34). Sharing sectors
    normally share the same AUG/AU/ASU; the 4890-vs-8843 exception requires
    them to DIFFER for PCS/AWS bands when both radio types are present in the
    shared group. CBAND/DOD/DOD_BWE cells are explicitly excluded per the
    blueprint comment ('For CBAND/DOD dont not trigger this check')."""
    antenna_rows = _rows(ciq_wb, 'Antenna Information')
    fiveg_rows = _rows(ciq_wb, '5G Info')
    eutran_rows = _rows(ciq_wb, 'eUtran Parameters')

    rru_by_cell = {}
    for row in eutran_rows:
        cell = row.get('EutranCellFDDId')
        if cell:
            rru_by_cell[cell] = str(row.get('RRU type', '')).strip()
    for row in fiveg_rows:
        cell = row.get('NRCellDU')
        if cell:
            rru_by_cell[cell] = str(row.get('RRU Type', '')).strip()

    colocation = {}
    for row in (fiveg_rows + eutran_rows):
        cell = row.get('NRCellDU') or row.get('EutranCellFDDId')
        colo = row.get('Co-Located Technology Cell')
        if not cell or is_cband_cell(cell) or is_dod_cell(cell):
            continue  # excluded per blueprint comment
        if cell and colo and str(colo).strip() and str(colo).strip().upper() != 'N/A':
            for other in str(colo).split(','):
                other = other.strip()
                if other and not (is_cband_cell(other) or is_dod_cell(other)):
                    colocation.setdefault(cell, set()).add(other)
                    colocation.setdefault(other, set()).add(cell)

    aug_by_cell = {r.get('EutranCellFDDId'): (r.get('AntennaUnitGroup'), r.get('AntennaUnit'), r.get('AntennaSubunit'))
                   for r in antenna_rows if r.get('EutranCellFDDId')}

    results = []
    checked_pairs = set()
    for cell, group in colocation.items():
        for other in group:
            pair = tuple(sorted((cell, other)))
            if pair in checked_pairs or cell == other:
                continue
            checked_pairs.add(pair)
            a1, a2 = aug_by_cell.get(cell), aug_by_cell.get(other)
            if not a1 or not a2:
                continue
            same = a1 == a2
            band1, _ = band_label(cell)
            band2, _ = band_label(other)
            # Band FAMILY only - AWS vs PCS - ignoring LTE/5G generation
            # entirely. Strip both the trailing carrier number AND the '5G_'
            # prefix, so 'PCS_1' (LTE) and '5G_PCS_1' both reduce to 'PCS'.
            # Confirmed: same band across LTE/5G (PCS+PCS, AWS+AWS) is
            # allowed to share; only a genuine AWS<->PCS crossing (in ANY
            # tech combination - LTE/LTE, LTE/5G, 5G/5G) must be unique.
            def _band_family(label):
                if not label:
                    return None
                stripped = re.sub(r'_\d+$', '', label)
                return re.sub(r'^5G_', '', stripped)
            fam1 = _band_family(band1)
            fam2 = _band_family(band2)
            aws_pcs_pair = (fam1 != fam2) and fam1 in ('AWS', 'PCS') and fam2 in ('AWS', 'PCS')
            rrus = {rru_by_cell.get(cell, ''), rru_by_cell.get(other, '')}
            # Name the specific radio that triggers the exception rather than
            # lumping them together - an engineer reading the verdict needs to
            # know which radio is involved. If both appear across the pair,
            # both are named.
            trigger_models = sorted({m for m in ('4890', '8843') if any(m in r for r in rrus)})
            uses_4890_or_8843 = bool(trigger_models)
            # Exception (confirmed): whenever a pair spans DIFFERENT AWS/PCS
            # bands and involves EITHER a 4890 or 8843 radio on either cell,
            # the two sectors must use DIFFERENT antenna ports. Same-band
            # multi-carrier pairs (e.g. AWS_1/AWS_1 on 2A_1 vs 2A_2) are not
            # this case - they're expected to share, like everything else.
            exception_applies = aws_pcs_pair and uses_4890_or_8843

            if exception_applies:
                radio_label = '/'.join(trigger_models)
                status = 'MATCH' if not same else 'MISMATCH'
                verdict = (f'Unique - {radio_label} Radio' if not same
                           else f'Not Unique - {radio_label} Radio')
            else:
                status = 'MATCH' if same else 'MISMATCH'
                verdict = 'shared' if same else 'Not shared'

            results.append({'rule': '#27/#34', 'node': node_id, 'cell': f'{cell} / {other}',
                             'status': status, 'verdict': verdict,
                             'aug_au_asu_1': a1, 'aug_au_asu_2': a2,
                             'note': verdict})
    return results


def check_dss_pre_existing(node_id, log_text, ciq_wb):
    """Blueprint #35 'pre existing DSS'. Warns when a Pre cell already has
    DSS active (non-zero essScLocalId AND essScPairId on its SectorCarrier
    or NRSectorCarrier), naming the band(s) it's active on.

    One combined line per node ('Pre existing DSS on: <LTE bands> | <5G
    bands>'), not one INFO row per cell — extract_dss_status() covers both
    SectorCarrier (LTE) and NRSectorCarrier (5G) already, so an active node
    can have bands on both sides at once.

    This was previously listed in run_validation.py's unavailable_notes as
    'no DSS signal found in Pre kget-all logs'. That note was wrong: the
    'get . essScLocalId' / 'get . essScPairId' commands carry it directly
    (confirmed against a real log — HXL04147's SectorCarrier=7_3/8_3/9_3
    report non-zero on both and correspond to that site's real DSS cells).
    pre_extract.extract_dss_status() does the extraction."""
    if not log_text:
        return [{'rule': '#35', 'node': node_id, 'cell': '-', 'status': 'SKIPPED',
                 'note': 'No Pre log for this node - DSS state unknown.'}]
    dss = pe.extract_dss_status(log_text)
    active = sorted(c for c, on in dss.items() if on)
    if not active:
        return [{'rule': '#35', 'node': node_id, 'cell': '-', 'status': 'MATCH',
                 'note': 'No pre existing DSS.'}]
    lte_bands = sorted({b for b in (band_label(c)[0] for c in active if not is_5g_cell(c)) if b})
    nr_bands = sorted({b for b in (band_label(c)[0] for c in active if is_5g_cell(c)) if b})
    sides = [', '.join(lte_bands) if lte_bands else '-', ', '.join(nr_bands) if nr_bands else '-']
    return [{'rule': '#35', 'node': node_id, 'cell': '-', 'status': 'INFO',
             'note': f"Pre existing DSS on: {sides[0]} | {sides[1]}"}]


def check_wcs_slim(node_id, log_text):
    """Row 94's WCS Slim half: every WCS-band LTE cell ('_3[A-F]_'
    sectors, band_label()'s own WCS marker) should point its ailgRef at
    the AirIfLoadProfile=WCS_Slim profile. pe.extract_ailg_ref() reads
    the profile id straight off each cell's own ailgRef line (confirmed
    on real logs the DN suffix always equals that profile's own
    airIfLoadProfileId, so no separate MO lookup is needed).

    Three fixed verdicts, per confirmed decision:
      no WCS cells at all         -> NA,       'No WCS sectors found.'
      every WCS cell = WCS_Slim   -> MATCH,    'AirIfLoadProfile is WCS_Slim for WCS sectors.'
      any WCS cell != WCS_Slim    -> MISMATCH, 'AirIfLoadProfile is non WCS_Slim for WCS sectors.'
    A WCS cell with no ailgRef line at all counts as non-WCS_Slim (not
    silently ignored) - confirmed real case, HXL00147's three WCS cells
    all resolve to AirIfLoadProfile=4, not WCS_Slim."""
    if not log_text:
        return [{'rule': '#WCS', 'node': node_id, 'cell': '-', 'status': 'SKIPPED',
                 'note': 'No Pre log for this node - WCS Slim state unknown.'}]
    ailg = pe.extract_ailg_ref(log_text)
    wcs_vals = {c: v for c, v in ailg.items() if band_label(c)[0] == 'WCS'}
    if not wcs_vals:
        return [{'rule': '#WCS', 'node': node_id, 'cell': '-', 'status': 'NA',
                 'note': 'No WCS sectors found.'}]
    all_slim = all(str(v or '').strip().upper() == 'WCS_SLIM' for v in wcs_vals.values())
    if all_slim:
        return [{'rule': '#WCS', 'node': node_id, 'cell': ', '.join(sorted(wcs_vals)), 'status': 'MATCH',
                 'note': 'AirIfLoadProfile is WCS_Slim for WCS sectors.'}]
    bad = sorted(c for c, v in wcs_vals.items() if str(v or '').strip().upper() != 'WCS_SLIM')
    return [{'rule': '#WCS', 'node': node_id, 'cell': ', '.join(bad), 'status': 'MISMATCH',
             'note': 'AirIfLoadProfile is non WCS_Slim for WCS sectors.'}]


def check_sector_id_4890(node_id, ciq_wb, e_name=None):
    """Blueprint #22 'Check for sectorID for 4890 Radio Type. "_s" should
    not be present'. CIQ-side check: any eUtran Parameters row whose RRU
    type is a 4890 and whose sectorId carries an '_s' suffix is flagged.

    Scoped by CELL-NAME PREFIX rather than an 'eNodeB Name' column — that
    column does not exist on this sheet, so filtering on it silently matched
    every row and produced one duplicate set of results per node."""
    out = []
    prefix = str(e_name or node_id).strip().upper()
    for r in _rows(ciq_wb, 'eUtran Parameters'):
        cell = r.get('EutranCellFDDId')
        if not cell:
            continue
        if str(cell).split('_')[0].strip().upper() != prefix:
            continue
        rru = str(r.get('RRU type') or '').upper()
        if '4890' not in rru:
            continue
        sector_id = str(r.get('sectorId') or '').strip()
        if re.search(r'_S\b|_S$', sector_id, re.I):
            out.append({'rule': '#22', 'node': node_id, 'cell': cell, 'status': 'MISMATCH',
                        'note': f'sectorId "{sector_id}" contains "_s" on a 4890 radio - remove the _s suffix.'})
        else:
            out.append({'rule': '#22', 'node': node_id, 'cell': cell, 'status': 'MATCH',
                        'note': f'sectorId "{sector_id}" OK for 4890 radio.'})
    return out


def check_losses_vs_antenna_sectors(node_id, ciq_wb, e_name=None, g_name=None):
    """'Checks if the sectors present in the Antenna info & Losses and
    delays tab' — both CIQ sheets are keyed by EutranCellFDDId (confirmed
    real CIQ), and every LTE sector should appear in both; a sector present
    in one but missing from the other is a flag.

    ALSO: 'Losses and Delays' is not LTE-only despite its own column being
    named EutranCellFDDId — confirmed real CIQ data: non-AIR-radio 5G
    cells (CBAND/DOD/DOD_BWE on a discrete RRU, not an integrated AIR
    radio) DO appear there under their own NRCellDU-style name in that
    same column. AIR-radio CBAND/DOD/DOD_BWE cells (integrated antenna,
    no separate feeder/connector losses to declare) are correctly ABSENT
    — confirmed on a real site where every AIR6472 N077 cell has no row
    here at all while non-AIR N005 cells do. So every non-AIR CBAND/DOD/
    DOD_BWE cell must be present in Losses and Delays; an AIR-radio one
    is exempt, not flagged missing."""
    if "Antenna Information" not in ciq_wb.sheetnames or "Losses and Delays" not in ciq_wb.sheetnames:
        return [{'rule': None, 'node': node_id, 'cell': '-', 'status': 'SKIPPED',
                 'note': 'Antenna Information or Losses and Delays sheet missing from this CIQ.'}]
    prefix = str(e_name or node_id).strip().upper()

    def _cells(sheet):
        out = set()
        for r in _rows(ciq_wb, sheet):
            cell = r.get('EutranCellFDDId')
            if cell and str(cell).split('_')[0].strip().upper() == prefix:
                out.add(str(cell).strip())
        return out

    antenna_cells = _cells("Antenna Information")
    losses_cells = _cells("Losses and Delays")

    out = []
    if antenna_cells or losses_cells:
        for cell in sorted(antenna_cells - losses_cells):
            out.append({'rule': None, 'node': node_id, 'cell': cell, 'status': 'MISMATCH',
                        'note': 'In Antenna Information but missing from Losses and Delays.'})
        for cell in sorted(losses_cells - antenna_cells):
            out.append({'rule': None, 'node': node_id, 'cell': cell, 'status': 'MISMATCH',
                        'note': 'In Losses and Delays but missing from Antenna Information.'})
        if not (antenna_cells - losses_cells) and not (losses_cells - antenna_cells):
            out.append({'rule': None, 'node': node_id, 'cell': '-', 'status': 'MATCH',
                        'note': f'{len(antenna_cells)} sector(s) present on both sheets.'})

    if g_name:
        non_air_5g_cells = []
        for row in _rows(ciq_wb, '5G Info'):
            cell = row.get('NRCellDU')
            if not (cell and str(cell).startswith(g_name)):
                continue
            if not (is_dod_cell(cell) or is_cband_cell(cell)):
                continue
            if 'AIR' in str(row.get('RRU Type', '')).upper():
                continue
            non_air_5g_cells.append(cell)
        missing_5g = [c for c in non_air_5g_cells if c not in losses_cells]
        if missing_5g:
            for cell in sorted(missing_5g):
                label, sector = band_label(cell)
                where = f"{label or 'unknown band'} {sector or 'unknown sector'}"
                out.append({'rule': None, 'node': node_id, 'cell': cell, 'label': label, 'sector': sector,
                            'status': 'MISMATCH', 'note': f'{where} missing in the Losses and Delays tab.'})
        elif non_air_5g_cells:
            out.append({'rule': None, 'node': node_id, 'cell': '-', 'status': 'MATCH',
                        'note': 'All sectors present in the Losses and Delays tab.'})

    if not out:
        out.append({'rule': None, 'node': node_id, 'cell': '-', 'status': 'SKIPPED',
                    'note': 'No LTE sectors found for this node on either sheet.'})
    return out


def check_carrier_progression(node_id, ciq_wb, e_name=None, g_name=None):
    """Carrier progression — within a node and technology, no two BANDS may
    share the same Carrier value.

    Confirmed against a real CIQ: Carrier reads like '1C', '3C', '5C', and
    a bandwidth-expansion carrier is written '3C BWE'. That BWE suffix is
    exactly why the comparison is on the FULL carrier string rather than a
    normalised stem — on a real site AWS Band 4 carries '3C' while AWS-3
    Band 66 carries '3C BWE', which is legal precisely because they are
    different carrier designations. Stripping 'BWE' to compare stems would
    flag that correct configuration as a clash.

    Scope is per node and per technology (the LTE and 5G sheets are checked
    separately): the same carrier label legitimately appears on an LTE node
    and a 5G node of the same site.

    A violation is one Carrier value mapped to two or more distinct bands."""
    out = []
    sheets = (('eUtran Parameters', 'EutranCellFDDId', 'eUTRA operating band', e_name, 'LTE'),
              ('5G Info', 'NRCellDU', 'Operating Band', g_name, '5G'))
    for sheet, cell_col, band_col, prefix, tech in sheets:
        bands_by_carrier = {}
        for row in _rows(ciq_wb, sheet):
            cell = row.get(cell_col)
            if not cell or (prefix and not str(cell).startswith(prefix)):
                continue
            carrier = str(row.get('Carrier') or '').strip().strip("'\"").strip()
            band = str(row.get(band_col) or '').strip()
            if not carrier or not band:
                continue
            bands_by_carrier.setdefault(carrier.upper(), {}).setdefault(band, []).append(str(cell))
        for carrier, bands in sorted(bands_by_carrier.items()):
            if len(bands) < 2:
                continue
            detail = '; '.join(f"{b} ({', '.join(sorted(cells))})" for b, cells in sorted(bands.items()))
            out.append({'rule': '#CARRIER', 'node': node_id,
                        'cell': ', '.join(sorted(c for cs in bands.values() for c in cs))[:120],
                        'status': 'MISMATCH',
                        'note': f"{tech} Carrier '{carrier}' is used by {len(bands)} different bands: {detail}."})
    if not out:
        out.append({'rule': '#CARRIER', 'node': node_id, 'cell': '-', 'status': 'MATCH',
                    'note': 'Each carrier maps to a single band.'})
    return out


def check_tilt_integer(node_id, ciq_wb, e_name=None, g_name=None):
    """Antenna tilt values in the CIQ must be whole numbers.

    Covers both sheets and every tilt column they carry:
      eUtran Parameters - mechanicalAntennaTilt, electricalAntennaTilt,
                          electricalAntennaTilt_2
      5G Info           - 'Mechanical AntennaTilt', 'Electrical Tilt'

    Values are read as text and may arrive with Excel's leading-apostrophe
    text marker (confirmed real CIQ: 5G 'Electrical Tilt' reads as "'0'"),
    which is stripped before parsing - without that every 5G row would
    false-flag. A blank cell is not a failure (nothing was specified); a
    value with a fractional part (2.5) is. A trailing '.0' is a whole
    number and passes."""
    out = []
    sheets = (('eUtran Parameters', 'EutranCellFDDId', e_name,
               ('mechanicalAntennaTilt', 'electricalAntennaTilt', 'electricalAntennaTilt_2')),
              ('5G Info', 'NRCellDU', g_name,
               ('Mechanical AntennaTilt', 'Electrical Tilt')))
    for sheet, cell_col, prefix, cols in sheets:
        for row in _rows(ciq_wb, sheet):
            cell = row.get(cell_col)
            if not cell or (prefix and not str(cell).startswith(prefix)):
                continue
            for col in cols:
                raw = row.get(col)
                if raw is None:
                    continue
                val = str(raw).strip().strip("'\"").strip()
                if not val:
                    continue
                try:
                    num = float(val)
                except ValueError:
                    out.append({'rule': '#TILT', 'node': node_id, 'cell': cell, 'status': 'MISMATCH',
                                'note': f"{col}='{val}' is not a number."})
                    continue
                if num != int(num):
                    out.append({'rule': '#TILT', 'node': node_id, 'cell': cell, 'status': 'MISMATCH',
                                'note': f"{col}='{val}' must be a whole number."})
    if not out:
        out.append({'rule': '#TILT', 'node': node_id, 'cell': '-', 'status': 'MATCH',
                    'note': 'All tilt values are whole numbers.'})
    return out


def check_rfbranch_per_aug(node_id, log_text):
    """Blueprint #34 'The RF branch number should not exceed 24 for each
    AUG'. Counts DISTINCT RfBranch numbers per AntennaUnitGroup across every
    SectorCarrier's rfBranchTxRef/rfBranchRxRef in the Pre log, via
    pre_extract.extract_rf_branch_refs()'s own source data.

    Pre-side rather than CIQ-side because the AntennaUnitGroup->RfBranch
    mapping only exists in the Pre kget-all log ('hget sector rfbranch');
    the CIQ has no equivalent per-AUG branch listing."""
    if not log_text:
        return [{'rule': '#34', 'node': node_id, 'cell': '-', 'status': 'SKIPPED',
                 'note': 'No Pre log for this node - AUG/RfBranch mapping unavailable.'}]
    from log_parser import get_command_block
    block = get_command_block(log_text, 'sector rfbranch') or ''
    per_aug = {}
    for m in re.finditer(r'AntennaUnitGroup=(\d+),RfBranch=(\d+)', block):
        per_aug.setdefault(m.group(1), set()).add(int(m.group(2)))
    if not per_aug:
        return [{'rule': '#34', 'node': node_id, 'cell': '-', 'status': 'SKIPPED',
                 'note': 'No AntennaUnitGroup/RfBranch references in this log (AAS/AIR node).'}]
    out = []
    for aug, branches in sorted(per_aug.items(), key=lambda kv: int(kv[0])):
        n = len(branches)
        status = 'MISMATCH' if n > 24 else 'MATCH'
        note = (f'AntennaUnitGroup={aug} has {n} RfBranches (limit 24).'
                if status == 'MISMATCH' else f'AntennaUnitGroup={aug}: {n} RfBranches.')
        out.append({'rule': '#34', 'node': node_id, 'cell': f'AntennaUnitGroup={aug}',
                    'status': status, 'note': note})
    return out
