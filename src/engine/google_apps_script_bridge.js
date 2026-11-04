/**
 * NSEBOT <-> GEMINI SPARK 2-WAY SHARED LEDGER & SIGNAL ROUTER
 * Google Apps Script Web App Deployment
 * 
 * Target Google Sheets:
 *   - Natural Gas:  19OiN3QrC75S5TRccF0QdzoC5SX1XGshzlJ2hPyIkW7I
 *   - Nifty Futures: 1iWS-yDJ40QGtfuunjv3PuHpDyIAXhIVF_OmBUsV6B5E
 * Web App Access: "Anyone" (Anonymous)
 * Execute as: "Me"
 */

const NG_SPREADSHEET_ID = "19OiN3QrC75S5TRccF0QdzoC5SX1XGshzlJ2hPyIkW7I";
const NIFTY_SPREADSHEET_ID = "1iWS-yDJ40QGtfuunjv3PuHpDyIAXhIVF_OmBUsV6B5E";
const LIVE_STATUS_FILE_NAME = "nsebot_live_status.json";
const NG_SIGNAL_FILE_NAME = "mcx_ng_live_signal.json";
const NIFTY_SIGNAL_FILE_NAME = "nifty_fut_live_signal.json";

/**
 * HTTP GET: Serves signal payloads or live status to NSEBOT.
 * Examples:
 *   ?symbol=NATURALGAS -> Returns latest mcx_ng_live_signal.json
 *   ?symbol=NIFTY      -> Returns latest nifty_fut_live_signal.json
 *   ?status=live       -> Returns latest nsebot_live_status.json
 */
function doGet(e) {
  try {
    const symbol = (e && e.parameter && e.parameter.symbol) ? e.parameter.symbol.toUpperCase() : "";
    const statusReq = (e && e.parameter && e.parameter.status) ? e.parameter.status.toLowerCase() : "";

    // 1. Live Bot Status & Positions Request
    if (statusReq === "live" || symbol === "STATUS") {
      const statusFile = getLatestFileByName(LIVE_STATUS_FILE_NAME);
      if (statusFile) {
        return ContentService.createTextOutput(statusFile.getBlob().getDataAsString())
          .setMimeType(ContentService.MimeType.JSON);
      }
      return jsonResponse({ error: "No live status file found yet." });
    }

    // 2. NIFTY Signal Request
    if (symbol.indexOf("NIFTY") !== -1) {
      const niftyFile = getLatestFileByName(NIFTY_SIGNAL_FILE_NAME);
      if (niftyFile) {
        return ContentService.createTextOutput(niftyFile.getBlob().getDataAsString())
          .setMimeType(ContentService.MimeType.JSON);
      }
      return jsonResponse({ error: "No Nifty signal file found." });
    }

    // 3. Default: NATURALGAS Signal Request
    const ngFile = getLatestFileByName(NG_SIGNAL_FILE_NAME);
    if (ngFile) {
      return ContentService.createTextOutput(ngFile.getBlob().getDataAsString())
        .setMimeType(ContentService.MimeType.JSON);
    }

    return jsonResponse({ error: "No signal files found." });

  } catch (err) {
    return jsonResponse({ error: err.toString() });
  }
}

/**
 * HTTP POST: Ingests execution feedback & live position snapshots from NSEBOT.
 * Actions:
 *   - "log_event": Logs TRADE_OPENED, SL_TRAILED, TRADE_CLOSED to NSEBOT_LIVE_LEDGER tab on dedicated sheet.
 *   - "sync_snapshot": Synchronizes NSEBOT_ACTIVE_POSITIONS on both sheets & updates nsebot_live_status.json in Drive.
 */
function doPost(e) {
  try {
    const rawContent = e && e.postData ? e.postData.contents : "{}";
    const data = JSON.parse(rawContent);
    const action = data.action || "log_event";

    // ── ACTION 1: LOG INDIVIDUAL EXECUTION EVENT ──
    if (action === "log_event") {
      let targetId = data.sheet_id;
      if (!targetId) {
        targetId = (data.symbol && data.symbol.toString().toUpperCase().indexOf("NIFTY") !== -1)
          ? NIFTY_SPREADSHEET_ID
          : NG_SPREADSHEET_ID;
      }

      const ss = SpreadsheetApp.openById(targetId);
      let sheet = ss.getSheetByName("NSEBOT_LIVE_LEDGER");
      if (!sheet) {
        sheet = ss.insertSheet("NSEBOT_LIVE_LEDGER");
        sheet.appendRow([
          "Timestamp (IST)",
          "Event",
          "Trade ID",
          "Symbol",
          "Side",
          "Lots",
          "Fill Price",
          "SL Price",
          "Target Price",
          "Exit Price",
          "P&L (INR)",
          "Status",
          "Reason"
        ]);
        formatHeaderRow(sheet);
      }

      const row = [
        data.timestamp_ist || new Date().toLocaleString("en-US", { timeZone: "Asia/Kolkata" }),
        data.event || "-",
        data.trade_id || "-",
        data.symbol || "-",
        data.side || "-",
        data.lots !== undefined ? data.lots : "-",
        data.fill_price !== undefined ? data.fill_price : "-",
        data.sl_price !== undefined ? data.sl_price : "-",
        data.target_price !== undefined ? data.target_price : "-",
        data.exit_price !== undefined ? data.exit_price : "-",
        data.pnl_rupees !== undefined ? data.pnl_rupees : "-",
        data.status || "-",
        data.reason || "-"
      ];

      sheet.appendRow(row);
      return jsonResponse({
        status: "success",
        action: "log_event",
        trade_id: data.trade_id,
        target_sheet_id: targetId
      });
    }

    // ── ACTION 2: SYNC ACTIVE POSITIONS SNAPSHOT ──
    if (action === "sync_snapshot") {
      const positions = data.active_positions || [];
      const timestampIst = data.timestamp_ist || new Date().toLocaleString("en-US", { timeZone: "Asia/Kolkata" });
      const ngSheetId = data.ng_sheet_id || NG_SPREADSHEET_ID;
      const niftySheetId = data.nifty_sheet_id || NIFTY_SPREADSHEET_ID;

      // Partition active positions by asset class
      const niftyPositions = positions.filter(function(p) {
        return p && p.symbol && p.symbol.toString().toUpperCase().indexOf("NIFTY") !== -1;
      });
      const ngPositions = positions.filter(function(p) {
        return !p || !p.symbol || p.symbol.toString().toUpperCase().indexOf("NIFTY") === -1;
      });

      // Update dedicated tabs on both sheets
      updateActivePositionsSheet(ngSheetId, ngPositions, timestampIst);
      updateActivePositionsSheet(niftySheetId, niftyPositions, timestampIst);

      // Persist unified snapshot to Google Drive file for direct Drive-based AI inspection
      updateOrCreateDriveFile(LIVE_STATUS_FILE_NAME, JSON.stringify(data, null, 2));

      return jsonResponse({
        status: "success",
        action: "sync_snapshot",
        total_open: positions.length,
        nifty_open: niftyPositions.length,
        ng_open: ngPositions.length
      });
    }

    // ── ACTION 3: CLEAR COMPLETED ROWS & OLD LEDGERS ──
    if (action === "clear_completed" || action === "clear_ledger") {
      const result = executeClearCompletedRows();
      return jsonResponse({
        status: "success",
        action: action,
        result: result
      });
    }

    return jsonResponse({ status: "ignored", reason: "Unknown action: " + action });

  } catch (err) {
    return jsonResponse({ status: "error", message: err.toString() });
  }
}

/** Helper: Overwrites or initializes the NSEBOT_ACTIVE_POSITIONS tab on a specific Google Sheet */
function updateActivePositionsSheet(spreadsheetId, positions, timestampIst) {
  try {
    const ss = SpreadsheetApp.openById(spreadsheetId);
    let sheet = ss.getSheetByName("NSEBOT_ACTIVE_POSITIONS");
    if (!sheet) {
      sheet = ss.insertSheet("NSEBOT_ACTIVE_POSITIONS");
    }

    sheet.clear();
    sheet.appendRow([
      "Last Synced (IST)",
      "Trade ID",
      "Symbol",
      "Side",
      "Lots",
      "Entry Price",
      "Stop Loss",
      "Target",
      "Status",
      "Setup Key",
      "Opened At"
    ]);
    formatHeaderRow(sheet);

    if (positions && positions.length > 0) {
      for (let i = 0; i < positions.length; i++) {
        const p = positions[i];
        sheet.appendRow([
          timestampIst,
          p.id || "-",
          p.symbol || "-",
          p.side || "-",
          p.lots !== undefined ? p.lots : "-",
          p.entry_underlying !== undefined ? p.entry_underlying : "-",
          p.sl_underlying !== undefined ? p.sl_underlying : "-",
          p.target_underlying !== undefined ? p.target_underlying : "-",
          p.trade_status || p.status || "-",
          p.signal_key || "-",
          p.opened_at || "-"
        ]);
      }
    } else {
      sheet.appendRow([
        timestampIst,
        "NONE",
        "NO ACTIVE POSITIONS",
        "-",
        0,
        "-",
        "-",
        "-",
        "IDLE",
        "-",
        "-"
      ]);
    }
    return true;
  } catch (err) {
    console.error("Failed to update active positions for sheet " + spreadsheetId + ": " + err);
    return false;
  }
}

/** Helper: Find the most recently created file with a given name in Google Drive */
function getLatestFileByName(filename) {
  const files = DriveApp.getFilesByName(filename);
  let latestFile = null;
  let latestTime = 0;

  while (files.hasNext()) {
    const file = files.next();
    const createdTime = file.getDateCreated().getTime();
    if (createdTime > latestTime) {
      latestTime = createdTime;
      latestFile = file;
    }
  }
  return latestFile;
}

/** Helper: Update or create a JSON file in Google Drive in place */
function updateOrCreateDriveFile(filename, content) {
  const existingFile = getLatestFileByName(filename);
  if (existingFile) {
    existingFile.setContent(content);
    return existingFile;
  } else {
    return DriveApp.createFile(filename, content, MimeType.PLAIN_TEXT);
  }
}

/** Helper: Format header row with bold font and background */
function formatHeaderRow(sheet) {
  const headerRange = sheet.getRange(1, 1, 1, sheet.getLastColumn());
  headerRange.setFontWeight("bold");
  headerRange.setBackground("#E8EAED");
}

/** Helper: Return JSON Content Output with CORS */
function jsonResponse(obj) {
  return ContentService.createTextOutput(JSON.stringify(obj))
    .setMimeType(ContentService.MimeType.JSON);
}

/**
 * Direct entry point to execute from Google Apps Script Editor (Run -> clearCompletedTradesAndLedger)
 */
function clearCompletedTradesAndLedger() {
  const res = executeClearCompletedRows();
  Logger.log("Cleanup completed: " + JSON.stringify(res));
  return res;
}

/**
 * Master cleanup routine:
 * 1. Clears data rows from NSEBOT_LIVE_LEDGER tab across both sheets (keeps header).
 * 2. Archives and clears completed/expired rows from main setup sheets.
 * 3. Prunes inactive/expired setups from mcx_ng_live_signal.json and nifty_fut_live_signal.json.
 */
function executeClearCompletedRows() {
  const summary = {
    ng_ledger_cleared: false,
    nifty_ledger_cleared: false,
    ng_setups_archived: 0,
    nifty_setups_archived: 0,
    drive_ng_pruned: 0,
    drive_nifty_pruned: 0
  };

  const COMPLETED_STATUSES = [
    "TARGET_1_HIT",
    "TARGET_2_HIT",
    "STOP_LOSS_HIT",
    "CLOSED",
    "COMPLETED",
    "EXPIRED",
    "EXPIRED_SESSION_CLOSE_CANCELLED",
    "CASH_PROTECT_EXIT",
    "CLOSED_EOD_SQUAREOFF",
    "CANCELLED",
    "INVALIDATED",
    "EXITED"
  ];

  // 1. Clean Ledger on NG & NIFTY sheets
  summary.ng_ledger_cleared = clearLedgerTab(NG_SPREADSHEET_ID);
  summary.nifty_ledger_cleared = clearLedgerTab(NIFTY_SPREADSHEET_ID);

  // 2. Archive completed rows from active setup sheet (first sheet)
  summary.ng_setups_archived = archiveCompletedSetupRows(NG_SPREADSHEET_ID, COMPLETED_STATUSES);
  summary.nifty_setups_archived = archiveCompletedSetupRows(NIFTY_SPREADSHEET_ID, COMPLETED_STATUSES);

  // 3. Prune Google Drive signal files
  summary.drive_ng_pruned = pruneDriveSignalFile(NG_SIGNAL_FILE_NAME, COMPLETED_STATUSES);
  summary.drive_nifty_pruned = pruneDriveSignalFile(NIFTY_SIGNAL_FILE_NAME, COMPLETED_STATUSES);

  return summary;
}

/** Helper: Deletes logged trade events from NSEBOT_LIVE_LEDGER tab (preserves row 1 header) */
function clearLedgerTab(spreadsheetId) {
  try {
    const ss = SpreadsheetApp.openById(spreadsheetId);
    const sheet = ss.getSheetByName("NSEBOT_LIVE_LEDGER");
    if (!sheet) return false;
    const lastRow = sheet.getLastRow();
    if (lastRow > 1) {
      sheet.deleteRows(2, lastRow - 1);
    }
    return true;
  } catch (e) {
    console.error("clearLedgerTab error for " + spreadsheetId + ": " + e);
    return false;
  }
}

/** Helper: Moves completed/expired setup rows from the main feed sheet to ARCHIVED_SETUPS tab */
function archiveCompletedSetupRows(spreadsheetId, completedStatuses) {
  try {
    const ss = SpreadsheetApp.openById(spreadsheetId);
    const sheet = ss.getSheets()[0];
    if (!sheet || sheet.getName().indexOf("NSEBOT") !== -1) return 0;

    const lastRow = sheet.getLastRow();
    const lastCol = sheet.getLastColumn();
    if (lastRow <= 1) return 0;

    const headers = sheet.getRange(1, 1, 1, lastCol).getValues()[0];
    let statusColIdx = -1;
    for (let c = 0; c < headers.length; c++) {
      const h = String(headers[c]).toLowerCase();
      if (h.indexOf("status") !== -1 || h.indexOf("action_bias") !== -1 || h.indexOf("trade_status") !== -1) {
        statusColIdx = c + 1;
        break;
      }
    }
    if (statusColIdx === -1) return 0;

    let archiveSheet = ss.getSheetByName("ARCHIVED_SETUPS");
    if (!archiveSheet) {
      archiveSheet = ss.insertSheet("ARCHIVED_SETUPS");
      archiveSheet.appendRow(headers);
      formatHeaderRow(archiveSheet);
    }

    let archivedCount = 0;
    // Walk from bottom to top so index deletion does not shift pending rows
    for (let r = lastRow; r >= 2; r--) {
      const val = String(sheet.getRange(r, statusColIdx).getValue()).trim().toUpperCase();
      const isCompleted = completedStatuses.some(function(s) { return val.indexOf(s) !== -1; });
      if (isCompleted) {
        const rowData = sheet.getRange(r, 1, 1, lastCol).getValues()[0];
        archiveSheet.appendRow(rowData);
        sheet.deleteRow(r);
        archivedCount++;
      }
    }
    return archivedCount;
  } catch (e) {
    console.error("archiveCompletedSetupRows error: " + e);
    return 0;
  }
}

/** Helper: Removes completed/expired setups from the JSON signal file in Google Drive */
function pruneDriveSignalFile(filename, completedStatuses) {
  try {
    const file = getLatestFileByName(filename);
    if (!file) return 0;
    const content = file.getBlob().getDataAsString();
    const data = JSON.parse(content);
    if (!data || !data.setups || !Array.isArray(data.setups)) return 0;

    const initialLen = data.setups.length;
    data.setups = data.setups.filter(function(s) {
      const st = String(s.status || s.trade_status || "").trim().toUpperCase();
      return !completedStatuses.some(function(cs) { return st.indexOf(cs) !== -1; });
    });
    const pruned = initialLen - data.setups.length;
    if (pruned > 0) {
      file.setContent(JSON.stringify(data, null, 2));
    }
    return pruned;
  } catch (e) {
    console.error("pruneDriveSignalFile error for " + filename + ": " + e);
    return 0;
  }
}

/**
 * ── ONE-TIME OAUTH AUTHORIZATION FUNCTION ──
 * Run this function ONCE in the Apps Script editor to trigger Google OAuth Authorization
 * for SpreadsheetApp and DriveApp scopes.
 *
 * Steps:
 * 1. Open the Apps Script editor: https://script.google.com
 * 2. Select 'authorizeAndTestPermissions' from the function dropdown and click 'Run'.
 * 3. In the popup: Click 'Review Permissions' -> Select your Google Account -> 'Advanced' -> 'Go to (unsafe)' -> 'Allow'.
 * 4. Once execution finishes, click: Deploy -> Manage deployments -> Click the pencil icon (Edit) -> Version: 'New version' -> Deploy.
 */
function authorizeAndTestPermissions() {
  Logger.log("1. Authorizing SpreadsheetApp access...");
  const ssNg = SpreadsheetApp.openById(NG_SPREADSHEET_ID);
  Logger.log("✅ NG Spreadsheet connected: " + ssNg.getName());

  const ssNifty = SpreadsheetApp.openById(NIFTY_SPREADSHEET_ID);
  Logger.log("✅ Nifty Spreadsheet connected: " + ssNifty.getName());

  Logger.log("2. Authorizing DriveApp access...");
  const root = DriveApp.getRootFolder();
  Logger.log("✅ Drive root connected: " + root.getName());

  Logger.log("🎉 SUCCESS: All permissions authorized! Now deploy as a NEW VERSION under Manage Deployments.");
}
