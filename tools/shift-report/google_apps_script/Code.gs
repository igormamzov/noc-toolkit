/**
 * NOC Shift Report — Apps Script Web API
 * Version: 0.1.0
 *
 * Deploy: Deploy → New deployment → Web app
 *   Execute as: Me
 *   Who has access: Anyone
 *
 * Endpoints:
 *   GET  ?action=read&sheet=Night-Shift-NEW     — read layout + tickets
 *   POST {action: "sync", sheet: "...", updates: [{row, value}]}  — update statuses
 *   POST {action: "addRow", sheet: "...", data: {summary, ticketId, jiraLink, status, slackText, slackLink}}
 *   POST {action: "startShift", sheet: "Night-Shift-NEW"}
 */

// ── Config ──────────────────────────────────────────────────────────────────
const TICKET_START_ROW = 8;
const API_KEY = PropertiesService.getScriptProperties().getProperty('API_KEY') || '';

// ── Entry points ────────────────────────────────────────────────────────────

function doGet(e) {
  if (!_checkAuth(e)) return _json({ error: 'unauthorized' }, 401);

  const ss = SpreadsheetApp.getActiveSpreadsheet();
  const sheetName = (e.parameter.sheet || 'Night-Shift-NEW');
  const ws = ss.getSheetByName(sheetName);
  if (!ws) return _json({ error: 'sheet not found: ' + sheetName });

  const action = e.parameter.action || 'read';

  if (action === 'read') {
    const layout = _getLayout(ws);
    const tickets = _getTickets(ws, layout);
    const date = _getDate(ws);
    return _json({ ok: true, sheetName, date, layout, tickets });
  }

  if (action === 'sheets') {
    const sheets = ss.getSheets().map(function(s) { return s.getName(); });
    return _json({ ok: true, sheets });
  }

  return _json({ error: 'unknown action: ' + action });
}

function doPost(e) {
  if (!_checkAuth(e)) return _json({ error: 'unauthorized' }, 401);

  var payload;
  try {
    payload = JSON.parse(e.postData.contents);
  } catch (err) {
    return _json({ error: 'invalid JSON' });
  }

  const ss = SpreadsheetApp.getActiveSpreadsheet();
  const sheetName = payload.sheet || 'Night-Shift-NEW';
  const ws = ss.getSheetByName(sheetName);
  if (!ws) return _json({ error: 'sheet not found: ' + sheetName });

  const action = payload.action;

  // ── sync: batch update status cells ──
  if (action === 'sync') {
    return _json(_doSync(ws, payload.updates || []));
  }

  // ── addRow: insert ticket into TTM section ──
  if (action === 'addRow') {
    return _json(_doAddRow(ws, payload.data || {}));
  }

  // ── startShift: full shift handoff ──
  if (action === 'startShift') {
    return _json(_doStartShift(ss, sheetName));
  }

  return _json({ error: 'unknown action: ' + action });
}

// ── Read helpers ────────────────────────────────────────────────────────────

function _getLayout(ws) {
  const lastRow = ws.getLastRow();
  const colA = ws.getRange(1, 1, lastRow, 1).getValues();

  var fromPrevRow = null, ttmRow = null, permalinksRow = null;

  for (var i = TICKET_START_ROW - 1; i < lastRow; i++) {
    var val = String(colA[i][0] || '').toLowerCase();
    if (!val) continue;

    if (val.indexOf('from the previous shifts') !== -1 && !fromPrevRow) {
      fromPrevRow = i + 1;
    } else if (val.indexOf('things to monitor') !== -1 && val.indexOf('from') === -1 && !ttmRow) {
      ttmRow = i + 1;
    } else if (val.indexOf('permalinks') !== -1 && !permalinksRow) {
      permalinksRow = i + 1;
      break;
    }
  }

  return {
    fromPrevRow: fromPrevRow || TICKET_START_ROW,
    fromPrevEnd: ttmRow ? ttmRow - 1 : null,
    ttmRow: ttmRow,
    ttmEnd: permalinksRow ? permalinksRow - 1 : null,
    permalinksRow: permalinksRow
  };
}

function _getTickets(ws, layout) {
  var startRow = layout.fromPrevRow;
  var endRow = layout.permalinksRow - 1;
  var numRows = endRow - startRow + 1;
  if (numRows <= 0) return [];

  // Read C:F (columns 3-6)
  var values = ws.getRange(startRow, 3, numRows, 4).getValues();

  // Read rich text for column D to get hyperlinks
  var richTexts = ws.getRange(startRow, 4, numRows, 1).getRichTextValues();

  // Read rich text for column F (slack links)
  var richTextsF = ws.getRange(startRow, 6, numRows, 1).getRichTextValues();

  var tickets = [];
  for (var i = 0; i < numRows; i++) {
    var ticketId = String(values[i][1] || '').trim();
    if (!ticketId) continue;

    var dLink = null;
    var rt = richTexts[i][0];
    if (rt) {
      var url = rt.getLinkUrl();
      if (url) dLink = url;
    }

    var fLink = null;
    var rtF = richTextsF[i][0];
    if (rtF) {
      var urlF = rtF.getLinkUrl();
      if (urlF) fLink = urlF;
    }

    tickets.push({
      row: startRow + i,
      summary: values[i][0] || '',
      ticketId: ticketId,
      ticketHyperlink: dLink,
      status: String(values[i][2] || ''),
      slackText: String(values[i][3] || ''),
      slackHyperlink: fLink,
      section: (startRow + i < layout.ttmRow) ? 'fromPrev' : 'ttm'
    });
  }
  return tickets;
}

function _getDate(ws) {
  return {
    day: ws.getRange(1, 1).getValue(),
    month: String(ws.getRange(2, 1).getValue() || '')
  };
}

// ── Sync ────────────────────────────────────────────────────────────────────

function _doSync(ws, updates) {
  // updates: [{row: 8, value: "IN PROGRESS John Doe"}, ...]
  var count = 0;
  for (var i = 0; i < updates.length; i++) {
    var u = updates[i];
    if (u.row && u.value !== undefined) {
      ws.getRange(u.row, 5).setValue(u.value); // column E
      count++;
    }
  }

  // Ensure text wrap on all ticket rows (C:F)
  var layout = _getLayout(ws);
  var startRow = layout.fromPrevRow;
  var endRow = (layout.permalinksRow || ws.getLastRow()) - 1;
  var numRows = endRow - startRow + 1;
  if (numRows > 0) {
    ws.getRange(startRow, 3, numRows, 4)
      .setWrapStrategy(SpreadsheetApp.WrapStrategy.WRAP);
    // Auto-resize rows so wrapped text is visible
    for (var r = startRow; r < startRow + numRows; r++) {
      ws.autoResizeRows(r, 1);
    }
  }

  SpreadsheetApp.flush();
  return { ok: true, updated: count };
}

// ── Add Row ─────────────────────────────────────────────────────────────────

function _doAddRow(ws, data) {
  // data: {summary, ticketId, jiraLink, status, slackText, slackLink}
  var layout = _getLayout(ws);
  var ttmRow = layout.ttmRow;
  var permalinksRow = layout.permalinksRow;

  // Check if TTM row already has data
  var ttmHasData = ws.getRange(ttmRow, 4).getValue();

  var targetRow;
  if (!ttmHasData) {
    targetRow = ttmRow;
  } else {
    // Insert row before Permalinks
    ws.insertRowBefore(permalinksRow);
    targetRow = permalinksRow;
    // Permalinks shifts down by 1
  }

  // Write data
  ws.getRange(targetRow, 3).setValue(data.summary || '');

  // D: ticket ID with hyperlink
  if (data.jiraLink) {
    var richD = SpreadsheetApp.newRichTextValue()
      .setText(data.ticketId || '')
      .setLinkUrl(data.jiraLink)
      .build();
    ws.getRange(targetRow, 4).setRichTextValue(richD);
  } else {
    ws.getRange(targetRow, 4).setValue(data.ticketId || '');
  }

  // E: status
  ws.getRange(targetRow, 5).setValue(data.status || '');

  // F: slack link
  if (data.slackLink) {
    var richF = SpreadsheetApp.newRichTextValue()
      .setText(data.slackText || 'slack_link')
      .setLinkUrl(data.slackLink)
      .build();
    ws.getRange(targetRow, 6).setRichTextValue(richF);
  }

  // Set text wrap on the new row (C:F) and auto-resize
  ws.getRange(targetRow, 3, 1, 4).setWrapStrategy(SpreadsheetApp.WrapStrategy.WRAP);
  ws.autoResizeRows(targetRow, 1);

  SpreadsheetApp.flush();
  return { ok: true, insertedRow: targetRow };
}

// ── Start Shift ─────────────────────────────────────────────────────────────

function _doStartShift(ss, targetSheetName) {
  var sourceSheetName = (targetSheetName === 'Night-Shift-NEW')
    ? 'Day-Shift-NEW' : 'Night-Shift-NEW';

  var sourceWs = ss.getSheetByName(sourceSheetName);
  var targetWs = ss.getSheetByName(targetSheetName);
  if (!sourceWs || !targetWs) {
    return { error: 'sheet not found' };
  }

  // 1. Read source tickets
  var sourceLayout = _getLayout(sourceWs);
  var sourceTickets = _getTickets(sourceWs, sourceLayout);

  // 2. Read source date
  var sourceDate = _getDate(sourceWs);

  // 3. Calculate new date
  var newDay = sourceDate.day;
  var newMonth = sourceDate.month;
  if (targetSheetName === 'Night-Shift-NEW') {
    newDay = Number(sourceDate.day) + 1;
    // Month boundary handling
    var monthDays = _daysInMonth(newMonth, new Date().getFullYear());
    if (monthDays && newDay > monthDays) {
      newDay = 1;
      newMonth = _nextMonth(newMonth);
    }
  }

  // 4. Update target date
  targetWs.getRange(1, 1).setValue(newDay);
  targetWs.getRange(2, 1).setValue(newMonth);

  // 5. Get target layout
  var targetLayout = _getLayout(targetWs);

  // 6. Clear "from previous shifts" section (keep header row)
  var fromStart = targetLayout.fromPrevRow;
  var fromEnd = targetLayout.fromPrevEnd;
  var currentFromCount = Math.max(fromEnd - fromStart + 1, 0);
  var sourceCount = Math.max(sourceTickets.length, 1);

  // Adjust rows: insert or delete to match source ticket count
  var delta = sourceCount - currentFromCount;
  if (delta > 0) {
    targetWs.insertRowsAfter(fromEnd, delta);
  } else if (delta < 0) {
    targetWs.deleteRows(fromStart + sourceCount, Math.abs(delta));
  }

  // Recalculate layout after structural changes
  var newLayout = _getLayout(targetWs);

  // 7. Write source tickets into "from previous shifts"
  for (var i = 0; i < sourceTickets.length; i++) {
    var t = sourceTickets[i];
    var row = newLayout.fromPrevRow + i;

    targetWs.getRange(row, 3).setValue(t.summary);

    // D: ticket ID with hyperlink
    if (t.ticketHyperlink) {
      var richD = SpreadsheetApp.newRichTextValue()
        .setText(t.ticketId)
        .setLinkUrl(t.ticketHyperlink)
        .build();
      targetWs.getRange(row, 4).setRichTextValue(richD);
    } else {
      targetWs.getRange(row, 4).setValue(t.ticketId);
    }

    targetWs.getRange(row, 5).setValue(t.status);

    // F: slack link
    if (t.slackHyperlink) {
      var richF = SpreadsheetApp.newRichTextValue()
        .setText(t.slackText)
        .setLinkUrl(t.slackHyperlink)
        .build();
      targetWs.getRange(row, 6).setRichTextValue(richF);
    } else {
      targetWs.getRange(row, 6).setValue(t.slackText);
    }
  }

  // 8. Format "from previous shifts" section
  var fpStart = newLayout.fromPrevRow;
  var fpCount = sourceTickets.length;
  if (fpCount > 0) {
    // Merge A:B across ticket rows and set section label
    var labelRange = targetWs.getRange(fpStart, 1, fpCount, 2);
    labelRange.breakApart();
    labelRange.merge();
    labelRange.setValue("Things to Monitor\nfrom the previous shifts");
    labelRange.setVerticalAlignment("middle");
    labelRange.setWrapStrategy(SpreadsheetApp.WrapStrategy.WRAP);

    // Set text wrap on ticket data (C:F) so long text is not clipped
    var dataRange = targetWs.getRange(fpStart, 3, fpCount, 4);
    dataRange.setWrapStrategy(SpreadsheetApp.WrapStrategy.WRAP);
    // Auto-resize rows so wrapped text is visible
    for (var r = fpStart; r < fpStart + fpCount; r++) {
      targetWs.autoResizeRows(r, 1);
    }
  }

  // 9. Clear TTM section (keep one empty row)
  var ttmRow = newLayout.ttmRow;
  var ttmEnd = newLayout.ttmEnd;
  var extraTtm = ttmEnd - ttmRow;
  if (extraTtm > 0) {
    targetWs.deleteRows(ttmRow + 1, extraTtm);
  }
  // Clear the TTM row data (C-F)
  var finalLayout = _getLayout(targetWs);
  targetWs.getRange(finalLayout.ttmRow, 3, 1, 4).clearContent();

  SpreadsheetApp.flush();

  return {
    ok: true,
    ticketsCopied: sourceTickets.length,
    dateDay: newDay,
    dateMonth: newMonth
  };
}

// ── Date helpers ────────────────────────────────────────────────────────────

var MONTH_MAP = {
  'Jan': 1, 'Feb': 2, 'Mar': 3, 'Apr': 4, 'May': 5, 'Jun': 6,
  'Jul': 7, 'Aug': 8, 'Sep': 9, 'Oct': 10, 'Nov': 11, 'Dec': 12
};
var NUM_TO_MONTH = {};
for (var m in MONTH_MAP) { NUM_TO_MONTH[MONTH_MAP[m]] = m; }

function _daysInMonth(monthStr, year) {
  var num = MONTH_MAP[monthStr];
  if (!num) return null;
  return new Date(year, num, 0).getDate();
}

function _nextMonth(monthStr) {
  var num = MONTH_MAP[monthStr];
  if (!num) return monthStr;
  var next = (num % 12) + 1;
  return NUM_TO_MONTH[next] || monthStr;
}

// ── Auth & JSON helpers ─────────────────────────────────────────────────────

function _checkAuth(e) {
  if (!API_KEY) return true; // no key configured = open access
  var param = (e.parameter && e.parameter.key) || '';
  if (param === API_KEY) return true;

  // Check POST body too
  if (e.postData && e.postData.contents) {
    try {
      var body = JSON.parse(e.postData.contents);
      if (body.key === API_KEY) return true;
    } catch (_) {}
  }
  return false;
}

function _json(obj, code) {
  return ContentService
    .createTextOutput(JSON.stringify(obj))
    .setMimeType(ContentService.MimeType.JSON);
}
