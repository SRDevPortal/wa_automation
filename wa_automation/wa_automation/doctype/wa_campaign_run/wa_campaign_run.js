frappe.ui.form.on("WA Campaign Run", {
  refresh(frm) {
    frm.disable_save();
    frm.add_custom_button(__("Recipients"), () => frappe.set_route("List", "WA Campaign Recipient", {run: frm.doc.name}));
    frm.add_custom_button(__("Campaign"), () => frappe.set_route("Form", "WA Campaign", frm.doc.campaign));
    frm.add_custom_button(__("Refresh Progress"), () => frm.reload_doc());
    const canManage = frappe.user.has_role(["System Manager", "WA Automation Manager", "WA Automation Sender"]);
    const act = (action, message) => frappe.confirm(__(message), async () => {
      await frappe.call({method:"wa_automation.run_actions.control", args:{name:frm.doc.name, action}, freeze:true});
      await frm.reload_doc();
    });
    if (canManage && ["Queued", "Preparing", "Sending", "Failed"].includes(frm.doc.status)) {
      frm.add_custom_button(__("Cancel Run"), () => act("cancel",
        "Cancel this run and pause its campaign? Pending messages will be removed from sending. Already submitted messages cannot be recalled. The run stays in history."));
    }
    if (canManage && ["Queued", "Preparing", "Sending", "Failed", "Cancelled"].includes(frm.doc.status)) {
      frm.add_custom_button(__("Retry Run"), () => act("retry",
        "Retry this run now using its saved template and filters? This can send real messages to pending recipients and recipients whose earlier request was rejected. Accepted messages and unknown outcomes will not be resent."));
    }
    frm.set_intro(__("Completed means dispatch processing has finished. Delivery and read totals continue to update from Interakt. Unknown outcomes are held without automatic resend."));
  }
});
