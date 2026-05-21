frappe.ui.form.on("Invoice Import", {
  refresh(frm) {
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
});

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
        primary_action_label: __("Create Draft Purchase Invoice"),
        primary_action() {
          const extracted = collect_review_data(dialog);
          frappe.call({
            method: "invoice_import.api.invoice_import.create_purchase_invoice_from_review",
            args: {
              invoice_import: frm.doc.name,
              extracted_json: JSON.stringify(extracted),
            },
            freeze: true,
            callback(result) {
              dialog.hide();
              frm.reload_doc();
              const pi = result.message && result.message.purchase_invoice;
              if (pi) {
                frappe.set_route("Form", "Purchase Invoice", pi);
              } else {
                const warnings = (result.message && result.message.warnings) || [];
                frappe.msgprint({
                  title: __("Purchase Invoice not created"),
                  message: warnings.length ? warnings.join("<br>") : __("The server did not create a draft Purchase Invoice."),
                  indicator: "orange",
                });
              }
            },
          });
        },
      });

      dialog.review_payload = payload;
      dialog.fields_dict.review_html.$wrapper.html(render_review_layout(payload));
      bind_review_events(dialog);
      dialog.show();
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
  const item_label = item._match_status?.item_name || item.item_name || item.item_code || "";
  return `
    <tr data-item-row>
      <td style="text-align:center;vertical-align:top;padding:0 3px;line-height:1;"><strong data-item-serial>${index + 1}</strong></td>
      <td style="width:64px;text-align:center;vertical-align:top;padding:0 3px;line-height:1;">${render_match_badge(item._match_status)}</td>
      <td style="width:220px;max-width:220px;vertical-align:top;padding:0 3px;">
        <div data-item-selector class="item-selector" style="width:100%;"></div>
        <input type="hidden" data-item-field="item_code" value="${frappe.utils.escape_html(String(item.item_code || item.item_name || ""))}">
        <input type="hidden" data-item-field="item_label" value="${frappe.utils.escape_html(String(item_label))}">
        <input type="hidden" data-item-field="source_description" value="${frappe.utils.escape_html(String(source_description))}">
        <input type="hidden" data-item-field="source_qty" value="${frappe.utils.escape_html(String(item.source_qty || item.qty || 1))}">
        <input type="hidden" data-item-field="source_uom" value="${frappe.utils.escape_html(String(item.source_uom || item.uom || "Nos"))}">
      </td>
      <td style="width:340px;max-width:340px;padding:0 3px;vertical-align:top;"><input data-item-field="description" class="form-control input-sm" style="width:100%;height:18px;line-height:1;padding:0 4px;font-size:9px;margin:0;" value="${frappe.utils.escape_html(String(display_description))}"></td>
      <td style="width:70px;padding:0 3px;vertical-align:top;"><input data-item-field="qty" class="form-control input-sm" style="width:100%;height:18px;line-height:1;padding:0 4px;font-size:9px;margin:0;" value="${frappe.utils.escape_html(String(item.qty || 1))}"></td>
      <td style="width:70px;padding:0 3px;vertical-align:top;"><input data-item-field="uom" class="form-control input-sm" style="width:100%;height:18px;line-height:1;padding:0 4px;font-size:9px;margin:0;" value="${frappe.utils.escape_html(String(item.uom || "Nos"))}"></td>
      <td style="width:100px;padding:0 3px;vertical-align:top;"><input data-item-field="rate" class="form-control input-sm" style="width:100%;height:18px;line-height:1;padding:0 4px;font-size:9px;margin:0;" value="${frappe.utils.escape_html(String(item.rate || ""))}"></td>
      <td style="width:100px;padding:0 3px;vertical-align:top;"><input data-item-field="amount" class="form-control input-sm" style="width:100%;height:18px;line-height:1;padding:0 4px;font-size:9px;margin:0;" value="${frappe.utils.escape_html(String(item.amount || ""))}"></td>
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
  });
  $wrapper.on("click", "[data-action='remove-item']", (event) => {
    const $row = $(event.currentTarget).closest("tr");
    if ($wrapper.find("[data-items-body] tr").length > 1) {
      $row.remove();
      update_item_serials($wrapper);
    }
  });
  $wrapper.on("change", "[data-item-field='qty']", (event) => {
    const $row = $(event.currentTarget).closest("tr");
    learn_uom_conversion_from_row($row);
  });
  update_item_serials($wrapper);
  init_item_selectors($wrapper);
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
          return { filters: { disabled: 0 } };
        },
        change() {
          const item_code = control.get_value() || "";
          $hidden.val(item_code);
          if (item_code && !$holder.data("initializing")) {
            set_item_selector_display($row, control, item_code);
            sync_row_description_from_item_code($row, item_code);
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
    }
    $hidden.val(control.get_value() || "");
    $holder.data("initializing", false);
    $holder.data("control-ready", true);
    $holder.data("control", control);
  });
}

function set_item_selector_display($row, control, item_code, preferred_label = "") {
  const $label = $row.find("[data-item-field='item_label']");
  const item_label = preferred_label || $label.val() || item_code || "";
  if (!item_code || !control) {
    return;
  }

  const apply_label = (label) => {
    const display = label || item_code;
    control.title_value_map = control.title_value_map || {};
    control.title_value_map[display] = item_code;
    control.set_input_value(display);
    $label.val(display);
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
