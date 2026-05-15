# POP3 -> PDF -> Google Sheets (GitHub Actions)

Bu sürüm bilgisayardan bağımsızdır. GitHub Actions günlük tetiklenir, POP3 maili okur, PDF tabloyu parse eder ve Google Sheets'e Apps Script Web App üzerinden yazar.

## 1) Google Sheets Apps Script Web App

Google Sheet'te `Extensions -> Apps Script` açıp aşağıdaki kodu ekle:

```javascript
function doPost(e) {
  const body = JSON.parse(e.postData.contents || "{}");
  const secret = body.webhook_secret || "";
  const expected = "CHANGE_ME_SECRET";
  if (!secret || secret !== expected) {
    return ContentService.createTextOutput(JSON.stringify({ ok: false, error: "unauthorized" }))
      .setMimeType(ContentService.MimeType.JSON);
  }

  const ss = SpreadsheetApp.getActiveSpreadsheet();
  const targetName = body.target_sheet || "Rapor";
  const logName = body.log_sheet || "Log";
  const target = ss.getSheetByName(targetName) || ss.insertSheet(targetName);
  const log = ss.getSheetByName(logName) || ss.insertSheet(logName);

  const headers = body.headers || [];
  const rows = body.rows || [];
  const status = body.status || "INFO";

  if (status === "SUCCESS" && headers.length && rows.length) {
    target.clearContents();
    target.clearFormats();
    target.getRange(1, 1, 1, headers.length).setValues([headers]);
    target.getRange(2, 1, rows.length, headers.length).setValues(rows);
    target.setFrozenRows(1);
    target.autoResizeColumns(1, headers.length);
  }

  if (log.getLastRow() === 0) {
    log.appendRow(["Timestamp", "MessageId", "Level", "Message", "HeaderCount", "RowCount"]);
  }

  log.appendRow([
    body.timestamp || new Date().toISOString(),
    body.message_id || "",
    status,
    body.note || "",
    headers.length,
    rows.length
  ]);

  return ContentService.createTextOutput(JSON.stringify({ ok: true }))
    .setMimeType(ContentService.MimeType.JSON);
}
```

Deploy:
- `Deploy -> New deployment -> Web app`
- Execute as: `Me`
- Who has access: `Anyone`
- URL'yi al.
- Kod içindeki `expected` değerini secret ile aynı yap.

## 2) GitHub Secrets

Repository -> Settings -> Secrets and variables -> Actions -> New repository secret:
- `POP3_HOST`
- `POP3_PORT` (örn `995`)
- `POP3_USERNAME`
- `POP3_PASSWORD`
- `MAIL_FROM_CONTAINS`
- `MAIL_SUBJECT_CONTAINS`
- `APPS_SCRIPT_WEBHOOK_URL`
- `WEBHOOK_SECRET`

## 3) Schedule

Workflow dosyası: `.github/workflows/daily_pop3_to_sheets.yml`
- Varsayılan cron: `30 6 * * *` (UTC)
- Turkiye saatiyle 09:30 için uygundur.
- Elle test için Actions ekranından `Run workflow` kullanılabilir.

## 4) Local test (opsiyonel)

`config.pop3.example.json` dosyasını `config.pop3.json` yapıp değerleri doldurarak:

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
python pop3_to_sheets.py
```

## Notlar

- POP3 event tetik desteklemez; GitHub Actions zamanlanmış kontrol yapar.
- İşlenen mail ID'leri `processed_ids.json` içinde tutulur ve artifact olarak saklanır.
- PDF parse başarısız olursa sheet overwrite yapılmaz; sadece Log'a hata düşer.
