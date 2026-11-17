import frappe
from frappe.model.document import Document
from wa_automation.audience import validate_config
from wa_automation.core import schedule_next


class WACampaign(Document):
    def validate(self):
        validate_config(self.as_dict())
        previous = self.get_doc_before_save()
        if previous and self.status != previous.status:
            frappe.throw("Use Send Now, Activate, Pause or Resume to change campaign status.")
        if self.is_new():
            self.status = "Draft"
        if previous and previous.status == "Active":
            frappe.throw("Pause the campaign before editing its configuration.")
        if self.delivery_mode != "Now":
            if not self.start_date or not self.send_time:
                frappe.throw("Start date and send time are required for scheduled campaigns.")
            if self.end_date and str(self.end_date) < str(self.start_date):
                frappe.throw("End date cannot be before start date.")
            try:
                schedule_next(self.as_dict())
            except ValueError as exc:
                frappe.throw(str(exc))
        self.next_run_at = previous.next_run_at if previous else None
