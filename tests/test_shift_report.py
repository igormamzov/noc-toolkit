"""Tests for noc-report-assistant (tool #6)."""

import pytest
from openpyxl import load_workbook

from shift_report import (
    ShiftReport,
    ShiftLayout,
    RowSnapshot,
    TICKET_START_ROW,
    STATUS_MAP,
    _rebuild_section_merge,
    _remove_hyperlinks_in_range,
    _ensure_permalink_merges,
    _apply_hyperlink_font,
    _copy_cell_style,
)


# ===================================================================
# _scan_layout
# ===================================================================

class TestScanLayout:

    def test_normal_sheet_with_header(self, report_path):
        """Both sections have headers — standard case."""
        wb = load_workbook(report_path)
        ws = wb["Night-Shift-NEW"]
        layout = ShiftReport._scan_layout(ws)

        assert layout.from_prev_row == 8
        assert layout.ttm_row > layout.from_prev_row
        assert layout.permalinks_row > layout.ttm_row
        assert layout.from_prev_end == layout.ttm_row - 1
        assert layout.ttm_end == layout.permalinks_row - 1

    def test_missing_from_prev_header(self, report_no_header):
        """Day-Shift-NEW has no 'from previous shifts' header — should fallback to row 8."""
        wb = load_workbook(report_no_header)
        ws = wb["Day-Shift-NEW"]
        layout = ShiftReport._scan_layout(ws)

        assert layout.from_prev_row == TICKET_START_ROW
        assert layout.ttm_row > layout.from_prev_row
        assert layout.permalinks_row > layout.ttm_row

    def test_trailing_spaces_in_ttm(self, report_path):
        """'Things to monitor        ' (trailing spaces) should still be detected."""
        wb = load_workbook(report_path)
        ws = wb["Night-Shift-NEW"]
        layout = ShiftReport._scan_layout(ws)
        # TTM should be found despite trailing spaces
        ttm_value = str(ws.cell(row=layout.ttm_row, column=1).value or "")
        assert "things to monitor" in ttm_value.lower().strip()

    def test_missing_ttm_raises(self, tmp_path):
        """Sheet without 'Things to monitor' should raise RuntimeError."""
        from openpyxl import Workbook
        wb = Workbook()
        ws = wb.active
        ws.cell(row=8, column=1, value="Things to Monitor\nfrom the previous shifts")
        ws.cell(row=12, column=1, value="Permalinks")
        # No "Things to monitor" (without "from") — should fail
        # Actually row 12 has Permalinks but no TTM
        # Need to NOT have a "Things to monitor" row
        file_path = tmp_path / "broken.xlsx"
        wb.save(file_path)

        wb2 = load_workbook(file_path)
        with pytest.raises(RuntimeError, match="Things to monitor"):
            ShiftReport._scan_layout(wb2.active)

    def test_missing_permalinks_raises(self, tmp_path):
        """Sheet without 'Permalinks' should raise RuntimeError."""
        from openpyxl import Workbook
        wb = Workbook()
        ws = wb.active
        ws.cell(row=8, column=1, value="Things to Monitor\nfrom the previous shifts")
        ws.cell(row=12, column=1, value="Things to monitor")
        # No Permalinks row
        file_path = tmp_path / "broken2.xlsx"
        wb.save(file_path)

        wb2 = load_workbook(file_path)
        with pytest.raises(RuntimeError, match="Permalinks"):
            ShiftReport._scan_layout(wb2.active)

    def test_newline_in_from_prev_header(self, report_path):
        """Header with newline: 'Things to Monitor\\nfrom the previous shifts'."""
        wb = load_workbook(report_path)
        ws = wb["Night-Shift-NEW"]
        layout = ShiftReport._scan_layout(ws)

        cell_value = str(ws.cell(row=layout.from_prev_row, column=1).value or "")
        assert "from the previous shifts" in cell_value.lower()


# ===================================================================
# _collect_source_rows
# ===================================================================

class TestCollectSourceRows:

    def test_collects_tickets_from_both_sections(self, report_path):
        """Should collect tickets from 'from previous' AND 'TTM' sections."""
        wb = load_workbook(report_path)
        ws = wb["Night-Shift-NEW"]
        layout = ShiftReport._scan_layout(ws)
        rows = ShiftReport._collect_source_rows(ws, layout)

        # Night has 3 from_prev + 1 TTM = 4 total
        assert len(rows) == 4
        assert all(isinstance(r, RowSnapshot) for r in rows)

    def test_ticket_ids_preserved(self, report_path):
        """Ticket IDs should match what we put in."""
        wb = load_workbook(report_path)
        ws = wb["Night-Shift-NEW"]
        layout = ShiftReport._scan_layout(ws)
        rows = ShiftReport._collect_source_rows(ws, layout)

        ticket_ids = [r.ticket_id for r in rows]
        assert "DSSD-29001" in ticket_ids
        assert "DSSD-29002" in ticket_ids
        assert "DSSD-29003" in ticket_ids
        assert "DRGN-50001" in ticket_ids

    def test_hyperlinks_captured(self, report_path):
        """Hyperlinks from cell.hyperlink should be captured in RowSnapshot."""
        wb = load_workbook(report_path)
        ws = wb["Night-Shift-NEW"]
        layout = ShiftReport._scan_layout(ws)
        rows = ShiftReport._collect_source_rows(ws, layout)

        first = rows[0]
        assert first.ticket_hyperlink is not None
        assert "DSSD-29001" in first.ticket_hyperlink
        assert first.slack_hyperlink is not None
        assert "slack.com" in first.slack_hyperlink

    def test_empty_section_returns_empty(self, report_path):
        """Day-Shift-NEW has 0 TTM tickets — 'from previous' only."""
        wb = load_workbook(report_path)
        ws = wb["Day-Shift-NEW"]
        layout = ShiftReport._scan_layout(ws)
        rows = ShiftReport._collect_source_rows(ws, layout)

        # Day has 2 from_prev + 0 TTM = 2 total
        assert len(rows) == 2


# ===================================================================
# _opposite_sheet
# ===================================================================

class TestOppositeSheet:

    def test_night_to_day(self):
        assert ShiftReport._opposite_sheet("Night-Shift-NEW") == "Day-Shift-NEW"

    def test_day_to_night(self):
        assert ShiftReport._opposite_sheet("Day-Shift-NEW") == "Night-Shift-NEW"


# ===================================================================
# _update_date
# ===================================================================

class TestUpdateDate:

    def test_night_increments_day(self, report_path):
        """Night shift target should get source_day + 1."""
        wb = load_workbook(report_path)
        source_ws = wb["Day-Shift-NEW"]  # source: day=10
        target_ws = wb["Night-Shift-NEW"]

        new_day, new_month = ShiftReport._update_date(
            target_ws, source_ws, "Night-Shift-NEW",
        )
        assert new_day == 11
        assert new_month == "Mar"
        assert target_ws.cell(row=1, column=1).value == 11

    def test_day_keeps_same_day(self, report_path):
        """Day shift target should keep same day as source."""
        wb = load_workbook(report_path)
        source_ws = wb["Night-Shift-NEW"]  # source: day=10
        target_ws = wb["Day-Shift-NEW"]

        new_day, new_month = ShiftReport._update_date(
            target_ws, source_ws, "Day-Shift-NEW",
        )
        assert new_day == 10
        assert new_month == "Mar"

    def test_month_boundary_mar31(self, report_month_boundary):
        """Mar 31 + 1 = Apr 1."""
        wb = load_workbook(report_month_boundary)
        source_ws = wb["Day-Shift-NEW"]  # day=31, month=Mar
        target_ws = wb["Night-Shift-NEW"]

        new_day, new_month = ShiftReport._update_date(
            target_ws, source_ws, "Night-Shift-NEW",
        )
        assert new_day == 1
        assert new_month == "Apr"

    def test_year_boundary_dec31(self, report_dec31):
        """Dec 31 + 1 = Jan 1."""
        wb = load_workbook(report_dec31)
        source_ws = wb["Day-Shift-NEW"]  # day=31, month=Dec
        target_ws = wb["Night-Shift-NEW"]

        new_day, new_month = ShiftReport._update_date(
            target_ws, source_ws, "Night-Shift-NEW",
        )
        assert new_day == 1
        assert new_month == "Jan"

    def test_day_shift_at_boundary_no_increment(self, report_month_boundary):
        """Day shift at Mar 31 keeps day=31 (no month rollover)."""
        wb = load_workbook(report_month_boundary)
        source_ws = wb["Night-Shift-NEW"]  # day=31, month=Mar
        target_ws = wb["Day-Shift-NEW"]

        new_day, new_month = ShiftReport._update_date(
            target_ws, source_ws, "Day-Shift-NEW",
        )
        assert new_day == 31
        assert new_month == "Mar"


# ===================================================================
# _build_status_string
# ===================================================================

class TestBuildStatusString:

    @pytest.fixture
    def assistant(self):
        return ShiftReport(
            jira_url="https://jira.example.com",
            jira_token="fake",
            dry_run=True,
        )

    def test_basic_format(self, assistant):
        result = assistant._build_status_string("Open", "John Doe", "")
        assert result == "OPEN John Doe"

    def test_status_map_work_in_progress(self, assistant):
        result = assistant._build_status_string("Work In Progress", "Alice", "")
        assert result == "IN PROGRESS Alice"

    def test_unassigned(self, assistant):
        result = assistant._build_status_string("Open", "Unassigned", "")
        assert result == "OPEN Unassigned"

    def test_preserves_parenthetical_note(self, assistant):
        old = "OPEN Jane Smith (recurring)"
        result = assistant._build_status_string("Open", "Jane Smith", old)
        assert result == "OPEN Jane Smith (recurring)"

    def test_no_note_when_absent(self, assistant):
        old = "OPEN Jane Smith"
        result = assistant._build_status_string("Closed", "Jane Smith", old)
        assert result == "CLOSED Jane Smith"
        assert "(" not in result


# ===================================================================
# _extract_ticket_id
# ===================================================================

class TestExtractTicketId:

    @pytest.fixture
    def assistant(self):
        return ShiftReport(
            jira_url="https://jira.example.com",
            jira_token="fake",
            dry_run=True,
        )

    def test_plain_text(self, assistant, tmp_path):
        from openpyxl import Workbook
        wb = Workbook()
        ws = wb.active
        ws.cell(row=1, column=1, value="DSSD-29001")
        result = assistant._extract_ticket_id(ws.cell(row=1, column=1))
        assert result == "DSSD-29001"

    def test_drgn_ticket(self, assistant, tmp_path):
        from openpyxl import Workbook
        wb = Workbook()
        ws = wb.active
        ws.cell(row=1, column=1, value="DRGN-50001")
        result = assistant._extract_ticket_id(ws.cell(row=1, column=1))
        assert result == "DRGN-50001"

    def test_no_match_returns_none(self, assistant, tmp_path):
        from openpyxl import Workbook
        wb = Workbook()
        ws = wb.active
        ws.cell(row=1, column=1, value="some random text")
        result = assistant._extract_ticket_id(ws.cell(row=1, column=1))
        assert result is None

    def test_empty_cell_returns_none(self, assistant, tmp_path):
        from openpyxl import Workbook
        wb = Workbook()
        ws = wb.active
        result = assistant._extract_ticket_id(ws.cell(row=1, column=1))
        assert result is None


# ===================================================================
# run() — sync statuses
# ===================================================================

class TestRunSync:

    def test_sync_updates_statuses(self, report_path, mock_jira):
        """Sync should update column E with fresh Jira data."""
        mock_jira("DSSD-29001", "Closed", "Kumar Raju")
        mock_jira("DSSD-29002", "Open", "Unassigned")
        mock_jira("DSSD-29003", "Work In Progress", "Jane Smith")
        mock_jira("DRGN-50001", "Open", "Unassigned")

        assistant = ShiftReport(
            jira_url="https://jira.example.com",
            jira_token="fake",
        )
        updates = assistant.run(report_path, "Night-Shift-NEW")

        assert len(updates) >= 3

        # Verify changes were written
        wb = load_workbook(report_path)
        ws = wb["Night-Shift-NEW"]
        layout = ShiftReport._scan_layout(ws)

        # Find DSSD-29001 and check its status
        for row in range(layout.from_prev_row, layout.permalinks_row):
            cell_d = str(ws.cell(row=row, column=4).value or "")
            if "DSSD-29001" in cell_d:
                status = ws.cell(row=row, column=5).value
                assert "CLOSED" in status
                assert "Kumar Raju" in status
                break

    def test_sync_preserves_notes(self, report_path, mock_jira):
        """Parenthetical notes like (recurring) should be preserved."""
        mock_jira("DSSD-29003", "Open", "Jane Smith")

        assistant = ShiftReport(
            jira_url="https://jira.example.com",
            jira_token="fake",
        )
        updates = assistant.run(report_path, "Night-Shift-NEW")

        for update in updates:
            if update.ticket_id == "DSSD-29003":
                assert "(recurring)" in update.new_value
                break

    def test_dry_run_no_save(self, report_path, mock_jira):
        """Dry run should not modify the file."""
        mock_jira("DSSD-29001", "Closed", "Kumar Raju")

        # Read original status
        wb_before = load_workbook(report_path)
        ws_before = wb_before["Night-Shift-NEW"]
        original_status = ws_before.cell(row=8, column=5).value

        assistant = ShiftReport(
            jira_url="https://jira.example.com",
            jira_token="fake",
            dry_run=True,
        )
        assistant.run(report_path, "Night-Shift-NEW")

        # Status should be unchanged on disk
        wb_after = load_workbook(report_path)
        ws_after = wb_after["Night-Shift-NEW"]
        assert ws_after.cell(row=8, column=5).value == original_status

    def test_sync_stops_at_permalinks(self, report_path, mock_jira):
        """Sync should not process rows below Permalinks."""
        mock_jira("DSSD-29001", "Closed", "Kumar")

        assistant = ShiftReport(
            jira_url="https://jira.example.com",
            jira_token="fake",
            dry_run=True,
        )
        updates = assistant.run(report_path, "Night-Shift-NEW")

        # Permalink rows should not appear in updates
        for update in updates:
            assert "Dashboard" not in update.ticket_id
            assert "Grafana" not in update.ticket_id


# ===================================================================
# start_shift()
# ===================================================================

class TestStartShift:

    def test_copies_tickets_to_target(self, report_path, mock_jira):
        """Start shift should copy all source tickets into target 'from previous' section."""
        # Mock all tickets that will be synced after copy
        mock_jira("DSSD-29010", "Open", "Unassigned")
        mock_jira("DSSD-29011", "In Progress", "Alice")

        assistant = ShiftReport(
            jira_url="https://jira.example.com",
            jira_token="fake",
        )
        # Night target ← Day source (2 from_prev + 0 TTM = 2 tickets)
        result = assistant.start_shift(report_path, "Night-Shift-NEW")

        assert result["tickets_copied"] == 2
        assert result["date_day"] == 11
        assert result["date_month"] == "Mar"

        # Verify the tickets are in the file
        wb = load_workbook(report_path)
        ws = wb["Night-Shift-NEW"]
        layout = ShiftReport._scan_layout(ws)

        ticket_ids = []
        for row in range(layout.from_prev_row, layout.ttm_row):
            val = str(ws.cell(row=row, column=4).value or "")
            if val.strip():
                ticket_ids.append(val.strip())

        assert "DSSD-29010" in ticket_ids
        assert "DSSD-29011" in ticket_ids

    def test_resets_ttm_section(self, report_path, mock_jira):
        """After start_shift, TTM section should have 1 empty row."""
        mock_jira("DSSD-29010", "Open", "Unassigned")
        mock_jira("DSSD-29011", "In Progress", "Alice")

        assistant = ShiftReport(
            jira_url="https://jira.example.com",
            jira_token="fake",
        )
        assistant.start_shift(report_path, "Night-Shift-NEW")

        wb = load_workbook(report_path)
        ws = wb["Night-Shift-NEW"]
        layout = ShiftReport._scan_layout(ws)

        # TTM should span exactly 1 row
        assert layout.ttm_end == layout.ttm_row
        # TTM row should be empty in columns C-F
        for col in [3, 4, 5, 6]:
            assert ws.cell(row=layout.ttm_row, column=col).value is None

    def test_writes_from_prev_header(self, report_no_header, mock_jira):
        """After start_shift on sheet without header, header text should be written."""
        mock_jira("DSSD-29001", "Open", "Unassigned")
        mock_jira("DSSD-29002", "In Progress", "John Doe")
        mock_jira("DSSD-29003", "Open", "Jane Smith")

        assistant = ShiftReport(
            jira_url="https://jira.example.com",
            jira_token="fake",
        )
        # Day target ← Night source (3 tickets)
        assistant.start_shift(report_no_header, "Day-Shift-NEW")

        wb = load_workbook(report_no_header)
        ws = wb["Day-Shift-NEW"]
        layout = ShiftReport._scan_layout(ws)

        header_text = str(ws.cell(row=layout.from_prev_row, column=1).value or "")
        assert "from the previous shifts" in header_text.lower()

    def test_delta_positive_inserts_rows(self, report_path, mock_jira):
        """When source has more tickets than target section, rows should be inserted."""
        # Day source has 2 tickets, Night target has 3 — delta = 2-3 = -1
        # Let's test the other way: Day target ← Night source (4 tickets) vs Day's 2
        for tid in ["DSSD-29001", "DSSD-29002", "DSSD-29003", "DRGN-50001"]:
            mock_jira(tid, "Open", "Unassigned")

        assistant = ShiftReport(
            jira_url="https://jira.example.com",
            jira_token="fake",
        )
        result = assistant.start_shift(report_path, "Day-Shift-NEW")

        assert result["tickets_copied"] == 4

        wb = load_workbook(report_path)
        ws = wb["Day-Shift-NEW"]
        layout = ShiftReport._scan_layout(ws)

        # 4 ticket rows in "from previous" section
        count = layout.from_prev_end - layout.from_prev_row + 1
        assert count == 4

    def test_delta_negative_deletes_rows(self, report_path, mock_jira):
        """When source has fewer tickets, excess rows should be deleted."""
        # Night target ← Day source (2 tickets) — Night currently has 3 from_prev
        mock_jira("DSSD-29010", "Open", "Unassigned")
        mock_jira("DSSD-29011", "In Progress", "Alice")

        assistant = ShiftReport(
            jira_url="https://jira.example.com",
            jira_token="fake",
        )
        result = assistant.start_shift(report_path, "Night-Shift-NEW")

        wb = load_workbook(report_path)
        ws = wb["Night-Shift-NEW"]
        layout = ShiftReport._scan_layout(ws)

        count = layout.from_prev_end - layout.from_prev_row + 1
        assert count == 2

    def test_file_reopens_cleanly(self, report_path, mock_jira):
        """After start_shift, file should reopen without errors."""
        mock_jira("DSSD-29010", "Open", "Unassigned")
        mock_jira("DSSD-29011", "In Progress", "Alice")

        assistant = ShiftReport(
            jira_url="https://jira.example.com",
            jira_token="fake",
        )
        assistant.start_shift(report_path, "Night-Shift-NEW")

        # This should not raise (openpyxl corruption = exception here)
        wb = load_workbook(report_path)
        assert "Night-Shift-NEW" in wb.sheetnames
        assert "Day-Shift-NEW" in wb.sheetnames

    def test_permalinks_intact_after_shift(self, report_path, mock_jira):
        """Permalinks section should survive start_shift intact."""
        mock_jira("DSSD-29010", "Open", "Unassigned")
        mock_jira("DSSD-29011", "In Progress", "Alice")

        assistant = ShiftReport(
            jira_url="https://jira.example.com",
            jira_token="fake",
        )
        assistant.start_shift(report_path, "Night-Shift-NEW")

        wb = load_workbook(report_path)
        ws = wb["Night-Shift-NEW"]
        layout = ShiftReport._scan_layout(ws)

        assert ws.cell(row=layout.permalinks_row, column=1).value == "Permalinks"
        # Permalink data rows should still exist
        assert ws.cell(row=layout.permalinks_row + 1, column=1).value == "NOC Dashboard"
        assert ws.cell(row=layout.permalinks_row + 2, column=1).value == "Grafana"

    def test_dry_run_returns_empty_sync(self, report_path):
        """Dry run should return empty sync_updates list."""
        assistant = ShiftReport(
            jira_url="https://jira.example.com",
            jira_token="fake",
            dry_run=True,
        )
        result = assistant.start_shift(report_path, "Night-Shift-NEW")

        assert result["sync_updates"] == []

    def test_month_boundary(self, report_month_boundary, mock_jira):
        """Start shift at Mar 31 → Night should get Apr 1."""
        mock_jira("DSSD-29010", "Open", "Unassigned")

        assistant = ShiftReport(
            jira_url="https://jira.example.com",
            jira_token="fake",
        )
        result = assistant.start_shift(report_month_boundary, "Night-Shift-NEW")

        assert result["date_day"] == 1
        assert result["date_month"] == "Apr"


# ===================================================================
# add_row()
# ===================================================================

class TestAddRow:

    def test_add_first_ticket_to_empty_ttm(self, report_path, mock_jira):
        """Adding to empty TTM should write directly into TTM row (no insert)."""
        mock_jira("DSSD-29999", "Open", "Unassigned", summary="New bug found")

        assistant = ShiftReport(
            jira_url="https://jira.example.com",
            jira_token="fake",
        )
        # Day-Shift-NEW has empty TTM
        row = assistant.add_row(
            report_path, "Day-Shift-NEW",
            jira_link="https://jira.example.com/browse/DSSD-29999",
            slack_link="https://company.slack.com/archives/C123/p999",
        )

        wb = load_workbook(report_path)
        ws = wb["Day-Shift-NEW"]
        assert ws.cell(row=row, column=4).value == "DSSD-29999"
        assert ws.cell(row=row, column=3).value == "New bug found"
        assert "OPEN" in ws.cell(row=row, column=5).value

    def test_add_second_ticket_inserts_row(self, report_path, mock_jira):
        """Adding to TTM with existing ticket should insert a new row."""
        mock_jira("DRGN-50002", "Open", "Bob", summary="Another issue")

        assistant = ShiftReport(
            jira_url="https://jira.example.com",
            jira_token="fake",
        )
        # Night-Shift-NEW has 1 TTM ticket already
        wb_before = load_workbook(report_path)
        ws_before = wb_before["Night-Shift-NEW"]
        layout_before = ShiftReport._scan_layout(ws_before)
        permalinks_before = layout_before.permalinks_row

        row = assistant.add_row(
            report_path, "Night-Shift-NEW",
            jira_link="https://jira.example.com/browse/DRGN-50002",
            slack_link="https://company.slack.com/archives/C123/p888",
        )

        wb_after = load_workbook(report_path)
        ws_after = wb_after["Night-Shift-NEW"]
        layout_after = ShiftReport._scan_layout(ws_after)

        # Permalinks should have shifted down by 1
        assert layout_after.permalinks_row == permalinks_before + 1
        assert ws_after.cell(row=row, column=4).value == "DRGN-50002"

    def test_add_row_file_reopens(self, report_path, mock_jira):
        """File should reopen cleanly after add_row."""
        mock_jira("DSSD-29999", "Open", "Unassigned", summary="Test")

        assistant = ShiftReport(
            jira_url="https://jira.example.com",
            jira_token="fake",
        )
        assistant.add_row(
            report_path, "Day-Shift-NEW",
            jira_link="https://jira.example.com/browse/DSSD-29999",
            slack_link="https://company.slack.com/archives/C123/p999",
        )

        # Should not raise
        wb = load_workbook(report_path)
        assert "Day-Shift-NEW" in wb.sheetnames


# ===================================================================
# Helper functions
# ===================================================================

class TestHelpers:

    def test_rebuild_section_merge(self):
        """_rebuild_section_merge should create A:B merge for given range."""
        from openpyxl import Workbook
        wb = Workbook()
        ws = wb.active
        ws.merge_cells(start_row=5, start_column=1, end_row=7, end_column=2)

        _rebuild_section_merge(ws, 5, 10)

        # Old merge (5-7) should be gone, new merge (5-10) should exist
        found = False
        for mr in ws.merged_cells.ranges:
            if mr.min_row == 5 and mr.max_row == 10 and mr.min_col == 1 and mr.max_col == 2:
                found = True
        assert found

    def test_remove_hyperlinks_in_range(self):
        """Should remove hyperlinks within the specified range."""
        from openpyxl import Workbook
        from openpyxl.worksheet.hyperlink import Hyperlink

        wb = Workbook()
        ws = wb.active
        # Directly populate _hyperlinks (as openpyxl does when loading from file)
        ws._hyperlinks = [
            Hyperlink(ref="A1", target="https://example.com/1"),
            Hyperlink(ref="C2", target="https://example.com/2"),
            Hyperlink(ref="A5", target="https://example.com/3"),
        ]

        _remove_hyperlinks_in_range(ws, 1, 1, 3, 6)

        remaining = [hl.target for hl in ws._hyperlinks]
        assert len(remaining) == 1
        assert "https://example.com/3" in remaining

    def test_apply_hyperlink_font(self):
        """Should set Jira-blue color and underline."""
        from openpyxl import Workbook
        wb = Workbook()
        ws = wb.active
        cell = ws.cell(row=1, column=1, value="DSSD-123")

        _apply_hyperlink_font(cell)

        assert cell.font.underline == "single"
        assert cell.font.color.rgb == "FF0052CC"

    def test_copy_cell_style(self):
        """Should copy fill, font, border, alignment."""
        from openpyxl import Workbook
        from openpyxl.styles import PatternFill, Font
        wb = Workbook()
        ws = wb.active

        source = ws.cell(row=1, column=1)
        source.fill = PatternFill(start_color="FFFF0000", end_color="FFFF0000", fill_type="solid")
        source.font = Font(bold=True, size=14)

        target = ws.cell(row=2, column=1)
        _copy_cell_style(source, target)

        assert target.fill.start_color.rgb == "FFFF0000"
        assert target.font.bold is True
        assert target.font.size == 14
