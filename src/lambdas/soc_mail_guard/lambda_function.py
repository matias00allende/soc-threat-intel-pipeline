import json
import logging
import os
from email.utils import parseaddr


logger = logging.getLogger()
logger.setLevel(logging.INFO)

ALLOWED_RECIPIENT = os.environ.get(
    "ALLOWED_RECIPIENT",
    "",
).strip().lower()

ALLOWED_DOMAINS = {
    value.strip().lower()
    for value in os.environ.get(
        "ALLOWED_SENDER_DOMAINS",
        "",
    ).split(",")
    if value.strip()
}

if not ALLOWED_RECIPIENT:
    raise RuntimeError("ALLOWED_RECIPIENT env var is required and must not be empty")

if not ALLOWED_DOMAINS:
    raise RuntimeError("ALLOWED_SENDER_DOMAINS env var is required and must not be empty")

PASS_STATUSES = {"PASS"}
NON_FAIL_STATUSES = {"PASS", "GRAY", "PROCESSING_FAILED", "NONE", "PENDING"}


def response(disposition: str, reason: str) -> dict:
    logger.info(json.dumps({"disposition": disposition, "reason": reason}))
    return {"disposition": disposition}


def parse_domain(value: str) -> str:
    address = parseaddr(value or "")[1] or (value or "")
    if "@" not in address:
        return ""
    return address.rsplit("@", 1)[1].strip().lower()


def domain_allowed(domain: str) -> bool:
    return any(domain == allowed or domain.endswith(f".{allowed}") for allowed in ALLOWED_DOMAINS)


def handler(event, context):
    try:
        records = event.get("Records", [])
        if not records:
            return response("STOP_RULE_SET", "Event without SES records")

        for record in records:
            ses = record.get("ses", {})
            mail = ses.get("mail", {})
            receipt = ses.get("receipt", {})

            recipients = [str(x).strip().lower() for x in (receipt.get("recipients") or mail.get("destination") or []) if str(x).strip()]
            if ALLOWED_RECIPIENT not in recipients:
                return response("STOP_RULE_SET", f"Recipient not allowed: {recipients}")

            source = str(mail.get("source", "")).strip()
            common_headers = mail.get("commonHeaders", {})
            from_headers = common_headers.get("from") or []
            candidate_sender = source or (from_headers[0] if from_headers else "")
            sender_domain = parse_domain(candidate_sender)

            if not sender_domain or not domain_allowed(sender_domain):
                return response("STOP_RULE_SET", f"Sender domain not allowed: {sender_domain or 'unknown'}")

            spam_status = str((receipt.get("spamVerdict") or {}).get("status", "")).upper()
            virus_status = str((receipt.get("virusVerdict") or {}).get("status", "")).upper()
            spf_status = str((receipt.get("spfVerdict") or {}).get("status", "")).upper()
            dkim_status = str((receipt.get("dkimVerdict") or {}).get("status", "")).upper()

            if spam_status not in PASS_STATUSES:
                return response("STOP_RULE_SET", f"Spam verdict rejected: {spam_status}")

            if virus_status not in PASS_STATUSES:
                return response("STOP_RULE_SET", f"Virus verdict rejected: {virus_status}")

            if spf_status and spf_status not in NON_FAIL_STATUSES:
                return response("STOP_RULE_SET", f"SPF verdict rejected: {spf_status}")

            if dkim_status and dkim_status not in NON_FAIL_STATUSES:
                return response("STOP_RULE_SET", f"DKIM verdict rejected: {dkim_status}")

        return response("CONTINUE", "Message accepted")
    except Exception as exc:
        logger.exception("Unhandled error while validating SES inbound mail")
        return response("STOP_RULE_SET", f"Unhandled exception: {exc}")
