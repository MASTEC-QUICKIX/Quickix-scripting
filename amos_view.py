"""
Pre kget-all log -> flat per-node/per-cell tables, mirroring QUICKIX's
embedded AMOS module (Node Summary / LTE Cells / NR Cells tables).

Reuses the confirmed extraction primitives in pre_extract.py and the two
private sector-config helpers in checks_sector.py (same functions the
rule-based checks already use) rather than re-parsing the log text with new
regexes — the goal here is a different VIEW of already-confirmed data, not
new extraction logic.

One field the HTML tool shows is NOT available from this project's Pre
kget-all commands, and is labelled as such rather than guessed:
    - Pre-existing DSS (no essScPairId/essScLocalId in these hget commands)

PTP status IS available (confirmed against real HXL00147/HXL04147/
HXIN090147F logs, see ptp_status() below) — the docstring note claiming
otherwise in an earlier version of this file was wrong.
"""
import re

import band_labels as bl
import checks_sector as cs
import pre_cell_inventory as pci
import pre_extract as pe


def _has_amf_signal(text):
    """Best-effort proxy for the HTML tool's TermPointToAmf presence check —
    a 5G core AMF termination point reference anywhere in the log."""
    return bool(text) and bool(re.search(r'TermPointToAmf', text, re.I))


def sa_nsa_status(text, nr_tac_by_cell):
    """Same rule as QUICKIX's findSaNsaStatus(): AMF present AND at least one
    7-digit nRTAC -> SA, else NSA."""
    has_7digit = any(str(v or '').isdigit() and len(str(v)) == 7 for v in nr_tac_by_cell.values())
    return "SA" if (_has_amf_signal(text) and has_7digit) else "NSA"


def ptp_status(text):
    """Enabled / Disabled / Not Present, from the 'st' command's row for
    Transport=1,Ptp=1,... (confirmed against real logs: HXL00147 and
    HXL04147 both have a 'Transport=1,Ptp=1,BoundaryOrdinaryClock=1,
    PtpBcOcPort=1' row with an Op. State column; HXIN090147F — a pure AAS/AIR
    5G node — has no Ptp=1 MO at all, i.e. genuinely Not Present, not a
    missing-data gap)."""
    if not text:
        return "Not Present"
    m = re.search(
        r'^\s*\d+\s+\d+\s*\([A-Z]+\)\s+(\d+)\s*\(([A-Z]+)\)\s+Transport=1,Ptp=1\b',
        text, re.M,
    )
    if not m:
        return "Not Present"
    return "Enabled" if m.group(2).upper() == "ENABLED" else "Disabled"


def node_secondary_name(node_id, text):
    """The node's OTHER identity when it's a dual-tech (MMBB) node — e.g.
    HXL04147's own log lists 5G cells prefixed 'HXIN010147_...', distinct
    from its own LTE node id. Same convention pre_post_config.py's
    build_pre_post_config_text() already uses for Pre-side MMBB labelling;
    this is the Node Summary table's own use of the same signal. Returns
    None when no distinct secondary prefix is found (LTE-only or 5G-only
    node, or a dual-tech node whose 5G cells share the same prefix)."""
    if not text:
        return None
    cells = pci.extract_pre_cells_for_node(text)
    prefixes = set()
    for c in cells:
        m = re.match(r'^([A-Za-z0-9]+?)_', c)
        if m and m.group(1).upper() != str(node_id).strip().upper():
            prefixes.add(m.group(1))
    return sorted(prefixes)[0] if prefixes else None


def build_node_summary(node_id, text):
    sw = pe.extract_sw_version(text) or {}
    lte_cells = pci.extract_pre_cells_for_node(text)
    nr_tac = pe.extract_nr_tac(text)
    has_lte = any(not bl.is_5g_cell(c) for c in lte_cells)
    has_nr = any(bl.is_5g_cell(c) for c in lte_cells)
    node_type = "LTE + 5G" if (has_lte and has_nr) else "LTE Only" if has_lte else "5G Only" if has_nr else "Unknown"
    boards = pe.extract_hardware(_parsed_cache(text))['boards']
    board_model = pe.model_token(boards[0]['model']) if boards else "NOT FOUND"
    secondary = node_secondary_name(node_id, text)
    node_label = f"{node_id} / {secondary}" if secondary else node_id
    return {
        "node": node_label,
        "sw_version": sw.get("sw_version", "NOT FOUND"),
        "sw_package": board_model,
        "type": node_type,
        "ptp_status": ptp_status(text),
        "sa_nsa_status": sa_nsa_status(text, nr_tac) if has_nr else "LTE Only",
        "vonr_status": "VoNR Active" if pe.extract_vonr_status(text) else "Not Active",
    }


_parse_log_cache = {}


def _parsed_cache(text):
    """parse_log() is somewhat expensive and build_node_summary() only needs
    it for extract_hardware(); avoid re-parsing the same text if this node's
    summary is ever built twice in one run (defensive — cheap either way,
    but there is no reason to pay for it twice)."""
    key = id(text)
    if key not in _parse_log_cache:
        from log_parser import parse_log
        _parse_log_cache[key] = parse_log(text)
    return _parse_log_cache[key]


def build_lte_cell_rows(node_id, text):
    """Node, Cell, Sector Carrier, RRUs, Radio type, Sharing Radio, TX, RX,
    RFBRANCHTXREF, RFBRANCHRXREF, SEF RFBRANCHES, Pre Existing DSS — matches
    QUICKIX HTML's LTE Cells table column-for-column."""
    cells = sorted(c for c in pci.extract_pre_cells_for_node(text) if not bl.is_5g_cell(c))
    radio_by_cell = pe.extract_cell_to_radio(text)
    fru_by_cell = pe.extract_cell_to_fru(text)
    cfg_by_cell = cs._extract_sector_config(text)
    branch_refs = pe.extract_rf_branch_refs(text)
    sector_carrier_by_cell = _extract_sector_carrier_numbers(text)
    dss_by_cell = pe.extract_dss_status(text)
    rilink_by_cell = pe.extract_cell_to_rilink_detail(text, fru_by_cell)
    ailg_by_cell = pe.extract_ailg_ref(text)

    # Sharing radio: same RRU + same band serving DIFFERENT sector letters —
    # same definition as QUICKIX's radioBandMap (cross-sector share only; a
    # single radio carrying multiple carriers on the SAME sector is normal).
    radio_band_map = {}
    for cell in cells:
        band, sector = bl.band_label(cell)
        rru = fru_by_cell.get(cell)
        if not (band and sector and rru):
            continue
        key = (rru, band)
        radio_band_map.setdefault(key, {}).setdefault(sector, set()).add(cell)

    rows = []
    for cell in cells:
        band, sector = bl.band_label(cell)
        fru = fru_by_cell.get(cell, "-")
        radio_model = pe._short_radio_name(radio_by_cell.get(cell)) or "-"
        cfg = cfg_by_cell.get(cell)
        sharing = "No"
        if band and sector and fru != "-":
            by_sector = radio_band_map.get((fru, band), {})
            shared = {c for sec, cs_ in by_sector.items() if sec != sector for c in cs_}
            if shared:
                sharing = ", ".join(sorted(shared))
        refs = branch_refs.get(cell, {})
        rilink = rilink_by_cell.get(cell, {})
        # WCS is the only LTE band this profile check applies to (digit
        # '3' sectors, e.g. '_3A_1' - band_label()'s own WCS marker).
        # Non-WCS cells show '-' (not applicable), never a fabricated
        # value; a WCS cell whose kget-all block has no ailgRef line at
        # all is shown as 'NOT FOUND' rather than silently blank, so a
        # real extraction gap isn't mistaken for 'not applicable'.
        is_wcs = band == "WCS"
        ailg_val = ailg_by_cell.get(cell) if is_wcs else None
        rows.append({
            "node": node_id, "cell": cell,
            "sector_carrier": sector_carrier_by_cell.get(cell, "-"),
            "rru": fru, "radio_type": radio_model, "sharing_radio": sharing,
            "tx": cfg["tx"] if cfg else "-", "rx": cfg["rx"] if cfg else "-",
            "rfbranch_tx_ref": refs.get("tx_ref") or "-", "rfbranch_rx_ref": refs.get("rx_ref") or "-",
            "sef_rfbranches": refs.get("sef_branches") or "-",
            "pre_existing_dss": "DSS Active" if dss_by_cell.get(cell) else "No",
            "rilink_id": rilink.get("rilink_id") or "-", "rilink_port": rilink.get("rilink_port") or "-",
            "air_if_load_profile": (ailg_val or "NOT FOUND") if is_wcs else "-",
        })
    return rows


def _extract_sector_carrier_numbers(text):
    """Cell -> the raw SectorCarrier/NRSectorCarrier index shown in QUICKIX's
    'Sector Carries' column (e.g. 'SectorCarrier=7_1' -> '7_1',
    'SectorCarrier=10' -> '10'). Reuses the same reservedBy cross-reference
    block extract_rf_branch_refs() already parses, just keeping the numeric
    suffix instead of the branch refs."""
    import re
    from log_parser import get_command_block
    id_block = get_command_block(text, 'SectorCarrier=|SectorEquipmentFunction') or ''
    cell_to_num = {}
    for m in re.finditer(r'^(?:SectorCarrier|NRSectorCarrier)=(\S+)\s+.*$', id_block, re.M):
        num, rest = m.group(1), m.group(0)
        for cell in re.findall(r'(?:EUtranCellFDD|NRCellDU)=(\S+)', rest):
            cell_to_num[cell] = num
    return cell_to_num


def build_nr_cell_rows(node_id, text):
    """Node, Cell, RRUs, TX, RX, SEF RFBRANCHES — matches QUICKIX HTML's
    5G NR Cells table. TX/RX use noOfTxAntennas/noOfRxAntennas (the
    configured-max fields, via _extract_sector_config_5g) per explicit
    instruction — NOT noOfUsedTxAntennas/noOfUsedRxAntennas.

    Note: on AAS/massive-MIMO radios these configured-max fields report 0
    for every carrier on the node regardless of whether that carrier is
    actually healthy or faulted (confirmed: FSL00877/FSL02877/FSL04877 all
    show 0/0 here even though FSL02877's carrier is the only one of the
    three with a real resource-activation fault — the used-antenna fields
    are what actually distinguish healthy from faulted there). This
    function intentionally does not surface that distinction; it shows
    exactly what was asked for."""
    cells = sorted(c for c in pci.extract_pre_cells_for_node(text) if bl.is_5g_cell(c))
    fru_by_cell = pe.extract_cell_to_fru(text)
    cfg_by_cell = cs._extract_sector_config_5g(text)
    branch_refs = pe.extract_rf_branch_refs(text)
    rilink_by_cell = pe.extract_cell_to_rilink_detail(text, fru_by_cell)

    rows = []
    for cell in cells:
        cfg = cfg_by_cell.get(cell)
        refs = branch_refs.get(cell, {})
        rilink = rilink_by_cell.get(cell, {})
        rows.append({
            "node": node_id, "cell": cell,
            "rru": fru_by_cell.get(cell, "-"),
            "tx": cfg["tx"] if cfg else "-", "rx": cfg["rx"] if cfg else "-",
            "sef_rfbranches": refs.get("sef_branches") or "-",
            "rilink_id": rilink.get("rilink_id") or "-", "rilink_port": rilink.get("rilink_port") or "-",
        })
    return rows


def build_amos_tables(node_logs):
    """node_logs: {node_id: log_text}. Returns (summary_rows, lte_rows, nr_rows)."""
    summary_rows, lte_rows, nr_rows = [], [], []
    for node_id, text in node_logs.items():
        if not text:
            continue
        summary_rows.append(build_node_summary(node_id, text))
        lte_rows += build_lte_cell_rows(node_id, text)
        nr_rows += build_nr_cell_rows(node_id, text)
    return summary_rows, lte_rows, nr_rows
