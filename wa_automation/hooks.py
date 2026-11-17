app_name = "wa_automation"
app_title = "WA Automation"
app_publisher = "SRIAAS"
app_description = "Bulk WhatsApp campaigns with dynamic audiences and weekly schedules"
app_email = "webdevelopersriaas@gmail.com"
app_license = "mit"
required_apps = ["wa_chat_hub"]
app_include_js = ["/assets/wa_automation/js/campaign_time.js?v=20261104.1"]
before_install = "wa_automation.install.before_install"
after_install = "wa_automation.install.after_install"
after_migrate = "wa_automation.install.after_migrate"
scheduler_events = {"cron": {"* * * * *": ["wa_automation.jobs.tick"]}}
doc_events = {
    "Chat Message": {
        "after_insert": "wa_automation.delivery.on_chat_message",
        "on_update": "wa_automation.delivery.on_chat_message",
    }
}
permission_query_conditions = {
    "WA Campaign": "wa_automation.permissions.campaign_query",
    "WA Campaign Run": "wa_automation.permissions.owned_query",
    "WA Campaign Recipient": "wa_automation.permissions.recipient_query",
}
has_permission = {
    "WA Campaign": "wa_automation.permissions.has_permission",
    "WA Campaign Run": "wa_automation.permissions.has_permission",
    "WA Campaign Recipient": "wa_automation.permissions.has_permission",
}
# WA Chat Hub invokes these callbacks after authenticating Interakt status webhooks.
# Returning False preserves its existing chat-history processing.
whatsapp_authentication_status = ["wa_automation.delivery.on_verified_status"]
