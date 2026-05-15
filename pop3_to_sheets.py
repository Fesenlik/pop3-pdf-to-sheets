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

        if from_contains and from_contains not in from_value:
            continue
        if subject_contains and subject_contains not in subject:
            continue

        for part in msg.walk():
            cdisp = str(part.get("Content-Disposition", ""))
            ctype = str(part.get_content_type() or "")
            filename = decode_mime(part.get_filename() or "")
            filename_lower = filename.lower()
            if "attachment" in cdisp.lower() and (filename.lower().endswith(".pdf") or "pdf" in ctype.lower()):
                if attachment_name_contains and attachment_name_contains not in filename_lower:
                    continue
                payload = part.get_payload(decode=True)
                if payload:
                    return {
                        "message_id": message_id,
                        "subject": subject,
                        "pdf_bytes": payload,
                    }
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


def main():
    cfg = load_config()
    state_path = cfg["state"]["processed_ids_path"]
    processed = load_state(state_path)

    now = datetime.now(timezone.utc).astimezone().strftime("%Y-%m-%d %H:%M:%S")

    pop_conn = pop3_connect(cfg["pop3"])
    try:
        item = fetch_latest_matching_pdf(pop_conn, cfg["mail_filter"], processed)
    finally:
        pop_conn.quit()

    base_payload = {
        "webhook_secret": cfg["webhook"]["secret"],
        "target_sheet": cfg["google"]["target_sheet_name"],
        "log_sheet": cfg["google"]["log_sheet_name"],
        "timestamp": now,
    }

    if not item:
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

    text = extract_pdf_text(item["pdf_bytes"])
    if len(text.strip()) < 50:
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
    if not headers or not rows:
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


if __name__ == "__main__":
    main()
