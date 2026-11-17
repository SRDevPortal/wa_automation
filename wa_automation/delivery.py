"""Consume status records from WA Chat Hub's already authenticated webhook path."""
import json
from datetime import timedelta

import frappe
from wa_automation.core import merge_status, utcnow


def callback_recipient(payload):
    candidates = [payload, payload.get("data") or {}, (payload.get("data") or {}).get("message") or {}]
    for item in list(candidates):
        meta = item.get("meta_data") or item.get("metaData")
        if isinstance(meta, str):
            try:
                meta = json.loads(meta)
            except ValueError:
                meta = {"callbackData": meta}
        if isinstance(meta, dict):
            candidates.append(meta)
    for item in candidates:
        value = item.get("callbackData") or item.get("callback_data")
        if isinstance(value, str) and value.startswith("wa_automation:"):
            return value.split(":", 1)[1]
    return None


def on_verified_status(channel_account, message_id, status, payload):
    recipient_id = callback_recipient(payload)
    if recipient_id:
        apply_status(recipient_id, channel_account, message_id, status)
    elif message_id:
        for name in frappe.get_all("WA Campaign Recipient", filters={"provider_message_id": message_id}, pluck="name"):
            apply_status(name, channel_account, message_id, status)
    return False


def apply_status(name, account, provider_id, incoming, chat_message=None):
    row = frappe.db.get_value("WA Campaign Recipient", name, ["status", "run", "accepted_at"], as_dict=True, for_update=True)
    if not row:
        return
    snapshot = json.loads(frappe.db.get_value("WA Campaign Run", row.run, "snapshot_json") or "{}")
    if snapshot.get("channel_account") != account:
        return
    status = merge_status(row.status, incoming)
    if status == row.status:
        return
    values = {"status": status}
    if provider_id:
        values["provider_message_id"] = provider_id
    if chat_message:
        values["chat_message"] = chat_message
    if not row.accepted_at:
        values["accepted_at"] = utcnow().replace(tzinfo=None)
    frappe.db.set_value("WA Campaign Recipient", name, values)


def on_chat_message(doc, method=None):
    if doc.direction != "Outbound" or doc.delivery_status not in {"Sent", "Delivered", "Read", "Failed"}:
        return
    account = frappe.db.get_value("Chat Conversation", doc.conversation, "channel_account")
    provider_id = doc.get("provider_message_id") or doc.get("channel_message_id")
    recipient_id = None
    callback = doc.get("provider_event_id") or ""
    try:
        raw = json.loads(doc.get("raw_transport_payload") or "{}")
        message = (raw.get("data") or {}).get("message") or {}
        metadata = message.get("meta_data") or raw.get("meta_data") or {}
        if isinstance(metadata, str):
            try:
                metadata = json.loads(metadata)
            except ValueError:
                metadata = {"callbackData": metadata}
        callback = callback or raw.get("callbackData") or message.get("callbackData") or metadata.get("callbackData") or metadata.get("callback_data") or ""
    except (ValueError, AttributeError, TypeError):
        callback = ""
    if isinstance(callback, str) and callback.startswith("wa_automation:"):
        recipient_id = callback.split(":", 1)[1]
    if not recipient_id and provider_id:
        # Match both provider identity and selected account, never ID alone.
        matches = frappe.get_all("WA Campaign Recipient", filters={"provider_message_id": provider_id}, fields=["name", "run"])
        for row in matches:
            snapshot = frappe.db.get_value("WA Campaign Run", row.run, "snapshot_json")
            if json.loads(snapshot or "{}").get("channel_account") == account:
                recipient_id = row.name
                break
    if not recipient_id or not frappe.db.exists("WA Campaign Recipient", recipient_id):
        return
    apply_status(recipient_id, account, provider_id, doc.delivery_status, doc.name)


def reconcile():
    # WA Chat Hub also uses database updates that do not trigger on_update.
    # Rotate through all eligible recipients. Repeatedly taking the oldest 500
    # would starve later records when those first rows have no new receipts.
    cursor_key = "wa_automation:delivery_reconcile_cursor"
    cursor = frappe.cache().get_value(cursor_key, expires=True)
    filters = {"status": ["in", ["Sending", "Unknown", "Accepted", "Sent", "Delivered"]],
        "creation": [">", frappe.utils.add_days(frappe.utils.now_datetime(), -30)]}
    if cursor:
        filters["name"] = [">", cursor]
    active = frappe.get_all("WA Campaign Recipient", filters=filters,
        fields=["name", "provider_message_id", "chat_message", "run"], order_by="name asc", limit_page_length=500)
    touched = set()
    for row in active:
        filters = {"name": row.chat_message} if row.chat_message else {"channel_message_id": row.provider_message_id} if row.provider_message_id else None
        if not filters:
            continue
        name = frappe.db.get_value("Chat Message", filters, "name")
        if name:
            on_chat_message(frappe.get_doc("Chat Message", name))
            touched.add(row.run)
    # Refresh recent totals, including runs updated by the document hook.
    touched.update(frappe.get_all("WA Campaign Recipient", filters={"modified": [">", frappe.utils.add_to_date(frappe.utils.now_datetime(), minutes=-5)]}, pluck="run"))
    from wa_automation.jobs import update_totals
    for name in touched:
        if name:
            update_totals(name)
    if len(active) == 500:
        frappe.cache().set_value(cursor_key, active[-1].name, expires_in_sec=3600)
    else:
        frappe.cache().delete_value(cursor_key)
