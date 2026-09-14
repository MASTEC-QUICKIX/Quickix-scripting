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
    """Blueprint section 13 'Radio Type verification' (#6). Cells | Pre |
    CIQ | RFDS | Match.

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
        ciq_rru = str(row.get('RRU type', '')).strip()
        rfds_rrh = cell_details.get(cell, {}).get('rrh', 'NOT FOUND' if rfds_pages is not None else 'NOT CHECKED')
        pre_val = _pre_for(cell)
        rru_token = ciq_rru.split()[-1] if ciq_rru else ''
        rfds_match = bool(rru_token) and rru_token in rfds_rrh
        pre_match = pre_val == 'NOT AVAILABLE' or pre_val == ciq_rru
        match = rfds_match and pre_match
        notes = []
        if not rfds_match:
            notes.append('RFDS does not confirm CIQ RRU type.')
        if not pre_match:
            notes.append(f'Radio swap indicated: Pre={pre_val} vs CIQ={ciq_rru}.')
        results.append({'rule': '#6', 'node': node_id, 'cell': cell, 'pre': pre_val,
                         'ciq': ciq_rru, 'rfds': rfds_rrh, 'status': 'MATCH' if match else 'MISMATCH',
                         'note': 'Confirmed.' if match else ' '.join(notes)})

    for row in _rows(ciq_wb, '5G Info'):
        cell = row.get('NRCellDU')
        if not (cell and g_name and str(cell).startswith(g_name)):
            continue
        ciq_rru = str(row.get('RRU Type', '')).strip()
        rfds_rrh = cell_details.get(cell, {}).get('rrh', 'NOT FOUND' if rfds_pages is not None else 'NOT CHECKED')
        pre_val = _pre_for(cell)
        rru_token = ciq_rru.split()[-1] if ciq_rru else ''
        rfds_match = bool(rru_token) and rru_token in rfds_rrh
        pre_match = pre_val == 'NOT AVAILABLE' or pre_val == ciq_rru
        match = rfds_match and pre_match
        notes = []
        if not rfds_match:
            notes.append('RFDS does not confirm CIQ RRU type.')
        if not pre_match:
            notes.append(f'Radio swap indicated: Pre={pre_val} vs CIQ={ciq_rru}.')
        results.append({'rule': '#6', 'node': node_id, 'cell': cell, 'pre': pre_val,
                         'ciq': ciq_rru, 'rfds': rfds_rrh, 'status': 'MATCH' if match else 'MISMATCH',
                         'note': 'Confirmed.' if match else ' '.join(notes)})
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
    cell's real sec_id/TX-RX/Power lives on its SOURCE node's log."""
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
        results.append({'rule': '#21/#22/#32', 'node': node_id, 'cell': cell,
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
            ciq_txrx = pe.parse_rbb_txrx(rbb) or 'NOT FOUND'
            ciq_ri = 'Double' if '_1D' in str(rbb or '') else ('Single' if '_1S' in str(rbb or '') else 'NOT FOUND')
            pre_ri = rilink.get(cell, 'NA')
            mismatches = []
            if pre_ri != 'NA' and ciq_ri != 'NOT FOUND' and pre_ri != ciq_ri:
                mismatches.append(f'RILink Pre={pre_ri} vs CIQ={ciq_ri}')
            results.append({'rule': '#21/#22/#32', 'node': node_id, 'cell': cell,
                             'sec_id': 'NA', 'pre_sec_id': 'NA',
                             'pre_txrx': pre_ri, 'ciq_txrx': f'{ciq_txrx} ({ciq_ri})',
                             'pre_power': 'NA', 'ciq_power': str(row.get('configuredMaxTxPower', '')).strip(),
                             'status': 'MISMATCH' if mismatches else 'MATCH',
                             'note': '; '.join(mismatches) if mismatches else 'RBB Type/RILink (standalone 5G radio).'})
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
    """Rule #10 - mmWave (N260) rachRootSequence must not exceed 137.
    CIQ-only check, no Pre comparison (per blueprint)."""
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
        exceeded = rach_val > 137
        results.append({'rule': '#10', 'node': node_id, 'cell': cell,
                         'status': 'MISMATCH' if exceeded else 'MATCH',
                         'rachRootSequence': rach_val,
                         'note': f'rachRootSequence exceeded 137 for the MMWave sector : {cell}' if exceeded else 'Within limit.'})
    return results


def check_sef_fru(node_id, ciq_wb):
    """Rule #9 - SEF/FRU sharing vs uniqueness, radio-type dependent:
      - 6472 present + both CBAND and DOD (or DOD_BWE) cells -> SEF/FRU
        SHARING is expected/correct (sharing radio by default).
      - 6419/6449 (single-band radios) -> SEF/FRU must be UNIQUE per
        CBAND|DOD cell.
      - 8863/4461/4467 -> RRU FieldReplaceableUnit must start with 'RRU'."""
    fiveg_rows = _rows(ciq_wb, '5G Info')
    cband_dod_rows = [r for r in fiveg_rows if r.get('NRCellDU') and
                       (is_cband_cell(r['NRCellDU']) or is_dod_cell(r['NRCellDU']))]

    results = []
    # group by RRU Type to determine which radio family governs each cell
    for row in cband_dod_rows:
        cell = row.get('NRCellDU')
        rru_type = str(row.get('RRU Type', '')).strip()
        sef = row.get('SectorEquipmentFunction')
        fru = row.get('RRU FieldReplaceableUnit')

        if '6472' in rru_type:
            # sharing expected - just record for cross-cell dup check below, not an error by itself
            results.append({'rule': '#9', 'node': node_id, 'cell': cell, 'status': 'INFO',
                             'rru_type': rru_type, 'sef': sef, 'fru': fru,
                             'note': '6472 radio - CBAND/DOD/DOD_BWE sharing this radio is expected.'})
        elif any(m in rru_type for m in ('6419', '6449')):
            dup = any(other is not row and other.get('SectorEquipmentFunction') == sef
                      for other in cband_dod_rows if is_cband_cell(other.get('NRCellDU')) or is_dod_cell(other.get('NRCellDU')))
            results.append({'rule': '#9', 'node': node_id, 'cell': cell,
                             'status': 'MISMATCH' if dup else 'MATCH',
                             'rru_type': rru_type, 'sef': sef, 'fru': fru,
                             'note': 'SEF/FRU must be unique for single-band radio but is shared.' if dup else 'Unique, as required.'})
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

    # cell-name prefix -> that node's XMU ports
    xmu_ports_by_prefix = {}
    for mm in mm_rows:
        e_name = str(mm.get('eNodeB Name') or '').strip()
        g_name = str(mm.get('gNodeB Name') or '').strip()
        node_ports = _ports_from(enb_by_name.get(e_name)) | _ports_from(gnb_by_name.get(g_name))
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

    shared_radio_group = {}  # cell -> group key, for 6472-sharing cells only
    for row in fiveg_rows:
        rru_type = str(row.get('RRU Type', '')).strip()
        if '6472' in rru_type:
            cell = row.get('NRCellDU')
            sef = row.get('SectorEquipmentFunction')
            if cell and sef:
                shared_radio_group[cell] = sef

    usage = {}
    for row in fiveg_rows:
        cell = row.get('NRCellDU')
        bbu = str(row.get('BB/XMU', '')).strip()
        is_xmu = 'XMU' in bbu.upper()
        for pc in port_cols:
            val = row.get(pc)
            if val is None or str(val).strip() == '':
                continue
            key = (bbu, str(val).strip())
            usage.setdefault(key, []).append(cell)
            if is_xmu:
                xmu_ports.add(str(val).strip())

    # Blueprint's RI port table has TWO port columns: LTE cells use one RI
    # port (second shows NA), 5G cells can use two. Collect each cell's full
    # port list so the second can be rendered.
    ports_by_cell = {}
    for row in fiveg_rows:
        cell = row.get('NRCellDU')
        if not cell:
            continue
        plist = [str(row.get(pc)).strip() for pc in port_cols
                 if row.get(pc) is not None and str(row.get(pc)).strip()]
        ports_by_cell[cell] = plist

    # XMU ports are keyed separately in `usage` (by their own BB/XMU value), so
    # a sector reusing an XMU port never collides there and its own row would
    # read Unique. Collect the conflicting (cell, port) pairs up front so the
    # sector's real row can be marked - previously this only appended an extra
    # MISMATCH row afterwards, leaving the sector's original row saying Unique
    # and the same cell appearing twice with contradictory verdicts.
    xmu_conflicts = {}
    for row in fiveg_rows:
        cell = row.get('NRCellDU')
        bbu = str(row.get('BB/XMU', '')).strip()
        if 'XMU' in bbu.upper():
            continue
        own_xmu_ports = _xmu_ports_for_cell(cell)
        for pc in port_cols:
            val = row.get(pc)
            if val is not None and str(val).strip() in own_xmu_ports:
                xmu_conflicts[(cell, str(val).strip())] = bbu

    results = []
    for (bbu, port), cells in usage.items():
        # if every cell sharing this port belongs to the same 6472 shared-radio
        # group, the sharing is expected (rule #9) - not a violation.
        groups = {shared_radio_group.get(c) for c in cells}
        exempt = len(cells) > 1 and len(groups) == 1 and None not in groups
        for cell in cells:
            _pl = [p for p in ports_by_cell.get(cell, []) if p != port]
            xmu_clash = (cell, port) in xmu_conflicts
            if len(cells) > 1 and not exempt:
                status = 'MISMATCH'
                note = f"Port {port} on {bbu} shared by multiple sectors: {cells}"
            elif xmu_clash:
                status = 'MISMATCH'
                note = f"Port {port} is assigned to an XMU on this node but reused by this sector."
            elif exempt:
                status, note = 'MATCH', 'Port shared as expected (6472 sharing radio, per rule #9).'
            else:
                status, note = 'MATCH', 'Port unique.'
            results.append({'rule': '#11/#26/#27', 'node': node_id, 'cell': cell, 'status': status,
                             'bbu': bbu, 'port': port, 'port2': _pl[0] if _pl else None, 'note': note})

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


def check_cells_vs_rfds(node_id, ciq_wb, rfds_pages, e_name, g_name):
    """Blueprint section 8 'Cells verification' (#6, #18) - every CIQ cell
    (LTE + 5G combined) checked for presence in RFDS. CIQ | RFDS | Match."""
    if rfds_pages is None:
        return [{'rule': '#6/#18', 'node': node_id, 'cell': None, 'status': 'SKIPPED', 'note': 'No RFDS provided.'}]
    import rfds_extract as rf
    rfds_cells = rf.extract_non_rf_inventory_cells(rfds_pages)
    results = []
    for row in _rows(ciq_wb, 'eUtran Parameters'):
        cell = row.get('EutranCellFDDId')
        if cell and e_name and str(cell).startswith(e_name):
            present = cell in rfds_cells
            results.append({'rule': '#6/#18', 'node': node_id, 'cell': cell,
                             'ciq_cell': cell, 'rfds_cell': cell if present else 'NOT FOUND',
                             'status': 'MATCH' if present else 'MISMATCH',
                             'note': 'Match.' if present else 'Not found in RFDS.'})
    for row in _rows(ciq_wb, '5G Info'):
        cell = row.get('NRCellDU')
        if cell and g_name and str(cell).startswith(g_name):
            present = cell in rfds_cells
            results.append({'rule': '#6/#18', 'node': node_id, 'cell': cell,
                             'ciq_cell': cell, 'rfds_cell': cell if present else 'NOT FOUND',
                             'status': 'MATCH' if present else 'MISMATCH',
                             'note': 'Match.' if present else 'Not found in RFDS.'})
    return results


def check_cell_id_vs_rfds(node_id, log_text, ciq_wb, rfds_pages, e_name, g_name, node_logs=None, moved_map=None):
    """Blueprint section 9 'Cell ID verification' (#6, #24) - Cells | Pre |
    CIQ [cellId/cellLocalId] | RFDS [RCN] | Match, LTE + 5G combined. If a
    cell is newly adding, Pre shows 'NA' per the blueprint's own example."""
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
        ciq_id = str(row.get('cellId', '')).strip()
        pre_id = (pre_lte.get(cell) or {}).get('cellId') or 'NA'
        rfds_rcn = cell_details.get(cell, {}).get('rcn', 'NOT FOUND' if rfds_pages is not None else 'NOT CHECKED')
        match = (ciq_id == rfds_rcn) and (pre_id in ('NA',) or pre_id == ciq_id)
        results.append({'rule': '#6/#24', 'node': node_id, 'cell': cell,
                         'pre': pre_id, 'ciq': ciq_id, 'rfds_rcn': rfds_rcn,
                         'status': 'MATCH' if match else 'MISMATCH',
                         'note': 'Match.' if match else f'Pre={pre_id}, CIQ={ciq_id}, RFDS={rfds_rcn}.'})

    parsed = __import__('log_parser').parse_log(log_text) if log_text else []
    pre_5g = pe.extract_5g_sector_params(parsed, log_text) if log_text else {}
    if node_logs and moved_map:
        pre_5g = pe.merge_moved_in_pre(pre_5g, node_logs, moved_map, pe.extract_5g_sector_params_from_text)
    for row in _rows(ciq_wb, '5G Info'):
        cell = row.get('NRCellDU')
        if not (cell and g_name and str(cell).startswith(g_name)):
            continue
        ciq_id = str(row.get('cellLocalId', '')).strip()
        pre_id = (pre_5g.get(cell) or {}).get('cellLocalId') or 'NA'
        rfds_rcn = cell_details.get(cell, {}).get('rcn', 'NOT FOUND' if rfds_pages is not None else 'NOT CHECKED')
        match = (ciq_id == rfds_rcn) and (pre_id in ('NA',) or pre_id == ciq_id)
        results.append({'rule': '#6/#24', 'node': node_id, 'cell': cell,
                         'pre': pre_id, 'ciq': ciq_id, 'rfds_rcn': rfds_rcn,
                         'status': 'MATCH' if match else 'MISMATCH',
                         'note': 'Match.' if match else f'Pre={pre_id}, CIQ={ciq_id}, RFDS={rfds_rcn}.'})
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
        note = 'Confirmed.' if not mismatched else \
            (f"Planned retune ({', '.join(mismatched)}) - verify." if planned else
             f"Mismatch on {', '.join(mismatched)} - no SOW context, check Revision History.")
        results.append({'rule': '#19', 'node': node_id, 'cell': cell, 'status': status, 'note': note, **fields})
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


def check_riport_uniqueness(node_id, enb_row, gnb_row, ciq_wb):
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

    Ports are scoped to a comma-split of 'Co-Located Technology Cell' —
    a cell can be listed for more than one co-located partner (a 3-way
    combine), and the check only requires EVERY other cell sharing that
    exact port to appear somewhere in this cell's own list (not the
    reverse — a real CIQ can leave the list one-directional on one side
    of a pair)."""
    def _cells(sheet, id_col, port_cols, co_col):
        out = []
        for row in _rows(ciq_wb, sheet):
            cid = str(row.get(id_col) or "").strip()
            if not cid or not cid.upper().startswith(str(node_id).upper()):
                continue
            ports = set()
            for pc in port_cols:
                v = str(row.get(pc) or "").strip()
                if v and v.upper() not in ("", "N/A", "NA", "NOT USED"):
                    ports.add(v.upper())
            co = {c.strip().upper() for c in str(row.get(co_col) or "").split(",")
                  if c.strip() and c.strip().upper() not in ("N/A", "NA")}
            out.append({"cell": cid, "ports": ports, "co_located": co})
        return out

    cells = (_cells("eUtran Parameters", "EutranCellFDDId",
                     ["DUS / XMU Port", "DUS / XMU Port Expansion"], "Co-Located Technology Cell")
             + _cells("5G Info", "NRCellDU", ["Port 1", "Port 2", "Port 3", "Port 4"], "Co-Located Technology Cell"))
    if not cells:
        return []

    port_to_cells = {}
    for c in cells:
        for p in c["ports"]:
            port_to_cells.setdefault(p, []).append(c)

    # Rule #2: ports this node's own 1st/2nd XMU declares are reserved
    # outright, regardless of Co-Located Technology Cell.
    xmu_ports = set()
    for row in (r for r in (enb_row, gnb_row) if r is not None):
        for which in ("1st", "2nd"):
            if str(row.get(f"{which} XMU", "")).strip().upper() != "YES":
                continue
            for i in (1, 2, 3):
                v = str(row.get(f"{which} XMU Port {i}") or "").strip()
                if v and v.upper() not in ("", "N/A", "NA", "NOT USED"):
                    xmu_ports.add(v.upper())

    out = []
    for port, group in port_to_cells.items():
        if port in xmu_ports:
            out.append({"rule": "#67", "node": node_id, "cell": port, "status": "MISMATCH",
                        "note": f"Port {port} is declared under this node's XMU but also assigned to: "
                                f"{', '.join(sorted(c['cell'] for c in group))}"})
            continue
        if len(group) < 2:
            out.append({"rule": "#67", "node": node_id, "cell": port, "status": "MATCH", "note": "Unique."})
            continue
        bad = [c for c in group if not all(other["cell"] in c["co_located"]
                                            for other in group if other is not c)]
        if bad:
            out.append({"rule": "#67", "node": node_id, "cell": port, "status": "MISMATCH",
                        "note": f"Port {port} reused by non-co-located cells: "
                                f"{', '.join(sorted(c['cell'] for c in group))}"})
        else:
            out.append({"rule": "#67", "node": node_id, "cell": port, "status": "MATCH",
                        "note": f"Port {port} shared by confirmed co-located cells: "
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
    declared = False
    for row in rows:
        du_type = du_type or str(row.get('DU type') or row.get('1st DU type') or '').strip()
        for which in ('1st', '2nd'):
            if str(row.get(f'{which} XMU', '')).strip().upper() != 'YES':
                continue
            declared = True
            for i in (1, 2, 3):
                v = row.get(f'{which} XMU Port {i}')
                if v is not None and str(v).strip().upper() not in ('', 'N/A', 'NA', 'NOT USED'):
                    xmu_ports.add(str(v).strip())
    if not declared:
        return []

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

    unique = not used_elsewhere
    return [{'rule': '#11/#25', 'node': node_id, 'cell': node_id,
              'du_type': du_type, 'xmu': 'Yes', 'xmu_ports': ', '.join(sorted(xmu_ports)) or 'NOT USED',
              'status': 'MATCH' if unique else 'MISMATCH',
              'note': 'Unique.' if unique else f'XMU ports reused elsewhere: {sorted(used_elsewhere)}'}]


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
        label, _ = band_label(cell)
        by_band.setdefault(label, []).append({'cell': cell, 'group': group, 'sub': sub, 'pci': pci, 'ciq_pci': ciq_pci})

    for label, cells in by_band.items():
        pci_counts = {}
        for c in cells:
            pci_counts.setdefault(c['pci'], []).append(c['cell'])
        for c in cells:
            dup = len(pci_counts[c['pci']]) > 1
            mismatch_calc = str(c['pci']) != c['ciq_pci']
            status = 'MISMATCH' if (dup or mismatch_calc) else 'MATCH'
            notes = []
            if dup:
                notes.append(f"PCI {c['pci']} shared with {[x for x in pci_counts[c['pci']] if x != c['cell']]}")
            if mismatch_calc:
                notes.append(f"Computed PCI {c['pci']} != CIQ PCI {c['ciq_pci']}")
            results.append({'rule': '#23', 'node': node_id, 'cell': c['cell'],
                             'group': c['group'], 'sub': c['sub'], 'pci': c['pci'],
                             'status': status, 'note': '; '.join(notes) if notes else 'Unique.'})
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
        label, _ = band_label(cell)
        by_band.setdefault(label, []).append({'cell': cell, 'nrpci': str(nrpci).strip()})

    for label, cells in by_band.items():
        counts = {}
        for c in cells:
            counts.setdefault(c['nrpci'], []).append(c['cell'])
        for c in cells:
            dup = len(counts[c['nrpci']]) > 1
            results.append({'rule': '#23', 'node': node_id, 'cell': c['cell'], 'nrpci': c['nrpci'],
                             'status': 'MISMATCH' if dup else 'MATCH',
                             'note': f"nRPCI {c['nrpci']} shared with {[x for x in counts[c['nrpci']] if x != c['cell']]}" if dup else 'Unique.'})
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
    DSS active (non-zero essScLocalId AND essScPairId on its SectorCarrier),
    naming the LTE band it's active on.

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
                 'note': 'No pre-existing DSS on this node.'}]
    out = []
    for cell in active:
        band, _ = band_label(cell)
        out.append({'rule': '#35', 'node': node_id, 'cell': cell, 'status': 'INFO',
                    'note': f'Pre existing DSS Activated on the {band or "unknown band"}'})
    return out


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


def check_losses_vs_antenna_sectors(node_id, ciq_wb, e_name=None):
    """'Checks if the sectors present in the Antenna info & Losses and
    delays tab' — both CIQ sheets are keyed by EutranCellFDDId (confirmed
    real CIQ), and every LTE sector should appear in both; a sector present
    in one but missing from the other is a flag."""
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
    if not antenna_cells and not losses_cells:
        return [{'rule': None, 'node': node_id, 'cell': '-', 'status': 'SKIPPED',
                 'note': 'No LTE sectors found for this node on either sheet.'}]
    out = []
    for cell in sorted(antenna_cells - losses_cells):
        out.append({'rule': None, 'node': node_id, 'cell': cell, 'status': 'MISMATCH',
                    'note': 'In Antenna Information but missing from Losses and Delays.'})
    for cell in sorted(losses_cells - antenna_cells):
        out.append({'rule': None, 'node': node_id, 'cell': cell, 'status': 'MISMATCH',
                    'note': 'In Losses and Delays but missing from Antenna Information.'})
    if not out:
        out.append({'rule': None, 'node': node_id, 'cell': '-', 'status': 'MATCH',
                    'note': f'{len(antenna_cells)} sector(s) present on both sheets.'})
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
