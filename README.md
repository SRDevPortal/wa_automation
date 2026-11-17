# WA Automation

Native Frappe 15 app for bulk and weekly Interakt template campaigns against Patient, Lead and CRM Lead. Requires `wa_chat_hub` on the same site.

## Use

Open **WA Automation → Campaigns → New**. Select an active Interakt account, choose a template, map its variables, choose the audience and phone field, and add field filters. Custom fields appear automatically. Use **Preview Audience** to review eligible recipients and exclusions; pagination never limits the eventual audience. Confirm audience consent or select an explicit consent Check field.

Choose **Now**, **Once**, or **Weekly**. Weekly campaigns support multiple weekdays, time, timezone and optional end date. Save, then **Send Now** or **Activate Schedule**. Every scheduled cycle queries current records again, so new matching patients and leads join the next cycle. Date filters are anchored to the scheduled occurrence. **Only new recipients** suppresses numbers already accepted or with an unresolved send outcome in this campaign. Minimum gap also applies within the campaign.

Pause before editing active campaigns. An unfinished run uses its frozen configuration and audience when resumed; edits affect future runs. **End Campaign** skips pending recipients and stops future occurrences. Opt-outs, source access, account availability, and phone changes are checked immediately before sending. Shared numbers with conflicting personalized payloads are excluded. Default timezone is Asia/Kolkata. Internal scheduling fields explicitly marked UTC store UTC instants.

**Send Test** uses one eligible source record to personalize a message to an explicitly entered test number. It sends a real WhatsApp message, uses the same rate limit and delivery tracking, and does not activate the campaign or suppress the source recipient in later runs.

**Run History** shows the audience snapshot and dispatch totals. **Recipients** shows exclusions, failures and status. Accepted means Interakt acknowledged the request; Sent, Delivered and Read come from WA Chat Hub's existing webhook records. Unknown means acceptance could not be determined and must not be blindly retried. Pausing stops pending sends; already submitted requests cannot be recalled.

Open a run to use **Cancel Run** or **Retry Run**. Cancel removes waiting queue work, skips recipients that have not started sending, and pauses the campaign. It preserves the run and delivery history as Cancelled. Retry uses the same run and saved configuration; it resumes pending recipients, recipients skipped specifically by cancellation, and failed requests with no evidence of provider acceptance. Accepted, Sent, Delivered, Read and Unknown outcomes are never reset. Run controls require sender access and campaign write permission. If a worker is processing a batch, pause the campaign and wait for that batch to finish before using the controls. A missed occurrence has no audience snapshot and must be scheduled again from the campaign.

## Install

```sh
bench get-app /path/to/wa_automation
bench --site your-site install-app wa_automation
bench --site your-site enable-scheduler
bench restart
```

Keep a `long` queue worker and the Frappe scheduler running. The cron job checks due campaigns once per minute; this is not a second-precision dispatcher. The default account budget is 60 messages/minute across this app's campaigns. Allow capacity for other apps sending through the same account. Missed occurrences outside the configured grace period are recorded and skipped; overlapping occurrences are skipped. No campaign is created or activated during installation.

For isolation from other apps' backlog, configure `wa_automation_queue` as `wa_campaign` in common site config, add `workers.wa_campaign` with `timeout: 900` and `background_workers: 1`, and run `bench worker --queue wa_campaign`. Keep that worker in the bench Procfile for development or the production process manager. Without the setting the app uses `long`. Changing the queue does not move existing queue entries; cancel or retry an unfinished run through its controls. The local development bench includes this dedicated worker.

Grant **WA Automation User** to campaign authors and **WA Automation Sender** to people allowed to send/activate. **WA Automation Manager** and **System Manager** can manage all campaigns. Users otherwise see their own campaigns, runs and recipients. They also need read permissions for the selected account and source records. Scheduled work rechecks the campaign owner's permissions and enabled status. Raw numbers/payloads in recipient records are restricted to managers.

Credentials remain in Chat Channel Account Password fields. No additional API keys are copied into this app. Existing WA Chat Hub webhook configuration is reused. Its signature verification should remain enabled. Chat history insertion is attempted after acceptance; a history write failure cannot cause a resend.

## Development and verification

Schemas are checked in. `scripts/build_schema.py` regenerates them when changing the schema. `python -m unittest discover -s wa_automation/tests -p 'test_core.py'` tests pure scheduling/filter/phone/rendering behavior. `bench --site your-site execute wa_automation.tests.integration.run` tests database and worker behavior using temporary fixtures and mocked provider responses, with an HTTP network guard. The fixtures are removed after each test. Browser fixtures similarly revoke their temporary session during cleanup. Never run tests against real messaging credentials.

Version 0.1 uses native campaign filters rather than related-document joins. Reuse an audience by duplicating a campaign. Template headers and dynamic URL buttons are supported through variable mappings; unsupported template formats must fail validation before dispatch.
