"""
RFDS extraction. RFDS files are zip archives (per-page N.jpeg + N.txt +
manifest.json), NOT real PDFs - identical format to what QUICKIX's
extract_pdf_text() already discovered for Pre/Post-checks PDFs. That
function is ported here as load_rfds_pages(); the OCR itself is already
done (baked into the per-page .txt files), so there is no OCR pipeline to
build - only targeted regex extraction per page, same philosophy as the
rest of this tool.

Two reliability tiers, confirmed against real RFDS samples:
  - 'Cell Details (Final)' pages are clean, row-per-cell text ->
    reliable anchored regex (extract_cell_details).
  - 'Non RF Inventory Details (Final)' pages have genuinely
    garbled OCR ordering (column-major reading order scrambles multi-line
    cell blocks and even splits node names mid-word across a line break,
    e.g. 'SCL05020,SCCN0050' + '20'). Only a coarse whitespace-collapsed
    presence check is reliable there, NOT full row/column parsing - use
    check_nodes_present_together() for the Primary/Secondary leg of rule
    #3/#31, not anything more structured.

Every function here targets the '(Final)' tables only. Validation is always
against the post-change design, so the '(Existing)' variants are never the
comparison target - the option to select them was removed rather than left
as a parameter that could be passed by mistake.
"""
import io
import json
import re
import zipfile


def load_rfds_pages(rfds_bytes):
    """Returns {page_number: page_text}. Ported from QUICKIX's
    extract_pdf_text(), split per-page instead of concatenated, so callers
    can target specific pages/headings without re-searching the whole doc.

    Tries the zip-bundle format first (every RFDS sample seen so far), then
    falls back to pdfplumber for a genuine PDF - confirmed necessary: a real
    user's RFDS came through as an actual PDF, not the zip bundle."""
    if rfds_bytes[:2] == b"PK":
        pages = {}
        with zipfile.ZipFile(io.BytesIO(rfds_bytes)) as zf:
            manifest = json.loads(zf.read("manifest.json"))
            for p in manifest["pages"]:
                text = zf.read(p["text"]["path"]).decode("utf-8", errors="replace")
                pages[p["page_number"]] = text
        return pages

    if rfds_bytes[:5] == b"%PDF-":
        import pdfplumber
        pages = {}
        with pdfplumber.open(io.BytesIO(rfds_bytes)) as pdf:
            for i, page in enumerate(pdf.pages, start=1):
                # x_tolerance=1: pdfplumber's default merges adjacent words with
                # no space between them on this RFDS format (confirmed against a
                # real sample - 'SiteDetails', 'FACode' instead of 'Site Details',
                # 'FA Code'), which breaks every heading-anchored regex below.
                # A lower x_tolerance correctly splits words at real visual gaps.
                pages[i] = page.extract_text(x_tolerance=1) or ""
        return pages

    raise ValueError("RFDS bytes are neither a zip bundle nor a genuine PDF "
                      "(unrecognized file format).")


def find_pages_by_heading(pages, heading_substr):
    """Returns concatenated text of every page whose text contains
    heading_substr (case-insensitive) - handles headings that span 2 pages
    (e.g. 'Cell Details (Final) Page 1 of 2' / 'Page 2 of 2')."""
    sub = heading_substr.lower()
    matched = [text for _, text in sorted(pages.items()) if sub in text.lower()]
    return "\n".join(matched)


_HEADER_NOISE_RE = re.compile(
    r'^(Cell Details \((Existing|Final)\)|Cell ID BBU/CRGNB.*|Page \d+ of \d+|Status)\s*$',
    re.M
)

_RRH_BAND_SUFFIX_RE = re.compile(r'^B\d|/')


def _extract_rrh(rest):
    """Isolates just the RRH/radio-model from the 'rest' capture (which also
    contains Sector Position and/or a bare frequency-band label mashed in,
    e.g. '4449 B5/B12 A-1-1,A-1-4 700' or '8843 B2/B66A AWS'). Keeps the
    first token (model number/name) plus any following band-suffix tokens
    (start with 'B<digit>' or contain '/'), stops at the first token that
    doesn't - confirmed against real Sector-Position and frequency-label
    formats. Without this, radio-type comparison substring-matched against
    the whole mashed string, which usually still worked by luck but wasn't
    actually comparing RRH to RRH."""
    if not rest:
        return rest
    toks = rest.split()
    rrh = toks[:1]
    for tok in toks[1:]:
        if _RRH_BAND_SUFFIX_RE.match(tok):
            rrh.append(tok)
        else:
            break
    return ' '.join(rrh)


def extract_cell_details(pages):
    """Rule #6 / #25: Cell ID -> RCN (+ BBU/CRGNB node, RRH radio model),
    from 'Cell Details (Final)'.

    The Sector-Position column wraps across two lines in real PDFs
    (e.g. 'A-1-9,A-1-10,A-1' then '-11,A-1-12' on the next line), so a
    single-line regex misses every row that wraps. Fix: join continuation
    lines (lines starting with '-digit' or ',') onto the preceding data line
    before applying the row regex, making the whole data record one logical
    line regardless of how many physical lines the PDF wraps it over.

    Returns {cell_id: {'rcn':, 'bbu':, 'rrh_and_secpos':, 'rrh':, 'status':}}.
    """
    heading = 'Cell Details (Final)'
    text = find_pages_by_heading(pages, heading)
    if not text:
        return {}
    clean = _HEADER_NOISE_RE.sub('', text)

    # Join continuation lines onto their preceding data line.
    # Confirmed patterns from real PDFs:
    #   '-11,A-1-12'  — starts with hyphen-digit
    #   '11,B-1-12'   — starts with digit-comma
    #   ',B-1-4'      — starts with comma-letter (sector-position list wrap)
    #
    # Guard against a real second bug (confirmed on RFDS14808/HXL00147): the
    # digit-comma pattern above also matches the START OF A GENUINE NEW ROW
    # whose RRH model number happens to be 2+ digits (e.g. '8843 B2/B66A |
    # RRUS-32 B2 | 4890') - '88' reads as 'digit,digit'. Blindly joining that
    # onto the PRECEDING line corrupted an already-complete row (one that
    # already ends in NEW/EXISTING/UPDATE/AF MIGRATED) and orphaned the
    # displaced row, losing it entirely. A line is only a real continuation
    # if the row being built onto is still incomplete (no status keyword at
    # its end yet).
    _ROW_TERMINATED_RE = re.compile(r'(NEW|EXISTING|UPDATE|AF MIGRATED)\s*$')
    # Second confirmed pattern (same real RFDS): the RRH value itself can
    # wrap onto its own line BEFORE the row's Cell ID/BBU, e.g.:
    #   '8843 B2/B66A | RRUS-32 B2 | 4890'
    #   'B25/B66 HXL04147_9A_1 HXL04147 A-1-9,A-4(D)-10 1900 8 ... EXISTING'
    # Here the wrapped RRH fragment's tail ('8843 ... 4890') sits on its own
    # line, and its head continuation ('B25/B66') lands as the FIRST token
    # of the FOLLOWING line, pushing that row's own Cell ID/BBU to 2nd/3rd
    # position. This is a reorder, not a simple append: rebuild the line as
    # '<cell> <bbu> <rrh tail> <rrh head> <rest of row>'.
    _CELL_TOKEN_RE = re.compile(r'^[A-Za-z0-9]+(?:_\d+)?_(?:N\d{3}|\d)[A-F](?:_\d+)?(?:_[EF])?$')
    raw_lines = [rl.rstrip('\r') for rl in clean.split('\n')]
    joined_lines = []
    i = 0
    while i < len(raw_lines):
        line = raw_lines[i]
        stripped = line.lstrip()
        if (stripped and not _CELL_TOKEN_RE.match(stripped.split(' ', 1)[0])
                and i + 1 < len(raw_lines)):
            nxt_toks = raw_lines[i + 1].lstrip().split()
            if len(nxt_toks) >= 3 and _CELL_TOKEN_RE.match(nxt_toks[1]) and _RRH_BAND_SUFFIX_RE.match(nxt_toks[0]):
                rebuilt = ' '.join([nxt_toks[1], nxt_toks[2], stripped, nxt_toks[0]] + nxt_toks[3:])
                joined_lines.append(rebuilt)
                i += 2
                continue
        is_new_token = bool(re.match(r'^[A-Za-z]-\d', stripped))
        is_digit_lead = bool(re.match(r'^\d', stripped))
        is_hyphen_lead = bool(re.match(r'^-\d', stripped))
        is_comma_lead = bool(re.match(r'^,[A-Za-z]', stripped))
        is_paren_lead = bool(re.match(r'^\)-\d', stripped))    # A-2(D\n)-3,A-2(D)-4 (wrap inside '(D)')
        is_continuation = bool(joined_lines) and not _ROW_TERMINATED_RE.search(joined_lines[-1]) and (
            is_hyphen_lead or                       # -11,A-1-12
            is_digit_lead or                        # 11,B-1-12  OR  '23 64921...' (ambiguous, resolved below)
            is_comma_lead or                        # ,B-1-4
            is_paren_lead or                        # )-3,A-2(D)-4
            is_new_token                             # B-1-5,B-1-6,B-1-7 (wrap lands exactly on a token boundary)
        )
        if is_continuation:
            # A hyphen-lead or comma-lead fragment supplies its own
            # connector character, so it always glues on directly (no
            # space) regardless of what precedes it. The other two forms
            # are genuinely ambiguous — 'A-2(D)-' + '2,...' (mid-token
            # completion) and 'B-1-12' + '23 64921...' (start of the next
            # FIELD) both begin with a digit, and 'A-2(D)-3,' + 'A-2(D)-4,'
            # (next comma-separated list item) and '1900' + 'A-1-5,...'
            # (start of the sector-position list after a different field)
            # both begin with a letter-hyphen-digit token — resolved by
            # whether the line being built onto already ends with its own
            # connector ('-' or ','): if so, this fragment continues that
            # same construct with no added space; otherwise a field/token
            # boundary is being crossed and a space is required, or e.g.
            # '...B-1-12' + '23 64921...' silently fuses into '...B-1-1223
            # 64921...' (confirmed real corruption).
            if is_hyphen_lead or is_comma_lead or is_paren_lead:
                needs_space = False
            else:
                needs_space = not joined_lines[-1].endswith(('-', ','))
            joined_lines[-1] = joined_lines[-1] + (' ' if needs_space else '') + stripped
        else:
            joined_lines.append(line)
        i += 1
    clean = '\n'.join(joined_lines)

    _CELL_DETAILS_ROW_RE = re.compile(
        r'^(?P<cell>\S+)[ \t]+(?P<bbu>\S+)[ \t]+(?P<rest>.+?)[ \t]+(?P<rcn>\d+)[ \t]+'
        r'(?P<useid>[\d.]+\.[A-Za-z0-9.]+)[ \t]+(?P<status>NEW|EXISTING|UPDATE|AF MIGRATED)[ \t\r]*$',
        re.M
    )
    result = {}
    for m in _CELL_DETAILS_ROW_RE.finditer(clean):
        result[m.group('cell')] = {
            'rcn': m.group('rcn'),
            'bbu': m.group('bbu'),
            'rrh_and_secpos': m.group('rest').strip(),
            'rrh': _extract_rrh(m.group('rest').strip()),
            'status': m.group('status'),
        }
    return result


def extract_site_details(pages):
    """Site ID / USID / FA Code / ATOLL Site Name for the report header.

    Two known layouts, confirmed against real samples of each:
      - Genuine PDF (pdfplumber): all fields sit on one clean data line, in
        header order - 'Site ID USID FA Code ATOLL Site Name Location Name
        RF Engineer' followed by 'SIFL013958 64921 10020064 FCL04120 ...'.
        Extra trailing columns (RF Engineer) are tolerated, not required.
      - Zip-bundle (OCR): column-major-scrambled - the FA Code VALUE (a bare
        8-digit number) lands several lines above its own header, so it's
        recovered separately as the lone 8-digit token on the page (site IDs
        are alphanumeric, USID is 6 digits, lat/long carry decimal points,
        so a bare 8-digit integer is unambiguous). Site ID/USID/ATOLL still
        come from the header+dataline pattern, just without FA Code inline.
    """
    text = find_pages_by_heading(pages, 'Site Details')
    if not text:
        return {}
    out = {}

    m = re.search(
        r'Site ID\s+USID\s+FA Code\s+ATOLL Site Name\s+Location Name[^\n]*\r?\n'
        r'(?P<site_id>\S+)\s+(?P<usid>\d+)\s+(?P<fa_code>\d+)\s+(?P<atoll>\S+)',
        text)
    if m:
        out['site_id'] = m.group('site_id')
        out['usid'] = m.group('usid')
        out['fa_code'] = m.group('fa_code')
        out['atoll_site_name'] = m.group('atoll')
        return out

    m = re.search(
        r'Site ID USID FA Code ATOLL Site Name Location Name\s*\r?\n'
        r'(?P<site_id>\S+)\s+(?P<usid>\d+)\s+(?P<atoll>\S+)',
        text)
    if m:
        out['site_id'] = m.group('site_id')
        out['usid'] = m.group('usid')
        out['atoll_site_name'] = m.group('atoll')
    fa = re.findall(r'(?<![\d.])\d{8}(?![\d.])', text)
    if fa:
        out['fa_code'] = fa[0]
    return out


_PORT_ROW_START_RE = re.compile(r'\b([A-Z]-\d{1,2})\s+')
_CELL_TOKEN_RE = re.compile(r'\b[A-Z][A-Z0-9]*_[A-Z0-9_]+\b')


def extract_port_level_details(pages):
    """Antenna Vendor/Model per cell, from 'Port Level Details (Final)'.

    Fixes vs previous version:
    - Antenna model like 'MA-2L4M-65F8-A12P-HG' wraps across two lines in
      real PDFs ('MA-2L4M-65F8-A12P-' then 'HG'). The old code collapsed all
      whitespace first, turning it into 'MA-2L4M-65F8-A12P- 1 HG' (port
      number absorbed into the model). Fix: join dangling-hyphen lines before
      collapsing whitespace, so the model is 'MA-2L4M-65F8-A12P-HG'.
    - Port number (1-2 digit integer between model and first cell token) must
      be stripped from the pre-cell prefix to isolate the pure vendor+model.

    Also returns a grouped structure so the caller can show all cells sharing
    the same antenna position as a list, not just per-cell lookups.

    Returns {cell_name: {'sec_pos':, 'vendor_model':, 'shared_cells': [...]}}
    where shared_cells is every cell on the same sec_pos+model antenna.
    """
    text = find_pages_by_heading(pages, 'Port Level Details (Final)')
    if not text:
        return {}

    # Fix 1: Join multi-line model/cell fragments. Three patterns confirmed:
    #   a) Dangling-hyphen: 'MA-2L4M-65F8-A12P- 1\nHG' — hyphen ends the token,
    #      next line is the suffix. Join by removing the newline (not the hyphen).
    #   b) Port number on same line as model prefix, suffix on next line:
    #      same as (a) after the pre-collapse join handles it.
    # Strategy: collapse all newlines into spaces first (like before), then
    # fix the 'MODEL-PREFIX PORTNUMBER SUFFIX' pattern in the model string.
    pass  # handled below after whitespace collapse

    clean = text.replace('\r\n', ' ').replace('\n', ' ')
    clean = re.sub(r'Sec-Pos\s+Vendor\s+Model\s+Port\s+Cells\s+Gain\s+Elec\s+Tilt\s+Azimuth\s+Offset\s+Coax\s+Type\s+Coax\s+Length', ' ', clean)
    clean = re.sub(r'Port Level Details \(Final\)', ' ', clean)
    clean = re.sub(r'Page \d+ of \d+', ' ', clean)
    clean = re.sub(r'\s+', ' ', clean).strip()

    starts = [m.start() for m in _PORT_ROW_START_RE.finditer(clean)]
    starts.append(len(clean))

    # First pass: collect all (sec_pos, vendor_model) -> [cells] groups
    groups = {}   # (sec_pos, vendor_model) -> set of cells
    per_cell = {} # cell -> (sec_pos, vendor_model)

    for i in range(len(starts) - 1):
        chunk = clean[starts[i]:starts[i + 1]].strip()
        m = _PORT_ROW_START_RE.match(chunk + ' ')
        if not m:
            continue
        sec_pos = m.group(1)
        rest = chunk[m.end():].strip()
        cells = _CELL_TOKEN_RE.findall(rest)
        if not cells:
            continue
        pre = rest[:rest.find(cells[0])].strip()
        pre_tokens = pre.split()
        # Strip trailing port-number tokens (1-3 bare digits). Also handle
        # the pattern 'MODEL-PREFIX PORTNUMBER SUFFIX' where the model suffix
        # landed on the next source line (e.g. 'MA-2L4M-65F8-A12P- 1 HG'):
        # the model is 'MA-2L4M-65F8-A12P-HG' and '1' is the port number.
        # Detect: bare 1-2 digit token with a hyphen-ending token before it
        # and a non-numeric token after it → remove the digit and merge.
        cleaned = []
        j = 0
        while j < len(pre_tokens):
            tok = pre_tokens[j]
            if (re.fullmatch(r'\d{1,2}', tok)
                    and j > 0 and cleaned and cleaned[-1].endswith('-')
                    and j + 1 < len(pre_tokens) and not pre_tokens[j+1][0].isdigit()):
                # skip the port-number token; next token glues to the hyphen
                j += 1
                cleaned[-1] = cleaned[-1] + pre_tokens[j]
            else:
                cleaned.append(tok)
            j += 1
        pre_tokens = cleaned
        while pre_tokens and re.fullmatch(r'\d{1,3}', pre_tokens[-1]):
            pre_tokens.pop()
        vendor_model = ' '.join(pre_tokens)
        key = (sec_pos, vendor_model)
        if key not in groups:
            groups[key] = set()
        for c in cells:
            groups[key].add(c)
            if c not in per_cell:
                per_cell[c] = (sec_pos, vendor_model)

    # Build result: for each cell, include the full list of cells sharing
    # that antenna position so the UI can display them as a group.
    result = {}
    for (sec_pos, vendor_model), cell_set in groups.items():
        shared = sorted(cell_set)
        for c in cell_set:
            result[c] = {'sec_pos': sec_pos, 'vendor_model': vendor_model,
                          'shared_cells': shared}
    return result


_RF_INV_ETYPES = ('ANTENNA', 'RRH', 'TMA', 'DIPLEXER', 'DUPLEXER', 'COMBINER', 'SWITCH', 'JUMPER')
_RF_INV_ANCHOR_RE = re.compile(
    r'([A-Z]-\d+(?:\(D\))?(?:,[A-Z]?-?\d+)?)\s+(' + '|'.join(_RF_INV_ETYPES) + r')\b'
)
_RF_INV_STATUS_RE = re.compile(r'\b(EXISTING|NEW|UPDATE|REMOVE|AF MIGRATED)\b')
_RF_INV_TECH_RE = re.compile(r'^(5G|LTE|4G)(,(5G|LTE|4G))*$')

# Column-header vocabulary for 'RF Inventory Details (Final)'. The header
# row wraps across a VARYING number of lines with a VARYING column order
# between RFDS exports (confirmed: every sample in one batch wrapped as
# 'Model Cascaded' + 'From/Cpri Info Sec- Pos Equipment Type Vendor Linked
# Cells ...', while a real user's export wrapped as '... Cascaded Sec- Pos
# Equipment Type Vendor Model Linked Cells ...' — Model in a different
# position entirely). Matching fixed header strings therefore silently
# fails on any export that wraps differently, and the unstripped header
# line is then consumed as a data record: its text becomes the 'model'
# for the first antenna row and the row-splitting shifts, so later cells
# lose their antenna entirely and report NOT FOUND. Detecting a header
# line by its VOCABULARY instead is order- and wrap-independent.
_RF_INV_HEADER_WORDS = {
    'model', 'cascaded', 'from/cpri', 'from', 'cpri', 'info', 'sec-', 'sec',
    'pos', 'sec-pos', 'equipment', 'type', 'equipmenttype', 'vendor', 'linked',
    'cells', 'linkedcells', 'technology', 'rrh', 'position', 'common', 'name',
    'commonname', 'status',
}
# Single-token header lines: only strip words that can never be a data
# value in this table (a Model is never literally 'Model', a Vendor never
# literally 'Vendor'). 'rrh'/'pos'/'type'/'name' are deliberately NOT here
# — 'RRH' is a real EquipmentType value in data rows.
_RF_INV_HEADER_SOLO = {'model', 'cascaded', 'technology', 'vendor', 'status', 'from/cpri'}


def _is_rf_inv_header_line(line):
    toks = line.split()
    if not toks:
        return False
    if not all(t.lower().strip(':') in _RF_INV_HEADER_WORDS for t in toks):
        return False
    return len(toks) >= 2 or toks[0].lower().strip(':') in _RF_INV_HEADER_SOLO


def _extract_rf_inventory_antennas_from_tables(rfds_bytes):
    """Genuine-PDF path for extract_rf_inventory_antennas().

    The text layer of a genuine-PDF RFDS interleaves each row's fields
    across the lines above AND below its anchor line (confirmed on a real
    user file — 'MA-2L4M-65F8 HXL01791_7A_1 -/-' / 'A-1 ANTENNA ACE
    5G,LTE - - UPDATE' / '-A12P-HG HXL01791_9A_1 -/-'), so no
    flatten-then-split-on-status scheme can recover it: the model comes out
    truncated with stray '-/-' glued on, and every linked cell listed
    BELOW the anchor is swallowed into the NEXT record, vanishing from
    this one (which is what made real antennas report NOT FOUND).

    pdfplumber's table extraction recovers the true cell boundaries, with
    the full LinkedCells list and both model fragments inside single cells.
    Returns None if this isn't a genuine PDF or no usable table is found,
    so the caller can fall back to the zip-bundle text parser."""
    if not rfds_bytes or rfds_bytes[:5] != b"%PDF-":
        return None
    pages_text = load_rfds_pages(rfds_bytes)
    heading = 'RF Inventory Details (Final)'
    target = {n for n, t in pages_text.items() if heading.lower() in t.lower()}
    if not target:
        return None

    import pdfplumber
    result, found_any = {}, False
    with pdfplumber.open(io.BytesIO(rfds_bytes)) as pdf:
        for page_num in sorted(target):
            for table in pdf.pages[page_num - 1].extract_tables():
                hdr_idx = None
                for i, row in enumerate(table):
                    norm = [re.sub(r'\s+', '', str(c or '')) for c in row]
                    if 'EquipmentType' in norm and 'LinkedCells' in norm:
                        hdr_idx = i
                        header = norm
                        break
                if hdr_idx is None:
                    continue
                col = {name: header.index(name) for name in
                       ('Sec-Pos', 'EquipmentType', 'Vendor', 'Model', 'LinkedCells')
                       if name in header}
                if 'EquipmentType' not in col or 'LinkedCells' not in col:
                    continue
                for row in table[hdr_idx + 1:]:
                    if len(row) <= max(col.values()):
                        continue
                    etype = re.sub(r'\s+', '', str(row[col['EquipmentType']] or '')).upper()
                    # 'INTEGRATED ANTENNA RADIO' (AIR-series: antenna built
                    # into the radio) carries a real antenna Model and
                    # LinkedCells, and the zip-bundle text path already
                    # matches these via its ANTENNA substring anchor — so
                    # excluding them here would make the two RFDS formats
                    # disagree, and would report NOT FOUND for every AIR
                    # cell whose model the RFDS plainly states.
                    if etype not in ('ANTENNA', 'INTEGRATEDANTENNARADIO'):
                        continue
                    found_any = True
                    # Model fragments wrap inside the cell as separate lines;
                    # a fragment starting with '-' is a mid-word continuation
                    # ('MA-2L4M-65F8' + '-A12P-HG'), anything else is a real
                    # space-separated token.
                    model = ''
                    for frag in str(row[col['Model']] or '').split('\n'):
                        frag = frag.strip()
                        if not frag:
                            continue
                        model = frag if not model else (
                            model + frag if frag.startswith('-') else model + ' ' + frag)
                    cells = [c.strip() for c in str(row[col['LinkedCells']] or '').split('\n') if c.strip()]
                    cells = [c for c in cells if _CELL_TOKEN_RE.fullmatch(c)]
                    if not cells:
                        continue
                    vendor = re.sub(r'\s+', ' ', str(row[col.get('Vendor', 0)] or '')).strip() if 'Vendor' in col else ''
                    sec_pos = re.sub(r'\s+', '', str(row[col.get('Sec-Pos', 0)] or '')) if 'Sec-Pos' in col else ''
                    shared = sorted(cells)
                    for c in cells:
                        if c not in result:
                            result[c] = {'model': model, 'vendor': vendor,
                                          'sec_pos': sec_pos, 'shared_cells': shared}
    return result if found_any else None


def extract_rf_inventory_antennas(pages, rfds_bytes=None):
    """Antenna Model + linked cells, from 'RF Inventory Details (Final)',
    filtered to EquipmentType == 'ANTENNA'. This is the authoritative
    per-antenna record — a fix over the previous approach of reading
    'Port Level Details (Final)' and inferring per-cell model text from
    Sec-Pos position, which doesn't carry an explicit equipment-type flag
    and mis-split multi-line models around embedded port numbers.

    Two real-PDF quirks fixed here:
      - The Model field sometimes wraps mid-word across two lines with NO
        hyphen ('MA-2L4M-65F8' / '-A12P-HG') — joined without inserting a
        space when the continuation starts with '-', since a hyphen-started
        continuation is always a mid-word wrap, never a new column value.
      - Some RFDS fonts encode a literal hyphen inside the Model string as
        an unprintable control character (\\x02) instead of '-' (confirmed:
        'CMA-BTLBHH\\x026516-20-20' should read 'CMA-BTLBHH-6516-20-20') —
        replaced before parsing.

    AIR-series radios have no separate ANTENNA row here (the antenna is
    integrated into the radio) — those cells simply won't appear in the
    result, which is correct, not a gap; callers already treat AIR-series
    antenna/loss checks as exempt rather than missing.

    Returns {cell_name: {'model':, 'vendor':, 'sec_pos':, 'shared_cells':}}
    where shared_cells lists every cell fed by that same physical antenna.
    """
    if rfds_bytes is not None:
        via_tables = _extract_rf_inventory_antennas_from_tables(rfds_bytes)
        if via_tables:
            return via_tables
    text = find_pages_by_heading(pages, 'RF Inventory Details (Final)')
    if not text:
        return {}
    text = text.replace('\x02', '-').replace('\u0002', '-')
    text = re.sub(r'^RF Inventory Details \(Final\).*$', '', text, flags=re.M)
    text = re.sub(r'^Page \d+ of \d+\s*$', '', text, flags=re.M)
    # Vocabulary-based header removal — see _is_rf_inv_header_line.
    text = '\n'.join('' if _is_rf_inv_header_line(ln.rstrip('\r').strip()) else ln
                     for ln in text.split('\n'))

    # Smart line-join: a continuation line starting with '-' is always a
    # mid-word wrap (join with no space); anything else is a normal
    # column/row wrap (join with a single space).
    flat_parts = []
    for raw_line in text.split('\n'):
        line = raw_line.rstrip('\r').strip()
        if not line:
            continue
        if flat_parts and line.startswith('-'):
            flat_parts[-1] = flat_parts[-1] + line
        elif (flat_parts and re.search(r'[A-Z]$', flat_parts[-1])
              and re.fullmatch(r'[A-Z]+', line.split(' ', 1)[0])
              and not _CELL_TOKEN_RE.match(line)
              and not _RF_INV_STATUS_RE.match(line)
              and not _PORT_ROW_START_RE.match(line + ' ')):
            # Mid-word OCR wrap with no hyphen, e.g. 'ROSENBERG'/'ER' or
            # 'ANDREW/CO'/'MMSCOPE' (real samples: 'ROSENBERGER' and
            # 'ANDREW/COMMSCOPE' each split across two lines with no
            # trailing space or hyphen on the first line). Guard: only
            # fires when the prev line ends in a letter and this line's
            # first token is a bare all-caps word that isn't a cell token,
            # status keyword, or a new Sec-Pos row start - so normal row
            # boundaries still split as before.
            flat_parts[-1] = flat_parts[-1] + line
        else:
            flat_parts.append(line)
    flat = re.sub(r'\s+', ' ', ' '.join(flat_parts)).strip()

    # Split into one record per row, using the Status keyword as the
    # (reliable, closed-vocabulary) row terminator.
    records, pos = [], 0
    for m in _RF_INV_STATUS_RE.finditer(flat):
        records.append(flat[pos:m.end()].strip())
        pos = m.end()

    result = {}
    for rec in records:
        m = _RF_INV_ANCHOR_RE.search(rec)
        if not m or m.group(2) != 'ANTENNA':
            continue
        before, after = rec[:m.start()], rec[m.end():]
        cells = _CELL_TOKEN_RE.findall(before)
        if not cells:
            continue
        model = re.sub(r'\s+', ' ', _CELL_TOKEN_RE.sub('', before)).strip()
        vendor_tokens = []
        for tok in after.split():
            if re.fullmatch(r'-(/-)?', tok) or _RF_INV_STATUS_RE.fullmatch(tok) or _RF_INV_TECH_RE.match(tok):
                break
            vendor_tokens.append(tok)
        vendor = ' '.join(vendor_tokens)
        shared = sorted(cells)
        for c in cells:
            if c not in result:
                result[c] = {'model': model, 'vendor': vendor, 'sec_pos': m.group(1), 'shared_cells': shared}
    return result


def load_rfds_tables(rfds_bytes):
    """Returns {page_number: [[row, ...], ...]} using pdfplumber's table
    extraction - genuine PDFs only (returns {} for the zip-bundle format,
    which has no real table structure to extract, only OCR'd text).

    Kept separate from load_rfds_pages() deliberately: extract_tables()
    gives much cleaner per-row columns than plain text for well-behaved
    rows (confirmed against a real sample), but the rest of this module's
    functions are text-regex-based and already handle both RFDS formats -
    changing their input shape to accommodate tables would touch a lot of
    already-working code for a capability only two new functions need."""
    if rfds_bytes[:5] != b"%PDF-":
        return {}
    import pdfplumber
    tables = {}
    with pdfplumber.open(io.BytesIO(rfds_bytes)) as pdf:
        for i, page in enumerate(pdf.pages, start=1):
            t = page.extract_tables()
            if t:
                tables[i] = t
    return tables


def _xmu_present_via_text(pages, node_id):
    """Best-effort fallback for when extract_xmu_by_node's table extraction
    has no entry for this node at all — confirmed real cause: the same
    wrapped-CommonName table collapse documented on extract_common_name_
    groups (2788253_RFDS-37756.pdf, FCL04120/FCON094120's row lands
    entirely in column 0, so this node never gets a table row to read
    'XMU' off of, even though it's genuinely on the page).

    NOT as reliable as the table path — this is a bounded text window,
    not a real per-row field read, so it's used only when the table
    extraction found nothing for this node. Finds this node's LAST
    Linked-Cells mention (equipment rows list all their cells before the
    vendor/model/config line, so the last cell mention sits closest to
    that line) and checks for 'XMU' in a window just after it — bounded
    tightly (150 chars) rather than to the next record's own marker,
    since there's no reliable single delimiter token confirmed to appear
    in every RFDS export. Returns None (not False) if this node's own
    cell prefix isn't found on the page at all — 'not present' should
    fall through to the existing 'node not in RFDS' handling, not be
    reported as 'no XMU'."""
    text = find_pages_by_heading(pages, 'Non RF Inventory Details (Final)')
    if not text:
        return None
    nid = str(node_id).strip().upper()
    matches = list(re.finditer(re.escape(nid) + r'_\S+', text.upper()))
    if not matches:
        return None
    window = text[matches[-1].end():matches[-1].end() + 150]
    return 'XMU' in window.upper()


def extract_xmu_by_node(rfds_bytes):
    """Node-specific XMU presence (rule #27) via pdfplumber table extraction,
    scoped to the 'Non RF Inventory Details' page(s) only (found via the
    text-based heading search first - confirmed necessary: scanning every
    page's tables blindly pulls in unrelated tables from Design Summary, RF
    Inventory, etc. and produces garbage node names). Returns {node: bool},
    or None if this RFDS is the zip-bundle/OCR format (no tables to
    extract) - callers should fall back to the page-wide check in that case.

    Verified against every RFDS sample available (5 genuine PDFs, incl. 3
    with real XMU rows):
      - XMU is always recorded in the EquipmentConfiguration column, as
        '1xXMU' within a '1x6601/ 1x5216/ 1xXMU'-style stack. It is never
        in Model or EquipmentType, so only Configuration is checked.
      - Column positions shift between the '(Existing)' and '(Final)' pages
        of the same document (11 vs 10 columns - 'EquipmentType' splits
        across two cells on one), so each table's own header row is located
        and column indices read from it rather than assumed.
      - CommonName legitimately contains newlines when a value wraps
        ('OKTN000082,OKL02\\n082' is ONE node pair, not a merged cell), so
        newlines are stripped and the value comma-split - an earlier version
        skipped these rows as 'merged' and would have missed a real XMU.
    """
    if rfds_bytes[:5] != b"%PDF-":
        return None

    pages_text = load_rfds_pages(rfds_bytes)
    heading = 'Non RF Inventory Details (Final)'
    target_pages = {num for num, text in pages_text.items() if heading.lower() in text.lower()}
    if not target_pages:
        return None

    import pdfplumber
    result = {}
    found_any_row = False
    with pdfplumber.open(io.BytesIO(rfds_bytes)) as pdf:
        for page_num in sorted(target_pages):
            for table in pdf.pages[page_num - 1].extract_tables():
                col_idx = {}
                for row in table:
                    cells = [str(c).replace('\n', '').strip() if c else '' for c in row]
                    if 'CommonName' in ''.join(cells):
                        for i, c in enumerate(cells):
                            if c == 'Vendor':
                                col_idx['vendor'] = i
                            elif c == 'Model':
                                col_idx['model'] = i
                            elif c == 'CommonName':
                                col_idx['common_name'] = i
                            elif 'Configuration' in c:
                                col_idx['config'] = i
                        break
                if 'common_name' not in col_idx or 'config' not in col_idx:
                    continue

                for row in table:
                    if len(row) <= max(col_idx.values()):
                        continue
                    cells = [str(c).replace('\n', '') if c else '' for c in row]
                    common_name = cells[col_idx['common_name']].strip()
                    config = cells[col_idx['config']].strip()
                    vendor = cells[col_idx['vendor']].strip() if 'vendor' in col_idx else ''
                    if not (common_name and vendor) or common_name == 'CommonName':
                        continue
                    found_any_row = True
                    has_xmu = 'XMU' in config.upper()
                    for name in common_name.split(','):
                        name = name.strip()
                        if name:
                            result[name] = result.get(name, False) or has_xmu

    if found_any_row:
        return result

    # Fallback: some documents collapse the whole data row into one merged
    # cell, leaving no column structure at all (confirmed on a real sample:
    # every column but the first comes back None, and the plain-text version
    # splits the row so the node name and its '1xXMU' marker land on
    # different lines). The LinkedCells values are still intact there, and
    # every cell name is prefixed with its own node - so node identity is
    # recoverable from those prefixes even when CommonName isn't readable.
    # XMU presence can only be established page-wide in this case, so it's
    # applied to every node found on the page rather than to one specific
    # node - which is the same precision the page-wide check gives, but at
    # least scoped to the correct page's nodes.
    page_text = '\n'.join(pages_text[n] for n in sorted(target_pages))
    if 'XMU' not in page_text.upper():
        return None
    nodes = set(re.findall(r'^([A-Z]{2,}[A-Z0-9]*?)_(?:N\d{3}|\d)[A-F](?:_\d+)?(?:_[EF])?$',
                            page_text, re.M))
    if not nodes:
        return None
    return {n: True for n in nodes}


def _has_single_equipment_record(pages):
    """True if 'Non RF Inventory Details (Final)' has exactly one equipment
    row (one 'BBU'/'RAN PROCESSOR'/model mention) - used to justify grouping
    every node prefix on the page as one CommonName pair when the table
    structure has fully collapsed (see check_nodes_present_together)."""
    text = find_pages_by_heading(pages, 'Non RF Inventory Details (Final)')
    if not text:
        return None
    records = len(re.findall(r'\bBBU\s+ERICSSON\b', text, re.I))
    return records == 1


def extract_common_name_groups(rfds_bytes):
    """Returns the list of CommonName node-groups from 'Non RF Inventory
    Details (Final)', e.g. [['HXL00147'], ['HXL04147','HXIN010147']].
    Genuine-PDF RFDS only (returns None for the zip-bundle/OCR format).

    Uses pdfplumber table extraction rather than page text, because a
    CommonName value that wraps mid-word is NOT recoverable from the text
    version: the two fragments end up separated by a whole interleaved line
    (confirmed on a real sample - 'HXL04147,HXIN0101' and its trailing '47'
    are split by the row's own 'BBU ERICSSON ...' line), so collapsing
    whitespace glues the fragment to the wrong neighbour. The table cell
    keeps them together as 'HXL04147,HXIN0101\\n47', which rejoins cleanly."""
    if rfds_bytes[:5] != b"%PDF-":
        return None

    pages_text = load_rfds_pages(rfds_bytes)
    heading = 'Non RF Inventory Details (Final)'
    target_pages = {num for num, text in pages_text.items() if heading.lower() in text.lower()}
    if not target_pages:
        return None

    import pdfplumber
    groups = []
    with pdfplumber.open(io.BytesIO(rfds_bytes)) as pdf:
        for page_num in sorted(target_pages):
            for table in pdf.pages[page_num - 1].extract_tables():
                cn_idx = None
                for row in table:
                    cells = [str(c).replace('\n', '').strip() if c else '' for c in row]
                    if 'CommonName' in cells:
                        cn_idx = cells.index('CommonName')
                        break
                if cn_idx is None:
                    continue
                for row in table:
                    if len(row) <= cn_idx or not row[cn_idx]:
                        continue
                    value = str(row[cn_idx]).replace('\n', '').strip()
                    if not value or value == 'CommonName':
                        continue
                    names = [n.strip() for n in value.split(',') if n.strip()]
                    if names:
                        groups.append(names)
    return groups or None


def check_nodes_present_together(pages, node_a, node_b, rfds_bytes=None):
    """Rule #3/#31 (RFDS leg): are node_a and node_b listed together as one
    CommonName group?

    Prefers the table-based lookup (extract_common_name_groups) when the
    RFDS bytes are available, since a wrapped CommonName can't be
    reassembled from page text in the general case - see that function's
    docstring. A POSITIVE table match is trusted immediately. A table
    MISS is not trusted as final, though - confirmed real case
    (2788253_RFDS-37756.pdf, FCL04120/FCON094120): pdfplumber's table
    extraction collapsed that entire row (26 wrapped LinkedCells lines)
    into a single blob cell, landing everything in column 0 and leaving
    the CommonName column None for that row - extract_common_name_groups
    correctly skips a row with no CommonName cell, so the group came back
    incomplete ([['FCL09220R']], missing this pair entirely) despite
    reading a real PDF with a real table. Falls through to the
    single-record and whitespace-collapsed text checks below whenever the
    table says 'not found', rather than returning False outright - those
    read PLAIN PAGE TEXT rather than pdfplumber's table-cell grid, so
    they don't inherit this row's cell-alignment failure (confirmed: the
    whitespace-collapsed text check finds 'FCL04120,FCON094120' correctly
    on this exact PDF, cleanly rejoining the 'FCON0941'+'20' line-wrap -
    the table path is a different code path with a different, unrelated
    failure mode).

    If the table extraction ran at all (rfds_bytes given, groups not
    None) but STILL can't confirm the pair even after the text fallback,
    the result is None (inconclusive/not checked), not False - a
    confirmed false negative already came from this exact combination
    once; a caller-visible MISMATCH should require an unambiguous
    contradiction, not just every fallback failing to find a match."""
    table_was_inconclusive = False
    if rfds_bytes is not None:
        groups = extract_common_name_groups(rfds_bytes)
        if groups is not None:
            a, b = str(node_a).strip().upper(), str(node_b).strip().upper()
            for g in groups:
                upper = [n.upper() for n in g]
                if a in upper and b in upper:
                    return True
            # Not found in the table extraction - fall through rather than
            # returning False here; see docstring for the confirmed false-
            # negative case this avoids.
            table_was_inconclusive = True
        # Table extraction found no usable rows at all - confirmed real case:
        # a 33-cell LinkedCells list collapsed the entire equipment row into
        # one merged cell, and the CommonName value itself wraps mid-digit
        # across an interleaved 'BBU ERICSSON ...' line ('FSNN0904' + '52'),
        # which defeats whitespace-collapse too. Fall back to a narrower but
        # defensible signal: if the page has exactly ONE equipment record
        # (one 'BBU'/model mention), every distinct node prefix on it
        # necessarily belongs to that one record.
        single_record = _has_single_equipment_record(pages)
        if single_record is True:
            prefixes = extract_non_rf_inventory_cells(pages)
            node_prefixes = {re.match(r'^([A-Za-z0-9]+?)_', c).group(1) for c in prefixes
                              if re.match(r'^([A-Za-z0-9]+?)_', c)}
            a, b = str(node_a).strip().upper(), str(node_b).strip().upper()
            if a in {p.upper() for p in node_prefixes} and b in {p.upper() for p in node_prefixes}:
                return True

    heading = 'Non RF Inventory Details (Final)'
    text = find_pages_by_heading(pages, heading)
    if not text:
        return None  # RFDS page not found - caller should treat as "not checked", not "failed"
    collapsed = re.sub(r'\s+', '', text)
    pair_a = f"{node_a},{node_b}"
    pair_b = f"{node_b},{node_a}"
    if (pair_a in collapsed) or (pair_b in collapsed):
        return True

    # A CommonName wrap where the completing fragment lands several lines
    # AWAY, not immediately adjacent - confirmed real case on this exact
    # PDF (2788253_RFDS-37756.pdf): pdfplumber's text extraction
    # interleaves a wrapped cell with OTHER columns' own line-wraps, so
    # 'FCL04120,FCON0941' and its completing '20' end up separated by a
    # whole unrelated line ('BBU ERICSSON FCL04120_9B_1 LTE,5G 64921 -
    # UPDATE') that a plain whitespace-collapse can't bridge. Uses a
    # SINGLE-space normalization (not the fully-collapsed 'collapsed'
    # above) for the window search specifically: collapsing all
    # whitespace to nothing merges '6672 20' into '667220', destroying
    # the very space needed to tell where the wrapped suffix ends -
    # confirmed real failure of an earlier version of this fix. A single
    # space preserves that boundary so \b can isolate '20' from the
    # unrelated '6672' next to it.
    spaced = re.sub(r'\s+', ' ', text)
    for name_a, name_b in ((node_a, node_b), (node_b, node_a)):
        a, b = str(name_a).strip().upper(), str(name_b).strip().upper()
        prefix_pat = re.escape(a) + ","
        for m in re.finditer(prefix_pat, spaced):
            start = m.end()
            for cut in range(len(b) - 1, 0, -1):
                prefix, suffix = b[:cut], b[cut:]
                if spaced[start:start + len(prefix)] != prefix:
                    continue
                window = spaced[start + len(prefix):start + len(prefix) + 200]
                if re.search(r'\b' + re.escape(suffix) + r'\b', window):
                    return True
                break  # longest matching prefix found for this occurrence; a
                       # shorter one would just be a substring of it — no point trying more

    # Every check missed. If the table extraction ran and returned SOME
    # groups (just not this one), that's a confirmed-possible false
    # negative, not proof of a real mismatch - report inconclusive rather
    # than a caller-visible MISMATCH.
    return None if table_was_inconclusive else False


def extract_non_rf_inventory_cells(pages):
    """Rule #18: which cells are mentioned at all on the Non RF Inventory
    page, for a coarse presence check (CIQ cell exists somewhere in RFDS).
    Same reliability caveat as check_nodes_present_together() - this finds
    cell-name-shaped tokens anywhere on the page, it does not attempt to
    attribute them to a specific equipment row."""
    heading = 'Non RF Inventory Details (Final)'
    text = find_pages_by_heading(pages, heading)
    if not text:
        return set()
    # cell names: SITEID[_USID]_<digit or Nxxx><sector letter>_<carrier>[_suffix]
    # The optional middle segment matters: some gNodeB cell names carry the
    # site USID between node and band (confirmed on a real sample -
    # 'HXIN090147F_056338_N077A_1'), and a pattern without it silently
    # matched none of that node's cells, flagging all six as missing from an
    # RFDS that plainly contained them.
    return set(re.findall(r'\b[A-Z0-9]+(?:_\d+)?_(?:N\d{3}|\d)[A-F](?:_\d+)?(?:_[EF])?\b', text))
