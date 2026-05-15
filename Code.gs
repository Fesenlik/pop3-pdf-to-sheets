/**
 * Gmail PDF -> Google Sheets automation.
 *
 * Setup:
 * 1) Fill CONFIG values below.
 * 2) Enable Advanced Google Services: Drive API.
 * 3) Add time trigger for runAutomation (every 5 min).
 */

const CONFIG = {
  queryFrom: 'reports@example.com',
  querySubjectKeyword: 'Isletmedeki Partiler',
  processedLabelName: 'processed-pdf-report',
  targetSheetName: 'Rapor',
  logSheetName: 'Log',
  timezone: 'Europe/Istanbul'
};

function runAutomation() {
  try {
    const label = getOrCreateLabel_(CONFIG.processedLabelName);
    const message = fetchLatestUnprocessedMessage_(label);

    if (!message) {
      logEvent_('', 'INFO', 'No matching unprocessed email found.', 0, 0);
      return;
    }

    const messageId = message.getId();
    const attachment = getFirstPdfAttachment_(message);

    if (!attachment) {
      logEvent_(messageId, 'WARN', 'No PDF attachment found on message.', 0, 0);
      markMessageProcessed_(message, label);
      return;
    }

    const textResult = extractPdfTextWithFallback_(attachment);
    if (!textResult || !textResult.text || textResult.text.trim().length < 50) {
      logEvent_(messageId, 'ERROR', 'PDF text extraction failed or too short.', 0, 0);
      return;
    }

    const parsed = parseDynamicTable_(textResult.text);
    if (!parsed.isValid) {
      logEvent_(messageId, 'ERROR', parsed.error || 'Failed to parse table from PDF.', 0, 0);
      return;
    }

    replaceSheetData_(parsed.headers, parsed.rows);
    markMessageProcessed_(message, label);

    logEvent_(
      messageId,
      'SUCCESS',
      `Processed via ${textResult.method}.`,
      parsed.headers.length,
      parsed.rows.length
    );
  } catch (err) {
    logEvent_('', 'ERROR', `Unhandled error: ${err && err.message ? err.message : String(err)}`, 0, 0);
    throw err;
  }
}

function installFiveMinuteTrigger() {
  ScriptApp.getProjectTriggers().forEach((t) => {
    if (t.getHandlerFunction() === 'runAutomation') {
      ScriptApp.deleteTrigger(t);
    }
  });

  ScriptApp.newTrigger('runAutomation')
    .timeBased()
    .everyMinutes(5)
    .create();
}

function fetchLatestUnprocessedMessage_(processedLabel) {
  const query = [
    `from:${CONFIG.queryFrom}`,
    `subject:${CONFIG.querySubjectKeyword}`,
    'has:attachment',
    '-in:chats',
    `-label:${processedLabel.getName()}`
  ].join(' ');

  const threads = GmailApp.search(query, 0, 20);
  if (!threads.length) {
    return null;
  }

  const allMessages = [];
  threads.forEach((thread) => {
    thread.getMessages().forEach((msg) => {
      if (!msg.isInChats()) {
        allMessages.push(msg);
      }
    });
  });

  allMessages.sort((a, b) => b.getDate().getTime() - a.getDate().getTime());

  for (let i = 0; i < allMessages.length; i += 1) {
    const msg = allMessages[i];
    const labels = msg.getThread().getLabels().map((l) => l.getName());
    if (labels.indexOf(processedLabel.getName()) === -1) {
      return msg;
    }
  }

  return null;
}

function getFirstPdfAttachment_(message) {
  const attachments = message.getAttachments({ includeInlineImages: false, includeAttachments: true });
  for (let i = 0; i < attachments.length; i += 1) {
    const att = attachments[i];
    const name = att.getName() || '';
    const ctype = att.getContentType() || '';
    if (/\.pdf$/i.test(name) || ctype.toLowerCase().indexOf('pdf') !== -1) {
      return att;
    }
  }
  return null;
}

function extractPdfTextWithFallback_(attachment) {
  const direct = extractPdfTextDirect_(attachment);
  if (direct && direct.trim().length >= 50) {
    return { method: 'direct', text: normalizeText_(direct) };
  }

  const ocr = extractPdfTextWithDriveOcr_(attachment);
  if (ocr && ocr.trim().length >= 50) {
    return { method: 'drive-ocr', text: normalizeText_(ocr) };
  }

  return null;
}

function extractPdfTextDirect_(attachment) {
  try {
    const file = DriveApp.createFile(attachment.copyBlob());
    const text = extractTextFromPdfFileId_(file.getId());
    file.setTrashed(true);
    return text;
  } catch (e) {
    return '';
  }
}

function extractPdfTextWithDriveOcr_(attachment) {
  let sourceFile;
  let ocrDoc;
  try {
    sourceFile = DriveApp.createFile(attachment.copyBlob());

    const resource = {
      title: `ocr_${Date.now()}`,
      mimeType: MimeType.GOOGLE_DOCS
    };

    const inserted = Drive.Files.copy(resource, sourceFile.getId(), {
      ocr: true,
      ocrLanguage: 'tr'
    });

    ocrDoc = DocumentApp.openById(inserted.id);
    return ocrDoc.getBody().getText();
  } catch (e) {
    return '';
  } finally {
    try {
      if (ocrDoc) {
        DriveApp.getFileById(ocrDoc.getId()).setTrashed(true);
      }
      if (sourceFile) {
        sourceFile.setTrashed(true);
      }
    } catch (cleanupErr) {
      // no-op cleanup safety
    }
  }
}

function extractTextFromPdfFileId_(fileId) {
  try {
    const copied = Drive.Files.copy(
      { title: `txt_${Date.now()}`, mimeType: MimeType.GOOGLE_DOCS },
      fileId,
      { ocr: false }
    );
    const doc = DocumentApp.openById(copied.id);
    const text = doc.getBody().getText();
    DriveApp.getFileById(copied.id).setTrashed(true);
    return text;
  } catch (e) {
    return '';
  }
}

function normalizeText_(text) {
  return text
    .replace(/\u00A0/g, ' ')
    .replace(/[ \t]+/g, ' ')
    .replace(/\r/g, '\n')
    .replace(/\n{3,}/g, '\n\n')
    .trim();
}

function parseDynamicTable_(text) {
  const lines = text
    .split(/\n/)
    .map((l) => l.trim())
    .filter((l) => l.length > 0);

  if (!lines.length) {
    return { isValid: false, error: 'No lines found in PDF text.' };
  }

  const candidateLines = lines
    .map((line, idx) => ({
      idx,
      line,
      parts: line.split(/\s{2,}|\t|\s\|\s|\|/).map((p) => p.trim()).filter(Boolean)
    }))
    .filter((x) => x.parts.length >= 3);

  if (!candidateLines.length) {
    return { isValid: false, error: 'No multi-column candidate lines found.' };
  }

  let header = null;
  for (let i = 0; i < candidateLines.length; i += 1) {
    const c = candidateLines[i];
    const alphaCount = c.parts.filter((p) => /[A-Za-zÇĞİÖŞÜçğıöşü]/.test(p)).length;
    const numericCount = c.parts.filter((p) => /\d/.test(p)).length;
    if (alphaCount >= 2 && alphaCount >= numericCount) {
      header = c;
      break;
    }
  }

  if (!header) {
    return { isValid: false, error: 'Unable to detect a header row.' };
  }

  const headers = header.parts.map((h, i) => {
    const clean = h.replace(/\s+/g, ' ').trim();
    return clean || `Column_${i + 1}`;
  });

  const rows = [];
  for (let i = header.idx + 1; i < lines.length; i += 1) {
    const raw = lines[i];
    const parts = raw.split(/\s{2,}|\t|\s\|\s|\|/).map((p) => p.trim()).filter(Boolean);

    if (!parts.length) {
      continue;
    }

    if (parts.length < Math.max(2, Math.floor(headers.length / 2))) {
      continue;
    }

    const normalized = alignAndNormalizeRow_(parts, headers.length);
    if (!normalized) {
      continue;
    }

    rows.push(normalized);
  }

  if (!rows.length) {
    return { isValid: false, error: 'Header found but no valid rows parsed.' };
  }

  return {
    isValid: true,
    headers,
    rows
  };
}

function alignAndNormalizeRow_(parts, expectedLen) {
  const row = parts.slice(0, expectedLen);

  while (row.length < expectedLen) {
    row.push('');
  }

  for (let i = 0; i < row.length; i += 1) {
    row[i] = normalizeCellValue_(row[i]);
  }

  return row;
}

function normalizeCellValue_(value) {
  const v = String(value || '').trim();

  if (/^-?\d{1,3}(\.\d{3})*(,\d+)?$/.test(v)) {
    const normalized = v.replace(/\./g, '').replace(',', '.');
    const num = Number(normalized);
    return Number.isNaN(num) ? v : num;
  }

  if (/^-?\d+(,\d+)?$/.test(v)) {
    const num2 = Number(v.replace(',', '.'));
    return Number.isNaN(num2) ? v : num2;
  }

  return v;
}

function replaceSheetData_(headers, rows) {
  if (!headers || !headers.length || !rows || !rows.length) {
    throw new Error('replaceSheetData requires non-empty headers and rows.');
  }

  const ss = SpreadsheetApp.getActiveSpreadsheet();
  const sheet = ss.getSheetByName(CONFIG.targetSheetName) || ss.insertSheet(CONFIG.targetSheetName);

  sheet.clearContents();
  sheet.clearFormats();

  const values = [headers].concat(rows);
  sheet.getRange(1, 1, values.length, headers.length).setValues(values);
  sheet.setFrozenRows(1);
  sheet.autoResizeColumns(1, headers.length);
}

function markMessageProcessed_(message, label) {
  message.getThread().addLabel(label);
}

function getOrCreateLabel_(name) {
  return GmailApp.getUserLabelByName(name) || GmailApp.createLabel(name);
}

function logEvent_(messageId, level, message, headerCount, rowCount) {
  const ss = SpreadsheetApp.getActiveSpreadsheet();
  const logSheet = ss.getSheetByName(CONFIG.logSheetName) || ss.insertSheet(CONFIG.logSheetName);

  if (logSheet.getLastRow() === 0) {
    logSheet
      .getRange(1, 1, 1, 6)
      .setValues([['Timestamp', 'MessageId', 'Level', 'Message', 'HeaderCount', 'RowCount']]);
    logSheet.setFrozenRows(1);
  }

  const ts = Utilities.formatDate(new Date(), CONFIG.timezone, 'yyyy-MM-dd HH:mm:ss');
  logSheet.appendRow([ts, messageId || '', level, message, headerCount || 0, rowCount || 0]);
}
