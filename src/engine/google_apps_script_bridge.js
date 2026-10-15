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
