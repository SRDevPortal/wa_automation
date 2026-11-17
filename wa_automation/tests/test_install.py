import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from wa_automation import install


class SettingsDefaultsTests(unittest.TestCase):
    def initialize(self, stored):
        state = dict(stored)
        db = Mock()
        db.get_singles_dict.side_effect = lambda *args, **kwargs: dict(state)
        db.set_single_value.side_effect = lambda doctype, values: state.update(values)
        # Any attempt to load/save a settings document fails this test.
        with patch.object(install, "frappe", SimpleNamespace(db=db)):
            install._ensure_settings_defaults()
            first_writes = db.set_single_value.call_count
            install._ensure_settings_defaults()
        self.assertEqual(db.set_single_value.call_count, first_writes)
        db.get_singles_dict.assert_called_with("WA Automation Settings", for_update=True)
        return state, db

    def test_first_install_initializes_defaults(self):
        state, db = self.initialize({})
        self.assertEqual(state, dict(sending_enabled=1, max_recipients=10000,
                                     messages_per_minute=60, max_attempts=3,
                                     schedule_grace_minutes=15))
        self.assertEqual(db.set_single_value.call_count, 1)

    def test_existing_settings_are_not_rewritten(self):
        stored = dict(sending_enabled="0", max_recipients="250",
                      messages_per_minute="8", max_attempts="1",
                      schedule_grace_minutes="0", scheduler_last_seen="2026-11-17 12:58:37",
                      modified="2026-11-17 12:58:37")
        state, db = self.initialize(stored)
        self.assertEqual(state, stored)
        db.set_single_value.assert_not_called()

    def test_partial_settings_preserve_disabled_sending_and_heartbeat(self):
        stored = dict(sending_enabled="0", messages_per_minute="8",
                      scheduler_last_seen="2026-11-17 12:58:37")
        state, db = self.initialize(stored)
        self.assertEqual({key: state[key] for key in stored}, stored)
        db.set_single_value.assert_called_once_with("WA Automation Settings",
            dict(max_recipients=10000, max_attempts=3, schedule_grace_minutes=15))


if __name__ == "__main__":
    unittest.main()
