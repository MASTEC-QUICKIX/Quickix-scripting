"""
Parser for Ericsson moshell "Pre kget-all" / hget log files.

These logs are a transcript of an interactive moshell session: each command the
engineer typed is echoed back as `<NODE>> <command>`, followed by its output.
Most output blocks we care about are fixed-width tables of the form:

    =================================================================================================================
    MO                gNBId   gNBIdLength
    =================================================================================================================
    GNBCUCPFunction=1 5590877 26
    =================================================================================================================
    Total: 1 MOs

A single command can produce several such tables back-to-back (e.g. one hget
block per matching MO type). We parse every table under every command into a
list of dict rows, keyed by the header tokens.
"""
import re
import functools

_PROMPT_RE = re.compile(r'^(?P<node>[A-Za-z0-9_]+)>\s*(?P<cmd>.*)$')
_SEP_RE = re.compile(r'^=+\s*$')
_TOTAL_RE = re.compile(r'^Total:\s*(\d+)\s*MOs?\s*$', re.I)


@functools.lru_cache(maxsize=16)
def split_commands(text):
    """Split a full log into a list of (node_id, command, block_text) in order.

    A "command" starts at a `<NODE>> <command_text>` prompt line and its block
    runs until the next prompt line (or end of file). Non-command prompt lines
    (bare `<NODE>> ` with nothing after it) are skipped as segment boundaries
    but don't start a new named command.
    """
    segments = []
    current = None  # (node, cmd, lines)
    for raw_line in text.splitlines():
        line = raw_line.rstrip('\n')
        m = _PROMPT_RE.match(line.strip())
        if m and m.group('cmd'):
            if current is not None:
                segments.append((current[0], current[1], "\n".join(current[2])))
            current = (m.group('node'), m.group('cmd').strip(), [])
        else:
            if current is not None:
                current[2].append(line)
    if current is not None:
        segments.append((current[0], current[1], "\n".join(current[2])))
    return segments


def _column_spans(header_line):
    """Given a header line like 'Proxy  Adm State     Op. State     MO',
    return [(name, start, end), ...] column spans.

    Column boundaries are runs of 2+ spaces — moshell consistently pads
    between distinct columns with 2+ spaces, but multi-word column labels
    (e.g. 'Adm State', 'Op. State') use a single space internally. Splitting
    on every whitespace-delimited token (the naive approach) incorrectly
    treats 'Adm' and 'State' as separate columns and corrupts every row
    under a two-word header — confirmed against real 'st cell'/'st nrcell'
    output, which is the whole reason this uses a 2+-space boundary instead."""
    spans = []
    boundaries = [0] + [m.end() for m in re.finditer(r' {2,}', header_line)]
    for i, start in enumerate(boundaries):
        end = boundaries[i + 1] if i + 1 < len(boundaries) else None
        segment = header_line[start:end] if end is not None else header_line[start:]
        name = segment.strip()
        if name:
            spans.append((name, start, end))
    return spans


_MO_ATTR_LINE_RE = re.compile(r'^(\S+)(?:\s{2,}(.*))?$')
_STRUCT_HDR_RE = re.compile(r'^Struct\s+\S+\s+has\s+\d+\s+members:\s*$')
_MO_CONT_RE = re.compile(r'^>>>\s*(?:\d+\.)?(\w+)\s*=\s*(.*)$')


def _parse_mo_block(lines, start, n):
    """Parse one 'kget all'/'hget all' MO block starting at the sep line
    that precedes 'Proxy Id'/'MO', i.e. lines[start] is that sep.

    Shape (confirmed real 'kget all' output — one block per MO, attributes
    listed vertically, NOT the fixed-width column table parse_tables()
    otherwise expects):
        ===...
        Proxy Id                             <n>
        MO                                   <dn>
        ===...
        attr1                                val1
        attr2                                (blank)
        Struct someField has N members:
         >>> 1.member = val
        multiValAttr[1]
         >>> multiValAttr = val
        ===...  (next block or end)

    ' >>> ...' continuation lines (Struct members, extra reservedBy values)
    are skipped — only the top-level 'name  value' line is kept, matching
    what every extract_*() caller actually looks up by exact attribute name.

    Returns (row_dict, next_index) or (None, start) if this isn't actually
    a Proxy Id/MO header block (caller falls back to the column-table path).
    """
    if not (start + 3 < n
            and lines[start + 1].lstrip().startswith('Proxy Id')
            and lines[start + 2].lstrip().startswith('MO')
            and _SEP_RE.match(lines[start + 3])):
        return None, start

    def _value(line):
        m = _MO_ATTR_LINE_RE.match(line)
        return (m.group(2) or '').strip() if m else ''

    row = {'MO': _value(lines[start + 2]), 'Proxy Id': _value(lines[start + 1])}
    j = start + 4
    while j < n and not _SEP_RE.match(lines[j]):
        line = lines[j]
        stripped = line.strip()
        if not stripped:
            j += 1
            continue
        cont = _MO_CONT_RE.match(stripped)
        if cont:
            # Struct-member ('>>> 1.productName = X') and multi-value
            # ('>>> reservedBy = X') continuation lines. A narrow 'get
            # <MO> productName'-style command flattens these straight to a
            # top-level 'productName' column; every extract_*() caller reads
            # them that way (row.get('productName') etc.), so mirror that
            # here too rather than dropping them. First value wins under the
            # bare name (matches existing single-value assumption elsewhere
            # in this project) and is never overwritten by a later one or by
            # a genuine top-level attribute of the same name; every value
            # (including the first) also collects under '<name>_all' for
            # callers that need the full multi-value list (e.g. a
            # SectorCarrier's several rfBranchTxRef entries).
            name = cont.group(1)
            val = cont.group(2).strip()
            if name not in row:
                row[name] = val
            row.setdefault(f'{name}_all', []).append(val)
            j += 1
            continue
        if _STRUCT_HDR_RE.match(stripped):
            j += 1
            continue
        m = _MO_ATTR_LINE_RE.match(line)
        if m:
            row[m.group(1)] = (m.group(2) or '').strip()
        j += 1
    return row, j


def parse_tables(block_text):
    """Extract every table found inside a command's output block. Handles two
    distinct moshell output shapes and returns both as the same list of dicts:
    {'header': [...], 'rows': [{col: value, ...}, ...]}.

    Format A — fixed-width column table (e.g. 'lt all', 'st cell'):
        ===...header...===...rows...===...Total: N MOs (optional)
    One row per data line, columns split by the header's 2+-space boundaries.

    Format B — 'kget all'/'hget all' bulk MO dump: one block per MO, with
    'Proxy Id'/'MO' as a two-line header and attributes listed vertically
    (one per line) rather than as table columns. Each MO becomes its own
    single-row table so all_rows() flattens it identically to Format A —
    every extract_*(parsed) caller already expects row.get('MO') / row.get(
    attr_name) directly (the wide-table shape _row_value()'s docstring
    describes), it just never had a parser that actually produced it for
    this log shape. Confirmed real gap: pure 'kget all'-only Pre logs (no
    narrow 'get <MO> <attr>' commands run) parsed to zero identity/hardware
    rows — find_command()+all_rows() had nothing to read even though the
    data was in the block the whole time.

    Deliberately tolerant: a missing 'Total:' line (some blocks omit it) doesn't
    stop table extraction, since the next separator line reliably closes the row
    section either way.
    """
    lines = block_text.splitlines()
    tables = []
    i = 0
    n = len(lines)
    while i < n:
        if _SEP_RE.match(lines[i]):
            mo_row, next_i = _parse_mo_block(lines, i, n)
            if mo_row is not None:
                tables.append({'header': list(mo_row.keys()), 'rows': [mo_row]})
                i = next_i
                continue
            # Expect: sep, header, sep, rows..., sep, (optional 'Total: N MOs')
            if i + 2 < n and _SEP_RE.match(lines[i + 2]):
                header_line = lines[i + 1]
                if header_line.strip() and not _SEP_RE.match(header_line):
                    spans = _column_spans(header_line)
                    if spans:
                        row_start = i + 3
                        j = row_start
                        row_lines = []
                        while j < n and not _SEP_RE.match(lines[j]):
                            row_lines.append(lines[j])
                            j += 1
                        rows = []
                        for rl in row_lines:
                            if not rl.strip():
                                continue
                            row = {}
                            for name, start, end in spans:
                                val = rl[start:end] if end is not None else rl[start:]
                                row[name] = val.strip()
                            rows.append(row)
                        tables.append({
                            'header': [s[0] for s in spans],
                            'rows': rows,
                        })
                        i = j
                        continue
        i += 1
    return tables


def get_command_block(text, command_substr):
    """Return the raw (unparsed) output block text for the first command
    containing command_substr, or None. Used when a command's output isn't
    reliably fixed-width (see extract_nr_tac in pre_extract.py for why)."""
    sub = command_substr.lower()
    for node, cmd, block in split_commands(text):
        if sub in cmd.lower():
            return block
    return None


@functools.lru_cache(maxsize=16)
def parse_log(text):
    """Top-level entry point. Returns a list of:
        {'node': str, 'command': str, 'tables': [ {header, rows}, ... ]}
    one entry per command found in the transcript, in file order.
    """
    out = []
    for node, cmd, block in split_commands(text):
        tables = parse_tables(block)
        if tables:
            out.append({'node': node, 'command': cmd, 'tables': tables})
    return out


def find_command(parsed, command_substr):
    """Return the first parsed command entry whose command text contains the
    given substring (case-insensitive), or None."""
    sub = command_substr.lower()
    for entry in parsed:
        if sub in entry['command'].lower():
            return entry
    return None


def all_rows(command_entry, mo_prefix=None):
    """Flatten every row across every table for a parsed command entry into
    one list, optionally keeping only rows whose 'MO' column starts with
    mo_prefix (case-insensitive)."""
    rows = []
    if not command_entry:
        return rows
    for table in command_entry['tables']:
        for row in table['rows']:
            if mo_prefix is None or row.get('MO', '').upper().startswith(mo_prefix.upper()):
                rows.append(row)
    return rows
