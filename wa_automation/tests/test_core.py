import unittest
from datetime import datetime, timezone
from wa_automation.core import schedule_next, compile_filters, normalize_number, render_values, preview_text, merge_status, stable_key


class CoreTests(unittest.TestCase):
    def config(self, **kw):
        return dict(delivery_mode="Weekly", timezone="Asia/Kolkata", start_date="2026-11-01", send_time="10:00:00", monday=1, **kw)

    def test_weekly_timezone(self):
        self.assertEqual(schedule_next(self.config(), datetime(2026, 11, 3, tzinfo=timezone.utc)).isoformat(), "2026-11-09T04:30:00+00:00")

    def test_exact_time_does_not_repeat(self):
        self.assertEqual(schedule_next(self.config(), datetime(2026, 11, 9, 4, 30, tzinfo=timezone.utc)).day, 16)

    def test_multiple_weekdays(self):
        self.assertEqual(schedule_next(self.config(wednesday=1), datetime(2026, 11, 3, tzinfo=timezone.utc)).day, 4)

    def test_expired(self):
        self.assertIsNone(schedule_next(self.config(end_date="2026-11-08"), datetime(2026, 11, 3, tzinfo=timezone.utc)))

    def test_no_weekdays(self):
        cfg = self.config(); cfg["monday"] = 0
        with self.assertRaises(ValueError): schedule_next(cfg)

    def test_once(self):
        cfg = self.config(); cfg.update(delivery_mode="Once", start_date="2026-11-04")
        instant = schedule_next(cfg, datetime(2026, 11, 3, tzinfo=timezone.utc))
        self.assertEqual(instant.day, 4)
        self.assertIsNone(schedule_next(cfg, instant))

    def test_dst_gap_skipped(self):
        cfg = dict(delivery_mode="Weekly", timezone="America/New_York", start_date="2026-03-01", send_time="02:30:00", sunday=1)
        result = schedule_next(cfg, datetime(2026, 3, 8, 0, tzinfo=timezone.utc))
        self.assertEqual(result.day, 15)

    def test_dst_fold_once(self):
        cfg = dict(delivery_mode="Weekly", timezone="America/New_York", start_date="2026-11-01", send_time="01:30:00", sunday=1)
        instant = schedule_next(cfg, datetime(2026, 11, 1, 0, tzinfo=timezone.utc))
        self.assertEqual(instant.hour, 5)
        self.assertEqual(schedule_next(cfg, instant).day, 8)

    def test_relative_date_recomputed(self):
        fields = {"creation": {"fieldtype": "Date"}}
        row = [{"field_name": "creation", "operator": "last days", "value": "7"}]
        first = compile_filters(row, fields, datetime(2026, 11, 2, tzinfo=timezone.utc), "Asia/Kolkata", "Asia/Kolkata")
        second = compile_filters(row, fields, datetime(2026, 11, 9, tzinfo=timezone.utc), "Asia/Kolkata", "Asia/Kolkata")
        self.assertEqual(first[0][2], ["2026-10-26", "2026-11-02"])
        self.assertEqual(second[0][2], ["2026-11-02", "2026-11-09"])

    def test_relative_datetime_storage_timezone(self):
        result = compile_filters([{"field_name":"creation","operator":"last days","value":"1"}], {"creation":{"fieldtype":"Datetime"}}, datetime(2026,11,2,0,tzinfo=timezone.utc), "America/New_York", "Asia/Kolkata")
        self.assertEqual(result[0][2][1], "2026-11-02 05:30:00")

    def test_forbidden_field(self):
        with self.assertRaises(ValueError): compile_filters([{"field_name":"secret","value":"x"}], {}, datetime.now(timezone.utc), "UTC", "UTC")

    def test_injection_operator(self):
        with self.assertRaises(ValueError): compile_filters([{"field_name":"name","operator":"= 1 OR 1=1","value":"x"}], {"name":{"fieldtype":"Data"}}, datetime.now(timezone.utc), "UTC", "UTC")

    def test_relative_on_text_rejected(self):
        with self.assertRaises(ValueError): compile_filters([{"field_name":"name","operator":"last days","value":"7"}], {"name":{"fieldtype":"Data"}}, datetime.now(timezone.utc), "UTC", "UTC")

    def test_international_phone(self):
        self.assertEqual(normalize_number("+44 20 7946 0958"), ("+442079460958", "+44", "2079460958"))

    def test_local_phone(self):
        self.assertEqual(normalize_number("9876543210")[0], "+919876543210")
        self.assertEqual(normalize_number("919876543210")[0], "+919876543210")
        self.assertEqual(normalize_number("00919876543210")[0], "+919876543210")

    def test_bad_phone(self):
        for value in ("", "123", "91", "911", "+91", "0091", "call me 9876543210"):
            with self.assertRaises(ValueError): normalize_number(value)

    def test_mapping_and_zero(self):
        result = render_values([{"component":"Body","position":1,"source_type":"Field","field_name":"value"}], {"value":0}, {"value"})
        self.assertEqual(result["body_values"], ["0"])

    def test_no_arbitrary_expression(self):
        with self.assertRaises(ValueError): render_values([{"component":"Body","position":1,"source_type":"Field","field_name":"__import__('os')"}], {}, {"name"})

    def test_fallback(self):
        self.assertEqual(render_values([{"component":"Body","position":1,"source_type":"Field","field_name":"name","fallback_value":"Friend"}], {"name":""}, {"name"})["body_values"], ["Friend"])

    def test_mapping_holes_rejected(self):
        with self.assertRaises(ValueError): render_values([{"component":"Body","position":2,"source_type":"Fixed","fixed_value":"x"}], {}, {})

    def test_preview(self):
        self.assertEqual(preview_text("Hi {{1}}, {{ 2 }}", ["A", "B"]), "Hi A, B")

    def test_status_monotonic(self):
        self.assertEqual(merge_status("Read", "Sent"), "Read")
        self.assertEqual(merge_status("Delivered", "Failed"), "Delivered")
        self.assertEqual(merge_status("Unknown", "Delivered"), "Delivered")

    def test_sent_can_receive_failure_but_failed_cannot_regress_to_sent(self):
        self.assertEqual(merge_status("Sent", "Failed"), "Failed")
        self.assertEqual(merge_status("Failed", "Sent"), "Failed")
        self.assertEqual(merge_status("Failed", "Delivered"), "Delivered")

    def test_unique_run_key(self):
        self.assertEqual(stable_key("c", "monday"), stable_key("c", "monday"))
        self.assertNotEqual(stable_key("c", "monday"), stable_key("c", "next-monday"))


if __name__ == "__main__": unittest.main()
