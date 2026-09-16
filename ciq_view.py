"""
CIQ raw table builders — mirrors QUICKIX HTML's "CIQ Checks" tab tables
(Node Integration, LTE eUtran Parameters, 5G NR Parameters), reading
straight off the CIQ workbook with no Pre/EDP/RFDS involved.
"""
import ciq_edp_reader as cer


def _xmu_ports(row, prefix):
    if not row:
        return []
    ports = [row.get(f"{prefix} Port 1"), row.get(f"{prefix} Port 2"), row.get(f"{prefix} Port 3")]
    return [str(p).strip() for p in ports if str(p or "").strip().upper() not in ("", "N/A", "NOT USED")]


def build_node_integration(ciq_wb):
    """Node, eNBId, eNodeB, gNBId, gNodeB, Mode, BB Type, MME Region, ENM,
    XMU count, Ports — one row per Mixed Mode Info entry."""
    enb_rows = {str(r.get("eNBId") or "").strip(): r for r in cer.enb_info_rows(ciq_wb)}
    gnb_rows = ({str(r.get("gNBId") or "").strip(): r for r in cer.sheet_rows_as_dicts(ciq_wb["gNB Info"])}
                if "gNB Info" in ciq_wb.sheetnames else {})

    rows = []
    for m in cer.mixed_mode_rows(ciq_wb):
        enb_id = str(m.get("eNBId") or "").strip()
        gnb_id = str(m.get("gNBId") or "").strip()
        enb_row = enb_rows.get(enb_id)
        gnb_row = gnb_rows.get(gnb_id)
        xmu_source = enb_row or gnb_row
        has_1xmu = bool(xmu_source) and str(xmu_source.get("1st XMU") or "").strip().upper() == "YES"
        has_2xmu = bool(xmu_source) and str(xmu_source.get("2nd XMU") or "").strip().upper() == "YES"
        xmu_count = int(has_1xmu) + int(has_2xmu)
        port_parts = []
        if has_1xmu:
            port_parts.append("1 XMU [" + ",".join(_xmu_ports(xmu_source, "1st XMU")) + "]")
        if has_2xmu:
            port_parts.append("2 XMU [" + ",".join(_xmu_ports(xmu_source, "2nd XMU")) + "]")

        mode = str(m.get("BBU Mode") or "").strip()
        if not gnb_id and enb_id:
            mode = "LTE Only"
        elif not enb_id and gnb_id:
            mode = "5G Only"

        rows.append({
            "node": m.get("Node to be built as"), "eNBId": enb_id or "-", "eNodeB": m.get("eNodeB Name") or "-",
            "gNBId": gnb_id or "-", "gNodeB": m.get("gNodeB Name") or "-", "mode": mode or "-",
            "bb_type": (enb_row or {}).get("DU type") or (gnb_row or {}).get("DU type") or "-",
            "mme_region": m.get("MME Region") or "-", "enm": m.get("ENM") or m.get("OSS") or "-",
            "xmu": f"{xmu_count} XMU" if xmu_count else "-", "ports": ", ".join(port_parts) or "-",
        })
    return rows


def build_controller_info(ciq_wb):
    """USID, Controller, ID — one row per Controller Info entry. Returns []
    when the sheet is absent or every row is entirely blank (mirrors
    QUICKIX HTML's ciqBuildControllerTable() hasData check: hide the card
    rather than show an empty/all-blank one. Note this treats literal
    "N/A" text as present data, same as the HTML — a genuinely blank cell
    is what hides the card, not a placeholder value)."""
    if "Controller Info" not in ciq_wb.sheetnames:
        return []
    rows = cer.sheet_rows_as_dicts(ciq_wb["Controller Info"])
    has_data = any(str(v or "").strip() for r in rows for v in r.values())
    if not has_data:
        return []
    out = []
    for r in rows:
        usid = r.get("USID") or r.get("US ID") or r.get("Usid") or "-"
        controller = r.get("Controller") or r.get("Controller Name") or "-"
        cid = r.get("Controller ID") or r.get("Controller Id") or r.get("ControllerID") or "-"
        out.append({"usid": usid, "controller": controller, "id": cid})
    return out


LTE_PARAM_COLS = [
    "EutranCellFDDId", "eNBId", "eUTRA operating band", "earfcnDl", "earfcnUl", "dlChannelBandwidth",
    "configuredOutputPower", "PCI", "sectorId", "cellId", "RRU type", "antenna model",
    "noOfTxAntennas", "noOfRxAntennas", "RBB type", "tac", "Co-Located Technology Cell", "Carrier",
]
NR_PARAM_COLS = [
    "NRCellDU", "gNBId", "Operating Band", "arfcnDL", "arfcnUL", "bSChannelBwDL", "bSChannelBwUL",
    "configuredMaxTxPower", "nRPCI", "cellLocalId", "nRTAC", "RRU Type", "Antenna Type",
    "Co-Located Technology Cell", "Carrier",
]


def build_param_table(ciq_wb, sheet_name, columns):
    """Raw sheet rows, restricted to the confirmed-present columns (missing
    columns are silently skipped rather than raising, since not every CIQ
    template carries every optional column)."""
    if sheet_name not in ciq_wb.sheetnames:
        return []
    all_rows = cer.sheet_rows_as_dicts(ciq_wb[sheet_name])
    if not all_rows:
        return []
    present = [c for c in columns if c in all_rows[0]]
    return [{c: r.get(c) for c in present} for r in all_rows]
