/* Native Desk campaign builder. All eligibility and permission checks run on the server. */
(() => {
  const api = "wa_automation.api.";
  const esc = frappe.utils.escape_html;
  async function saved(frm) {
    if (frm.is_new() || frm.is_dirty()) await frm.save();
  }
  async function call(method, args) {
    return (await frappe.call({method: api + method, args, freeze: true})).message;
  }
  function option_fields(frm, fields) {
    frm.wa_fields = fields;
    const names = fields.map(f => f.fieldname).sort();
    for (const name of ["phone_field", "sort_field"]) {
      frm.set_df_property(name, "fieldtype", "Select");
      frm.set_df_property(name, "options", ["", ...names].join("\n"));
    }
    for (const table of ["filters", "variables"]) {
      frm.fields_dict[table].grid.update_docfield_property("field_name", "fieldtype", "Select");
      frm.fields_dict[table].grid.update_docfield_property("field_name", "options", ["", ...names].join("\n"));
    }
    frm.set_df_property("phone_field", "description", fields.filter(f => /phone|mobile|whatsapp/i.test(f.fieldname)).map(f => `${esc(f.label)}: ${esc(f.fieldname)}`).join(" · "));
    frm.refresh_fields();
  }
  async function load_fields(frm) {
    if (!frm.doc.reference_doctype) return;
    const type = frm.doc.reference_doctype;
    const fields = (await frappe.call({method: api + "fields", args: {reference_doctype: type}})).message;
    if (frm.doc.reference_doctype !== type) return;
    option_fields(frm, fields);
    if (!frm.doc.phone_field) {
      const defaults = type === "Patient" ? ["mobile", "phone"] : ["whatsapp_no", "mobile_no", "phone"];
      await frm.set_value("phone_field", defaults.find(n => fields.some(f => f.fieldname === n)) || "");
    }
  }
  function template_picker(frm, templates) {
    const wrap = frm.fields_dict.template_picker.$wrapper.empty();
    const line = $('<div class="form-group"><label class="control-label">Account templates</label><select class="form-control"><option value="">Select a template…</option></select><p class="text-muted small" style="margin-top:8px">Changing the template replaces its variable mappings. Body and header positions start at 1; button positions refer to the button order.</p></div>').appendTo(wrap);
    const select = line.find("select");
    templates.forEach((t, i) => {
      $('<option>').val(String(i)).text(`${t.name} (${t.language_code || "en"})`).prop("selected", t.name === frm.doc.template_name && (t.language_code || "en") === frm.doc.language_code).appendTo(select);
    });
    select.prop("disabled", frm.doc.status === "Active");
    select.on("change", async () => {
      if (select.val() === "") return;
      const t = templates[Number(select.val())];
      await frm.set_value({template_name: t.name, language_code: t.language_code || "en", template_preview: t.body_preview || ""});
      frm.clear_table("variables");
      const nameField = frm.doc.reference_doctype === "Patient" ? "patient_name" : "lead_name";
      for (const [component, count] of [["Body", t.body_variable_count || (t.body_variables || []).length || 0], ["Header", t.header_variable_count || 0]]) {
        for (let pos = 1; pos <= count; pos++) frm.add_child("variables", {component, position: pos, source_type: "Field", field_name: pos === 1 && frm.wa_fields?.some(f => f.fieldname === nameField) ? nameField : ""});
      }
      frm.refresh_field("variables");
      frm.dirty();
    });
  }
  async function load_templates(frm, refresh = false) {
    if (!frm.doc.channel_account) return;
    const account = frm.doc.channel_account;
    const request = (frm.wa_template_request || 0) + 1;
    frm.wa_template_request = request;
    const wrap = frm.fields_dict.template_picker.$wrapper;
    wrap.find(".wa-template-status").remove();
    $('<p class="text-muted small wa-template-status">').text(refresh
      ? __("Refreshing account templates… You can continue editing.")
      : __("Loading saved account templates…")).appendTo(wrap);
    const current = () => frm.doc.channel_account === account && frm.wa_template_request === request;
    try {
      const data = (await frappe.call({method: api + "templates",
        args: {channel_account: account, refresh: refresh ? 1 : 0, cached_only: refresh ? 0 : 1},
        freeze: false})).message || [];
      if (!current()) return;
      template_picker(frm, data);
      if (!refresh || !data.length) {
        $('<p class="text-muted small wa-template-status">').text(data.length
          ? __("Saved account templates. Use Refresh Templates to check for updates.")
          : __("Use Refresh Templates to load this account’s templates. Your saved template and variables are unchanged.")).appendTo(wrap);
      }
    } catch (error) {
      if (!current()) return;
      wrap.find(".wa-template-status").remove();
      $('<p class="text-muted small wa-template-status">').text(
        __("Could not load account templates. You can continue editing and retry Refresh Templates.")
      ).appendTo(wrap);
    }
  }
  async function preview(frm, start = 0) {
    await saved(frm);
    const data = await call("preview", {name: frm.doc.name, start});
    const wrap = frm.fields_dict.audience_preview.$wrapper.empty();
    $(`<div class="alert alert-info"><strong>${data.matched.toLocaleString()}</strong> matching records · <strong>${data.eligible.toLocaleString()}</strong> eligible numbers · ${data.skipped.toLocaleString()} excluded<br><small>Each weekly run refreshes this audience. This preview does not send messages.</small></div>`).appendTo(wrap);
    const table = $('<table class="table table-bordered"><thead><tr><th>Record</th><th>Number</th><th>Eligibility</th><th>Message preview</th><th></th></tr></thead><tbody></tbody></table>').appendTo(wrap);
    data.rows.forEach(row => {
      const tr = $('<tr>').appendTo(table.find("tbody"));
      $('<td>').text(row.reference_name).appendTo(tr);
      $('<td>').text(row.masked_phone || "—").appendTo(tr);
      $('<td>').text(row.status === "Pending" ? "Eligible" : row.last_error).appendTo(tr);
      $('<td style="white-space:pre-wrap;max-width:360px">').text(row.preview || "—").appendTo(tr);
      const td = $('<td>').appendTo(tr);
      if (frm.doc.status !== "Active" && row.status === "Pending") $('<button class="btn btn-xs btn-default">Exclude</button>').appendTo(td).on("click", async () => {
        await call("exclude", {name: frm.doc.name, reference_name: row.reference_name});
        await frm.reload_doc();
        await preview(frm, start);
      });
    });
    const pages = $('<div class="flex" style="gap:8px;margin-bottom:16px">').appendTo(wrap);
    $('<button class="btn btn-default btn-sm">Previous</button>').prop("disabled", start === 0).appendTo(pages).on("click", () => preview(frm, Math.max(0, start - data.page_length)));
    $('<span class="text-muted">').text(`${data.matched ? start + 1 : 0}–${Math.min(start + data.page_length, data.matched)} of ${data.matched}`).appendTo(pages);
    $('<button class="btn btn-default btn-sm">Next</button>').prop("disabled", start + data.page_length >= data.matched).appendTo(pages).on("click", () => preview(frm, start + data.page_length));
  }
  async function state(frm, action) {
    await saved(frm);
    await call("set_state", {name: frm.doc.name, action});
    await frm.reload_doc();
  }
  frappe.ui.form.on("WA Campaign", {
    setup(frm) {
      frm.set_query("channel_account", () => ({filters: {channel_type: "Interakt", is_active: 1}}));
    },
    async refresh(frm) {
      frm.add_custom_button(__("Refresh Templates"), () => load_templates(frm, true));
      frm.add_custom_button(__("Preview Audience"), () => preview(frm));
      if (!frm.is_new()) {
        frm.add_custom_button(__("Run History"), () => frappe.set_route("List", "WA Campaign Run", {campaign: frm.doc.name}));
        const maySend = frappe.user.has_role(["System Manager", "WA Automation Manager", "WA Automation Sender"]);
        if (maySend) {
          if (frm.doc.status === "Active") frm.add_custom_button(__("Pause"), () => state(frm, "pause"));
          else if (frm.doc.delivery_mode !== "Now" || frm.doc.status === "Paused") frm.add_custom_button(__(frm.doc.status === "Paused" ? "Resume" : "Activate Schedule"), () => state(frm, frm.doc.status === "Paused" ? "resume" : "activate"));
          if (["Active", "Paused"].includes(frm.doc.status)) frm.add_custom_button(__("End Campaign"), () => {
            frappe.confirm(__("End this campaign and skip all messages that have not started sending? Already submitted messages cannot be recalled."), () => state(frm,"end"));
          });
          frm.add_custom_button(__("Send Test"), async () => {
            await saved(frm);
            const dialog = new frappe.ui.Dialog({title:__("Send one test message"), fields:[
              {fieldname:"reference_name",label:__("Source record for personalization"),fieldtype:"Link",options:frm.doc.reference_doctype,reqd:1},
              {fieldname:"test_number",label:__("Test WhatsApp number (include country code)"),fieldtype:"Data",reqd:1},
              {fieldname:"note",fieldtype:"HTML",options:"<p class='text-muted'>This sends one real message to the test number. Choose an eligible record from Preview Audience. It does not activate the campaign or count toward its recipient history.</p>"}
            ], primary_action_label:__("Send Test"), primary_action:async values => {
              dialog.get_primary_btn().prop("disabled",true);
              try {
                const key = window.crypto?.randomUUID ? window.crypto.randomUUID() : frappe.utils.get_random(24);
                const run = await call("send_test",{name:frm.doc.name,...values,request_key:key});
                dialog.hide(); frappe.set_route("Form","WA Campaign Run",run);
              } finally {dialog.get_primary_btn().prop("disabled",false);}
            }});
            dialog.show();
          });
          if (frm.doc.delivery_mode === "Now") frm.add_custom_button(__("Send Now"), async () => {
            await saved(frm);
            const summary = await call("preview", {name: frm.doc.name, page_length: 1});
            frappe.confirm(__("Queue this template for all {0} currently eligible numbers?", [summary.eligible]), async () => {
              const key = window.crypto?.randomUUID ? window.crypto.randomUUID() : frappe.utils.get_random(24);
              const run = await call("send_now", {name: frm.doc.name, request_key: key});
              frappe.set_route("Form", "WA Campaign Run", run);
            });
          });
        }
        if (frm.doc.delivery_mode !== "Now") {
          const next = (await frappe.call({method: api + "schedule_preview", args: {name: frm.doc.name}})).message;
          frm.fields_dict.schedule_preview.$wrapper.html(`<div class="text-muted" style="padding:12px 0"><strong>Next scheduled times (IST)</strong><br>${(next || []).map(esc).join("<br>") || "No future occurrences"}<br>Indian Standard Time (Asia/Kolkata)</div>`);
        }
      }
      await load_fields(frm);
      if (frm.doc.channel_account) await load_templates(frm);
    },
    async reference_doctype(frm) {
      frm.clear_table("filters"); frm.clear_table("variables");
      await frm.set_value({phone_field: "", excluded_records: "", sort_field: "creation", consent_confirmed: 0});
      await load_fields(frm);
    },
    async channel_account(frm) {
      frm.wa_template_request = (frm.wa_template_request || 0) + 1;
      frm.fields_dict.template_picker.$wrapper.empty();
      frm.clear_table("variables");
      await frm.set_value({template_name: "", template_preview: "", header_media_url: ""});
      await load_templates(frm);
    },
  });
  frappe.ui.form.on("WA Template Variable", {
    source_type(frm, cdt, cdn) {
      const row = frm.fields_dict.variables.grid.grid_rows_by_docname[cdn];
      if (!row) return;
      // Both inputs depend on Source Type. Refresh their row metadata and cells
      // together so switching to Fixed immediately exposes its editable value.
      row.refresh();
      for (const fieldname of ["field_name", "fixed_value"]) {
        const column = row.columns[fieldname];
        if (column) column.toggle(!column.df.hidden);
      }
    },
  });
})();
