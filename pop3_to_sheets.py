import email
import json
import poplib
import re
from datetime import datetime, timezone
from email.header import decode_header
from pathlib import Path
from typing import Any

import pdfplumber
import requests


def load_config(path: str = "config.pop3.json") -> dict[str, Any]:
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def decode_mime(value: str | None) -> str:
    if not value:
        return ""
    parts = decode_header(value)
    out = []
    for p, enc in parts:
        if isinstance(p, bytes):
            out.append(p.decode(enc or "utf-8", errors="replace"))
        else:
            out.append(p)
    return "".join(out)


def load_state(path: str) -> set[str]:
    p = Path(path)
    if not p.exists():
        return set()
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        return set(data.get("processed_ids", []))
    except Exception:
        return set()


def save_state(path: str, ids: set[str]) -> None:
    Path(path).write_text(
        json.dumps({"processed_ids": sorted(list(ids))[-5000:]}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def pop3_connect(cfg: dict[str, Any]):
    host = cfg["host"]
    port = int(cfg.get("port", 995))
    use_ssl = bool(cfg.get("use_ssl", True))

    conn = poplib.POP3_SSL(host, port) if use_ssl else poplib.POP3(host, port)
    conn.user(cfg["username"])
    conn.pass_(cfg["password"])
    return conn


def fetch_latest_matching_pdf(pop_conn, mail_filter: dict[str, str], processed_ids: set[str]):
    _, items, _ = pop_conn.list()
    print(f"[DEBUG] mailbox_message_count={len(items)}")
    if not items:
        return None

    message_numbers = [int(x.decode().split(" ")[0]) for x in items]
    message_numbers.sort(reverse=True)

    from_contains = mail_filter.get("from_contains", "").lower().strip()
    subject_contains = mail_filter.get("subject_contains", "").lower().strip()
    attachment_name_contains = mail_filter.get("attachment_name_contains", "").lower().strip()

    for msg_num in message_numbers:
        _, lines, _ = pop_conn.retr(msg_num)
        raw = b"\r\n".join(lines)
        msg = email.message_from_bytes(raw)

        message_id = decode_mime(msg.get("Message-Id", "")).strip() or f"pop3-{msg_num}"
        if message_id in processed_ids:
            continue

        from_value = decode_mime(msg.get("From", "")).lower()
        subject = decode_mime(msg.get("Subject", "")).lower()
        print(f"[DEBUG] checking_msg_num={msg_num} message_id={message_id}")
        print(f"[DEBUG] from={from_value}")
        print(f"[DEBUG] subject={subject}")

        if from_contains and from_contains not in from_value:
            print(f"[DEBUG] skip_reason=from_filter from_contains={from_contains}")
            continue
        if subject_contains and subject_contains not in subject:
            print(f"[DEBUG] skip_reason=subject_filter subject_contains={subject_contains}")
            continue

        for part in msg.walk():
            cdisp = str(part.get("Content-Disposition", ""))
            ctype = str(part.get_content_type() or "")
            filename = decode_mime(part.get_filename() or "")
            filename_lower = filename.lower()
            if "attachment" in cdisp.lower() and (filename.lower().endswith(".pdf") or "pdf" in ctype.lower()):
                print(f"[DEBUG] found_pdf_attachment filename={filename}")
                if attachment_name_contains and attachment_name_contains not in filename_lower:
                    print(
                        f"[DEBUG] skip_reason=attachment_name_filter "
                        f"attachment_name_contains={attachment_name_contains}"
                    )
                    continue
                payload = part.get_payload(decode=True)
                if payload:
                    print(f"[DEBUG] selected_pdf filename={filename} message_id={message_id}")
                    return {
                        "message_id": message_id,
                        "subject": subject,
                        "pdf_bytes": payload,
                    }
    print("[DEBUG] no_matching_message_or_pdf_found")
    return None


def normalize_text(text: str) -> str:
    return re.sub(r"\n{3,}", "\n\n", re.sub(r"[ \t]+", " ", text.replace("\xa0", " "))).strip()


def extract_pdf_text(pdf_bytes: bytes) -> str:
    tmp = Path("_tmp_report.pdf")
    tmp.write_bytes(pdf_bytes)
    try:
        texts = []
        with pdfplumber.open(str(tmp)) as pdf:
            for page in pdf.pages:
                texts.append(page.extract_text() or "")
        return normalize_text("\n".join(texts))
    finally:
        if tmp.exists():
            tmp.unlink()


def extract_pdf_table(pdf_bytes: bytes):
    tmp = Path("_tmp_report.pdf")
    tmp.write_bytes(pdf_bytes)
    try:
        headers = None
        collected_rows = []

        table_settings = {
            "vertical_strategy": "lines",
            "horizontal_strategy": "lines",
            "intersection_tolerance": 5,
            "snap_tolerance": 3,
            "join_tolerance": 3,
        }

        with pdfplumber.open(str(tmp)) as pdf:
            for page in pdf.pages:
                for table in page.extract_tables(table_settings=table_settings):
                    cleaned = []
                    for row in table:
                        if not row:
                            continue
                        normalized = [((cell or "").strip()) for cell in row]
                        if any(normalized):
                            cleaned.append(normalized)

                    if not cleaned:
                        continue

                    first_row = cleaned[0]
                    first_row_text = " ".join(first_row).lower()

                    # Skip report totals/footer tables.
                    if "toplam" in first_row_text and len(cleaned) <= 3:
                        continue

                    normalized_first = [re.sub(r"\s+", " ", (c or "")).strip() for c in first_row]
                    header_probe = " ".join(normalized_first).lower()
                    looks_like_header = (
                        "sipariş no" in header_probe
                        and "parti no" in header_probe
                        and ("ham adı" in header_probe or "ham adi" in header_probe)
                    )

                    if looks_like_header:
                        headers = [h if h else f"Column_{i+1}" for i, h in enumerate(normalized_first)]
                        for row in cleaned[1:]:
                            normalized_row = [re.sub(r"\s+", " ", (c or "")).strip() for c in row]
                            if not any(normalized_row):
                                continue
                            row_fixed = normalized_row[: len(headers)] + [""] * max(0, len(headers) - len(normalized_row))
                            collected_rows.append([normalize_cell(v) for v in row_fixed])
                        continue

                    # Data-only table: accept after we already have headers.
                    if headers and len(first_row) >= max(3, len(headers) - 3):
                        for row in cleaned:
                            normalized_row = [re.sub(r"\s+", " ", (c or "")).strip() for c in row]
                            if not any(normalized_row):
                                continue
                            row_fixed = normalized_row[: len(headers)] + [""] * max(0, len(headers) - len(normalized_row))
                            collected_rows.append([normalize_cell(v) for v in row_fixed])

        if not headers or not collected_rows:
            return None, None
        return headers, collected_rows
    finally:
        if tmp.exists():
            tmp.unlink()


def parse_dynamic_table(text: str):
    lines = [l.strip() for l in text.splitlines() if l.strip()]
    if not lines:
        return None, None

    candidates = []
    for idx, line in enumerate(lines):
        parts = [p.strip() for p in re.split(r"\s{2,}|\t|\|", line) if p.strip()]
        if len(parts) >= 3:
            candidates.append((idx, parts))
    if not candidates:
        return None, None

    header_idx, headers = candidates[0]
    headers = [h if h else f"Column_{i+1}" for i, h in enumerate(headers)]

    rows = []
    for line in lines[header_idx + 1 :]:
        parts = [p.strip() for p in re.split(r"\s{2,}|\t|\|", line) if p.strip()]
        if len(parts) < max(2, len(headers) // 2):
            continue
        row = parts[: len(headers)] + [""] * max(0, len(headers) - len(parts))
        rows.append([normalize_cell(v) for v in row])

    return headers, rows


def normalize_cell(value: str):
    v = (value or "").strip()
    if re.fullmatch(r"-?\d{1,3}(\.\d{3})*(,\d+)?", v):
        try:
            return float(v.replace(".", "").replace(",", "."))
        except Exception:
            return v
    if re.fullmatch(r"-?\d+(,\d+)?", v):
        try:
            return float(v.replace(",", "."))
        except Exception:
            return v
    return v


def post_to_apps_script(url: str, webhook_secret: str, payload: dict[str, Any]) -> None:
    headers = {"Content-Type": "application/json"}
    if webhook_secret:
        headers["X-Webhook-Secret"] = webhook_secret

    resp = requests.post(url, headers=headers, data=json.dumps(payload, ensure_ascii=False).encode("utf-8"), timeout=60)
    resp.raise_for_status()
    try:
        body = resp.json()
    except Exception:
        raise RuntimeError(f"Apps Script returned non-JSON response: {resp.text[:300]}")
    if not body.get("ok"):
        raise RuntimeError(f"Apps Script returned failure: {body}")


def main():
    cfg = load_config()
    state_path = cfg["state"]["processed_ids_path"]
    processed = load_state(state_path)

    now = datetime.now(timezone.utc).astimezone().strftime("%Y-%m-%d %H:%M:%S")

    pop_conn = pop3_connect(cfg["pop3"])
    print("[DEBUG] pop3_connected=true")
    try:
        item = fetch_latest_matching_pdf(pop_conn, cfg["mail_filter"], processed)
    finally:
        pop_conn.quit()
        print("[DEBUG] pop3_connection_closed=true")

    base_payload = {
        "webhook_secret": cfg["webhook"]["secret"],
        "target_sheet": cfg["google"]["target_sheet_name"],
        "log_sheet": cfg["google"]["log_sheet_name"],
        "timestamp": now,
    }

    if not item:
        print("[DEBUG] result=no_matching_email")
        post_to_apps_script(
            cfg["webhook"]["url"],
            cfg["webhook"]["secret"],
            {
                **base_payload,
                "message_id": "",
                "status": "INFO",
                "note": "No matching unprocessed email.",
                "headers": [],
                "rows": [],
            },
        )
        return

    headers, rows = extract_pdf_table(item["pdf_bytes"])
    print(f"[DEBUG] table_extract_headers={len(headers) if headers else 0} table_extract_rows={len(rows) if rows else 0}")

    if not headers or not rows:
        text = extract_pdf_text(item["pdf_bytes"])
        print(f"[DEBUG] extracted_text_length={len(text.strip())}")
        if len(text.strip()) < 50:
            print("[DEBUG] result=pdf_text_too_short")
            post_to_apps_script(
                cfg["webhook"]["url"],
                cfg["webhook"]["secret"],
                {
                    **base_payload,
                    "message_id": item["message_id"],
                    "status": "ERROR",
                    "note": "PDF text extraction failed/too short",
                    "headers": [],
                    "rows": [],
                },
            )
            return
        headers, rows = parse_dynamic_table(text)
        print(f"[DEBUG] text_parse_headers={len(headers) if headers else 0} text_parse_rows={len(rows) if rows else 0}")

    if not headers or not rows:
        print("[DEBUG] result=table_parse_failed")
        post_to_apps_script(
            cfg["webhook"]["url"],
            cfg["webhook"]["secret"],
            {
                **base_payload,
                "message_id": item["message_id"],
                "status": "ERROR",
                "note": "Table parse failed",
                "headers": [],
                "rows": [],
            },
        )
        return

    post_to_apps_script(
        cfg["webhook"]["url"],
        cfg["webhook"]["secret"],
        {
            **base_payload,
            "message_id": item["message_id"],
            "status": "SUCCESS",
            "note": "Processed and replaced target sheet",
            "headers": headers,
            "rows": rows,
        },
    )

    processed.add(item["message_id"])
    save_state(state_path, processed)
    print("[DEBUG] result=success")


if __name__ == "__main__":
    main()
