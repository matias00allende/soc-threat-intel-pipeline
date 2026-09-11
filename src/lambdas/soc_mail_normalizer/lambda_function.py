import base64
import hashlib
import html
import json
import logging
import os
import re
import urllib.error
import urllib.request
from email import policy
from email.parser import BytesParser

import boto3
from botocore.exceptions import ClientError


logger = logging.getLogger()
logger.setLevel(logging.INFO)

s3 = boto3.client("s3")

NORM_BUCKET = os.environ["NORM_BUCKET"]
NORM_PREFIX = os.environ["NORM_PREFIX"]
WEBHOOK_URL = os.environ.get("WEBHOOK_URL", "").strip()
WEBHOOK_TIMEOUT_SECONDS = int(os.environ.get("WEBHOOK_TIMEOUT_SECONDS", "10"))
WEBHOOK_REQUIRED = os.environ.get("WEBHOOK_REQUIRED", "false").strip().lower() == "true"
DISPATCH_PREFIX = os.environ.get("DISPATCH_PREFIX", "normalized/dispatched/")
WEBHOOK_TOKEN = os.environ.get("WEBHOOK_TOKEN", "").strip()
WEBHOOK_TOKEN_HEADER = os.environ.get("WEBHOOK_TOKEN_HEADER", "X-Webhook-Token").strip() or "X-Webhook-Token"
WEBHOOK_BASIC_USER = os.environ.get("WEBHOOK_BASIC_USER", "").strip()
WEBHOOK_BASIC_PASSWORD = os.environ.get("WEBHOOK_BASIC_PASSWORD", "").strip()

URL_RE = re.compile(r'https?://[^\s<>"\']+', re.IGNORECASE)
IP_RE = re.compile(r'\b(?:\d{1,3}\.){3}\d{1,3}\b')
SHA256_RE = re.compile(r'\b[a-fA-F0-9]{64}\b')
DOMAIN_RE = re.compile(r'\b(?=.{1,253}\b)(?:[a-zA-Z0-9](?:[a-zA-Z0-9-]{0,61}[a-zA-Z0-9])?\.)+[A-Za-z]{2,63}\b')


def strip_html(text: str) -> str:
    text = re.sub(r'(?is)<script.*?>.*?</script>', ' ', text)
    text = re.sub(r'(?is)<style.*?>.*?</style>', ' ', text)
    text = re.sub(r'(?is)<br\s*/?>', '\n', text)
    text = re.sub(r'(?is)</p\s*>', '\n', text)
    text = re.sub(r'(?is)<.*?>', ' ', text)
    text = html.unescape(text)
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r'\n{3,}', '\n\n', text)
    return text.strip()


def sanitize_text(text: str) -> str:
    text = text.replace('\x00', ' ')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r'\n{3,}', '\n\n', text)
    return text.strip()


def parse_email(raw_bytes: bytes):
    msg = BytesParser(policy=policy.default).parsebytes(raw_bytes)

    text_parts = []
    html_parts = []
    attachments = []

    if msg.is_multipart():
        for part in msg.walk():
            content_disposition = (part.get_content_disposition() or "").lower()
            content_type = (part.get_content_type() or "").lower()

            if content_disposition == "attachment":
                attachments.append({
                    "filename": part.get_filename(),
                    "content_type": content_type,
                    "size_bytes": len(part.get_payload(decode=True) or b""),
                })
                continue

            try:
                payload = part.get_content()
            except Exception:
                payload = None

            if not payload:
                continue

            if content_type == "text/plain":
                text_parts.append(str(payload))
            elif content_type == "text/html":
                html_parts.append(str(payload))
    else:
        payload = msg.get_content()
        ctype = (msg.get_content_type() or "").lower()
        if ctype == "text/html":
            html_parts.append(str(payload))
        else:
            text_parts.append(str(payload))

    plain = "\n\n".join([sanitize_text(t) for t in text_parts if t and str(t).strip()])
    html_text = "\n\n".join([strip_html(h) for h in html_parts if h and str(h).strip()])

    best_body = plain if plain else html_text
    merged_text = "\n\n".join([x for x in [plain, html_text] if x]).strip()

    return {
        "subject": str(msg.get("subject", "")),
        "from": str(msg.get("from", "")),
        "to": str(msg.get("to", "")),
        "cc": str(msg.get("cc", "")),
        "date": str(msg.get("date", "")),
        "message_id": str(msg.get("message-id", "")),
        "reply_to": str(msg.get("reply-to", "")),
        "body_text_best_effort": best_body,
        "body_text_merged": merged_text,
        "attachments": attachments,
    }


def unique_sorted(values):
    return sorted(list(set([v.strip().lower() for v in values if v and str(v).strip()])))


def prelim_iocs(text: str):
    urls = unique_sorted(URL_RE.findall(text or ""))
    ips = unique_sorted(IP_RE.findall(text or ""))
    sha256 = unique_sorted(SHA256_RE.findall(text or ""))
    domains = unique_sorted([d for d in DOMAIN_RE.findall(text or "") if not d.lower().startswith("amazonaws.com")])
    return {
        "ips": ips,
        "domains": domains,
        "urls": urls,
        "sha256": sha256,
    }


def object_exists(bucket: str, key: str) -> bool:
    try:
        s3.head_object(Bucket=bucket, Key=key)
        return True
    except ClientError as exc:
        if exc.response.get("Error", {}).get("Code") in {"404", "NoSuchKey", "NotFound"}:
            return False
        raise


def build_webhook_headers() -> dict:
    headers = {"Content-Type": "application/json"}

    if WEBHOOK_TOKEN:
        headers[WEBHOOK_TOKEN_HEADER] = WEBHOOK_TOKEN

    if WEBHOOK_BASIC_USER or WEBHOOK_BASIC_PASSWORD:
        raw = f"{WEBHOOK_BASIC_USER}:{WEBHOOK_BASIC_PASSWORD}".encode("utf-8")
        headers["Authorization"] = f"Basic {base64.b64encode(raw).decode('ascii')}"

    return headers


def dispatch_to_n8n(doc: dict, normalized_key: str, dispatch_key: str):
    if not WEBHOOK_URL:
        logger.info("Skipping n8n webhook because WEBHOOK_URL is not configured")
        return

    if object_exists(NORM_BUCKET, dispatch_key):
        logger.info("Dispatch marker already exists, skipping webhook for %s", normalized_key)
        return

    payload = {
        "email": doc["email"],
        "normalized_key": normalized_key,
        "source": "ses",
        "raw_bucket": doc["source"]["bucket"],
        "raw_key": doc["source"]["key"],
    }

    request = urllib.request.Request(
        WEBHOOK_URL,
        data=json.dumps(payload).encode("utf-8"),
        headers=build_webhook_headers(),
        method="POST",
    )

    try:
        with urllib.request.urlopen(request, timeout=WEBHOOK_TIMEOUT_SECONDS) as response:
            status = getattr(response, "status", response.getcode())
            body = response.read().decode("utf-8", errors="replace")

        if 200 <= status < 300:
            s3.put_object(
                Bucket=NORM_BUCKET,
                Key=dispatch_key,
                Body=json.dumps({"normalized_key": normalized_key, "status": status}).encode("utf-8"),
                ContentType="application/json",
            )
            logger.info("Dispatched normalized mail to n8n with status %s", status)
            return

        message = f"n8n webhook returned status {status}: {body}"
        if WEBHOOK_REQUIRED:
            raise RuntimeError(message)
        logger.warning(message)
    except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError, RuntimeError) as exc:
        if WEBHOOK_REQUIRED:
            raise
        logger.warning("Best-effort webhook dispatch failed for %s: %s", normalized_key, exc)


def handler(event, context):
    for record in event.get("Records", []):
        src_bucket = record["s3"]["bucket"]["name"]
        src_key = record["s3"]["object"]["key"]

        if not src_key.startswith("raw/inbox/"):
            continue

        obj = s3.get_object(Bucket=src_bucket, Key=src_key)
        raw_bytes = obj["Body"].read()

        parsed = parse_email(raw_bytes)
        iocs = prelim_iocs(parsed.get("body_text_merged", ""))

        digest = hashlib.sha256(f"{src_bucket}:{src_key}".encode()).hexdigest()
        out_key = f"{NORM_PREFIX}{digest}.json"
        dispatch_key = f"{DISPATCH_PREFIX}{digest}.json"

        doc = {
            "source": {
                "bucket": src_bucket,
                "key": src_key,
                "etag": record["s3"]["object"].get("eTag"),
            },
            "email": parsed,
            "prelim_iocs": iocs,
            "pipeline": {
                "normalizer": os.environ.get("AWS_LAMBDA_FUNCTION_NAME"),
            },
        }

        s3.put_object(
            Bucket=NORM_BUCKET,
            Key=out_key,
            Body=json.dumps(doc, ensure_ascii=False, indent=2).encode("utf-8"),
            ContentType="application/json",
        )

        dispatch_to_n8n(doc, out_key, dispatch_key)

    return {"ok": True}
