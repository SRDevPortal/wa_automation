"""Run via bench execute wa_automation.tests.integration.run. No live provider calls."""
import io
import json
import unittest
import uuid
from datetime import timedelta
from unittest.mock import patch

import frappe

from wa_automation import api, audience, jobs, delivery, run_actions
from wa_automation.core import utcnow
from wa_automation.jobs import enqueue_run as real_enqueue_run

CATALOG = [{"name": "wa_automation_test", "language_code": "en", "body_preview": "Hello {{1}}", "body_variable_count": 1}]
FIELDS = {name: {"fieldname": name, "fieldtype": kind} for name, kind in (("name", "Data"), ("patient_name", "Data"), ("mobile", "Data"), ("creation", "Datetime"))}


def source(name="FAKE-PATIENT-1", phone="9876543210", display="Test"):
    return frappe._dict(name=name, mobile=phone, patient_name=display)


class IntegrationTests(unittest.TestCase):
    def setUp(self):
        frappe.set_user("Administrator")
        self.account_name = "WA-AUTOMATION-TEST-" + uuid.uuid4().hex[:10]
        # db_insert avoids unrelated account synchronization hooks. There are NO credentials.
        self.account = frappe.get_doc({"doctype": "Chat Channel Account", "name": self.account_name,
            "account_name": self.account_name, "channel_type": "Interakt", "is_active": 1,
            "interakt_default_country_code": "+91"})
        self.account.db_insert()
        # Dispatcher tests mock the provider and supply a non-working credential in memory only.
        self.account.get_password = lambda *args, **kwargs: "NOT-A-REAL-API-KEY"
        self.doc = frappe.get_doc({"doctype": "WA Campaign", "title": self.account_name,
            "channel_account": self.account_name, "template_name": "wa_automation_test", "language_code": "en",
            "reference_doctype": "Patient", "phone_field": "mobile", "sort_field": "creation", "sort_direction": "desc",
            "match_mode": "All", "consent_confirmed": 1, "delivery_mode": "Now", "timezone": "Asia/Kolkata",
            "repeat_policy": "Every matching run", "variables": [{"component":"Body", "position":1, "source_type":"Field", "field_name":"patient_name"}]}).insert(ignore_permissions=True)
        self.enqueue = patch("wa_automation.jobs.enqueue_run").start()
        self.catalog = patch("wa_automation.audience.approved_templates", return_value=CATALOG).start()
        self.source_patch = patch("wa_automation.audience.source_records", return_value=(FIELDS, [source()]))
        self.source = self.source_patch.start()
        # Fake source identifiers intentionally have no Patient rows. Never create real contacts in tests.
        patch("frappe.model.document.Document._validate_links").start()

    def tearDown(self):
        patch.stopall()
        frappe.set_user("Administrator")
        frappe.db.delete("WA Campaign Recipient", {"campaign": self.doc.name})
        frappe.db.delete("WA Campaign Run", {"campaign": self.doc.name})
        for dt in ("WA Campaign Filter", "WA Template Variable"):
            frappe.db.delete(dt, {"parent": self.doc.name})
        frappe.db.delete("WA Campaign", {"name": self.doc.name})
        frappe.db.delete("Chat Channel Account", {"name": self.account_name})
        frappe.cache().delete_value(f"wa_interakt_templates::{self.account_name}")
        frappe.cache().delete_value(f"wa_automation:template_picker:{self.account_name}")
        frappe.db.commit()

    def prepared(self):
        name = jobs.create_run(self.doc, utcnow(), uuid.uuid4().hex)
        run = frappe.get_doc("WA Campaign Run", name)
        jobs.prepare_run(run)
        recipient = frappe.get_doc("WA Campaign Recipient", {"run": name})
        return run, recipient

    def send(self, recipient, outcome=None, error=None):
        with patch("wa_automation.audience.recheck_recipient", return_value=self.account), patch("wa_chat_hub.outbound.send_interakt_message", return_value=outcome, side_effect=error) as transport, patch("wa_automation.jobs.append_history"):
            jobs.dispatch(self.doc.as_dict(), recipient)
        return transport

    def test_real_metadata_has_custom_fields(self):
        fields = audience.available_fields("Patient")
        self.assertIn("mobile", fields)
        self.assertNotIn("patient_relation", fields)

    def test_new_queue_refreshes_inherited_worker_cache(self):
        from frappe.utils.background_jobs import get_queues_timeout, validate_queue
        try:
            with patch.dict(frappe.conf, {"workers": {}}):
                get_queues_timeout.cache_clear()
                self.assertNotIn("wa_campaign", get_queues_timeout())
            with patch.dict(frappe.conf, {"workers":{"wa_campaign":{"timeout":900}}, "wa_automation_queue":"wa_campaign"}):
                def enqueue(method, **kwargs):
                    validate_queue(kwargs["queue"])
                with patch("frappe.enqueue", side_effect=enqueue) as transport:
                    real_enqueue_run("queue-regression-test")
                self.assertEqual(transport.call_args.kwargs["queue"], "wa_campaign")
                self.assertEqual(transport.call_args.kwargs["job_id"], "wa_automation:queue-regression-test")
        finally:
            get_queues_timeout.cache_clear()

    def test_template_picker_cache_miss_never_calls_provider(self):
        self.assertEqual(api.templates(self.account_name, cached_only=1), [])
        self.catalog.assert_not_called()

    def test_template_picker_keeps_display_cache_after_provider_cache_expires(self):
        key = f"wa_interakt_templates::{self.account_name}"
        frappe.cache().set_value(key, CATALOG, expires_in_sec=300)
        first = api.templates(self.account_name, cached_only=1)
        frappe.cache().delete_value(key)
        self.assertEqual(api.templates(self.account_name, cached_only=1), first)
        self.assertEqual(first[0]["name"], "wa_automation_test")
        self.catalog.assert_not_called()

    def test_template_refresh_updates_picker_cache(self):
        result = api.templates(self.account_name, refresh=1)
        self.catalog.assert_called_once_with({"channel_account": self.account_name}, True)
        self.catalog.reset_mock()
        self.assertEqual(api.templates(self.account_name, cached_only=1), result)
        self.catalog.assert_not_called()

    def test_template_picker_checks_account_permission_before_cache(self):
        frappe.cache().set_value(f"wa_automation:template_picker:{self.account_name}", CATALOG)
        with patch("wa_automation.audience.account_for", side_effect=frappe.PermissionError):
            with self.assertRaises(frappe.PermissionError):
                api.templates(self.account_name, cached_only=1)

    def test_removed_preferences_in_legacy_snapshot_are_ignored(self):
        config = self.doc.as_dict()
        config.update(consent_field="removed_consent", opt_out_field="removed_opt_out")
        audience.validate_config(config, ready=True)
        record = source()
        record.update(removed_consent=0, removed_opt_out=1)
        self.assertIsNone(audience.suppression_reason(config, record))
        record.unsubscribed = 1
        self.assertEqual(audience.suppression_reason(config, record), "Opted out")

    def test_removed_consent_field_cannot_replace_confirmation(self):
        config = self.doc.as_dict()
        config.update(consent_field="removed_consent", consent_confirmed=0)
        with self.assertRaisesRegex(frappe.ValidationError, "Confirm this audience"):
            audience.validate_config(config, ready=True)

    def test_new_patient_next_cycle(self):
        first = audience.build_audience(self.doc.as_dict(), catalog=CATALOG)
        self.source.return_value = (FIELDS, [source(), source("FAKE-PATIENT-2", "9876543211", "New")])
        second = audience.build_audience(self.doc.as_dict(), catalog=CATALOG)
        self.assertEqual(len(first), 1)
        self.assertEqual(sum(r["status"] == "Pending" for r in second), 2)

    def test_duplicate_phone(self):
        self.source.return_value = (FIELDS, [source(), source("FAKE-2")])
        rows = audience.build_audience(self.doc.as_dict(), catalog=CATALOG)
        self.assertEqual([r["status"] for r in rows], ["Pending", "Skipped"])

    def test_shared_number_conflict_excludes_both(self):
        self.source.return_value = (FIELDS, [source(), source("FAKE-2", display="Other")])
        rows = audience.build_audience(self.doc.as_dict(), catalog=CATALOG)
        self.assertEqual([r["status"] for r in rows], ["Skipped", "Skipped"])

    def test_missing_variable(self):
        self.source.return_value = (FIELDS, [source(display="")])
        self.assertEqual(audience.build_audience(self.doc.as_dict(), catalog=CATALOG)[0]["status"], "Skipped")

    def test_incomplete_country_prefix_does_not_stop_valid_recipients(self):
        self.source.return_value = (FIELDS, [source("BAD-PHONE", "91"), source()])
        rows = audience.build_audience(self.doc.as_dict(), catalog=CATALOG)
        self.assertEqual([r["status"] for r in rows], ["Skipped", "Pending"])

    def test_future_retries_do_not_block_ready_recipients_beyond_batch_limit(self):
        run, recipient = self.prepared()
        frappe.db.set_value("WA Campaign Recipient", recipient.name, {"status":"Retry", "next_attempt_at":(utcnow()+timedelta(hours=1)).replace(tzinfo=None)})
        for i in range(1, 101):
            doc = frappe.copy_doc(recipient)
            doc.update(dict(name=None, recipient_key=f"{run.name}-{i}", reference_name=f"FAKE-{i}",
                status="Retry" if i < 100 else "Pending", next_attempt_at=(utcnow()+timedelta(hours=1)).replace(tzinfo=None) if i < 100 else None))
            doc.insert(ignore_permissions=True)
        with patch("wa_automation.jobs.dispatch", return_value=True) as dispatch:
            jobs.process_run(run.name)
        dispatch.assert_called_once()
        self.assertEqual(dispatch.call_args.args[1].reference_name, "FAKE-100")

    def test_failure_receipt_after_sent_updates_recipient_and_totals(self):
        run, recipient = self.prepared()
        self.send(recipient, {"sent":True, "provider_message_id":"receipt-order-test"})
        delivery.apply_status(recipient.name,self.account_name,"receipt-order-test","Sent")
        delivery.apply_status(recipient.name,self.account_name,"receipt-order-test","Failed")
        recipient.reload()
        self.assertEqual(recipient.status, "Failed")
        jobs.update_totals(run.name)
        self.assertEqual(frappe.db.get_value("WA Campaign Run",run.name,"failed"),1)

    def test_delivery_reconciliation_reaches_recipients_after_first_500(self):
        rows = [frappe._dict(name=f"AUDIT-{i:04}", provider_message_id=f"provider-{i}", chat_message=f"chat-{i}", run="audit-run") for i in range(501)]
        cursor_key = "wa_automation:delivery_reconcile_cursor"
        previous = frappe.cache().get_value(cursor_key, expires=True)
        frappe.cache().delete_value(cursor_key)
        def page(doctype, **kwargs):
            if kwargs.get("pluck"):
                return []
            cursor = (kwargs["filters"].get("name") or [None, ""])[1]
            return [r for r in rows if r.name > cursor][:kwargs["limit_page_length"]]
        try:
            with patch("frappe.get_all", side_effect=page), patch("frappe.db.get_value", side_effect=lambda dt, filters, field: filters["name"]), patch("frappe.get_doc", side_effect=lambda dt, name: frappe._dict(name=name)), patch("wa_automation.delivery.on_chat_message") as receipt, patch("wa_automation.jobs.update_totals"):
                delivery.reconcile()
                delivery.reconcile()
                self.assertEqual(len({call.args[0].name for call in receipt.call_args_list}), 501)
        finally:
            if previous is None:
                frappe.cache().delete_value(cursor_key)
            else:
                frappe.cache().set_value(cursor_key, previous, expires_in_sec=3600)

    def test_wrong_account_template_rejected(self):
        with self.assertRaises(frappe.ValidationError): audience.build_audience(self.doc.as_dict(), catalog=[])

    def test_repeated_click_same_run(self):
        first = jobs.create_run(self.doc, utcnow(), "same-request")
        second = jobs.create_run(self.doc, utcnow(), "same-request")
        self.assertEqual(first, second)
        self.assertEqual(frappe.db.count("WA Campaign Run", {"campaign": self.doc.name}), 1)

    def test_overlapping_run_blocked(self):
        jobs.create_run(self.doc, utcnow(), "one")
        with self.assertRaises(frappe.ValidationError): jobs.create_run(self.doc, utcnow(), "two")

    def test_accepted_is_not_delivered(self):
        run, recipient = self.prepared()
        transport = self.send(recipient, {"sent":True, "provider_message_id":"test-provider"})
        transport.assert_called_once()
        self.assertEqual(transport.call_args.args[0].name, self.account_name)
        recipient.reload()
        self.assertEqual(recipient.status, "Accepted")
        jobs.update_totals(run.name)
        run.reload()
        self.assertEqual((run.accepted, run.delivered, run.status), (1, 0, "Completed"))

    def test_unknown_not_retried(self):
        from wa_chat_hub.delivery_outcomes import PatientTemplateOutcomeUnknownError
        run, recipient = self.prepared()
        self.send(recipient, error=PatientTemplateOutcomeUnknownError("timeout"))
        recipient.reload()
        self.assertEqual(recipient.status, "Unknown")
        self.assertFalse(recipient.next_attempt_at)
        jobs.update_totals(run.name)
        run.reload()
        self.assertEqual(run.unknown, 1)

    def test_429_backoff(self):
        from wa_chat_hub.delivery_outcomes import PatientTemplateNotSentError
        _, recipient = self.prepared()
        error = PatientTemplateNotSentError("limited", retryable=True); error.status_code = 429
        self.send(recipient, error=error)
        recipient.reload()
        self.assertEqual(recipient.status, "Retry")
        self.assertTrue(recipient.next_attempt_at)

    def test_5xx_is_unknown(self):
        from wa_chat_hub.delivery_outcomes import PatientTemplateNotSentError
        _, recipient = self.prepared()
        error = PatientTemplateNotSentError("server error", retryable=True); error.status_code = 500
        self.send(recipient, error=error)
        recipient.reload()
        self.assertEqual(recipient.status, "Unknown")

    def test_pause_prevents_dispatch(self):
        _, recipient = self.prepared()
        frappe.db.set_value("WA Campaign", self.doc.name, "status", "Paused")
        transport = self.send(recipient, {"sent":True, "provider_message_id":"unused"})
        transport.assert_not_called()
        recipient.reload()
        self.assertEqual(recipient.status, "Pending")

    def test_webhook_account_isolation_and_order(self):
        _, recipient = self.prepared()
        payload = {"data":{"message":{"meta_data":{"callbackData":"wa_automation:" + recipient.name}}}}
        delivery.on_verified_status("wrong-account", "provider", "Delivered", payload)
        recipient.reload(); self.assertEqual(recipient.status, "Pending")
        delivery.on_verified_status(self.account_name, "provider", "Read", payload)
        delivery.on_verified_status(self.account_name, "provider", "Sent", payload)
        recipient.reload(); self.assertEqual(recipient.status, "Read")

    def test_new_only_excludes_accepted(self):
        _, recipient = self.prepared()
        self.send(recipient, {"sent":True, "provider_message_id":"test-provider"})
        config = self.doc.as_dict(); config["repeat_policy"] = "Only new recipients"
        rows = audience.build_audience(config, catalog=CATALOG)
        self.assertEqual(rows[0]["status"], "Skipped")
        self.source.return_value = (FIELDS, [source("FAKE-2", "9876543211", "New")])
        self.assertEqual(audience.build_audience(config, catalog=CATALOG)[0]["status"], "Pending")

    def test_disabled_owner(self):
        with self.assertRaises(frappe.ValidationError):
            with jobs.as_owner("nonexistent-user@example.invalid"): pass

    def test_source_permission_denied(self):
        with patch("frappe.has_permission", return_value=False):
            with self.assertRaises(frappe.PermissionError): audience.available_fields("Patient")

    def test_unknown_filter_field_denied(self):
        config = self.doc.as_dict()
        config["filters"] = [{"field_name":"not_a_real_field", "operator":"=", "value":"x"}]
        with self.assertRaises(frappe.ValidationError): audience.validate_config(config)

    def test_unknown_sort_field_denied(self):
        config = self.doc.as_dict(); config["sort_field"] = "name desc; drop table"
        with self.assertRaises(frappe.ValidationError): audience.validate_config(config)

    def test_real_source_query_is_permission_aware(self):
        self.source_patch.stop()
        with patch("frappe.get_list", return_value=[]) as query:
            config = self.doc.as_dict(); config["filters"] = [{"field_name":"creation", "operator":"last days", "value":"7"}]
            audience.source_records(config, utcnow())
        self.assertEqual(query.call_args.args[0], "Patient")
        self.assertEqual(query.call_args.kwargs["filters"][0][1], "between")
        self.assertNotIn("ignore_permissions", query.call_args.kwargs)

    def test_all_installed_source_doctypes_support_actual_filtered_queries(self):
        self.source_patch.stop()
        for doctype in ("Patient", "Lead", "CRM Lead"):
            if not frappe.db.exists("DocType", doctype):
                continue
            with self.subTest(doctype=doctype):
                fields = audience.available_fields(doctype)
                phone = next(n for n in ("mobile", "mobile_no", "whatsapp_no", "phone") if n in fields)
                config = self.doc.as_dict()
                config.update(reference_doctype=doctype, phone_field=phone,
                    variables=[{"component":"Body", "position":1, "source_type":"Fixed", "fixed_value":"Audit"}],
                    filters=[{"field_name":"name", "operator":"=", "value":self.account_name}])
                self.assertFalse(frappe.db.exists(doctype,self.account_name))
                self.assertEqual(audience.build_audience(config, catalog=CATALOG), [])

    def test_any_filters_use_or_query(self):
        self.source_patch.stop()
        with patch("frappe.get_list", return_value=[]) as query:
            config = self.doc.as_dict()
            config.update(match_mode="Any", filters=[{"field_name":"name","operator":"=","value":"A"}, {"field_name":"name","operator":"=","value":"B"}])
            audience.source_records(config, utcnow())
        self.assertEqual(query.call_args.kwargs["filters"], [])
        self.assertEqual(len(query.call_args.kwargs["or_filters"]), 2)

    def test_429_retry_is_bounded(self):
        from wa_chat_hub.delivery_outcomes import PatientTemplateNotSentError
        run, recipient = self.prepared()
        maximum = int(frappe.db.get_single_value("WA Automation Settings", "max_attempts") or 3)
        recipient.attempts = maximum - 1
        error = PatientTemplateNotSentError("limited",retryable=True)
        error.status_code=429
        self.send(recipient,error=error)
        recipient.reload()
        self.assertEqual(recipient.status,"Failed")
        self.assertFalse(recipient.next_attempt_at)
        jobs.update_totals(run.name)
        self.assertEqual(frappe.db.get_value("WA Campaign Run",run.name,"status"),"Completed")

    def test_account_rate_limit_applies_within_same_worker_batch(self):
        self.source.return_value = (FIELDS, [source(),source("FAKE-2","9876543211")])
        run,_ = self.prepared()
        recipients = [frappe.get_doc("WA Campaign Recipient",n) for n in frappe.get_all("WA Campaign Recipient",filters={"run":run.name},pluck="name",order_by="creation asc")]
        get_setting = frappe.db.get_single_value
        def setting(doctype, field, *args, **kwargs):
            return 1 if doctype == "WA Automation Settings" and field == "messages_per_minute" else get_setting(doctype,field,*args,**kwargs)
        with patch("frappe.db.get_single_value",side_effect=setting), patch("wa_automation.jobs.time.time",return_value=1800000000):
            first = self.send(recipients[0],{"sent":True,"provider_message_id":"rate-one"})
            second = self.send(recipients[1],{"sent":True,"provider_message_id":"must-not-send"})
            first.assert_called_once()
            second.assert_not_called()
        recipients[1].reload()
        self.assertEqual(recipients[1].status,"Pending")

    def test_minimum_gap_excludes_recently_accepted_numbers(self):
        _, recipient = self.prepared()
        self.send(recipient,{"sent":True,"provider_message_id":"gap-test"})
        config = self.doc.as_dict()
        config.minimum_gap_days = 7
        current = audience.build_audience(config,catalog=CATALOG)
        later = audience.build_audience(config,anchor=utcnow()+timedelta(days=8),catalog=CATALOG)
        self.assertEqual(current[0]["last_error"],"Minimum message gap has not elapsed")
        self.assertEqual(later[0]["status"],"Pending")

    def test_process_run_end_to_end(self):
        name = jobs.create_run(self.doc, utcnow(), "worker-test")
        with patch("wa_automation.audience.recheck_recipient", return_value=self.account), patch("wa_chat_hub.outbound.send_interakt_message", return_value={"sent":True,"provider_message_id":"worker-provider"}) as transport, patch("wa_automation.jobs.append_history"):
            jobs.process_run(name)
            jobs.process_run(name)
            transport.assert_called_once()
        self.assertEqual(frappe.db.get_value("WA Campaign Run", name, "status"), "Completed")

    def test_old_run_receipts_do_not_stop_new_queued_run(self):
        old, recipient = self.prepared()
        self.send(recipient, {"sent": True, "provider_message_id": "old-provider"})
        jobs.update_totals(old.name)
        finished = frappe.db.get_value("WA Campaign Run", old.name, "finished_at")
        newer = jobs.create_run(self.doc, utcnow(), "new-scheduled-cycle")
        frappe.db.set_value("WA Campaign Recipient", recipient.name, "status", "Delivered")
        jobs.update_totals(old.name)
        self.assertEqual(frappe.db.get_value("WA Campaign", self.doc.name, "status"), "Active")
        self.assertEqual(frappe.db.get_value("WA Campaign Run", newer, "status"), "Queued")
        self.assertEqual(frappe.db.get_value("WA Campaign Run", old.name, "finished_at"), finished)
        with patch("wa_automation.audience.recheck_recipient", return_value=self.account), patch("wa_chat_hub.outbound.send_interakt_message", return_value={"sent":True,"provider_message_id":"new-provider"}) as transport, patch("wa_automation.jobs.append_history"):
            jobs.process_run(newer)
            transport.assert_called_once()
        self.assertEqual(frappe.db.get_value("WA Campaign Run", newer, "status"), "Completed")

    def test_old_run_receipts_do_not_stop_reactivated_campaign(self):
        run, recipient = self.prepared()
        self.send(recipient, {"sent": True, "provider_message_id": "old-provider"})
        jobs.update_totals(run.name)
        frappe.db.set_value("WA Campaign", self.doc.name, {"status":"Active", "next_run_at":None})
        jobs.update_totals(run.name)
        self.assertEqual(frappe.db.get_value("WA Campaign", self.doc.name, "status"), "Active")

    def test_cancel_queued_run_releases_campaign_for_new_schedule(self):
        name = jobs.create_run(self.doc, utcnow(), "cancel-queued")
        run_actions.control(name, "cancel")
        run_actions.control(name, "cancel")
        jobs.update_totals(name)
        self.assertEqual(frappe.db.get_value("WA Campaign Run", name, "status"), "Cancelled")
        self.assertEqual(frappe.db.get_value("WA Campaign", self.doc.name, "status"), "Paused")
        with patch("wa_chat_hub.outbound.send_interakt_message") as transport:
            jobs.process_run(name)
            transport.assert_not_called()
        self.assertNotEqual(jobs.create_run(self.doc, utcnow(), "new-after-cancel"), name)

    def test_cancel_removes_waiting_queue_entry(self):
        from unittest.mock import Mock
        from rq.job import JobStatus
        name = jobs.create_run(self.doc, utcnow(), "queued-entry")
        job = Mock()
        job.get_status.return_value = JobStatus.QUEUED
        with patch("frappe.utils.background_jobs.get_job", return_value=job):
            run_actions.control(name, "cancel")
        job.cancel.assert_called_once()

    def test_retry_cancelled_run_uses_same_run(self):
        name = jobs.create_run(self.doc, utcnow(), "cancel-then-retry")
        run_actions.control(name, "cancel")
        self.enqueue.reset_mock()
        run_actions.control(name, "retry")
        self.enqueue.assert_called_once_with(name)
        self.assertEqual(frappe.db.count("WA Campaign Run", {"campaign":self.doc.name}), 1)
        self.assertEqual(frappe.db.get_value("WA Campaign Run", name, "status"), "Queued")
        self.assertEqual(frappe.db.get_value("WA Campaign", self.doc.name, "status"), "Active")

    def test_retry_preserves_accepted_unknown_and_preexisting_skips(self):
        self.source.return_value = (FIELDS, [source(f"FAKE-{i}", f"987654321{i}") for i in range(4)])
        name = jobs.create_run(self.doc, utcnow(), "mixed-cancel")
        jobs.prepare_run(frappe.get_doc("WA Campaign Run", name))
        rows = frappe.get_all("WA Campaign Recipient", filters={"run":name}, pluck="name", order_by="creation asc")
        frappe.db.set_value("WA Campaign Recipient", rows[0], {"status":"Accepted", "provider_message_id":"accepted-id", "accepted_at":utcnow().replace(tzinfo=None)})
        frappe.db.set_value("WA Campaign Recipient", rows[1], "status", "Sending")
        frappe.db.set_value("WA Campaign Recipient", rows[2], {"status":"Skipped", "last_error":"Opted out"})
        run_actions.control(name, "cancel")
        self.assertEqual(frappe.db.get_value("WA Campaign Recipient", rows[1], "status"), "Unknown")
        run_actions.control(name, "retry")
        self.assertEqual([frappe.db.get_value("WA Campaign Recipient", r, "status") for r in rows],
            ["Accepted", "Unknown", "Skipped", "Pending"])
        with patch("wa_automation.audience.recheck_recipient", return_value=self.account), patch("wa_chat_hub.outbound.send_interakt_message", return_value={"sent":True,"provider_message_id":"retried-id"}) as transport, patch("wa_automation.jobs.append_history"):
            jobs.process_run(name)
            transport.assert_called_once()

    def test_retry_rejects_other_unfinished_run(self):
        old = jobs.create_run(self.doc, utcnow(), "old-cancelled")
        run_actions.control(old, "cancel")
        jobs.create_run(self.doc, utcnow(), "new-open")
        with self.assertRaisesRegex(frappe.ValidationError, "Another run is unfinished"):
            run_actions.control(old, "retry")

    def test_run_control_rejects_worker_holding_lock(self):
        name = jobs.create_run(self.doc, utcnow(), "worker-lock")
        with frappe.cache().lock(f"wa_automation:run:{name}", timeout=60):
            with self.assertRaisesRegex(frappe.ValidationError, "currently processing"):
                run_actions.control(name, "cancel")
        self.assertEqual(frappe.db.get_value("WA Campaign Run", name, "status"), "Queued")

    def test_run_control_requires_sender(self):
        name = jobs.create_run(self.doc, utcnow(), "permission-test")
        with patch("wa_automation.run_actions.require_sender", side_effect=frappe.PermissionError):
            with self.assertRaises(frappe.PermissionError):
                run_actions.control(name, "cancel")
        self.assertEqual(frappe.db.get_value("WA Campaign Run", name, "status"), "Queued")

    def test_retry_does_not_resend_delivery_failures_after_acceptance(self):
        run, recipient = self.prepared()
        self.send(recipient, {"sent":True,"provider_message_id":"accepted-then-failed"})
        frappe.db.set_value("WA Campaign Recipient", recipient.name, "status", "Failed")
        run_actions.control(run.name, "cancel")
        with self.assertRaisesRegex(frappe.ValidationError, "No safe pending"):
            run_actions.control(run.name, "retry")

    def test_test_send_does_not_activate_campaign(self):
        name = api.send_test(self.doc.name, "FAKE-PATIENT-1", "+919876543211", "test-preview")
        self.assertEqual(frappe.db.get_value("WA Campaign", self.doc.name, "status"), "Draft")
        row = frappe.get_doc("WA Campaign Recipient", {"run":name})
        self.assertTrue(row.is_test)
        self.assertEqual(row.phone, "+919876543211")
        self.assertEqual(json.loads(row.payload_json)["phoneNumber"], "9876543211")
        with patch("wa_automation.audience.recheck_recipient", return_value=self.account), patch("wa_chat_hub.outbound.send_interakt_message", return_value={"sent":True,"provider_message_id":"test-only-provider"}), patch("wa_automation.jobs.append_history"):
            jobs.process_run(name)
        self.assertEqual(frappe.db.get_value("WA Campaign", self.doc.name, "status"), "Draft")
        config = self.doc.as_dict(); config["repeat_policy"] = "Only new recipients"
        self.assertEqual(audience.build_audience(config, catalog=CATALOG)[0]["status"], "Pending")

    def test_end_cancels_pending(self):
        run, recipient = self.prepared()
        api.set_state(self.doc.name, "end")
        recipient.reload(); run.reload()
        self.assertEqual(recipient.status, "Skipped")
        self.assertEqual(run.status, "Completed")
        self.assertEqual(frappe.db.get_value("WA Campaign", self.doc.name, "status"), "Completed")

    def test_crash_recovery_never_resends(self):
        run, recipient = self.prepared()
        frappe.db.set_value("WA Campaign Recipient", recipient.name, {"status":"Sending", "dispatch_started_at":(utcnow()-timedelta(minutes=10)).replace(tzinfo=None)})
        frappe.db.set_value("WA Campaign", self.doc.name, "status", "Paused")
        jobs.recover_stale_sends()
        recipient.reload()
        self.assertEqual(recipient.status, "Unknown")

    def test_stale_scheduler_cannot_reactivate_paused_campaign(self):
        due = utcnow()
        frappe.db.set_value("WA Campaign", self.doc.name, {"delivery_mode":"Weekly", "status":"Paused", "next_run_at":due.replace(tzinfo=None)})
        self.assertIsNone(jobs.create_run(self.doc, due, "scheduled:" + due.isoformat()))
        self.assertEqual(frappe.db.count("WA Campaign Run", {"campaign":self.doc.name}), 0)

    def test_missing_credentials_fail_without_submission(self):
        _, recipient = self.prepared()
        self.account.get_password = lambda *args, **kwargs: None
        transport = self.send(recipient)
        transport.assert_not_called()
        recipient.reload(); self.assertEqual(recipient.status, "Failed")

    def test_resume_past_once_schedule_with_unfinished_run(self):
        run, recipient = self.prepared()
        frappe.db.set_value("WA Campaign", self.doc.name, {"delivery_mode":"Once", "status":"Paused", "next_run_at":None, "start_date":"2020-01-01", "send_time":"10:00:00"})
        api.set_state(self.doc.name, "resume")
        self.assertEqual(frappe.db.get_value("WA Campaign", self.doc.name, "status"), "Active")

    def isolated_tick(self, stale_candidate=False):
        """Exercise real database filters, scoped only to this test campaign."""
        get_all = frappe.get_all
        def scoped(doctype, *args, **kwargs):
            if doctype == "WA Campaign":
                if stale_candidate:
                    return [self.doc.name]
                filters = kwargs.get("filters") or []
                if isinstance(filters, dict):
                    filters = [[key, *(value if isinstance(value, list) else ["=", value])]
                               for key, value in filters.items()]
                kwargs["filters"] = [*filters, ["name", "=", self.doc.name]]
            elif doctype == "WA Campaign Run":
                kwargs["filters"] = {**kwargs.get("filters", {}), "campaign":self.doc.name}
            return get_all(doctype, *args, **kwargs)
        with patch("frappe.get_all", side_effect=scoped), patch("wa_automation.jobs.recover_stale_sends"), patch("wa_automation.delivery.reconcile"):
            jobs.tick()

    def test_once_waiting_for_worker_survives_next_scheduler_tick(self):
        # A later tick must not interpret the cleared next_run_at as overdue.
        due = utcnow() - timedelta(minutes=1)
        frappe.db.set_value("WA Campaign", self.doc.name, {"delivery_mode":"Once", "status":"Active",
            "next_run_at":due.replace(tzinfo=None), "start_date":"2020-01-01", "send_time":"10:00:00"})
        self.isolated_tick()
        self.doc.reload()
        self.assertIsNone(self.doc.next_run_at)
        self.assertEqual(self.doc.status, "Active")
        run_name = frappe.db.get_value("WA Campaign Run", {"campaign":self.doc.name}, "name")
        self.assertTrue(run_name)
        self.isolated_tick()
        self.doc.reload()
        self.assertEqual(self.doc.status, "Active")
        self.assertFalse(self.doc.last_error)
        self.assertEqual(frappe.db.count("WA Campaign Run", {"campaign":self.doc.name}), 1)
        with patch("wa_automation.audience.recheck_recipient", return_value=self.account), patch("wa_chat_hub.outbound.send_interakt_message", return_value={"sent":True,"provider_message_id":"scheduled-fake"}) as transport, patch("wa_automation.jobs.append_history"):
            jobs.process_run(run_name)
            transport.assert_called_once()
        self.assertEqual(frappe.db.get_value("WA Campaign Run", run_name, "status"), "Completed")

    def test_immediate_queued_run_is_not_a_due_schedule(self):
        jobs.create_run(self.doc, utcnow(), "immediate-before-tick")
        self.isolated_tick()
        self.doc.reload()
        self.assertEqual(self.doc.status, "Active")
        self.assertFalse(self.doc.last_error)

    def test_schedule_cleared_after_query_is_skipped(self):
        frappe.db.set_value("WA Campaign", self.doc.name, {"status":"Paused", "next_run_at":None})
        self.isolated_tick(stale_candidate=True)
        self.doc.reload()
        self.assertEqual(self.doc.status, "Paused")
        self.assertFalse(self.doc.last_error)
        self.assertEqual(frappe.db.count("WA Campaign Run", {"campaign":self.doc.name}), 0)


def run():
    # Network guard applies even if unrelated app hooks run during fixture inserts.
    with patch("requests.sessions.Session.request", side_effect=AssertionError("Network calls are forbidden in WA Automation tests")):
        output = io.StringIO()
        result = unittest.TextTestRunner(stream=output, verbosity=2).run(unittest.defaultTestLoader.loadTestsFromTestCase(IntegrationTests))
    print(output.getvalue())
    if not result.wasSuccessful():
        raise RuntimeError("WA Automation integration tests failed")
    return {"tests": result.testsRun, "passed": True, "live_messages_sent": 0}
