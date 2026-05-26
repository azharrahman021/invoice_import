frappe.ui.form.on("Invoice Import", {
  refresh(frm) {
    sync_recent_import_defaults(frm);
    sync_template_location_defaults(frm);
    if (frm.doc.attachment && !frm.is_new()) {
      frm.add_custom_button(__("Reprocess"), () => {
        frappe.call({
          method: "invoice_import.api.invoice_import.enqueue_reprocess",
          args: { invoice_import: frm.doc.name },
          freeze: true,
          callback() {
            frm.reload_doc();
          },
        });
      });

      frm.add_custom_button(__("Review Extraction"), () => show_review_dialog(frm));
    }

    if (frm.doc.linked_purchase_invoice) {
      frm.add_custom_button(__("Open Purchase Invoice"), () => {
        frappe.set_route("Form", "Purchase Invoice", frm.doc.linked_purchase_invoice);
      });
    }

    mark_low_confidence(frm);
  },

  invoice_template(frm) {
    sync_template_location_defaults(frm, true);
  },
});

function sync_recent_import_defaults(frm, force = false) {
  if (!frm.is_new() && frm.doc.company && frm.doc.warehouse) {
    return;
  }
  frappe.call({
    method: "invoice_import.api.invoice_import.get_recent_import_defaults",
    callback(response) {
      const defaults = response.message || {};
      if (defaults.company && (force || !frm.doc.company)) {
        frm.set_value("company", defaults.company);
      }
      if (defaults.warehouse && (force || !frm.doc.warehouse)) {
        frm.set_value("warehouse", defaults.warehouse);
      }
    },
  });
}

function sync_template_location_defaults(frm, force = false) {
  if (!frm.doc.invoice_template) {
    return;
  }
  frappe.call({
    method: "invoice_import.api.invoice_import.get_template_defaults",
    args: {
      template_name: frm.doc.invoice_template,
    },
    callback(response) {
      const defaults = response.message || {};
      if (defaults.company && (force || !frm.doc.company)) {
        frm.set_value("company", defaults.company);
      }
      if (defaults.warehouse && (force || !frm.doc.warehouse)) {
        frm.set_value("warehouse", defaults.warehouse);
      }
    },
  });
}

function show_review_dialog(frm) {
  frappe.call({
    method: "invoice_import.api.invoice_import.get_review_payload",
    args: { invoice_import: frm.doc.name },
    freeze: true,
    callback(response) {
      const payload = response.message || {};
      const dialog = new frappe.ui.Dialog({
        title: __("Invoice Review"),
        size: "extra-large",
        fields: [{ fieldtype: "HTML", fieldname: "review_html" }],
        primary_action_label: payload.linked_purchase_invoice ? __("Save Changes") : __("Create Draft Purchase Invoice"),
        secondary_action_label: payload.linked_purchase_invoice ? __("Create New Draft Purchase Invoice") : "",
        secondary_action: payload.linked_purchase_invoice
          ? () => submit_review(dialog, frm, "create_new")
          : null,
        primary_action() {
          submit_review(dialog, frm, payload.linked_purchase_invoice ? "update_existing" : "create_new");
        },
      });

      dialog.review_payload = payload;
      dialog.fields_dict.review_html.$wrapper.html(render_review_layout(payload));
      bind_review_events(dialog);
      dialog.show();
    },
  });
}

function submit_review(dialog, frm, action) {
  const extracted = collect_review_data(dialog);
  frappe.call({
    method: "invoice_import.api.invoice_import.create_purchase_invoice_from_review",
    args: {
      invoice_import: frm.doc.name,
      extracted_json: JSON.stringify(extracted),
      action,
    },
    freeze: true,
    callback(result) {
      const response = result.message || {};
      dialog.hide();
      if (response.purchase_invoice) {
        frm.doc.linked_purchase_invoice = response.purchase_invoice;
      }
      if (response.status) {
        frm.doc.status = response.status === "updated" ? "Draft Created" : frm.doc.status;
      }
      frm.refresh_fields();
      if (response.status === "review_required") {
        frappe.msgprint({
          title: __("Review Required"),
          message: (response.warnings && response.warnings.length)
            ? response.warnings.join("<br>")
            : __("The review could not be saved into a Purchase Invoice."),
          indicator: "orange",
        });
        return;
      }
      if (response.warnings && response.warnings.length) {
        frappe.msgprint({
          title: __("Review Saved"),
          message: response.warnings.join("<br>"),
          indicator: "orange",
        });
      }
    },
    error(error) {
      frappe.msgprint({
        title: __("Review Save Failed"),
        message: error && error.message ? error.message : __("Unable to save review changes."),
        indicator: "red",
      });
    },
  });
}

function render_review_layout(payload) {
  const data = payload.extracted_json || {};
  const templateName = payload.invoice_template_name || payload.invoice_template || "";
  const preview = (payload.attachment || "").toLowerCase().endsWith(".pdf")
    ? `<iframe src="${frappe.utils.escape_html(payload.attachment)}" style="width:100%;height:26vh;border:1px solid var(--border-color);border-radius:8px;"></iframe>`
    : `<img src="${frappe.utils.escape_html(payload.attachment)}" style="width:100%;max-height:26vh;object-fit:contain;border:1px solid var(--border-color);border-radius:8px;" />`;
  const items = Array.isArray(data.items) && data.items.length ? data.items : [{}];
  return `
    <div style="display:flex;flex-direction:column;gap:14px;">
      <div>${preview}</div>
      <div style="display:flex;flex-direction:column;gap:12px;max-height:42vh;overflow-y:auto;padding-right:4px;">
        <div style="border:1px solid var(--border-color);border-radius:8px;padding:12px;background:var(--card-bg);">
          <div style="display:flex;justify-content:space-between;gap:12px;align-items:flex-start;margin-bottom:8px;">
            <div>
              <div style="font-weight:600;">${frappe.utils.escape_html(templateName)}</div>
              ${
                payload.reference_purchase_invoice
                  ? `<div style="font-size:12px;color:var(--text-muted);margin-top:4px;"><strong>${__("Reference")}:</strong> ${frappe.utils.escape_html(payload.reference_purchase_invoice)}${payload.reference_purchase_invoice_profile && payload.reference_purchase_invoice_profile.supplier_name ? ` | ${frappe.utils.escape_html(payload.reference_purchase_invoice_profile.supplier_name)}` : ""}</div>`
                  : ""
              }
            </div>
            <div style="text-align:right;font-size:12px;">
              <div><strong>${__("Status")}:</strong> ${frappe.utils.escape_html(payload.status || "")}</div>
              <div><strong>${__("Confidence")}:</strong> ${frappe.utils.escape_html(String(payload.confidence_score || 0))}%</div>
            </div>
          </div>
        </div>
        ${render_field_group("Header", [
          ["company", "Company", payload.company || ""],
          ["warehouse", "Warehouse", payload.warehouse || ""],
          ["supplier", "Supplier", data.supplier || ""],
          ["supplier_gstin", "Supplier GSTIN", data.supplier_gstin || ""],
          ["supplier_vat", "Supplier VAT", data.supplier_vat || ""],
          ["invoice_number", "Invoice Number", data.invoice_number || ""],
          ["invoice_date", "Invoice Date", data.invoice_date || ""],
          ["due_date", "Due Date", data.due_date || ""],
          ["po_number", "PO Number", data.po_number || ""],
          ["payment_terms", "Payment Terms", data.payment_terms || ""],
          ["currency", "Currency", data.currency || "INR"],
          ["subtotal", "Subtotal", data.subtotal || ""],
          ["grand_total", "Grand Total", data.grand_total || ""],
        ])}
        <div style="border:1px solid var(--border-color);border-radius:8px;padding:12px;background:var(--card-bg);">
          <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:8px;">
            <strong>${__("Items")}</strong>
            <button type="button" class="btn btn-default btn-xs" data-action="add-item">${__("Add Row")}</button>
          </div>
          <div style="overflow:auto;">
            <table class="table table-bordered" style="margin-bottom:0;">
              <thead>
                <tr>
                  <th style="width:40px;text-align:center;padding:4px 6px;">#</th>
                  <th style="width:78px;text-align:center;padding:4px 6px;">${__("Match")}</th>
                  <th style="width:220px;max-width:220px;padding:4px 6px;">${__("Item")}</th>
                  <th style="width:340px;max-width:340px;padding:4px 6px;">${__("Description")}</th>
                  <th style="width:70px;padding:4px 6px;">${__("Qty")}</th>
                  <th style="width:70px;padding:4px 6px;">${__("UOM")}</th>
                  <th style="width:90px;padding:4px 6px;">${__("HSN")}</th>
                  <th style="width:100px;padding:4px 6px;">${__("Rate")}</th>
                  <th style="width:100px;padding:4px 6px;">${__("Amount")}</th>
                  <th style="width:36px;padding:4px 6px;"></th>
                </tr>
              </thead>
              <tbody data-items-body>
                ${items.map((item, index) => render_item_row(item, index)).join("")}
              </tbody>
            </table>
          </div>
        </div>
        ${render_field_group("Notes", [
          ["processing_logs", "Processing Notes", payload.processing_logs || ""],
        ], true)}
      </div>
    </div>
  `;
}

function render_field_group(title, fields, multiline = false) {
  return `
    <div style="border:1px solid var(--border-color);border-radius:8px;padding:12px;background:var(--card-bg);">
      <strong style="display:block;margin-bottom:10px;">${frappe.utils.escape_html(title)}</strong>
      <div style="display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:10px;">
        ${fields
          .map(
            ([name, label, value]) => `
              <label style="display:flex;flex-direction:column;gap:4px;">
                <span style="font-size:12px;color:var(--text-muted)">${frappe.utils.escape_html(label)}</span>
                ${
                  multiline
                    ? `<textarea data-field="${frappe.utils.escape_html(name)}" rows="3" class="form-control">${frappe.utils.escape_html(String(value || ""))}</textarea>`
                    : `<input data-field="${frappe.utils.escape_html(name)}" class="form-control" value="${frappe.utils.escape_html(String(value || ""))}">`
                }
              </label>
            `,
          )
          .join("")}
      </div>
    </div>
  `;
}

function render_item_row(item, index) {
  const display_description =
    item.source_description ||
    item.original_description ||
    item._source_description ||
    item.description ||
    item.item_name ||
    "";
  const source_description = item.source_description || item.original_description || item._source_description || item.description || "";
  const item_code = item.matched_item_code || item.item_code || "";
  const item_label = item.matched_item_name || item.item_label || item.item_name || "";
  const hsn_code = item.hsn_sac || item.gst_hsn_code || "";
  return `
    <tr data-item-row>
      <td style="text-align:center;vertical-align:top;padding:0 3px;line-height:1;"><strong data-item-serial>${index + 1}</strong></td>
      <td style="width:64px;text-align:center;vertical-align:top;padding:0 3px;line-height:1;">${render_match_badge(item._match_status)}</td>
      <td style="width:220px;max-width:220px;vertical-align:top;padding:0 3px;">
        <div data-item-selector class="item-selector" style="width:100%;"></div>
        <div data-item-match-hint style="margin-top:3px;">${render_matched_item_hint(item._match_status)}</div>
        <input type="hidden" data-item-field="item_code" value="${frappe.utils.escape_html(String(item_code))}">
        <input type="hidden" data-item-field="item_label" value="${frappe.utils.escape_html(String(item_label))}">
        <input type="hidden" data-item-field="source_description" value="${frappe.utils.escape_html(String(source_description))}">
        <input type="hidden" data-item-field="source_qty" value="${frappe.utils.escape_html(String(item.source_qty || item.qty || 1))}">
        <input type="hidden" data-item-field="source_uom" value="${frappe.utils.escape_html(String(item.source_uom || item.uom || "Nos"))}">
        <input type="hidden" data-item-field="purchase_uom" value="${frappe.utils.escape_html(String(item.purchase_uom || ""))}">
        <input type="hidden" data-item-field="stock_uom" value="${frappe.utils.escape_html(String(item.stock_uom || ""))}">
        <input type="hidden" data-item-field="uom_options" value="${frappe.utils.escape_html(JSON.stringify(item.uom_options || []))}">
      </td>
      <td style="width:340px;max-width:340px;padding:0 3px;vertical-align:top;"><input data-item-field="description" class="form-control input-sm" style="width:100%;height:18px;line-height:1;padding:0 4px;font-size:9px;margin:0;" value="${frappe.utils.escape_html(String(display_description))}"></td>
      <td style="width:70px;padding:0 3px;vertical-align:top;"><input data-item-field="qty" class="form-control input-sm" style="width:100%;height:18px;line-height:1;padding:0 4px;font-size:9px;margin:0;" value="${frappe.utils.escape_html(String(item.qty || 1))}" oninput="window.invoice_import_update_review_row_qty && window.invoice_import_update_review_row_qty(this)" onkeyup="window.invoice_import_update_review_row_qty && window.invoice_import_update_review_row_qty(this)" onchange="window.invoice_import_update_review_row_qty && window.invoice_import_update_review_row_qty(this)"></td>
      <td style="width:70px;padding:0 3px;vertical-align:top;">
        <div data-uom-selector class="uom-selector" style="width:100%;"></div>
        <input type="hidden" data-item-field="uom" value="${frappe.utils.escape_html(String(item.uom || item.purchase_uom || item.stock_uom || "Nos"))}">
      </td>
      <td style="width:90px;padding:0 3px;vertical-align:top;"><input data-item-field="hsn_sac" class="form-control input-sm" style="width:100%;height:18px;line-height:1;padding:0 4px;font-size:9px;margin:0;" value="${frappe.utils.escape_html(String(hsn_code))}"></td>
      <td style="width:100px;padding:0 3px;vertical-align:top;"><input data-item-field="rate" class="form-control input-sm" style="width:100%;height:18px;line-height:1;padding:0 4px;font-size:9px;margin:0;" value="${frappe.utils.escape_html(String(item.rate || ""))}"></td>
      <td style="width:100px;padding:0 3px;vertical-align:top;"><input data-item-field="amount" class="form-control input-sm" style="width:100%;height:18px;line-height:1;padding:0 4px;font-size:9px;margin:0;" value="${frappe.utils.escape_html(String(item.amount || ""))}" oninput="window.invoice_import_update_review_row_amount && window.invoice_import_update_review_row_amount(this)" onkeyup="window.invoice_import_update_review_row_amount && window.invoice_import_update_review_row_amount(this)" onchange="window.invoice_import_update_review_row_amount && window.invoice_import_update_review_row_amount(this)"></td>
      <input type="hidden" data-item-field="source_amount" value="${frappe.utils.escape_html(String(item.amount || ""))}">
      <td style="padding:0 2px;vertical-align:top;"><button type="button" class="btn btn-xs btn-danger" data-action="remove-item" style="padding:0 3px;line-height:1;font-size:9px;">x</button></td>
    </tr>
  `;
}

function render_matched_item_hint(status) {
  const item_code = status && status.item_code ? status.item_code : "";
  const item_name = status && status.item_name ? status.item_name : "";
  if (!item_code && !item_name) {
    return `<div style="display:block;font-size:10px;color:#842029;line-height:1.2;font-weight:600;">${__("No ERPNext item")}</div>`;
  }
  const text = item_name && item_name !== item_code ? `${item_code} - ${item_name}` : item_code || item_name;
  return `<div title="${frappe.utils.escape_html(text)}" style="display:block;font-size:10px;color:var(--text-muted);line-height:1.2;font-weight:600;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;">${frappe.utils.escape_html(text)}</div>`;
}

function render_match_badge(status) {
  const state = status || {};
  const quality = state.quality || "unmatched";
  const palette = {
    strong: { bg: "#d8f3dc", fg: "#1b5e20", label: __("Exact") },
    good: { bg: "#e7f5d9", fg: "#33691e", label: __("Good") },
    weak: { bg: "#fff3cd", fg: "#7a4d00", label: __("Check") },
    unmatched: { bg: "#f8d7da", fg: "#842029", label: __("No Match") },
    error: { bg: "#e9ecef", fg: "#495057", label: __("Error") },
  };
  const color = palette[quality] || palette.unmatched;
  const score = Number(state.score || 0);
  const title = [state.item_code, state.method, state.comment].filter(Boolean).join(" | ");
  return `
    <span title="${frappe.utils.escape_html(title)}" style="display:inline-flex;align-items:center;justify-content:center;min-width:52px;border-radius:999px;padding:0 5px;background:${color.bg};color:${color.fg};font-size:8px;font-weight:600;line-height:12px;">
      ${frappe.utils.escape_html(color.label)}${score ? ` ${Math.round(score)}%` : ""}
    </span>
  `;
}

function bind_review_events(dialog) {
  const $wrapper = dialog.fields_dict.review_html.$wrapper;
  $wrapper.on("click", "[data-action='add-item']", () => {
    const $tbody = $wrapper.find("[data-items-body]");
    $tbody.append(render_item_row({}, $tbody.find("tr").length));
    update_item_serials($wrapper);
    init_item_selectors($wrapper);
    init_uom_selectors($wrapper);
  });
  $wrapper.on("click", "[data-action='remove-item']", (event) => {
    const $row = $(event.currentTarget).closest("tr");
    if ($wrapper.find("[data-items-body] tr").length > 1) {
      $row.remove();
      update_item_serials($wrapper);
    }
  });
  $wrapper.on("input keyup change blur", "[data-item-field='qty']", (event) => {
    const $row = $(event.currentTarget).closest("tr");
    update_rate_from_qty_change($row);
    learn_uom_conversion_from_row($row);
  });
  $wrapper.on("input change", "[data-item-field='amount']", (event) => {
    const $row = $(event.currentTarget).closest("tr");
    $row.find("[data-item-field='source_amount']").val(($row.find("[data-item-field='amount']").val() || "").trim());
    update_rate_from_qty_change($row);
  });
  update_item_serials($wrapper);
  init_item_selectors($wrapper);
  init_uom_selectors($wrapper);
  attach_review_row_recalcs($wrapper);
}

function collect_review_data(dialog) {
  const $wrapper = dialog.fields_dict.review_html.$wrapper;
  const getField = (name) => ($wrapper.find(`[data-field='${name}']`).val() || "").trim();
  const items = [];
  $wrapper.find("[data-items-body] tr").each((_, row) => {
    const $row = $(row);
    const item = {
      item_code: ($row.find("[data-item-field='item_code']").val() || "").trim(),
      description: ($row.find("[data-item-field='description']").val() || "").trim(),
      source_qty: to_number($row.find("[data-item-field='source_qty']").val(), 1),
      source_uom: ($row.find("[data-item-field='source_uom']").val() || "Nos").trim() || "Nos",
      qty: to_number($row.find("[data-item-field='qty']").val(), 1),
      uom: ($row.find("[data-item-field='uom']").val() || "Nos").trim() || "Nos",
      item_name: ($row.find("[data-item-field='item_label']").val() || "").trim(),
      hsn_sac: ($row.find("[data-item-field='hsn_sac']").val() || "").trim(),
      rate: to_number($row.find("[data-item-field='rate']").val(), 0),
      amount: to_number($row.find("[data-item-field='amount']").val(), 0),
      tax_percent: to_number($row.find("[data-item-field='tax_percent']").val(), 0),
    };
    if (item.description || item.amount || item.rate || item.qty) {
      items.push(item);
    }
  });

  return {
    company: getField("company"),
    warehouse: getField("warehouse"),
    supplier: getField("supplier"),
    supplier_gstin: getField("supplier_gstin"),
    supplier_vat: getField("supplier_vat"),
    invoice_number: getField("invoice_number"),
    invoice_date: getField("invoice_date"),
    due_date: getField("due_date"),
    po_number: getField("po_number"),
    payment_terms: getField("payment_terms"),
    currency: getField("currency") || "INR",
    subtotal: to_number(getField("subtotal"), 0),
    grand_total: to_number(getField("grand_total"), 0),
    items,
    warnings: [],
  };
}

function update_item_serials($wrapper) {
  $wrapper.find("[data-items-body] tr").each((index, row) => {
    $(row).find("[data-item-serial]").text(index + 1);
  });
}

function init_item_selectors($wrapper) {
  $wrapper.find("[data-item-selector]").each((_, holder) => {
    const $holder = $(holder);
    if ($holder.data("control-ready")) {
      return;
    }

    const $row = $holder.closest("tr");
    const $hidden = $row.find("[data-item-field='item_code']");
    const $label = $row.find("[data-item-field='item_label']");
    $holder.data("initializing", true);
    const control = frappe.ui.form.make_control({
      parent: holder,
      df: {
        fieldtype: "Link",
        label: "",
      options: "Item",
      only_input: true,
      default: $hidden.val() || "",
      get_query() {
          return {
            query: "invoice_import.api.invoice_import.search_item_for_review",
            filters: {
              disabled: 0,
              has_variants: 0,
              is_purchase_item: 1,
            },
          };
        },
        change() {
          const raw_value = control.get_value() || "";
          const item_code = control.title_value_map?.[raw_value] || raw_value;
          $hidden.val(item_code);
          $label.val(raw_value);
          if (item_code && !$holder.data("initializing")) {
            set_item_selector_display($row, control, item_code, raw_value);
            sync_row_description_from_item_code($row, item_code);
            sync_row_uom_from_item_code($row, item_code);
            learn_item_alias_from_row($row, item_code);
          }
        },
      },
      render_input: true,
    });
    control.refresh();
    if (control.$wrapper) {
      control.$wrapper.css({ width: "100%", minHeight: "0", marginBottom: "0" });
      control.$input.css({ width: "100%", height: "16px", padding: "0 4px", fontSize: "9px", lineHeight: "1", marginBottom: "0" });
    }
    if ($hidden.val()) {
      control.set_value($hidden.val());
      set_item_selector_display($row, control, $hidden.val(), $label.val());
      sync_row_description_from_item_code($row, $hidden.val());
      sync_row_uom_from_item_code($row, $hidden.val());
    }
    init_uom_selector_for_row($row);
    attach_review_row_recalcs($row);
    $hidden.val(control.get_value() || "");
    $holder.data("initializing", false);
    $holder.data("control-ready", true);
    $holder.data("control", control);
  });
}

function init_uom_selectors($wrapper) {
  $wrapper.find("[data-uom-selector]").each((_, holder) => {
    const $holder = $(holder);
    if ($holder.data("control-ready")) {
      return;
    }

    const $row = $holder.closest("tr");
    init_uom_selector_for_row($row);
  });
}

function init_uom_selector_for_row($row) {
  const $holder = $row.find("[data-uom-selector]").first();
  if (!$holder.length || $holder.data("control-ready")) {
    return;
  }

  const $hidden = $row.find("[data-item-field='uom']");
  const default_uom = ($hidden.val() || $row.find("[data-item-field='purchase_uom']").val() || $row.find("[data-item-field='stock_uom']").val() || "Nos").trim() || "Nos";
  const get_uom_options = () => get_row_uom_options($row);
  $holder.data("initializing", true);
  const control = frappe.ui.form.make_control({
    parent: $holder[0],
    df: {
      fieldtype: "Link",
      label: "",
      options: "UOM",
      only_input: true,
      default: default_uom,
      get_query() {
        const preferred_uoms = get_uom_options();
        return {
          query: "invoice_import.api.invoice_import.search_uom_for_review",
          filters: {
            preferred_uoms: JSON.stringify(preferred_uoms),
          },
        };
      },
      change() {
        const value = control.get_value() || "";
        $hidden.val(value);
      },
    },
    render_input: true,
  });
  control.refresh();
  if (control.$wrapper) {
    control.$wrapper.css({ width: "100%", minHeight: "0", marginBottom: "0" });
    control.$input.css({ width: "100%", height: "16px", padding: "0 4px", fontSize: "9px", lineHeight: "1", marginBottom: "0" });
  }
  control.set_value(default_uom);
  $hidden.val(default_uom);
  $holder.data("initializing", false);
  $holder.data("control-ready", true);
  $holder.data("control", control);
}

function set_uom_selector_display($row, control, uom_value = "") {
  const $hidden = $row.find("[data-item-field='uom']");
  const value = (uom_value || "").trim();
  if (!value || !control) {
    return;
  }
  control.set_value(value);
  if (control.$input && control.$input.length) {
    control.$input.val(value);
  }
  $hidden.val(value);
}

function attach_review_row_recalcs($scope) {
  const $rows = $scope.is("tr") ? $scope : $scope.find("[data-items-body] tr");
  $rows.each((_, row) => {
    const $row = $(row);
    if ($row.data("recalc-bound")) {
      return;
    }
    const qtyInput = $row.find("[data-item-field='qty']").get(0);
    const amountInput = $row.find("[data-item-field='amount']").get(0);
    if (qtyInput) {
      ["input", "keyup", "change", "blur"].forEach((eventName) => {
        qtyInput.addEventListener(eventName, () => {
          update_rate_from_qty_change($row);
          learn_uom_conversion_from_row($row);
        });
      });
    }
    if (amountInput) {
      ["input", "keyup", "change", "blur"].forEach((eventName) => {
        amountInput.addEventListener(eventName, () => {
          $row.find("[data-item-field='source_amount']").val(($row.find("[data-item-field='amount']").val() || "").trim());
          update_rate_from_qty_change($row);
        });
      });
    }
    $row.data("recalc-bound", true);
  });
}

function get_row_uom_options($row) {
  const raw = ($row.find("[data-item-field='uom_options']").val() || "").trim();
  if (!raw) {
    return [];
  }
  try {
    const parsed = JSON.parse(raw);
    if (Array.isArray(parsed)) {
      return parsed.map((value) => String(value || "").trim()).filter(Boolean);
    }
  } catch (error) {
    // fall through to comma-separated parsing
  }
  return raw
    .split(",")
    .map((value) => String(value || "").trim())
    .filter(Boolean);
}

function set_row_uom_options($row, options) {
  const normalized = Array.isArray(options)
    ? options.map((value) => String(value || "").trim()).filter(Boolean)
    : [];
  $row.find("[data-item-field='uom_options']").val(JSON.stringify(normalized));
}

function sync_row_uom_from_item_code($row, item_code) {
  if (!item_code) {
    return;
  }
  frappe.call({
    method: "invoice_import.api.invoice_import.get_item_uom_context",
    args: {
      item_code,
    },
    callback(response) {
      const item = (response.message && response.message) || {};
      const purchase_uom = (item.purchase_uom || "").trim();
      const stock_uom = (item.stock_uom || "").trim();
      const uom_options = Array.isArray(item.uom_options) ? item.uom_options : [];
      const default_uom = purchase_uom || stock_uom || "Nos";
      const $purchase = $row.find("[data-item-field='purchase_uom']");
      const $stock = $row.find("[data-item-field='stock_uom']");
      $purchase.val(purchase_uom);
      $stock.val(stock_uom);
      set_row_uom_options($row, uom_options);
      const control = $row.find("[data-uom-selector]").data("control");
      if (control) {
        set_uom_selector_display($row, control, default_uom);
      } else {
        $row.find("[data-item-field='uom']").val(default_uom);
      }
    },
  });
}

function set_item_selector_display($row, control, item_code, preferred_label = "") {
  const $label = $row.find("[data-item-field='item_label']");
  const $hint = $row.find("[data-item-match-hint]");
  const item_label = preferred_label || $label.val() || item_code || "";
  if (!item_code || !control) {
    return;
  }

  const apply_label = (label) => {
    control.title_value_map = control.title_value_map || {};
    control.title_value_map[item_code] = item_code;
    if (label) {
      control.title_value_map[label] = item_code;
    }
    const display_value = label || item_code;
    if (control.$input && control.$input.length) {
      control.$input.val(display_value);
    } else if (control.set_input_value) {
      control.set_input_value(display_value);
    }
    $label.val(label || display_value || "");
    if ($hint.length) {
      const hint = label && label !== item_code ? `${item_code} - ${label}` : item_code;
      $hint.text(hint);
    }
  };

  if (item_label && item_label !== item_code) {
    apply_label(item_label);
    return;
  }

  frappe.call({
    method: "frappe.client.get_value",
    args: {
      doctype: "Item",
      filters: { name: item_code },
      fieldname: "item_name",
    },
    callback(response) {
      const label = (response.message && response.message.item_name) || item_code;
      apply_label(label);
    },
  });
}

function sync_row_description_from_item_code($row, item_code) {
  if (!item_code) {
    return;
  }
  const $description = $row.find("[data-item-field='description']");
  const source_description = ($row.find("[data-item-field='source_description']").val() || "").trim();
  if ($description.length && !$description.val() && source_description) {
    $description.val(source_description);
  }
}

function learn_item_alias_from_row($row, item_code) {
  const source_description = ($row.find("[data-item-field='source_description']").val() || "").trim();
  if (!source_description || !cur_frm || !cur_frm.doc || !cur_frm.doc.name) {
    return;
  }
  frappe.call({
    method: "invoice_import.api.invoice_import.learn_item_alias_from_review",
    args: {
      invoice_import: cur_frm.doc.name,
      source_description,
      item_code,
    },
  });
}

function learn_uom_conversion_from_row($row) {
  const item_code = ($row.find("[data-item-field='item_code']").val() || "").trim();
  const source_description = ($row.find("[data-item-field='source_description']").val() || "").trim();
  const source_qty = to_number($row.find("[data-item-field='source_qty']").val(), 0);
  const source_uom = ($row.find("[data-item-field='source_uom']").val() || "").trim();
  const target_qty = to_number($row.find("[data-item-field='qty']").val(), 0);
  if (!item_code || !source_description || !source_qty || !target_qty || Math.abs(source_qty - target_qty) < 0.0001) {
    return;
  }
  frappe.call({
    method: "invoice_import.api.invoice_import.learn_uom_conversion_from_review",
    args: {
      invoice_import: cur_frm.doc.name,
      source_description,
      item_code,
      source_qty,
      source_uom,
      target_qty,
    },
  });
}

function update_rate_from_qty_change($row) {
  const qty = to_number($row.find("[data-item-field='qty']").val(), 0);
  const amount = to_number($row.find("[data-item-field='source_amount']").val() || $row.find("[data-item-field='amount']").val(), 0);
  const source_qty = to_number($row.find("[data-item-field='source_qty']").val(), 0);
  if (!qty || !amount || (!source_qty && !qty)) {
    return;
  }

  const rate = amount / qty;
  const formatted_rate = Number.isFinite(rate) ? rate.toFixed(2) : "";
  const $rate = $row.find("[data-item-field='rate']");
  $rate.val(formatted_rate);
  $rate.prop("value", formatted_rate);
  $rate.trigger("change");
}

window.invoice_import_update_review_row_qty = function (input) {
  const row = input && input.closest ? input.closest("tr") : null;
  if (!row) {
    return;
  }
  const $row = $(row);
  update_rate_from_qty_change($row);
  learn_uom_conversion_from_row($row);
};

window.invoice_import_update_review_row_amount = function (input) {
  const row = input && input.closest ? input.closest("tr") : null;
  if (!row) {
    return;
  }
  const $row = $(row);
  $row.find("[data-item-field='source_amount']").val(($row.find("[data-item-field='amount']").val() || "").trim());
  update_rate_from_qty_change($row);
};

function to_number(value, fallback) {
  const num = Number(String(value || "").replace(/,/g, ""));
  return Number.isFinite(num) ? num : fallback;
}

function mark_low_confidence(frm) {
  const score = Number(frm.doc.confidence_score || 0);
  if (!score || score >= 70) {
    return;
  }
  frm.dashboard.set_headline_alert(__("Low confidence extraction. Manual correction is required."), "orange");
}
