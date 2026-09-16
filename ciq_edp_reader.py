"""
CIQ (Excel) and EDP (legacy .xls) readers for the fields the node-level
Pre-checks-validation rules need. Kept separate from pre_extract.py since
these read spreadsheets rather than the kget-all log text.
"""
import openpyxl
import re
import xlrd


def sheet_rows_as_dicts(ws):
    """First row = header. Returns a list of dicts, one per data row."""
    rows_iter = ws.iter_rows(values_only=True)
    header = next(rows_iter)
    header = [str(h).strip() if h is not None else '' for h in header]
    out = []
    for row in rows_iter:
        if all(v is None for v in row):
            continue
        out.append({header[i]: row[i] for i in range(min(len(header), len(row)))})
    return out


def load_ciq(path):
    """Open a CIQ workbook. Falls back to a stripped copy when openpyxl
    rejects the stylesheet.

    Confirmed on a real CIQ: some files carry 6-digit RGB colour values (or
    a bare '0') where the spec requires 8-digit aRGB, and openpyxl refuses to
    open the whole workbook over it - 'Colors must be aRGB hex values'. This
    tool only ever reads cell values, never formatting, so the fix is to pad
    those values to valid aRGB in an in-memory copy and open that. The
    original file is never modified."""
    try:
        return openpyxl.load_workbook(path, data_only=True)
    except ValueError as exc:
        if 'aRGB' not in str(exc) and 'stylesheet' not in str(exc):
            raise
        return _load_ciq_with_repaired_styles(path)


def _load_ciq_with_repaired_styles(path):
    import io
    import re
    import shutil
    import zipfile

    def _fix(match):
        val = match.group(1)
        if re.fullmatch(r'[0-9A-Fa-f]{8}', val):
            return match.group(0)
        if re.fullmatch(r'[0-9A-Fa-f]{6}', val):
            return f'rgb="FF{val}"'
        return 'rgb="FF000000"'

    buf = io.BytesIO()
    with zipfile.ZipFile(path) as src, zipfile.ZipFile(buf, 'w', zipfile.ZIP_DEFLATED) as dst:
        for item in src.infolist():
            data = src.read(item.filename)
            if item.filename == 'xl/styles.xml':
                text = data.decode('utf-8', 'replace')
                data = re.sub(r'rgb="([^"]*)"', _fix, text).encode('utf-8')
            dst.writestr(item, data)
    buf.seek(0)
    return openpyxl.load_workbook(buf, data_only=True)


def mixed_mode_rows(ciq_wb):
    return sheet_rows_as_dicts(ciq_wb['Mixed Mode Info'])


def enb_info_rows(ciq_wb):
    return sheet_rows_as_dicts(ciq_wb['eNB Info'])


def gnb_info_5g_info_rows(ciq_wb):
    return sheet_rows_as_dicts(ciq_wb['5G Info'])


def find_mm_row(mm_rows, node_id):
    """Find a Mixed Mode Info row where the node is Primary (Node to be built
    as) OR Secondary (eNodeB Name / gNodeB Name), matched case-insensitively."""
    nid = str(node_id).strip().upper()
    for row in mm_rows:
        candidates = (row.get('Node to be built as'), row.get('eNodeB Name'), row.get('gNodeB Name'))
        if any(str(c).strip().upper() == nid for c in candidates if c is not None):
            return row
    return None


def find_enb_row(enb_rows, node_id):
    nid = str(node_id).strip().upper()
    for row in enb_rows:
        if str(row.get('eNodeB Name', '')).strip().upper() == nid:
            return row
    return None


# ---------------------------------------------------------------------------
# EDP (.xls) — locates the real header row dynamically (row 25 in the samples,
# but not guaranteed fixed — matches QUICKIX's own locate_edp_header_row
# philosophy of not trusting a hardcoded row number).
# ---------------------------------------------------------------------------

def load_edp(path):
    wb = xlrd.open_workbook(path)
    return wb.sheet_by_index(0)


def locate_edp_header_row(ws):
    for r in range(ws.nrows):
        first_cell = ws.cell_value(r, 0)
        if str(first_cell).strip() == 'EDP_SITE_ID':
            return r
    raise ValueError("Could not locate EDP header row (expected 'EDP_SITE_ID' in column A)")


def build_edp_index(ws):
    """Returns (header_list, rows) where rows is a list of dicts keyed by
    header name, one per EDP data row (there can be several rows per site,
    e.g. one per SIAD port entry)."""
    header_row = locate_edp_header_row(ws)
    header = [str(ws.cell_value(header_row, c)).strip() for c in range(ws.ncols)]
    rows = []
    for r in range(header_row + 1, ws.nrows):
        rows.append({header[c]: ws.cell_value(r, c) for c in range(ws.ncols)})
    return header, rows


def edp_rows_for_site(edp_rows, node_id):
    nid = str(node_id).strip().upper()
    return [r for r in edp_rows if str(r.get('SITE_NAME', '')).strip().upper() == nid]


def edp_primary_secondary(edp_rows, node_ids):
    """Rule #3/#31: for each node id in node_ids, look up its EDP row(s) and
    determine Primary (SIAD_PORT_FACING_BBU populated) vs Secondary (blank).
    Returns {node_id: 'PRIMARY'|'SECONDARY'|'NOT FOUND IN EDP'}."""
    result = {}
    for nid in node_ids:
        rows = edp_rows_for_site(edp_rows, nid)
        if not rows:
            result[nid] = 'NOT FOUND IN EDP'
            continue
        port = rows[0].get('SIAD_PORT_FACING_BBU')
        result[nid] = 'PRIMARY' if str(port).strip() not in ('', 'None') else 'SECONDARY'
    return result


def _norm_cabinet(v):
    return re.sub(r'\s+', '', str(v or '')).upper()


def edp_discover_secondary(edp_rows, primary_id):
    """Finds whatever EDP itself thinks the Secondary is for primary_id,
    WITHOUT relying on CIQ having told us its name. Confirmed real EDP
    structure: every row belonging to one physical site — Primary,
    Secondary, and any ancillary-equipment rows — shares the same
    SITE_USID (and EDP_SITE_ID). A site can host SEVERAL primary/secondary
    pairs at once (confirmed real case, SITE_USID 64921: FCL04120/
    FCON094120 AND FCL09220R AND FCL07900R/FCON097900 all on one site) —
    matching on 'any blank-port BBU row in the group' picked whichever one
    came first in iteration order for EVERY primary at that site,
    regardless of whose it actually was.

    A Secondary's own CABINET is its Primary's cabinet number with a
    trailing 'V' (confirmed convention, same one _cabinet_pairing_map in
    rrnrbl_checklist.py already relies on once CIQ tells it who's paired
    with whom) — that suffix match is what actually scopes this to the
    RIGHT pair, not just narrows it. A primary whose own cabinet has no
    'V'-suffixed match anywhere on the site (e.g. FCL09220R, cabinet 'BBU
    02', with no 'BBU 02V' row at this site at all) genuinely has no EDP
    Secondary — returns None rather than guessing.

    Returns the Secondary's own SITE_NAME, or None."""
    prim_rows = edp_rows_for_site(edp_rows, primary_id)
    if not prim_rows:
        return None
    site_usid = str(prim_rows[0].get('SITE_USID', '')).strip()
    prim_cab = _norm_cabinet(prim_rows[0].get('CABINET'))
    if not site_usid or not prim_cab:
        return None
    expected_cab = prim_cab if prim_cab.endswith('V') else prim_cab + 'V'
    for r in edp_rows:
        if str(r.get('SITE_USID', '')).strip() != site_usid:
            continue
        site_name = str(r.get('SITE_NAME', '')).strip()
        if not site_name or site_name.upper() == str(primary_id).strip().upper():
            continue
        if _norm_cabinet(r.get('CABINET')) == expected_cab:
            return site_name
    return None


def resolve_g_name(ciq_wb, mm_row, e_name=None, rfds_pages=None, rfds_bytes=None, mm_rows_all=None):
    """Best-effort recovery of a node's gNodeB Name when Mixed Mode Info's
    own field is blank. This matters because EVERY 5G check in
    checks_sector.py (cells_vs_rfds, radio_type, sector_swap, nr_tac,
    antenna_type_rfds, gnb_du_type, gnb_identity, cell_id_vs_rfds, pci_5g,
    params_5g, arfcn_bw_5g, ssb_5g, nrcelldu_nrcellcu, losses_vs_antenna)
    filters 5G Info/eUtran Parameters rows by `cell.startswith(g_name)` and
    guards with `if not g_name: skip` — a blank g_name doesn't just fail to
    flag anything, it makes every 5G cell for that node vanish from every
    result list, silently. Confirmed real gap: blanking gNodeB Name in
    Mixed Mode Info made an otherwise-untouched node's 5G sectors disappear
    from the Cell/RRU/Antenna verification table entirely, PASS count and
    all, instead of showing up as a mismatch.

    Falls back, in order:
      1. gNBId (same Mixed Mode Info row) matched against gNB Info's or 5G
         Info's own gNBId column — same node, same numeric identity,
         Name field just happens to be blank here.
      2. RFDS CommonName grouping (same mechanism check_primary_secondary
         uses independently for EDP/RFDS) — the OTHER member of e_name's
         RFDS group, but only if THAT name is one CIQ's own gNB Info/5G
         Info tabs recognise as a real gNodeB identity (so a stray LTE-only
         secondary in the RFDS group is never mistaken for a 5G one).
      3. Sole-candidate elimination — only when mm_rows_all is given AND
         this row's own BBU Mode is TMBB/MMBB (a dual-identity node, so a
         5G identity is genuinely expected; SMBB is single-identity by
         definition and must never be guessed at here). If gNB Info/5G
         Info together name exactly ONE gNodeB identity across the whole
         CIQ that no OTHER Mixed Mode Info row has already claimed with
         its own real gNodeB Name, it can only belong to this node.
         Confirmed real case both gNBId and RFDS grouping failed on:
         gNodeB Name AND gNBId both blanked, RFDS is a genuine PDF but its
         table extraction drops this exact wrapped-CommonName row (a known,
         separately-documented limitation) — gNB Info/5G Info still had
         the one and only gNodeB identity in the file, unclaimed by
         anything else. Two or more simultaneously-unresolved TMBB/MMBB
         rows with 2+ unclaimed candidates is genuinely ambiguous and is
         correctly left unresolved rather than guessed.

    Returns None if genuinely unrecoverable — a real single-identity (4G-
    only) node, or no signal survives anywhere in this CIQ/RFDS."""
    g_name = str((mm_row or {}).get('gNodeB Name') or '').strip()
    if g_name:
        return g_name

    known_gnb_names = set()
    gnb_id_map = {}
    if ciq_wb and 'gNB Info' in ciq_wb.sheetnames:
        for r in sheet_rows_as_dicts(ciq_wb['gNB Info']):
            name = str(r.get('gNodeB Name') or '').strip()
            gid = str(r.get('gNBId') or '').strip()
            if name:
                known_gnb_names.add(name)
                if gid:
                    gnb_id_map.setdefault(gid, name)
    if ciq_wb and '5G Info' in ciq_wb.sheetnames:
        for r in sheet_rows_as_dicts(ciq_wb['5G Info']):
            name = str(r.get('gNB Name') or '').strip()
            gid = str(r.get('gNBId') or '').strip()
            if name:
                known_gnb_names.add(name)
                if gid:
                    gnb_id_map.setdefault(gid, name)

    gid = str((mm_row or {}).get('gNBId') or '').strip()
    if gid and gid in gnb_id_map:
        return gnb_id_map[gid]

    if e_name and rfds_pages is not None:
        import rfds_extract as rf
        groups = rf.extract_common_name_groups(rfds_bytes) if rfds_bytes else None
        if groups:
            grp = next((g for g in groups if str(e_name).strip().upper() in [n.upper() for n in g]), None)
            if grp:
                for cand in grp:
                    if cand.upper() != str(e_name).strip().upper() and cand.upper() in {n.upper() for n in known_gnb_names}:
                        return cand

    bbu_mode = str((mm_row or {}).get('BBU Mode') or '').strip().upper()
    if mm_rows_all is not None and bbu_mode in ('TMBB', 'MMBB') and known_gnb_names:
        claimed = {str(r.get('gNodeB Name') or '').strip().upper()
                   for r in mm_rows_all if str(r.get('gNodeB Name') or '').strip()}
        unclaimed = {n for n in known_gnb_names if n.upper() not in claimed}
        if len(unclaimed) == 1:
            return next(iter(unclaimed))

    return None


def find_revision_history_sheet(ciq_wb):
    """Same fuzzy match as QUICKIX's findRevisionHistorySheet(): exact name
    'Revision History' (any case/whitespace) first, else any sheet whose
    name contains 'revision'."""
    for name in ciq_wb.sheetnames:
        if name.strip().lower() == 'revision history':
            return name
    for name in ciq_wb.sheetnames:
        if 'revision' in name.lower():
            return name
    return None


def read_revision_history(ciq_wb):
    """Pulls columns A-E as a raw grid (the sheet mixes two stacked mini-
    tables, so no single header row is forced) — same approach as QUICKIX's
    extractRevisionHistory(). Returns (sheet_name, rows) where rows is a
    list of 5-value lists, blank rows dropped; (None, []) if no such sheet."""
    sheet_name = find_revision_history_sheet(ciq_wb)
    if not sheet_name:
        return None, []
    ws = ciq_wb[sheet_name]
    rows = []
    for row in ws.iter_rows(values_only=True):
        five = [row[i] if i < len(row) and row[i] is not None else '' for i in range(5)]
        if any(str(v).strip() != '' for v in five):
            rows.append(five)
    return sheet_name, rows
