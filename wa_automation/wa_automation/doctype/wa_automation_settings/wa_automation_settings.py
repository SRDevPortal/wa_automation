import frappe
from frappe.model.document import Document
from frappe.utils import cint


class WAAutomationSettings(Document):
    def validate(self):
        for name, low, high in (("max_recipients", 1, 100000), ("messages_per_minute", 1, 600), ("max_attempts", 1, 10), ("schedule_grace_minutes", 1, 1440)):
            if not low <= cint(self.get(name)) <= high:
                frappe.throw(f"{name} must be between {low} and {high}.")
