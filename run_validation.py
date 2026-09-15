"""
End-to-end runner: CIQ + EDP + RFDS + Pre kget-all logs -> validation PDF,
following the Blueprint's 'Pre checks validation format' sheet section-by-
section (see pdf_report.py for the exact numbering).
"""
import ciq_edp_reader as cer
import pre_extract as pe
import checks_node as cn
import checks_sector as cs
import rfds_extract as rf
import sow_analysis as sa
import pre_cell_inventory as pci
import pre_post_config as ppc
import port_conversion as pconv
import scope_of_work_text as sowt
import pdf_report as pr
import warnings_text as wt


def run(ciq_path, edp_path, rfds_path, node_logs_text, out_pdf):
    ciq_wb = cer.load_ciq(ciq_path)
    edp_ws = cer.load_edp(edp_path)
    _, edp_rows = cer.build_edp_index(edp_ws)

    rfds_bytes = open(rfds_path, 'rb').read() if rfds_path else None
    rfds_pages = rf.load_rfds_pages(rfds_bytes) if rfds_bytes else None
    site_details = cn.build_site_details(ciq_wb, rfds_pages)

    node_logs = dict(node_logs_text)

    mm_rows = cer.mixed_mode_rows(ciq_wb)
    ciq_nodes = [str(r.get('Node to be built as')).strip() for r in mm_rows if r.get('Node to be built as')]
    all_nodes = list(dict.fromkeys(ciq_nodes + [n for n in node_logs if n not in ciq_nodes]))

    pre_pairs, pre_nodes = pci.build_pre_inventory(node_logs)
    sow = sa.classify_carriers(ciq_wb, mm_rows, pre_pairs, pre_nodes)
    sow['target_band_sectors'] = sowt.build_target_band_sectors(ciq_wb, mm_rows)

    deleted_nodes = {str(n).strip() for n in sow.get('deleted_nodes', [])}
    checked_nodes = [n for n in all_nodes if n not in deleted_nodes]

    # Full site-wide map: {target_cell: (source_node, source_cell)}. Passed
    # to every check needing Pre lookup by cell name - a moved-in cell's
    # real Pre history sits on its source node's own log (confirmed on a
    # real rehome), not the target node's, which has never seen the cell.
    moved_map = pe.build_moved_cell_source_map(ciq_wb)

    retuned_cells = set()
    for r in (cer.sheet_rows_as_dicts(ciq_wb['Sector Del_Movement']) if 'Sector Del_Movement' in ciq_wb.sheetnames else []):
        src_dl, tgt_dl = str(r.get('Source channelNumberDL', '')).strip(), str(r.get('Target channelNumberDL', '')).strip()
        src_bw, tgt_bw = str(r.get('Source Bandwidth', '')).strip(), str(r.get('Target Bandwidth', '')).strip()
        if (src_dl != tgt_dl) or (src_bw != tgt_bw):
            for key in ('Source Sector', 'Target Sector'):
                if r.get(key):
                    retuned_cells.add(str(r[key]).strip())

    results = {k: [] for k in (
        'sw_version', 'identity', 'primary_secondary', 'board_type', 'xmu',
        'cells_vs_rfds', 'cell_id_vs_rfds', 'params_4g', 'params_5g',
        'pci_4g', 'pci_5g', 'radio_type', 'sector_swap', 'radio_sharing',
        'port_uniqueness', 'xmu_port_overlap', 'antenna', 'nbiot', 'nr_tac', 'tac', 'sef_fru',
        'dss', 'sector_id_4890', 'rfbranch_per_aug', 'ptp_matrix', 'losses_vs_antenna',
        'tilt', 'mmwave_rach', 'radio_port_conflict', 'carrier_progression',
    )}
    sa_note_nodes = []
    unavailable_notes = [
        "Radio Type 'Pre' (#6): best-effort via Cell->SectorCarrier->SEF chain; no confirmed SEF->RRU product link exists, shown as SEF number or NOT AVAILABLE.",
        "Sector/TX-RX/Power 'Link' column (#21/#22/#32): no confirmed CIQ column for DATA1/DATA2-style port designation - omitted.",
    ]

    enb_rows_all = cer.enb_info_rows(ciq_wb)

    for node_id in checked_nodes:
        log_text = node_logs.get(node_id)
        has_pre = bool(log_text)
        mm_row = cer.find_mm_row(mm_rows, node_id)
        enb_row = cer.find_enb_row(enb_rows_all, node_id)
        e_name = str(mm_row.get('eNodeB Name') or '').strip() if mm_row else node_id
        g_name = str(mm_row.get('gNodeB Name') or '').strip() if mm_row else None

        node_checks = cn.run_node_checks(node_id, log_text, ciq_wb, edp_rows, rfds_pages, rfds_bytes)
        by_rule = {r['rule']: r for r in node_checks}
        if '#1' in by_rule:
            results['sw_version'].append(by_rule['#1'])
        if '#2/12/14/17' in by_rule:
            results['identity'].append(by_rule['#2/12/14/17'])
        if '#3/#31' in by_rule:
            results['primary_secondary'].append(by_rule['#3/#31'])
        if '#5/#15/#13' in by_rule:
            results['board_type'].append(by_rule['#5/#15/#13'])
        if '#27' in by_rule:
            results['xmu'].append(by_rule['#27'])
        if '#16' in by_rule:
            results['tac'].append(by_rule['#16'])

        results['cells_vs_rfds'] += cs.check_cells_vs_rfds(node_id, ciq_wb, rfds_pages, e_name, g_name)
        results['cell_id_vs_rfds'] += cs.check_cell_id_vs_rfds(node_id, log_text, ciq_wb, rfds_pages, e_name, g_name, node_logs, moved_map)
        results['params_4g'] += cs.check_rf_params_4g(node_id, log_text, ciq_wb, has_pre, retuned_cells, node_logs, moved_map)
        import log_parser as lp
        parsed = lp.parse_log(log_text) if log_text else []
        results['params_5g'] += cs.check_rf_params_5g(node_id, parsed, log_text, ciq_wb, has_pre, retuned_cells, node_logs, moved_map)
        results['pci_4g'] += cs.check_pci_uniqueness(node_id, ciq_wb, e_name)
        results['pci_5g'] += cs.check_nr_pci_uniqueness(node_id, ciq_wb, g_name)
        results['radio_type'] += cs.check_radio_type(node_id, log_text, ciq_wb, rfds_pages, e_name, g_name, node_logs, moved_map)
        results['sector_swap'] += cs.check_sector_swap_config(node_id, log_text, ciq_wb, e_name, g_name, node_logs, moved_map)

        # Blueprint rules wired in after the initial build-out — see each
        # function's docstring for why two of them were previously listed as
        # unavailable (both notes were wrong; the signals do exist).
        results['dss'] += cs.check_dss_pre_existing(node_id, log_text, ciq_wb)
        results['sector_id_4890'] += cs.check_sector_id_4890(node_id, ciq_wb, e_name)
        results['rfbranch_per_aug'] += cs.check_rfbranch_per_aug(node_id, log_text)
        results['losses_vs_antenna'] += cs.check_losses_vs_antenna_sectors(node_id, ciq_wb, e_name)
        results['tilt'] += cs.check_tilt_integer(node_id, ciq_wb, e_name, g_name)
        results['carrier_progression'] += cs.check_carrier_progression(node_id, ciq_wb, e_name, g_name)
        # CIQ-only checks that existed in checks_sector.py but were
        # never wired into the pipeline, so they never ran.
        results['mmwave_rach'] += cs.check_mmwave_rach(node_id, ciq_wb)
        results['radio_port_conflict'] += cs.check_radio_port_conflict(node_id, ciq_wb)
        results['ptp_matrix'] += cn.check_ptp_matrix(node_id, log_text, edp_rows, is_new_node=not has_pre)

        gnb_row = None
        if mm_row is not None and g_name and 'gNB Info' in ciq_wb.sheetnames:
            for r in cer.sheet_rows_as_dicts(ciq_wb['gNB Info']):
                if str(r.get('gNodeB Name', '')).strip().upper() == g_name.upper():
                    gnb_row = r
                    break
        results['xmu_port_overlap'] += cs.check_xmu_port_overlap(node_id, enb_row, gnb_row, ciq_wb)
        results['port_uniqueness'] += cs.check_riport_uniqueness(node_id, enb_row, gnb_row, ciq_wb, e_name, g_name)
        results['nbiot'] += cs.check_nbiot(node_id, log_text, ciq_wb)

        nr_tac_rows = cs.check_nr_tac(node_id, log_text, ciq_wb, has_pre, False, g_name, node_logs, moved_map)
        results['nr_tac'] += nr_tac_rows
        for r in nr_tac_rows:
            if r.get('pre_nrtac') and str(r['pre_nrtac']).isdigit() and len(str(r['pre_nrtac'])) == 7:
                sa_note_nodes.append(node_id)


    # Site-wide checks: these compare cells against EVERY other cell in the
    # CIQ (port uniqueness, radio sharing, antenna pairs, SEF/FRU), so they
    # are inherently one-per-site, not one-per-node. Running them inside the
    # per-node loop emitted every row once per node - a 3-node site showed
    # each cell three times. They take a node_id purely as a row label, so
    # the primary node is passed.
    site_label = checked_nodes[0] if checked_nodes else ''
    results['radio_sharing'] = cs.check_radio_sharing_pairs(site_label, ciq_wb)
    results['port_uniqueness'] = cs.check_port_uniqueness(site_label, ciq_wb)
    results['antenna'] = cs.check_antenna_uniqueness(site_label, ciq_wb)
    results['sef_fru'] = cs.check_sef_fru(site_label, ciq_wb)

    results['sa_note'] = ', '.join(sorted(set(sa_note_nodes))) if sa_note_nodes else None
    results['unavailable_notes'] = unavailable_notes

    # Warning text per blueprint column C/D specs, rendered beneath each table
    results['warn_xmu'] = wt.xmu_warnings(results['xmu'])
    results['warn_primary_secondary'] = wt.primary_secondary_warnings(results['primary_secondary'])
    results['warn_board_type'] = wt.board_type_warnings(results['board_type'])
    results['warn_params_4g'] = wt.param_warnings(results['params_4g'])
    results['warn_params_5g'] = wt.param_warnings(results['params_5g'])
    results['warn_pci'] = wt.pci_warnings(results['pci_4g'] + results['pci_5g'])
    results['warn_radio_type'] = wt.radio_type_warnings(results['radio_type'])
    results['warn_sector_swap'] = wt.sector_swap_warnings(results['radio_type'])
    results['warn_nr_tac'] = wt.nr_tac_warnings(results['nr_tac'], sa_note_nodes)
    results['warn_air_radio'] = wt.air_radio_warnings(results['sef_fru']) + wt.air3283_warnings(ciq_wb)
    results['warn_antenna'] = []  # RF-branch-count warning pending confirmation of the limit's basis

    pre_text, post_text = ppc.build_pre_post_config_text(node_logs, ciq_wb)
    scope_lines = sowt.scope_lines_to_readable_text(sowt.format_scope_of_work(sow, ciq_wb))
    for r in results.get('board_type', []):
        pass  # port conversion handled separately below

    port_conv_results = [pconv.check_port_conversion(n, node_logs.get(n), edp_rows, bool(node_logs.get(n))) for n in checked_nodes]
    for r in port_conv_results:
        if r.get('pending'):
            scope_lines.append(f"Port speed 1G to 10G conversion with MPST: {r['node']}.")

    if out_pdf:
        pr.build_report(out_pdf, site_details, pre_text, post_text, scope_lines, results,
                         skipped_deleted=sorted(deleted_nodes))
    return (out_pdf, results, site_details, ciq_wb, edp_rows, checked_nodes, rfds_pages,
            pre_text, post_text, scope_lines, sow)


if __name__ == '__main__':
    (out_pdf, results, site_details, ciq_wb, edp_rows, checked_nodes, rfds_pages,
     pre_text, post_text, scope_lines, sow) = run(
        '/mnt/project/SCL05020_SCCN005020_LTE_5G_LTE_1C_5G_3C_CBAND_No_MM_07_22_2026_Template_5_6.xlsx',
        '/mnt/project/EDP_Published_CISCO_EDPs_v408042026100556803.xls',
        '/mnt/project/RFDS38619.pdf',
        {'SCL05020': '/mnt/user-data/uploads/SCL05020.log'},
        '/tmp/report_v3.pdf',
    )
    print('done')
