"""
Node-level field extraction from a parsed Pre kget-all / hget log
(see log_parser.py for the underlying table parser).

Each function takes `parsed` = log_parser.parse_log(text) and returns either
a value, or None if the node's log doesn't contain that data (e.g. a newly
built node has no Pre kget-all at all, so callers should treat "no log
provided for this node" as a distinct case from "log present but field
missing").
"""
import re

import ciq_edp_reader as cer

from log_parser import find_command, all_rows, get_command_block

_SW_VERSION_RE = re.compile(
    r'Current SwVersion:\s*(?P<package>\S+)\s*\(\s*(?P<version>[^)]+?)\s*\)'
)

# The 'nrsectorcarrier|nrcelldu ^arfcn|ssbfrequency|...|cellLocalId|nRTAC' combo
# command doesn't pad its output to fixed-width columns (blank AutoSelected
# fields emit no token at all, rather than a padded blank one), so the generic
# table parser misaligns every column after the first. Confirmed against real
# output — 'true'/'false' (nRTACInSib1Enabled) is the one field guaranteed
# present, so it's used as a sync point; the optional non-capturing group
# transparently absorbs ssbDurationAutoSelected whether or not it's blank.
# The 'nrsectorcarrier|nrcelldu ...' combo command's column SET varies between
# nodes - some include nRTACInSib1Enabled, some don't (confirmed on one site:
# HXL04147 has it, HXIN090147F doesn't). A positional regex that hardcodes the
# boolean silently misaligns every later field on the other variant, which
# showed up as ssbOffset ('0') being reported as ssbFrequency. So the header
# line is parsed to find each attribute's index, and values are read by name.
# Blank AutoSelected fields emit no token at all, so a plain split() is used
# and short rows are read defensively rather than assuming full width.
_NR_CELLDU_LINE_RE = re.compile(r'^NRCellDU=(\S+)\s+(.*)$', re.M)

# Same root cause as _NR_CELLDU_RE: moshell sizes each column to the widest
# value across ALL rows in the result (here, mixed EUtranCellFDD=... and
# SectorCarrier=... MO instances of very different lengths), so shorter rows'
# data lands shifted left of where the header claims that column starts.
# Confirmed against real output — anchored regex, not position, is the only
# reliable way to read this table. pimAggressorCellId/pimVictimCellId are each
# optionally blank (no token at all when empty); sectorCarrierRef's value is
# itself 3 whitespace-separated tokens ('[1]', '=', 'SectorCarrier=X').
_EUTRAN_CELL_RE = re.compile(
    r'EUtranCellFDD=(?P<cell>\S+)\s+(?P<cellId>\S+)\s+(?P<dlChannelBandwidth>\S+)\s+(?:true|false)\s+'
    r'(?P<earfcndl>\S+)\s+(?P<earfcnul>\S+)\s+(?P<physicalLayerCellId>\S+)\s+(?P<physicalLayerCellIdGroup>\S+)\s+'
    r'(?P<physicalLayerSubCellId>\S+)\s+(?:\S+\s+)?(?:\S+\s+)?(?P<rachRootSequence>\S+)\s+'
    r'(?:\[\d+\]\s*=\s*\S+\s+)?(?P<tac>\S+)\s+(?:true|false)',
    re.I
)


def extract_ul_channel_bandwidth(text):
    """ulChannelBandwidth per cell - only available via 'kget all' full dump
    (not a targeted hget block), so best-effort: returns {} if kget all
    wasn't captured or timed out for this node (confirmed: happens on real
    sites, e.g. FSL00877's kget all timed out server-side).

    Scoped to each 'EUtranCellFDD=X' MO block individually (split first,
    then search each block) rather than one regex sweep over the whole
    multi-megabyte log - the naive whole-file version is catastrophically
    slow (confirmed: multi-minute hang on a 25MB log)."""
    result = {}
    if not text:
        return result
    headers = list(re.finditer(r'^MO\s+\S*(?<!External)EUtranCellFDD=([^\s,]+)\s*$', text, re.M))
    for i, block_m in enumerate(headers):
        cell = block_m.group(1)
        if cell in result:
            continue
        window_end = headers[i + 1].start() if i + 1 < len(headers) else block_m.end() + 50000
        window = text[block_m.end():window_end]
        m = re.search(r'ulChannelBandwidth\s+(\S+)', window)
        if m:
            result[cell] = m.group(1)
    return result


def extract_cell_range(text):
    """cellRange per LTE cell - same 'kget all' full per-object attribute
    dump as extract_ul_channel_bandwidth() (one 'MO ... EUtranCellFDD=X'
    header followed by one attribute per line, NOT the compact hget table
    _EUTRAN_CELL_RE reads) - confirmed on a real log (FCL04120.txt):
    'MO ... EUtranCellFDD=FCL04120_2A_1' followed later in that same block
    by 'cellId  22' then 'cellRange  23' as sibling attribute lines.
    Best-effort: returns {} if kget all wasn't captured for this node."""
    result = {}
    if not text:
        return result
    headers = list(re.finditer(r'^MO\s+\S*(?<!External)EUtranCellFDD=([^\s,]+)\s*$', text, re.M))
    for i, block_m in enumerate(headers):
        cell = block_m.group(1)
        if cell in result:
            continue
        window_end = headers[i + 1].start() if i + 1 < len(headers) else block_m.end() + 50000
        window = text[block_m.end():window_end]
        m = re.search(r'^cellRange\s+(\S+)', window, re.M)
        if m:
            result[cell] = m.group(1)
    return result


def extract_cell_range_5g(text):
    """cellRange per 5G cell - same 'kget all' full per-object attribute
    dump as extract_cell_range(), keyed on 'MO ... NRCellDU=X' instead of
    EUtranCellFDD=X. Confirmed on a real log (FCL04120.txt):
    'MO ... NRCellDU=FCON094120_N005A_1' block contains 'cellLocalId  25'
    then 'cellRange  23000' as sibling lines - CIQ's own '5G Info!CellRange'
    column carries the SAME raw value (23000, no unit scaling needed;
    confirmed against real CIQ for that exact cell). Best-effort: returns
    {} if kget all wasn't captured for this node."""
    result = {}
    if not text:
        return result
    headers = list(re.finditer(r'^MO\s+\S*NRCellDU=([^\s,]+)\s*$', text, re.M))
    for i, block_m in enumerate(headers):
        cell = block_m.group(1)
        if cell in result:
            continue
        window_end = headers[i + 1].start() if i + 1 < len(headers) else block_m.end() + 50000
        window = text[block_m.end():window_end]
        m = re.search(r'^cellRange\s+(\S+)', window, re.M)
        if m:
            result[cell] = m.group(1)
    return result


def extract_cell_to_sef(text):
    """Cell -> SectorEquipmentFunction number, via the
    SectorCarrier=|SectorEquipmentFunction hget block's reservedBy
    cross-references (Cell -> SectorCarrier -> SEF chain). No confirmed
    link from SEF number to a specific RRU product name exists in Pre
    kget-all data, so this stops at the SEF number - callers needing the
    Pre-side radio product should treat it as NOT AVAILABLE rather than
    guess further down this chain."""
    block = get_command_block(text, 'SectorCarrier=|SectorEquipmentFunction')
    if not block:
        return {}
    cell_to_sc = {}
    for m in re.finditer(r'^(SectorCarrier=\S+)\s.*?EUtranCellFDD=(\S+)', block, re.M):
        cell_to_sc[m.group(2)] = m.group(1)
    sc_to_sef = {}
    for m in re.finditer(r'^(SectorEquipmentFunction=\S+)\s.*?SectorCarrier=(\S+)', block, re.M):
        sc_to_sef[f'SectorCarrier={m.group(2)}'] = m.group(1)
    return {cell: sc_to_sef.get(sc) for cell, sc in cell_to_sc.items() if sc_to_sef.get(sc)}


def extract_cell_to_radio(text):
    """Cell -> Pre-side radio model, resolved through the full MO chain:

        EUtranCellFDD/NRCellDU -> SectorCarrier/NRSectorCarrier
                                            ('hget SectorCarrier=|SectorEquipmentFunction ...')
        SectorCarrier   -> RfBranch refs   ('hget sector rfbranch')
        RfBranch        -> FieldReplaceableUnit=RRU-N  ('hget rfbranch auport|rfportref')
        RRU-N           -> product name    ('hget FieldReplaceableUnit product')

    An earlier version stopped at the SectorEquipmentFunction number because
    no SEF->RRU link was confirmed; the link does exist, just via RfBranch
    rather than SEF, so the Radio Type table showed '(SEF ...=2)' where the
    engineer needed the actual radio.

    Falls back to the carrier's SectorEquipmentFunction's OWN rfBranchRef
    when the carrier's own refs are empty — same confirmed real case as
    extract_cell_to_fru(): an NR carrier co-sited with LTE carriers under
    one shared SEF (e.g. FSL00877's NRSectorCarrier=FSNN090877_N005A_1,
    whose own TX/RX refs are '[0]=' but which shares
    SectorEquipmentFunction=1 with two LTE SectorCarriers; SEF=1's own
    rfBranchRef correctly resolves to a real RRU). An earlier version of
    this function had NO such fallback and NO NRSectorCarrier handling at
    all in its two carrier-matching regexes (both were hardcoded to
    'SectorCarrier=' only) — meaning it could never resolve ANY NR cell's
    radio model, confirmed: HXIN010147_N002A_1/N005A_1 and
    FSNN090877_N005A_1 all returned nothing before this fix.

    Returns {cell: 'RRUS 4449'} style short model names, or {} when any
    command in the chain is absent."""
    if not text:
        return {}

    fru_product = {}
    for m in re.finditer(r'^FieldReplaceableUnit=(\S+)\s+(\S+(?:\s+\S+)*?)\s{2,}',
                          get_command_block(text, 'FieldReplaceableUnit product') or '', re.M):
        fru_product[m.group(1)] = m.group(2).strip()

    branch_to_fru = {}
    for m in re.finditer(r'^(AntennaUnitGroup=\d+,RfBranch=\d+)\s+.*?FieldReplaceableUnit=([^,\s]+)',
                          get_command_block(text, 'rfbranch auport|rfportref') or '', re.M):
        branch_to_fru[m.group(1)] = m.group(2)

    branch_block = get_command_block(text, 'sector rfbranch') or ''
    carrier_to_radio = {}
    for m in re.finditer(r'^((?:SectorCarrier|NRSectorCarrier)=\S+)\s+(.*)$', branch_block, re.M):
        refs = re.findall(r'AntennaUnitGroup=\d+,RfBranch=\d+', m.group(2))
        models = {fru_product.get(branch_to_fru[r]) for r in refs if r in branch_to_fru}
        models.discard(None)
        if models:
            carrier_to_radio[m.group(1)] = sorted(models)[0]

    sef_to_radio = {}
    for m in re.finditer(r'^(SectorEquipmentFunction=\S+)\s+\[\d+\]\s*=\s*(.*)$', branch_block, re.M):
        rest = m.group(2)
        refs = re.findall(r'AntennaUnitGroup=\d+,RfBranch=\d+', rest)
        models = {fru_product.get(branch_to_fru[r]) for r in refs if r in branch_to_fru}
        models.discard(None)
        if not models:
            # Direct FieldReplaceableUnit reference (AAS/integrated-antenna
            # radios) - resolve straight to its product name, same fallback
            # extract_cell_to_fru() uses for the FRU id itself.
            for fru in re.findall(r'FieldReplaceableUnit=([^,\s]+)', rest):
                if fru in fru_product:
                    models.add(fru_product[fru])
        if models:
            sef_to_radio[m.group(1)] = sorted(models)[0]

    id_block = get_command_block(text, 'SectorCarrier=|SectorEquipmentFunction') or ''
    sc_to_sef = {}
    for m in re.finditer(r'^(SectorEquipmentFunction=\S+)\s+.*$', id_block, re.M):
        sef_mo, rest = m.group(1), m.group(0)
        for sc in re.findall(r'(?:SectorCarrier|NRSectorCarrier)=\S+', rest):
            sc_to_sef[sc] = sef_mo

    result = {}
    for m in re.finditer(r'^((?:SectorCarrier|NRSectorCarrier)=\S+)\s+(.*)$', id_block, re.M):
        carrier, rest = m.group(1), m.group(2)
        radio = carrier_to_radio.get(carrier)
        if not radio:
            sef = sc_to_sef.get(carrier)
            radio = sef_to_radio.get(sef) if sef else None
        if not radio:
            continue
        for cell in re.findall(r'EUtranCellFDD=(\S+)', rest):
            result[cell] = radio
        for cell in re.findall(r'NRCellDU=(\S+)', rest):
            result[cell] = radio
    return result


def _format_branch_refs(refs, sep=" | ", pair_sep=","):
    """['AntennaUnitGroup=1,RfBranch=9', 'AntennaUnitGroup=1,RfBranch=10', ...]
    -> '1,9 | 1,10 | ...' (or '1-9 | 1-10 | ...' when pair_sep='-'). Refs are
    sorted by (AUG, RfBranch) as integers for a stable, numerically-ordered
    display — confirmed against QUICKIX's own rendering convention."""
    pairs = []
    for ref in refs:
        m = re.match(r'AntennaUnitGroup=(\d+),RfBranch=(\d+)', ref)
        if m:
            pairs.append((int(m.group(1)), int(m.group(2))))
    pairs.sort()
    return sep.join(f"{aug}{pair_sep}{rb}" for aug, rb in pairs)


def extract_rf_branch_refs(text):
    """Cell -> {'tx_ref': str, 'rx_ref': str, 'sef_branches': str}, matching
    QUICKIX HTML's RFBRANCHTXREF / RFBRANCHRXREF / SEF RFBRANCHES columns.

    Confirmed against real Pre kget-all logs (HXL00147 / HXL04147 /
    HXIN090147F):
        'hget sector rfbranch' gives, per SectorCarrier (LTE) or
            NRSectorCarrier (5G): rfBranchRxRef / rfBranchTxRef, each a list
            of 'AntennaUnitGroup=N,RfBranch=M' refs -> TX/RX ref columns,
            joined with ',' inside each pair and ' | ' between pairs
            (e.g. SectorCarrier=10's rfBranchTxRef -> '1,9 | 1,10 | 1,11 | 1,12').
        The same command's second table gives SectorEquipmentFunction's own
            rfBranchRef (its own AntennaUnitGroup/RfBranch list, which can be
            a SUPERSET of any one SectorCarrier's refs when the SEF serves
            multiple SectorCarriers/carriers on the same sector) -> SEF
            RFBRANCHES column, joined with '-' inside each pair instead of
            ',' (matches QUICKIX's own formatting convention for this column).
        'SectorCarrier=|SectorEquipmentFunction ... reservedBy' gives the
            Cell -> SectorCarrier -> SectorEquipmentFunction chain needed to
            attach the right SEF's rfBranchRef list to each cell.

    Returns {} if the required commands aren't present in this log (older
    log captures / different hget command set)."""
    if not text:
        return {}

    # ── Cell -> SectorCarrier, and SectorCarrier -> SectorEquipmentFunction ──
    id_block = get_command_block(text, 'SectorCarrier=|SectorEquipmentFunction') or ''
    cell_to_sc = {}
    for m in re.finditer(r'^((?:SectorCarrier|NRSectorCarrier)=\S+)\s+.*$', id_block, re.M):
        sc_mo, rest = m.group(1), m.group(0)
        for cell in re.findall(r'(?:EUtranCellFDD|NRCellDU)=(\S+)', rest):
            cell_to_sc[cell] = sc_mo
    sc_to_sef = {}
    for m in re.finditer(r'^(SectorEquipmentFunction=\S+)\s+.*$', id_block, re.M):
        sef_mo, rest = m.group(1), m.group(0)
        for sc in re.findall(r'(?:SectorCarrier|NRSectorCarrier)=\S+', rest):
            sc_to_sef[sc] = sef_mo

    # ── 'hget sector rfbranch': two tables in one block — SectorCarrier's
    # (and NRSectorCarrier's) rfBranchRxRef/rfBranchTxRef, then
    # SectorEquipmentFunction's rfBranchRef. Parsed as one combined block
    # since both use the same '[N] = <refs...>' cross-reference syntax and
    # a shared MO-name regex distinguishes which table a row belongs to.
    #
    # Parsed ONE LINE AT A TIME rather than with a single re.finditer(...,
    # re.M) over the whole block — confirmed real bug (real ALL01748 log,
    # SectorCarrier=13_1's refs going missing): \s*/\s+ match '\n' too, so
    # when a row's own bracket value is EMPTY (e.g. 'SectorCarrier=1 [0] =
    # <padding> [0] = <padding>'), the trailing \s* before the final
    # capture group greedily eats through the row's own newline and
    # swallows the ENTIRE NEXT ROW into this (empty) row's capture —
    # corrupting that next row's refs and vanishing it from the dict
    # entirely. Every SectorCarrier/SEF immediately following an
    # empty-refs row was silently lost this way. Splitting on lines first
    # means \s can never reach past the row's own newline, since the
    # string being matched no longer contains one. ──
    branch_block = get_command_block(text, 'sector rfbranch') or ''
    sc_tx, sc_rx, sef_refs = {}, {}, {}
    _sc_row_re = re.compile(r'^((?:SectorCarrier|NRSectorCarrier)=\S+)\s+\[\d+\]\s*=\s*([^\[]*?)\s+\[\d+\]\s*=\s*(.*)$')
    _sef_row_re = re.compile(r'^(SectorEquipmentFunction=\S+)\s+\[\d+\]\s*=\s*(.*)$')
    for line in branch_block.splitlines():
        m = _sc_row_re.match(line)
        if m:
            mo, rx_part, tx_part = m.group(1), m.group(2), m.group(3)
            sc_rx[mo] = re.findall(r'AntennaUnitGroup=\d+,RfBranch=\d+', rx_part)
            sc_tx[mo] = re.findall(r'AntennaUnitGroup=\d+,RfBranch=\d+', tx_part)
            continue
        m = _sef_row_re.match(line)
        if m:
            sef_refs[m.group(1)] = re.findall(r'AntennaUnitGroup=\d+,RfBranch=\d+', m.group(2))

    result = {}
    for cell, sc in cell_to_sc.items():
        tx_ref = _format_branch_refs(sc_tx.get(sc, []), pair_sep=",")
        rx_ref = _format_branch_refs(sc_rx.get(sc, []), pair_sep=",")
        sef = sc_to_sef.get(sc)
        sef_branches = _format_branch_refs(sef_refs.get(sef, []), pair_sep="-") if sef else ""
        result[cell] = {"tx_ref": tx_ref, "rx_ref": rx_ref, "sef_branches": sef_branches}
    return result


def extract_cell_to_fru(text):
    """Cell -> raw FieldReplaceableUnit id (e.g. 'RRU-10'), via the same
    Cell->SectorCarrier->RfBranch->FRU chain as extract_cell_to_radio(), but
    keeping the FRU id itself instead of resolving it to a product name —
    matches QUICKIX HTML's 'RRUs' column (distinct from 'Radio type', which
    shows the resolved model).

    Falls back to the carrier's SectorEquipmentFunction's OWN rfBranchRef
    list when the carrier's own rfBranchTxRef/RxRef are empty — confirmed
    real case: NR carriers co-sited with LTE carriers under one shared SEF
    (e.g. FSL00877's NRSectorCarrier=FSNN090877_N005A_1, whose own TX/RX
    refs are '[0]=' but which shares SectorEquipmentFunction=1 with two LTE
    SectorCarriers; SEF=1's own rfBranchRef correctly resolves to RRU-1).
    Without this fallback such carriers report no RRU at all despite one
    being unambiguously identifiable from the shared SEF."""
    if not text:
        return {}
    branch_to_fru = {}
    for m in re.finditer(r'^(AntennaUnitGroup=\d+,RfBranch=\d+)\s+.*?FieldReplaceableUnit=([^,\s]+)',
                          get_command_block(text, 'rfbranch auport|rfportref') or '', re.M):
        branch_to_fru[m.group(1)] = m.group(2)

    branch_block = get_command_block(text, 'sector rfbranch') or ''
    carrier_to_fru = {}
    for m in re.finditer(r'^((?:SectorCarrier|NRSectorCarrier)=\S+)\s+(.*)$', branch_block, re.M):
        refs = re.findall(r'AntennaUnitGroup=\d+,RfBranch=\d+', m.group(2))
        frus = {branch_to_fru.get(r) for r in refs if r in branch_to_fru}
        frus.discard(None)
        if frus:
            carrier_to_fru[m.group(1)] = ", ".join(sorted(frus))
    sef_to_fru = {}
    # Per-line (not one re.finditer(..., re.M) over the whole block) for the
    # same reason as extract_rf_branch_refs's SEF loop: \[\d+\]\s*=\s*(.*)$
    # has a \s* that matches '\n' too, so a SEF with an EMPTY rfBranchRef
    # ('[0] = ' + padding, no data) would otherwise swallow the entire next
    # line into its own (empty) capture, corrupting that next SEF's FRU and
    # dropping it from the dict.
    _sef_row_re = re.compile(r'^(SectorEquipmentFunction=\S+)\s+\[\d+\]\s*=\s*(.*)$')
    for line in branch_block.splitlines():
        m = _sef_row_re.match(line)
        if not m:
            continue
        rest = m.group(2)
        refs = re.findall(r'AntennaUnitGroup=\d+,RfBranch=\d+', rest)
        frus = {branch_to_fru.get(r) for r in refs if r in branch_to_fru}
        frus.discard(None)
        if not frus:
            # AAS/integrated-antenna radios (confirmed: HXIN090147F) have NO
            # AntennaUnitGroup/RfBranch chain at all - the SEF's own
            # rfBranchRef names FieldReplaceableUnit=AAS-... directly
            # ('SectorEquipmentFunction=...N077A_1 [1] =
            # FieldReplaceableUnit=AAS-056284_N077A_1,Transceiver=1'). Without
            # this direct-reference fallback, every AAS/CBAND cell on such a
            # node reports no RRU at all, which is the bug this fallback
            # fixes: CBAND sites can be served by either plain RRU-N radios
            # (AUG/RfBranch chain) or AAS radios (direct FRU reference), and
            # only the first case was previously handled.
            direct = re.findall(r'FieldReplaceableUnit=([^,\s]+)', rest)
            frus = set(direct)
        if frus:
            sef_to_fru[m.group(1)] = ", ".join(sorted(frus))

    id_block = get_command_block(text, 'SectorCarrier=|SectorEquipmentFunction') or ''
    sc_to_sef = {}
    for m in re.finditer(r'^(SectorEquipmentFunction=\S+)\s+.*$', id_block, re.M):
        sef_mo, rest = m.group(1), m.group(0)
        for sc in re.findall(r'(?:SectorCarrier|NRSectorCarrier)=\S+', rest):
            sc_to_sef[sc] = sef_mo

    result = {}
    for m in re.finditer(r'^((?:SectorCarrier|NRSectorCarrier)=\S+)\s+.*$', id_block, re.M):
        carrier, rest = m.group(1), m.group(0)
        fru = carrier_to_fru.get(carrier)
        if not fru:
            sef = sc_to_sef.get(carrier)
            fru = sef_to_fru.get(sef) if sef else None
        if not fru:
            continue
        for cell in re.findall(r'(?:EUtranCellFDD|NRCellDU)=(\S+)', rest):
            result[cell] = fru
    return result


def extract_dss_status(text):
    """Cell -> True/False Pre-existing DSS, from SectorCarrier's/
    NRSectorCarrier's essScPairId/essScLocalId ('get . essScLocalId' /
    'get . essScPairId' commands) both being non-zero — same rule as
    QUICKIX HTML's dssActive flag. Confirmed against a real log (HXL04147):
    SectorCarrier=7_3/8_3/9_3 all report non-zero essScLocalId + essScPairId
    and correspond to the site's actual DSS-active cells; SectorCarrier=
    7_1/7_2/etc. report 0/0 and are not DSS.

    Real bug this fixes: the local_id/pair_id regexes only matched literal
    'SectorCarrier=' at line start, so 'NRSectorCarrier=X essScLocalId ...'
    (confirmed real line: 'NRSectorCarrier=FCON094120_N005A_1  essScLocalId')
    never matched at all - every 5G cell's Pre DSS status silently came back
    False regardless of its actual value, even when its PAIRED LTE cell
    correctly showed active (confirmed: HXL04147_9A_1 Pre=Yes, its DSS
    partner HXIN010147_N002A_1 Pre=No, for what should be the same pair).

    SECOND real bug, found and fixed in the same pass (confirmed on a real
    log, HXL04147.log): a cell with a genuinely BLANK essScLocalId/
    essScPairId value (no DSS at all, e.g. HXIN010147_N005A_1/B_1/C_1) was
    being read as ACTIVE. The value regex was '\\s+(\\S+)' — \\s+ matches
    newlines too, so on a blank value it skipped straight over the line
    break and captured the NEXT SC's own identifier string as if it were
    THIS line's value (confirmed: N005A_1's essScLocalId was read as the
    literal string 'NRSectorCarrier=HXIN010147_N005B_1') - a non-'0',
    non-empty string, so it read as active. This produced an alternating
    false-positive/true-negative chain down consecutive blank rows (N005A_1
    wrongly True, N005B_1 correctly False only because its own line got
    consumed as N005A_1's 'value' and never matched on its own, N005C_1
    wrongly True again from bleeding into whatever followed it) - exactly
    the 'multi-line regex with greedy \\s* crosses row boundaries on empty
    fields' failure mode already on this project's own list of confirmed
    parser gotchas, just not yet applied here. Fixed by restricting the
    whitespace BEFORE the value to '[ \\t]+' (horizontal only, can't cross
    a newline) - a genuinely blank value now correctly fails to match
    instead of absorbing the next line.

    An earlier version of this file claimed no DSS signal exists in Pre
    kget-all logs — that was wrong; 'get . essScLocalId'/'get . essScPairId'
    carry it directly, just not through the same 'hget' table commands the
    other per-cell fields use."""
    if not text:
        return {}
    local_id = {}
    for m in re.finditer(r'^((?:NR)?SectorCarrier=\S+)[ \t]+essScLocalId[ \t]+(\S+)', text, re.M):
        local_id[m.group(1)] = m.group(2)
    pair_id = {}
    for m in re.finditer(r'^((?:NR)?SectorCarrier=\S+)[ \t]+essScPairId[ \t]+(\S+)', text, re.M):
        pair_id[m.group(1)] = m.group(2)

    sc_active = {}
    for sc in set(local_id) | set(pair_id):
        l, p = str(local_id.get(sc, "0")).strip(), str(pair_id.get(sc, "0")).strip()
        sc_active[sc] = (l != "0" and l != "" and p != "0" and p != "")

    # Cell -> SectorCarrier, same reservedBy cross-reference used elsewhere.
    id_block = get_command_block(text, 'SectorCarrier=|SectorEquipmentFunction') or ''
    result = {}
    for m in re.finditer(r'^((?:SectorCarrier|NRSectorCarrier)=\S+)\s+.*$', id_block, re.M):
        sc_mo, rest = m.group(1), m.group(0)
        active = sc_active.get(sc_mo, False)
        for cell in re.findall(r'(?:EUtranCellFDD|NRCellDU)=(\S+)', rest):
            result[cell] = active
    return result


def extract_nr_used_antennas(text):
    """NRSectorCarrier -> {'tx': str, 'rx': str}, from noOfUsedTxAntennas /
    noOfUsedRxAntennas in the 'SectorCarrier=|SectorEquipmentFunction ...
    reserved' hget block.

    Deliberately separate from checks_sector._extract_sector_config_5g()
    (which reads noOfTxAntennas/noOfRxAntennas — the CONFIGURED max, used
    for Pre-vs-CIQ comparison elsewhere) rather than changing that function:
    confirmed on a real AAS/massive-MIMO node (HXIN090147F) that
    noOfTxAntennas/noOfRxAntennas report 0 while noOfUsedTxAntennas/
    noOfUsedRxAntennas report the real active count (64) — QUICKIX HTML's
    own 5G NR Cells TX/RX display uses the USED count, not the configured
    max, so this is a different (display-only) reading of the same block,
    not a correction to the comparison logic.

    NOT position/token-index based — this table has the same moshell
    column-width-shifts-per-row behaviour documented on _EUTRAN_CELL_RE
    above, and confirmed broken on a real log: on a DEACTIVATED carrier
    (FSL02877's FSNN092877_N005B_1), massiveMimoSleepState/noOfRxAntennas/
    noOfTxAntennas are ALL blank (no token at all, not even '0'), so a
    token-index lookup silently reads 'operationalState's own value
    ('0 (DISABLED)') as if it were noOfUsedTxAntennas. Anchored instead:
    the two bare numeric fields immediately preceding operationalState's
    'N (STATE)' marker are noOfUsedRxAntennas/noOfUsedTxAntennas — when
    that anchor can't be found (both preceding fields also blank, as on
    the DEACTIVATED row), correctly return no reading for that cell rather
    than a wrong one."""
    if not text:
        return {}
    block = get_command_block(text, 'SectorCarrier=|SectorEquipmentFunction') or ''
    result = {}
    for m in re.finditer(
        r'^(NRSectorCarrier=\S+)\s+.*?(\d+)\s+(\d+)\s+\d+\s*\([A-Z_]+\)\s+(?:\[\d+\]|reservedBy)',
        block, re.M,
    ):
        result[m.group(1).split('=', 1)[1]] = {'rx': m.group(2), 'tx': m.group(3)}
    return result


def _short_radio_name(product):
    """Normalise a Pre productName to the CIQ's 'RRUS <model>' shape so the
    two columns are visually comparable.

        'Radio 4449 B5 B12A KRC 161 752/1 ...' -> 'RRUS 4449'
        'Radio 4890HP 48B2/B25 48B66 M01 ...'  -> 'RRUS 4890'
        'RRUS 32 B30 KRC 161 423/1 ...'        -> 'RRUS 32'
        'Radio 6472 B77G ...'                  -> 'RRUS AIR6472'

    Takes the token immediately after the family word (Radio/RRUS/AIR)
    rather than the first 4-digit run anywhere in the string - the latter
    matched serial/part numbers on models like 'RRUS 32' (2 digits) and
    missed suffixed models like '4890HP'."""
    if not product:
        return None
    m = re.match(r'\s*(?:Radio|RRUS|AIR)\s+([A-Za-z]*\d+)', product, re.I)
    if not m:
        return product.split()[0]
    model = re.match(r'([A-Za-z]*\d+)', m.group(1)).group(1)
    if re.match(r'^(?:64|66|84)\d{2}$', model):
        return f'RRUS AIR{model}'
    return f'RRUS {model}'


def build_moved_cell_source_map(ciq_wb):
    """From Sector Del_Movement: {target_cell: (source_node, source_cell)}
    for every row that has both a Source and a Target (i.e. a real move,
    not a delete). Cells moving in from another physical node keep their
    real Pre-side history on the SOURCE node's kget-all log under the
    SOURCE cell name - the target node's own log has never seen them, since
    they haven't physically moved yet at Pre-scripting time (confirmed on a
    real rehome: FSL00452's own log has no 'FSL00452_2B_1' at all; the real
    Pre data sits on FSL02452's log as 'FSL02452_2B_1'). Every check that
    looks up Pre data by cell name needs this remap or it reports NA for
    every moved-in cell despite real data being available."""
    result = {}
    for r in cer.sheet_rows_as_dicts(ciq_wb['Sector Del_Movement']) if 'Sector Del_Movement' in ciq_wb.sheetnames else []:
        src_node, src_sector = r.get('Source Node name'), r.get('Source Sector')
        tgt_node, tgt_sector = r.get('Target Node name'), r.get('Target Sector')
        if src_node and src_sector and tgt_node and tgt_sector and str(tgt_node).strip().upper() != 'DELETE':
            result[str(tgt_sector).strip()] = (str(src_node).strip(), str(src_sector).strip())
    return result


def remap_pre_dict(source_dict, cell_rename_map):
    """cell_rename_map: {source_cell: target_cell}. Returns a new dict with
    keys renamed to the target side, for merging a source node's Pre
    extraction into a target node's results."""
    return {cell_rename_map[c]: v for c, v in source_dict.items() if c in cell_rename_map}


def merge_moved_in_pre(base_dict, node_logs, moved_map, extract_fn):
    """base_dict: {cell: value} already extracted from this node's own log.
    moved_map: {target_cell: (source_node, source_cell)} - typically the
    subset from build_moved_cell_source_map() relevant to this node (or the
    full map; entries not matching base_dict's node are simply not filled,
    since target_cell won't be looked up by an unrelated node's checks).
    node_logs: {node_id: text}. extract_fn: text -> {cell: value}, e.g.
    extract_lte_sector_params, extract_nr_tac, extract_cell_to_radio.

    For each target cell NOT already in base_dict, pulls the value from its
    source node's own log (extracted fresh via the same extract_fn) under
    the source cell name, and inserts it under the target name. This is
    what makes a moved-in cell show real Pre data instead of NA - the
    target node's own log has never seen it; the real history is on the
    source node's log (confirmed on a real rehome)."""
    merged = dict(base_dict)
    by_source = {}
    for target_cell, (source_node, source_cell) in moved_map.items():
        if target_cell in merged:
            continue
        by_source.setdefault(source_node, {})[source_cell] = target_cell
    for source_node, rename in by_source.items():
        source_text = node_logs.get(source_node)
        if not source_text:
            continue
        source_vals = extract_fn(source_text)
        for source_cell, target_cell in rename.items():
            if source_cell in source_vals:
                merged[target_cell] = source_vals[source_cell]
    return merged


def extract_cell_to_rilink(text):
    """Cell -> 'Single'/'Double' RILink count, from 'hget rilink=' riPortRef2
    (FieldReplaceableUnit=RRU-N,RiPort=...) counted per RRU-N, joined via the
    same RfBranch->RRU chain as extract_cell_to_radio."""
    if not text:
        return {}
    fru_by_branch = {}
    for m in re.finditer(r'^(AntennaUnitGroup=\d+,RfBranch=\d+)\s+.*?FieldReplaceableUnit=([^,\s]+)',
                          get_command_block(text, 'rfbranch auport|rfportref') or '', re.M):
        fru_by_branch[m.group(1)] = m.group(2)
    rru_links = {}
    for m in re.finditer(r'FieldReplaceableUnit=(RRU-\S+),RiPort=', get_command_block(text, 'rilink=') or ''):
        rru_links[m.group(1)] = rru_links.get(m.group(1), 0) + 1
    carrier_rru = {}
    for m in re.finditer(r'^(SectorCarrier=\S+)\s+(.*)$', get_command_block(text, 'sector rfbranch') or '', re.M):
        refs = re.findall(r'AntennaUnitGroup=\d+,RfBranch=\d+', m.group(2))
        rrus = {fru_by_branch[r] for r in refs if r in fru_by_branch}
        if rrus:
            carrier_rru[m.group(1)] = sorted(rrus)[0]
    result = {}
    block = get_command_block(text, 'SectorCarrier=|SectorEquipmentFunction')
    for m in re.finditer(r'^(SectorCarrier=\S+)\s+(.*)$', block or '', re.M):
        carrier, rest = m.group(1), m.group(2)
        rru = carrier_rru.get(carrier)
        if not rru:
            continue
        n = rru_links.get(rru, 0)
        label = 'Double' if n >= 2 else ('Single' if n == 1 else None)
        if not label:
            continue
        for cell in re.findall(r'EUtranCellFDD=(\S+)', rest):
            result[cell] = label
        for cell in re.findall(r'NRCellDU=(\S+)', rest):
            result[cell] = label
    return result


def extract_cell_to_rilink_detail(text, fru_by_cell):
    """Cell -> {'rilink_id': str, 'rilink_port': str}, from the 'hget
    rilink=' rows.

    Confirmed against real logs (HXL00147, HXL04147, HXIN090147F): the
    table's column order is fixed (riPortRef1 THEN riPortRef2 on every
    row) and the two sides are never swapped —
        riPortRef1 = baseband-side connection: either straight to the
            board slot (FieldReplaceableUnit=1,RiPort=<letter A-F>) or to
            an XMU expansion port (FieldReplaceableUnit=XMU03-1-1,
            RiPort=<number>).
        riPortRef2 = the radio side (FieldReplaceableUnit=RRU-N or
            AAS-..., RiPort=DATA_1/DATA_2).
    The cell's own already-resolved radio FRU (fru_by_cell, from
    extract_cell_to_fru() — works for both RRU-N and AAS-... radios) is
    matched against riPortRef2 to find which RiLink row/sector a cell
    belongs to, but the id and port reported are riPortRef1's — the
    baseband/XMU-side port (letter or number), not the radio-side DATA_n
    port.

    A cell whose radio FRU is linked via more than one RiLink row (dual-
    link radio) gets both ids/ports joined with '+'. Returns {} if the
    rilink= command isn't present in this log."""
    if not text or not fru_by_cell:
        return {}
    fru_to_links = {}
    for line in (get_command_block(text, 'rilink=') or '').splitlines():
        m = re.match(r'^RiLink=(\d+)\b(.*)$', line)
        if not m:
            continue
        rilink_id, rest = m.group(1), m.group(2)
        pairs = re.findall(r'FieldReplaceableUnit=(\S+?),RiPort=(\S+)', rest)
        if len(pairs) < 2:
            continue
        (ref1_fru, ref1_port), (ref2_fru, _ref2_port) = pairs[0], pairs[1]
        # riPortRef1 is on the board slot (FRU='1', no bracket needed) OR on
        # an XMU expansion unit — when it's an XMU, the unit itself isn't
        # otherwise shown anywhere in this table, so it's appended in
        # brackets after the port (e.g. '13 (XMU03-1-1)') to disambiguate
        # which physical XMU a port number belongs to (confirmed real case,
        # ALL01748: XMU03-1-1 and XMU03-1-2 both use overlapping port
        # numbers, so the bare port number alone is ambiguous).
        port_display = f"{ref1_port} ({ref1_fru})" if ref1_fru.upper().startswith("XMU") else ref1_port
        fru_to_links.setdefault(ref2_fru, []).append((rilink_id, port_display))

    result = {}
    for cell, fru_str in fru_by_cell.items():
        if not fru_str or fru_str == "-":
            continue
        links = []
        for fru in (f.strip() for f in fru_str.split(",")):
            links += fru_to_links.get(fru, [])
        if links:
            result[cell] = {
                "rilink_id": "+".join(i for i, _ in links),
                "rilink_port": "+".join(p for _, p in links),
            }
    return result


def parse_rbb_txrx(rbb_type):
    """'RBB44_1D' -> '4x4'. Returns None if not RBB44/42/22-style."""
    if not rbb_type:
        return None
    m = re.match(r'RBB(\d)(\d)', str(rbb_type))
    return f'{m.group(1)}x{m.group(2)}' if m else None


def parse_rbb_link(rbb_type):
    """RBB Type link count: the DIGIT right after the underscore (not the
    trailing letter) is what determines Single vs Double link — '_1D' is
    Single, '_2E' is Double, regardless of the letter, which is unrelated
    to link count. Returns None if no '_<digit><letter>' suffix is found
    at all, rather than guessing."""
    if not rbb_type:
        return None
    m = re.search(r'_([12])[A-Za-z]', str(rbb_type))
    if not m:
        return None
    return 'Single' if m.group(1) == '1' else 'Double'


def node_id_from_log(text):
    """The node ID as it appears at the moshell prompt, e.g. 'SCL05020'."""
    m = re.search(r'^([A-Za-z0-9_]+)>\s', text, re.M)
    return m.group(1) if m else None


def extract_ptp_status(text):
    """PTP presence/state, from Transport=1...Ptp=1...operationalState.
    Confirmed against real logs (HXL00147, HXL04147: Ptp=1 present and
    ENABLED; HXIN090147F: no Ptp=1 MO at all, correctly NOT PRESENT) — an
    earlier version of this docstring claimed this was unconfirmed/unused
    in this project's hget command set; that was wrong. amos_view.py's own
    ptp_status() duplicates this same rule for the Node Summary table (kept
    separate rather than merged so amos_view.py's Title Case return value
    doesn't require a caller-side transform); this function's UPPER CASE
    return stays as-is for any other unconfirmed caller relying on it.
    Returns 'ENABLED' / 'DISABLED' / 'NOT PRESENT'.
    """
    m = re.search(r'Transport\s*=\s*1[\s\S]{0,300}?Ptp\s*=\s*1[\s\S]{0,300}?operationalState\s*[:=]?\s*(\w+)', text, re.I)
    if not m:
        return 'NOT PRESENT'
    state = m.group(1).upper()
    return 'ENABLED' if state in ('ENABLED', 'UP', 'TRUE', '1') else 'DISABLED'


def extract_sw_version(text):
    """Rule #1: SW Version / SW Package, from the 'cvcu'/'cvls' backup-version
    block's 'Current SwVersion: <package> (<version>)' line.
    Returns {'sw_package': str, 'sw_version': str} or None if not found."""
    m = _SW_VERSION_RE.search(text)
    if not m:
        return None
    return {'sw_package': m.group('package'), 'sw_version': m.group('version')}


def _row_value(row, attr_name):
    """A row's value for attr_name, regardless of table shape. Wide tables
    (bulk kget-all/lt all dumps) have the attribute as its own column:
    row.get(attr_name) directly. Narrow tables (individual 'get <MO>
    <attr>' commands - confirmed real shape, e.g. 'get ^ENodeBFunction=1
    eNBId') have exactly one attribute per row under generic 'Attribute'/
    'Value' columns instead: {'MO':.., 'Attribute': 'eNBId', 'Value':
    '704120'} - row.get(attr_name) on THIS shape returns None even though
    the value is right there, since 'eNBId' is a value, not a key."""
    if attr_name in row:
        return row.get(attr_name)
    if row.get('Attribute') == attr_name:
        return row.get('Value')
    return None


def extract_identity(parsed):
    """Rule #2/12/14/17 (collapsed): eNBId (from ENodeBFunction=1) and gNBId.
    Returns {'eNBId': str|None, 'gNBId': str|None, 'gNBIdLength': str|None}.
    None values mean that identity type genuinely isn't present on this node
    (e.g. an LTE-only node has no gNBId at all).

    Two separate find_command() calls, not one search for 'eNBId|gNBId' —
    confirmed real bug: find_command() does plain substring matching, not
    regex, so a single combined search for the literal text 'eNBId|gNBId'
    can only ever match a command whose own text contains that exact pipe
    character. Real logs run these as two SEPARATE commands
    ('get ^ENodeBFunction=1 eNBId' and 'get GNBCUUPFunction=1 gNBId'),
    neither of which contains 'eNBId|gNBId' as a substring — so this
    returned nothing for eNBId OR gNBId on every node, regardless of which
    MO gNBId is reported under.

    gNBId source: GNBDUFunction preferred when present, but NOT the only
    source — confirmed real case (FCL04120/FCL07900R/FCL09220R): the
    'get gNBId' command output only ever showed a GNBCUUPFunction=1 row,
    never GNBDUFunction. Falls back to GNBCUUPFunction, then
    GNBCUCPFunction, whichever this particular log actually reports it
    under.

    Uses _row_value() rather than row.get(attr) directly — confirmed real
    case: these same commands parse into the narrow MO/Attribute/Value
    table shape (one attribute per row), not the wide one-column-per-
    attribute shape row.get('eNBId') assumes."""
    result = {'eNBId': None, 'gNBId': None, 'gNBIdLength': None}
    fallback_gnbid, fallback_gnbid_len = None, None
    for entry in (find_command(parsed, 'eNBId'), find_command(parsed, 'gNBId')):
        if entry is None:
            continue
        for row in all_rows(entry):
            mo = row.get('MO', '')
            enbid = _row_value(row, 'eNBId')
            gnbid = _row_value(row, 'gNBId')
            gnbid_len = _row_value(row, 'gNBIdLength')
            if mo.startswith('ENodeBFunction') and enbid:
                result['eNBId'] = enbid
            elif mo.startswith('GNBDUFunction'):
                if gnbid:
                    result['gNBId'] = gnbid
                if gnbid_len:
                    result['gNBIdLength'] = gnbid_len
            elif mo.startswith(('GNBCUUPFunction', 'GNBCUCPFunction')):
                if gnbid and fallback_gnbid is None:
                    fallback_gnbid = gnbid
                if gnbid_len and fallback_gnbid_len is None:
                    fallback_gnbid_len = gnbid_len
    if result['gNBId'] is None and fallback_gnbid is not None:
        result['gNBId'] = fallback_gnbid
    if result['gNBIdLength'] is None and fallback_gnbid_len is not None:
        result['gNBIdLength'] = fallback_gnbid_len
    return result


def extract_hardware(parsed):
    """Rule #5/#15 (board type) + XMU presence for #11/#26/#27.
    Returns {'boards': [{'mo':..,'model':..}], 'radios': [...], 'xmus': [...]}
    split by FieldReplaceableUnit MO prefix. 'model' is the productName with
    the leading family word (Baseband/Radio/SAU/SUP) kept, since RFDS text
    needs the same family word stripped/matched on the numeric token by the
    caller (mirrors QUICKIX's hw_string()/extract_pre_hw() approach of
    comparing the last whitespace token)."""
    entry = find_command(parsed, 'FieldReplaceableUnit product')
    boards, radios, xmus, other = [], [], [], []
    if not entry:
        return {'boards': boards, 'radios': radios, 'xmus': xmus, 'other': other}
    for row in all_rows(entry):
        mo = row.get('MO', '')
        item = {'mo': mo, 'model': row.get('productName', '').strip()}
        if mo.upper().startswith('FIELDREPLACEABLEUNIT=XMU'):
            xmus.append(item)
        elif 'RRU-' in mo.upper() or item['model'].upper().startswith('RADIO'):
            radios.append(item)
        elif (item['model'].upper().startswith('BASEBAND')
              or item['model'].upper().startswith('RAN PROCESSOR')) and 'XMU' not in mo.upper():
            # 'RAN Processor NNNN' is the same class of board as 'Baseband
            # NNNN' - Ericsson uses both names for the DU (confirmed: a real
            # node reports 'RAN Processor 6651', and the RFDS Non-RF
            # Inventory uses 'RAN PROCESSOR 6672' for the same field).
            # Matching only 'Baseband' left that node with no board at all,
            # which surfaced as 'NOT FOUND' in the Pre/Post configuration.
            boards.append(item)
        else:
            other.append(item)
    return {'boards': boards, 'radios': radios, 'xmus': xmus, 'other': other}


def model_token(product_name):
    """Last whitespace token of a productName, e.g. 'Baseband 6630' -> '6630',
    'Radio 4449 B5 B12A' -> 'B12A' (radios need a different join key upstream;
    for boards this reliably isolates the bare model number)."""
    if not product_name:
        return None
    tokens = product_name.strip().split()
    return tokens[-1] if tokens else None


def extract_tac(text):
    """Rule #16: LTE TAC per cell. Takes raw log TEXT (see _EUTRAN_CELL_RE's
    comment for why this table needs regex, not the fixed-width parser).
    Returns {cell_name: tac_str, ...}. Cells on one node normally share a
    single TAC; callers should flag internally-inconsistent TACs as their own
    anomaly rather than silently picking one."""
    block = get_command_block(text, 'EUtranCell.DD|Sector')
    result = {}
    if not block:
        return result
    for m in _EUTRAN_CELL_RE.finditer(block):
        result[m.group('cell')] = m.group('tac')
    return result


def _parse_nrcell_block(block, mo_prefix, header_key='cellLocalId'):
    """Parse NRCellCU=/NRCellDU= rows into {cell: {attr: value}}, reading
    each attribute's CHARACTER POSITION from the block's own header line.

    Two independent quirks make simpler approaches wrong here:
      - The column set varies between nodes (one node's NRCellDU table has
        nRTACInSib1Enabled, another's doesn't), so a positional regex with a
        hardcoded boolean misaligns everything after it.
      - Blank trailing/optional fields emit NO token at all, so splitting the
        row and zipping against the header list also misaligns - confirmed:
        a 9-attribute header with only 6 values mapped ssbFrequency onto
        ssbOffset's '0' instead of the real 395070.
    Slicing by the header's own column offsets handles both, since moshell
    pads values to their column start."""
    if not block:
        return {}
    result = {}
    spans = None
    row_re = re.compile(r'^' + mo_prefix + r'=(\S+)')
    for line in block.splitlines():
        stripped = line.rstrip()
        if stripped.startswith('MO ') and header_key in stripped:
            spans = [(m.group(0), m.start()) for m in re.finditer(r'\S+', stripped)][1:]
            continue
        if not spans:
            continue
        m = row_re.match(stripped)
        if not m:
            continue
        row = {}
        for i, (attr, start) in enumerate(spans):
            end = spans[i + 1][1] if i + 1 < len(spans) else len(stripped)
            val = stripped[start:end].strip() if start < len(stripped) else ''
            if val:
                row[attr] = val
        result[m.group(1)] = row
    return result


def extract_nr_tac(text):
    """Rule #7/#8: NR TAC per 5G cell.

    Sourced from the 'hget ^NRCell|syncsignal ...' command's NRCellCU table,
    which is a clean three-column block (cellLocalId/nCI/nRTAC) present on
    every node checked - rather than the 'nrsectorcarrier|nrcelldu' combo
    command, whose column set varies by node. A blank nRTAC there is a real
    value (NSA cells report nothing), so it maps to None, not a parse
    failure."""
    block = get_command_block(text, 'NRCell|syncsignal sectorCarrierRef')
    rows = _parse_nrcell_block(block, 'NRCellCU')
    return {cell: vals['nRTAC'] for cell, vals in rows.items() if vals.get('nRTAC')}


def extract_lte_sector_params(text):
    """Rule #19 (LTE half): earfcndl/earfcnul/dlChannelBandwidth/tac/
    rachRootSequence/cellId per cell, from the same combined EUtranCellFDD
    block used by extract_tac(). Returns {cell_name: {field: value}}."""
    block = get_command_block(text, 'EUtranCell.DD|Sector')
    result = {}
    if not block:
        return result
    fields = ('cellId', 'dlChannelBandwidth', 'earfcndl', 'earfcnul', 'rachRootSequence', 'tac')
    for m in _EUTRAN_CELL_RE.finditer(block):
        result[m.group('cell')] = {f: m.group(f) for f in fields}
    return result


def extract_5g_sector_params(parsed, text):
    """Rule #19 (5G half) + #25: arfcnDL/arfcnUL/bSChannelBwDL/bSChannelBwUL
    (from NRSectorCarrier, reliably fixed-width -> generic table parser) plus
    ssbFrequency/cellLocalId/nRTAC (from NRCellDU, needs regex -- see
    _NR_CELLDU_RE). Returns {cell_name: {field: value}}."""
    result = {}
    # NRSectorCarrier table: parsed by header character position for the same
    # reason as _parse_nrcell_block - the generic fixed-width table parser
    # merges these four columns into one field, because this header separates
    # them with single spaces where that parser expects 2+ (confirmed: it
    # returned a single 'arfcnDL arfcnUL bSChannelBwDL bSChannelBwUL' key).
    carrier_block = get_command_block(text, 'NRSector arfcn')
    for cell, vals in _parse_nrcell_block(carrier_block, 'NRSectorCarrier', 'arfcnDL').items():
        result.setdefault(cell, {}).update({
            'arfcnDL': vals.get('arfcnDL'),
            'arfcnUL': vals.get('arfcnUL'),
            'bSChannelBwDL': vals.get('bSChannelBwDL'),
            'bSChannelBwUL': vals.get('bSChannelBwUL'),
        })
    block = get_command_block(text, 'nrsectorcarrier|nrcelldu')
    du_rows = _parse_nrcell_block(block, 'NRCellDU')
    for cell, vals in du_rows.items():
        result.setdefault(cell, {}).update({
            'ssbFrequency': vals.get('ssbFrequency'),
            'ssbOffset': vals.get('ssbOffset'),
            'ssbDuration': vals.get('ssbDuration'),
            'cellLocalId': vals.get('cellLocalId'),
        })
    # nRTAC comes from the cleaner NRCellCU table (see extract_nr_tac)
    nr_tacs = extract_nr_tac(text)
    for cell, tac in nr_tacs.items():
        result.setdefault(cell, {})['nRTAC'] = tac
    return result


def extract_5g_sector_params_from_text(text):
    """Text-only adapter for extract_5g_sector_params, so it fits
    merge_moved_in_pre's extract_fn(text) -> dict interface (that function
    normally also needs a pre-parsed `parsed` from the caller's own log,
    but a source node's log needs its own separate parse anyway)."""
    import log_parser as lp
    return extract_5g_sector_params(lp.parse_log(text), text)


def extract_nbiot_cells(parsed):
    """Rule #4: NBIoT cell presence, from 'hget ^nbiotcell ...'. Returns a list
    of {'cell':.., 'cellid':.., 'physicalLayerCellId':.., 'tac':..} — empty
    list means no NBIoT cells on this node (check does not trigger)."""
    entry = find_command(parsed, 'nbiotcell')
    out = []
    if not entry:
        return out
    for row in all_rows(entry):
        out.append({
            'cell': row.get('MO', '').split('=', 1)[-1],
            'cellid': row.get('cellid'),
            'physicalLayerCellId': row.get('physicalLayerCellId'),
            'tac': row.get('tac'),
        })
    return out


def extract_bearer_oam_ipv6(text):
    """Bearer/OAM VLAN ID, IPv6 address, and default-router IPv6 address
    from a Pre kget-all log - the Pre-side counterparts of the EDP fields
    BEARER_ENODEB_SB_VLAN_ID, IPV6_ENODEB_BEARER_IP,
    IPV6_SIAD_BEARER_IP_DEF_ROUTER, OAM_ENODEB_SIAD_OAM_VLAN,
    IPV6_ENODEB_OAM_IP, IPV6_SIAD_OAM_IP_DEF_ROUTER.

    Confirmed against real logs across all node shapes:
      - LTE-only (HXL00147): bearer router 'LTE', OAM router 'vr_OAM'.
      - 5G-only (HXIN090147F): bearer router 'NR', OAM router 'OAM'
        (a pure 5G node has NO 'LTE'-named router at all - this differs
        from the dual-tech case below, so 'NR' must be tried too).
      - Dual-tech/TMBB (HXL04147, FCL04120/FCON094120, OKTN000082/
        OKL02082): BOTH the LTE (eNodeB) and NR (gNodeB) bearer configs
        live under the SAME 'Router=LTE' - distinguished only by
        interface/nexthop SUFFIX, not by router name or by which one is
        'Primary' in Mixed Mode Info:
          - LTE-technology side:  InterfaceIPv6=1,  NextHop=1
          - NR-technology side:   InterfaceIPv6=NR, NextHop=NR
        Confirmed on TWO real sites where this suffix maps to OPPOSITE
        Primary/Secondary roles each time (FCL04120: eNodeB=Primary is
        the '=1' side; OKTN000082: gNodeB=Primary is the '=NR' side) -
        so the suffix is purely LTE-vs-NR technology, never a role. OAM
        has only ONE interface either way (confirmed both sites) - it is
        genuinely shared between the two identities, not a missing
        extraction.

    Chain used - VlanPort lookup stays scoped to its own command's block
    (confirmed necessary: scanning the whole file let an unrelated VlanPort
    with a matching reservedBy target win by appearing later - see the
    setdefault comment below), while the IP-address and NextHop lookups
    scan the whole raw log text rather than relying on matching one
    specific command's block (get_command_block() only returns the FIRST
    command whose text contains a given substring, which is fragile
    against a differently-worded command or a different capture tool) -
    same underlying MO chain, just searched for more broadly where that's
    safe to do:
      1. VlanPort records ('Transport=1,VlanPort=<id>' MO blocks, within
         the 'Transport=1,VlanPort=' command's own output) - each one's
         own vlanId AND its reservedBy attribute, which names the
         Router+InterfaceIPv6 that actually uses that VLAN (this is the
         only confirmed link between a VlanPort and a specific router
         interface - there is no attribute on the InterfaceIPv6 side
         pointing back to its VLAN).
      2. AddressIPv6 records ('Router=X,InterfaceIPv6=Y,AddressIPv6=Z'
         attribute rows), searched across the whole log - only the
         primaryAddress=true record is used, since an interface can carry
         more than one AddressIPv6 child.
      3. NextHop records ('Router=X,RouteTableIPv6Static=1,Dst=1,
         NextHop=Y' attribute rows, Y matching the SAME '1'/'NR' suffix
         as the interface above), searched across the whole log - the
         interface's default-router IPv6 address.

    Returns a dict; any field this log's captured commands don't cover is
    None rather than guessed. VLAN IDs found this way should be expected
    to occasionally disagree with EDP's published value - confirmed on a
    real site where Pre reported bearer/OAM VLAN 212/211 while EDP
    published 221/220 for the same node; that is a genuine finding this
    comparison exists to catch, not an extraction bug.

    bearer_vlan/bearer_ip/bearer_router_ip (no suffix) are a convenience
    alias - whichever of the LTE/NR pair is present, for single-technology
    (non-TMBB) nodes that only ever have one. On a TMBB node with BOTH
    present, the alias is the LTE side; callers that need to attribute the
    right value to the right node identity (Primary vs Secondary - which
    is which varies by site, per the confirmed cases above) must use
    bearer_vlan_lte/bearer_vlan_nr directly, matched against whichever
    identity (eNodeB Name vs gNodeB Name) that node actually is."""
    if not text:
        return {}

    router_iface_to_vlan = {}
    vlan_block = get_command_block(text, 'vlanport') or ''
    for rec in re.split(r'\n(?=\d+ +Transport=1,VlanPort=)', vlan_block):
        header_m = re.match(r'^\d+ +Transport=1,VlanPort=\S+', rec)
        if not header_m:
            continue
        vlan_m = re.search(r'^vlanId\s+(\S+)', rec, re.M)
        rb_m = re.search(r'>>> reservedBy = (?:[A-Za-z]+=\S+?,)*?(Router=\S+?,InterfaceIPv6=\S+)', rec)
        if vlan_m and rb_m:
            # setdefault, not assignment: confirmed real case (HXL00147) where
            # a SECOND, unrelated VlanPort (ULCoMP - a CoMP-signaling VLAN,
            # vlanId=1) ALSO lists 'reservedBy = Router=vr_OAM,InterfaceIPv6=1',
            # the same interface as the genuine OAM bearer VLAN (211). A plain
            # assignment let ULCoMP's later-appearing record overwrite the
            # correct one, since dict[key]=value always keeps the LAST match
            # in file order; setdefault keeps the FIRST, which is the real
            # OAM VLAN in every log checked (HXL00147/HXIN090147F/HXL04147/
            # TNL04504/OKL00082/OKTN000082) - the real bearer/OAM VlanPorts
            # are numbered low (2xx) and listed before feature VLANs like
            # ULCoMP/ERAN in this command's own output order.
            router_iface_to_vlan.setdefault(rb_m.group(1), vlan_m.group(1))

    bearer_key_lte = next((k for k in router_iface_to_vlan
                           if re.match(r'Router=(?:LTE|NR),InterfaceIPv6=(?!NR\b)\S+', k)), None)
    bearer_key_nr = next((k for k in router_iface_to_vlan
                          if re.match(r'Router=(?:LTE|NR),InterfaceIPv6=NR$', k)), None)
    oam_key = next((k for k in router_iface_to_vlan if re.match(r'Router=(?:vr_OAM|OAM),InterfaceIPv6=', k)), None)
    bearer_vlan_lte = router_iface_to_vlan.get(bearer_key_lte)
    bearer_vlan_nr = router_iface_to_vlan.get(bearer_key_nr)
    oam_vlan = router_iface_to_vlan.get(oam_key)

    def _primary_address(router_iface_key):
        if not router_iface_key:
            return None
        pat = re.escape(router_iface_key) + r',AddressIPv6=\d+\s+primaryAddress\s+true'
        if not re.search(pat, text):
            return None
        addr_m = re.search(re.escape(router_iface_key) + r',AddressIPv6=\d+\s+address\s+(\S+)', text)
        return addr_m.group(1) if addr_m else None

    bearer_ip_lte = _primary_address(bearer_key_lte)
    bearer_ip_nr = _primary_address(bearer_key_nr)
    oam_ip = _primary_address(oam_key)

    def _nexthop_address(router_name, suffix='1'):
        # \r?\n, not bare \n: confirmed real bug — text decoded via
        # bytes.decode() (app.py's actual path: u.getvalue().decode(...))
        # keeps literal \r\n, unlike Python's open() in text mode, which
        # silently normalizes \r\n -> \n (universal newlines) and had
        # been masking this in every test run so far. A bare \n right
        # after the '=====' divider line failed to match the real \r
        # sitting there, so this NEVER matched in production even though
        # it matched every local test.
        pat = (rf'Router={re.escape(router_name)},RouteTableIPv6Static=1,Dst=1,'
               rf'NextHop={re.escape(suffix)}\s*\n=+\r?\naddress\s+(\S+)')
        m = re.search(pat, text)
        return m.group(1) if m else None

    bearer_router_ip_lte = _nexthop_address('LTE', '1') or _nexthop_address('NR', '1')
    bearer_router_ip_nr = _nexthop_address('LTE', 'NR') or _nexthop_address('NR', 'NR')
    oam_router_ip = _nexthop_address('vr_OAM', '1') or _nexthop_address('OAM', '1')

    return {
        'bearer_vlan': bearer_vlan_lte or bearer_vlan_nr, 'oam_vlan': oam_vlan,
        'bearer_ip': bearer_ip_lte or bearer_ip_nr, 'oam_ip': oam_ip,
        'bearer_router_ip': bearer_router_ip_lte or bearer_router_ip_nr, 'oam_router_ip': oam_router_ip,
        # Explicit per-technology values for TMBB nodes carrying both -
        # caller matches these to Primary/Secondary by identity (eNodeB
        # vs gNodeB), not by which one happens to come first. OAM has no
        # _lte/_nr split - confirmed genuinely shared, single interface.
        'bearer_vlan_lte': bearer_vlan_lte, 'bearer_ip_lte': bearer_ip_lte, 'bearer_router_ip_lte': bearer_router_ip_lte,
        'bearer_vlan_nr': bearer_vlan_nr, 'bearer_ip_nr': bearer_ip_nr, 'bearer_router_ip_nr': bearer_router_ip_nr,
    }


# Confirmed board-generation -> transport EthernetPort name mapping (G2
# boards can show either TN_A or TN_B in practice, hence trying both).
BOARD_TRANSPORT_PORTS = {
    "6630": ["TN_A", "TN_B"], "5216": ["TN_A", "TN_B"],   # G2
    "6648": ["TN_IDL_B"], "6651": ["TN_IDL_B"],            # G3
    "6672": ["TN_IDL_C"],                                   # G4
}


def extract_transport_port_mode(text, board_model):
    """admOperatingMode ('9 (10G_FULL)' / '6 (1G_FULL)' -> '10GE'/'1GE') off
    the Transport=1,EthernetPort=<name> MO expected for this board
    generation - confirmed directly against real logs: G2 (6630/5216)
    tries TN_A then TN_B, G3 (6648/6651) tries TN_IDL_B, G4 (6672) tries
    TN_IDL_C. Returns (port_name_used, mapped_size) or (None, None) if
    NONE of the known port names (this board's own, or any other
    generation's) appear in this particular log.

    board_model is CIQ's DU type - the TARGET/POST board, which on a
    board-swap site can be a different generation than what the Pre log
    actually shows (the swap hasn't happened yet). Confirmed real case:
    FCL04120's CIQ DU type is '6672' (G4, port TN_IDL_C), but its Pre log
    is still on the pre-swap 5216 (G2, port TN_B) - searching only
    '6672's candidates finds nothing even though the log plainly has a
    working transport port. So the target board's own candidates are
    tried first (fast path, and disambiguates when a log could
    technically match more than one generation), then every OTHER known
    board's candidates as a fallback - a log only ever has ONE of these
    port names configured, and the port-name sets don't overlap across
    generations, so this fallback can't pick the wrong one."""
    if not text:
        return None, None
    primary = BOARD_TRANSPORT_PORTS.get(str(board_model).strip(), [])
    seen = set(primary)
    fallback = [p for ports in BOARD_TRANSPORT_PORTS.values() for p in ports if p not in seen]
    for port in primary + fallback:
        m = re.search(re.escape(f"EthernetPort={port}") + r'\r?\n=+\r?\nadmOperatingMode\s+\d+\s*\((\w+)\)', text)
        if m:
            mapped = {"10G_FULL": "10GE", "1G_FULL": "1GE"}.get(m.group(1), m.group(1))
            return port, mapped
    return None, None
