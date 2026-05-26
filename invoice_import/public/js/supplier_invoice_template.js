frappe.ui.form.on("Supplier Invoice Template", {
  refresh(frm) {
    sync_template_defaults(frm);
    if (frm.is_new() && !(frm.doc.format_notes || "").trim()) {
      frm.set_value("format_notes", get_default_format_notes());
    }

    if (!frm.is_new()) {
      frm.add_custom_button(__("Graphical Mapper"), () => open_graphical_mapper(frm));
    }
  },

  supplier(frm) {
    sync_template_defaults(frm, true);
  },

  reference_purchase_invoice(frm) {
    sync_template_defaults(frm, true);
  },
});

function sync_template_defaults(frm, force = false) {
  frappe.call({
    method: "invoice_import.api.invoice_import.get_template_defaults",
    args: {
      template_name: frm.doc.name && !frm.is_new() ? frm.doc.name : "",
      supplier: frm.doc.supplier || "",
      reference_purchase_invoice: frm.doc.reference_purchase_invoice || "",
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

function get_default_format_notes() {
  return `Header:
- Supplier name appears near top
- Invoice No. label: "Invoice No."
- Invoice Date label: "Date" or "Invoice Date"
- GSTIN appears near supplier name

Item table:
- Item rows start after the heading line
- Columns usually include: description, hsn_sac, qty, uom, rate, amount
- Item names may wrap to next line and should be merged
- Ignore footer totals line

UOM / HSN rules:
- Common UOMs: Nos, PCS, Box, Pack, Roll, Bag, Doz
- HSN is usually 6 to 8 digits
- Allow HSN prefix matching
- Prefer purchase UOM if item has one

Matching rules:
- Prefer learned supplier aliases first
- If no alias exists, use HSN/spec/size/brand matching
- Leave weak matches blank for manual review

Totals / tax rules:
- Grand total appears near bottom
- Ignore numeric-only totals line in item parsing
- Tax summary may appear separately at the end

Known aliases:
- [source description] => [ERPNext item code]
- [source description] => [ERPNext item code]`;
}

function open_graphical_mapper(frm) {
  frappe.require(["pdfjs.bundle.css", "print_designer.bundle.css"]);
  const state = {
    pdfDoc: null,
    pageNumber: 1,
    scale: 1,
    zoom: 1,
    activeIndex: -1,
    mappings: clone_mappings(frm.doc.field_mappings || []),
    drawing: false,
    start: null,
  };

  const dialog = new frappe.ui.Dialog({
    title: __("Supplier Invoice Template Mapper"),
    size: "extra-large",
    fields: [{ fieldtype: "HTML", fieldname: "mapper_html" }],
    primary_action_label: __("Save Mappings"),
    primary_action: () => save_mappings(frm, dialog, state),
  });

  dialog.fields_dict.mapper_html.$wrapper.html(render_mapper_shell(frm, state));
  bind_mapper_events(frm, dialog, state);
  dialog.show();
  load_pdf_preview(frm, dialog, state);
}

function render_mapper_shell(frm, state) {
  const rows = render_mapping_rows(state.mappings);
  return `
    <div style="display:grid;grid-template-columns:minmax(0,1.35fr) minmax(360px,0.65fr);gap:14px;">
      <div style="border:1px solid var(--border-color);border-radius:8px;background:var(--card-bg);padding:10px;">
        <div style="display:flex;justify-content:space-between;align-items:center;gap:10px;margin-bottom:10px;">
          <div style="display:flex;align-items:center;gap:8px;">
            <button type="button" class="btn btn-xs btn-default" data-action="prev-page">${__("Prev")}</button>
            <input type="number" min="1" value="${state.pageNumber}" data-field="page_number" class="form-control input-sm" style="width:72px;">
            <span style="font-size:12px;color:var(--text-muted)" data-page-info>${__("Page")} ${state.pageNumber}</span>
            <button type="button" class="btn btn-xs btn-default" data-action="next-page">${__("Next")}</button>
          </div>
          <div style="display:flex;align-items:center;gap:6px;">
            <button type="button" class="btn btn-xs btn-default" data-action="zoom-out">${__("Zoom -")}</button>
            <span data-zoom-label style="font-size:12px;color:var(--text-muted);min-width:56px;text-align:center;">100%</span>
            <button type="button" class="btn btn-xs btn-default" data-action="zoom-in">${__("Zoom +")}</button>
          </div>
          <div style="font-size:12px;color:var(--text-muted)">
            <div>${frappe.utils.escape_html(frm.doc.template_name || frm.doc.name)}</div>
            <div data-sample-file style="font-size:11px;"></div>
          </div>
        </div>
        <div style="display:flex;gap:8px;align-items:center;margin-bottom:10px;">
          <button type="button" class="btn btn-sm btn-primary" data-action="upload-sample">${__("Upload Sample PDF")}</button>
          <button type="button" class="btn btn-sm btn-default" data-action="reload-sample">${__("Reload Sample")}</button>
        </div>
        <div style="position:relative;width:100%;overflow:auto;border:1px solid var(--border-color);border-radius:8px;background:#f8f8f8;min-height:72vh;">
          <div data-preview-wrapper style="position:relative;display:inline-block;width:100%;">
            <img data-pdf-image style="width:100%;display:block;background:#fff;user-select:none;-webkit-user-drag:none;" />
            <div data-selection-layer style="position:absolute;inset:0;pointer-events:none;"></div>
          </div>
        </div>
        <div style="font-size:12px;color:var(--text-muted);margin-top:8px;">
          ${__("Enter Source Label text exactly as it appears in the PDF. Use | to add aliases. The PDF preview is reference only.")}
        </div>
      </div>
      <div style="display:flex;flex-direction:column;gap:10px;max-height:76vh;overflow:auto;">
        <div style="border:1px solid var(--border-color);border-radius:8px;padding:12px;background:var(--card-bg);">
          <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:8px;">
            <strong>${__("Field Mappings")}</strong>
            <button type="button" class="btn btn-xs btn-primary" data-action="add-mapping">${__("Add")}</button>
          </div>
          <div style="font-size:12px;color:var(--text-muted);margin-bottom:8px;">
            ${__("Click a row and fill Source Label / Target Field.")}
          </div>
          <div data-mapping-list>${rows}</div>
        </div>
        <div style="border:1px solid var(--border-color);border-radius:8px;padding:12px;background:var(--card-bg);">
          <strong style="display:block;margin-bottom:8px;">${__("Selected Mapping")}</strong>
          <div style="display:grid;grid-template-columns:1fr 1fr;gap:10px;">
          <label style="display:flex;flex-direction:column;gap:4px;">
            <span style="font-size:12px;color:var(--text-muted)">${__("Source Label")}</span>
              <input class="form-control input-sm" data-edit="source_label" placeholder="e.g. Document No | INV.NO">
          </label>
            <label style="display:flex;flex-direction:column;gap:4px;">
              <span style="font-size:12px;color:var(--text-muted)">${__("Target Field")}</span>
              <select class="form-control input-sm" data-edit="target_field">
                ${target_field_options()}
              </select>
            </label>
            <label style="display:flex;flex-direction:column;gap:4px;">
              <span style="font-size:12px;color:var(--text-muted)">${__("Page")}</span>
              <input type="number" min="1" class="form-control input-sm" data-edit="page_number" value="1">
            </label>
            <label style="display:flex;flex-direction:column;gap:4px;">
              <span style="font-size:12px;color:var(--text-muted)">${__("Value Hint")}</span>
              <input class="form-control input-sm" data-edit="value_hint">
            </label>
          </div>
          <label style="display:flex;flex-direction:column;gap:4px;margin-top:10px;">
            <span style="font-size:12px;color:var(--text-muted)">${__("Notes")}</span>
            <textarea rows="3" class="form-control" data-edit="notes"></textarea>
          </label>
          <div style="display:flex;justify-content:space-between;align-items:center;gap:8px;margin-top:8px;">
            <div style="font-size:12px;color:var(--text-muted);">
              ${__("Region data is kept only for legacy mappings. Text labels drive extraction now.")}
            </div>
            <button type="button" class="btn btn-xs btn-danger" data-action="delete-selected">
              ${__("Delete Selected")}
            </button>
          </div>
        </div>
      </div>
    </div>
  `;
}

function render_mapping_rows(mappings) {
  if (!mappings.length) {
    return `<div style="font-size:12px;color:var(--text-muted)">${__("No mappings yet.")}</div>`;
  }
  return `
    <table class="table table-bordered table-sm" style="margin-bottom:0;">
      <thead>
        <tr>
          <th>${__("Field")}</th>
          <th>${__("Region")}</th>
        </tr>
      </thead>
      <tbody>
        ${mappings
          .map(
            (row, index) => `
              <tr data-mapping-index="${index}" style="cursor:pointer;">
                <td>
                  <div><strong>${frappe.utils.escape_html(row.target_field || "")}</strong></div>
                  <div style="font-size:11px;color:var(--text-muted)">${frappe.utils.escape_html(row.source_label || "")}</div>
                </td>
                <td style="font-size:11px;color:var(--text-muted)">
                  ${frappe.utils.escape_html(describe_region(row.region_json))}
                </td>
                <td>
                  <button type="button" class="btn btn-xs btn-danger" data-action="delete-mapping" data-mapping-index="${index}">
                    ${__("Delete")}
                  </button>
                </td>
              </tr>
            `,
          )
          .join("")}
      </tbody>
    </table>
  `;
}

function target_field_options() {
  const groups = [
    {
      label: "Items",
      values: [
        ["item_description", "Item"],
        ["item_qty", "Accepted Qty"],
        ["item_uom", "UOM"],
        ["item_hsn_sac", "HSN/SAC"],
        ["item_rate", "Rate (INR)"],
        ["item_amount", "Amount (INR)"],
        ["item_mrp", "MRP"],
      ],
    },
    {
      label: "Header",
      values: ["supplier", "supplier_gstin", "invoice_number", "invoice_date", "due_date", "po_number", "payment_terms", "currency", "grand_total"],
    },
    {
      label: "Taxes",
      values: ["tax_description", "tax_amount"],
    },
  ];
  return groups
    .map(
      (group) => `
        <optgroup label="${frappe.utils.escape_html(group.label)}">
          ${(group.values || [])
            .map((value) => {
              const field_value = Array.isArray(value) ? value[0] : value;
              const field_label = Array.isArray(value) ? value[1] : value;
              return `<option value="${field_value}">${frappe.utils.escape_html(field_label)}</option>`;
            })
            .join("")}
        </optgroup>
      `,
    )
    .join("");
}

function bind_mapper_events(frm, dialog, state) {
  const $wrapper = dialog.fields_dict.mapper_html.$wrapper;
  const image = () => $wrapper.find("[data-pdf-image]")[0];
  const layer = () => $wrapper.find("[data-selection-layer]")[0];
  const pageInfo = () => $wrapper.find("[data-page-info]");

  const sync_form = () => {
    $wrapper.find("[data-edit='source_label']").val(state.mappings[state.activeIndex]?.source_label || "");
    $wrapper.find("[data-edit='target_field']").val(state.mappings[state.activeIndex]?.target_field || "");
    $wrapper.find("[data-edit='page_number']").val(state.mappings[state.activeIndex]?.page_number || state.pageNumber || 1);
    $wrapper.find("[data-edit='value_hint']").val(state.mappings[state.activeIndex]?.value_hint || "");
    $wrapper.find("[data-edit='notes']").val(state.mappings[state.activeIndex]?.notes || "");
  };

  const refresh_rows = () => {
    $wrapper.find("[data-mapping-list]").html(render_mapping_rows(state.mappings));
    $wrapper.find("[data-mapping-index]").removeClass("table-active");
    if (state.activeIndex >= 0) {
      $wrapper.find(`[data-mapping-index='${state.activeIndex}']`).addClass("table-active");
    }
    sync_form();
  };

  $wrapper.on("click", "[data-action='add-mapping']", () => {
    state.mappings.push({
      source_label: "",
      target_field: "invoice_number",
      page_number: state.pageNumber,
      region_json: "",
      value_hint: "",
      required: 0,
      notes: "",
    });
    state.activeIndex = state.mappings.length - 1;
    refresh_rows();
  });

  $wrapper.on("click", "[data-mapping-index]", (event) => {
    if (event.target && event.target.closest("[data-action='delete-mapping']")) {
      return;
    }
    state.activeIndex = Number(event.currentTarget.getAttribute("data-mapping-index"));
    refresh_rows();
  });

  $wrapper.on("click", "[data-action='delete-mapping'], [data-action='delete-selected']", (event) => {
    event.preventDefault();
    event.stopPropagation();
    const attr = event.currentTarget.getAttribute("data-mapping-index");
    const target_index = attr === null || attr === "" ? state.activeIndex : Number(attr);
    if (Number.isNaN(target_index) || target_index < 0 || target_index >= state.mappings.length) {
      return;
    }
    state.mappings.splice(target_index, 1);
    if (!state.mappings.length) {
      state.activeIndex = -1;
    } else if (state.activeIndex === target_index) {
      state.activeIndex = Math.min(target_index, state.mappings.length - 1);
    } else if (state.activeIndex > target_index) {
      state.activeIndex -= 1;
    } else if (state.activeIndex >= state.mappings.length) {
      state.activeIndex = state.mappings.length - 1;
    }
    refresh_rows();
  });

  $wrapper.on("click", "[data-action='prev-page']", () => {
    if (state.pageNumber > 1) {
      state.pageNumber -= 1;
      $wrapper.find("[data-field='page_number']").val(state.pageNumber);
      pageInfo().text(__("Page") + " " + state.pageNumber);
      load_pdf_preview(frm, dialog, state);
    }
  });

  $wrapper.on("click", "[data-action='next-page']", () => {
    if (state.pageCount && state.pageNumber >= state.pageCount) {
      return;
    }
    state.pageNumber += 1;
    $wrapper.find("[data-field='page_number']").val(state.pageNumber);
    pageInfo().text(__("Page") + " " + state.pageNumber);
    load_pdf_preview(frm, dialog, state);
  });

  $wrapper.on("click", "[data-action='reload-sample']", () => {
    load_pdf_preview(frm, dialog, state);
  });

  $wrapper.on("click", "[data-action='upload-sample']", () => {
    open_sample_uploader(frm, dialog, state);
  });

  $wrapper.on("click", "[data-action='zoom-in']", () => {
    state.zoom = Math.min(2.5, round(state.zoom + 0.1));
    apply_zoom($wrapper, state);
  });

  $wrapper.on("click", "[data-action='zoom-out']", () => {
    state.zoom = Math.max(0.5, round(state.zoom - 0.1));
    apply_zoom($wrapper, state);
  });

  $wrapper.on("change", "[data-field='page_number']", (event) => {
    state.pageNumber = Number(event.currentTarget.value || 1);
    pageInfo().text(__("Page") + " " + state.pageNumber);
  });

  $wrapper.on("input", "[data-edit]", (event) => {
    if (state.activeIndex < 0) return;
    const field = event.currentTarget.getAttribute("data-edit");
    state.mappings[state.activeIndex][field] = event.currentTarget.value;
    if (field === "page_number") {
      state.mappings[state.activeIndex][field] = Number(event.currentTarget.value || 1);
    }
    refresh_rows();
  });

  let drag = null;
  layer().addEventListener("mousedown", (event) => {
    if (state.activeIndex < 0) {
      frappe.msgprint(__("Select or add a mapping row first."));
      return;
    }
    const rect = layer().getBoundingClientRect();
    drag = {
      x1: event.clientX - rect.left,
      y1: event.clientY - rect.top,
      x2: event.clientX - rect.left,
      y2: event.clientY - rect.top,
    };
  });
  window.addEventListener("mousemove", (event) => {
    if (!drag) return;
    const rect = layer().getBoundingClientRect();
    drag.x2 = event.clientX - rect.left;
    drag.y2 = event.clientY - rect.top;
    draw_selection(layer(), drag);
  });
  window.addEventListener("mouseup", () => {
    if (!drag) return;
    const rect = layer().getBoundingClientRect();
    const x = Math.min(drag.x1, drag.x2);
    const y = Math.min(drag.y1, drag.y2);
    const width = Math.abs(drag.x2 - drag.x1);
    const height = Math.abs(drag.y2 - drag.y1);
    drag = null;
    clear_overlay(layer());
    if (width < 8 || height < 8) return;

    const pdfMeta = state.pageMeta || { width: rect.width, height: rect.height };
    const region = {
      page_number: state.pageNumber,
      unit: "percent",
      x: round((x / rect.width) * 100),
      y: round((y / rect.height) * 100),
      width: round((width / rect.width) * 100),
      height: round((height / rect.height) * 100),
      page_width: round(pdfMeta.width || rect.width),
      page_height: round(pdfMeta.height || rect.height),
    };
    state.mappings[state.activeIndex].page_number = state.pageNumber;
    state.mappings[state.activeIndex].region_json = JSON.stringify(region);
    refresh_rows();
  });

  refresh_rows();
}

async function load_pdf_preview(frm, dialog, state) {
  return load_pdf_preview_from_url(frm, dialog, state, null);
}

async function load_pdf_preview_from_url(frm, dialog, state, file_url_override) {
  const $wrapper = dialog.fields_dict.mapper_html.$wrapper;
  const layer = $wrapper.find("[data-selection-layer]")[0];
  $wrapper.find("[data-sample-file]").text(__("Loading preview..."));
  const template = await new Promise((resolve) => {
    frappe.call({
      method: "frappe.client.get",
      args: { doctype: "Supplier Invoice Template", name: frm.doc.name },
      callback(r) {
        resolve(r.message || {});
      },
    });
  });
  const sample_file = file_url_override || template.sample_invoice_file || frm.doc.sample_invoice_file;
  $wrapper.find("[data-sample-file]").text(sample_file ? `${__("Sample File")}: ${sample_file}` : __("Sample File not found"));
  if (!sample_file) {
    $wrapper.find("[data-pdf-image]").attr("src", "");
    if (layer) {
      layer.innerHTML = "";
    }
    return;
  }
  const result = await new Promise((resolve, reject) => {
    frappe.call({
      method: "invoice_import.api.invoice_import.get_template_sample_page_image",
      args: {
        file_url: sample_file,
        page_number: state.pageNumber,
        resolution: 144,
      },
      callback(r) {
        resolve(r.message || {});
      },
      error(err) {
        reject(err);
      },
    });
  });
  const preview_image = image();
  const preview_wrapper = $wrapper.find("[data-preview-wrapper]")[0];
  if (preview_image) {
    preview_image.onload = () => {
      state.pageMeta = { width: preview_image.naturalWidth || result.page_width || 1, height: preview_image.naturalHeight || result.page_height || 1 };
      draw_existing_regions(preview_image, layer, state);
    };
    preview_image.src = result.file_url || "";
  }
  if (preview_wrapper) {
    preview_wrapper.style.width = `${state.zoom * 100}%`;
  }
  $wrapper.find("[data-zoom-label]").text(`${Math.round(state.zoom * 100)}%`);
  layer.innerHTML = "";
  if (preview_image && preview_image.complete) {
    state.pageMeta = { width: preview_image.naturalWidth || result.page_width || 1, height: preview_image.naturalHeight || result.page_height || 1 };
    draw_existing_regions(preview_image, layer, state);
  }
  state.pageCount = Number(result.page_count || state.pageCount || 1);
}

function draw_existing_regions(image, layer, state) {
  const overlay = document.createElement("div");
  overlay.style.position = "absolute";
  overlay.style.inset = "0";
  overlay.style.pointerEvents = "none";
  layer.appendChild(overlay);
  state.mappings.forEach((row, index) => {
    if (!row.region_json) return;
    try {
      const region = JSON.parse(row.region_json);
      if ((region.page_number || state.pageNumber) !== state.pageNumber) return;
      const box = document.createElement("div");
      box.style.position = "absolute";
      box.style.border = index === state.activeIndex ? "2px solid #0b74de" : "2px solid #d35400";
      box.style.background = "rgba(11, 116, 222, 0.12)";
      box.style.left = `${region.x}%`;
      box.style.top = `${region.y}%`;
      box.style.width = `${region.width}%`;
      box.style.height = `${region.height}%`;
      box.title = row.target_field || "";
      overlay.appendChild(box);
    } catch {
      return;
    }
  });
}

function draw_selection(layer, drag) {
  if (!layer) return;
  if (!drag) return;
  const x = Math.min(drag.x1, drag.x2);
  const y = Math.min(drag.y1, drag.y2);
  const w = Math.abs(drag.x2 - drag.x1);
  const h = Math.abs(drag.y2 - drag.y1);
  clear_overlay(layer);
  const overlay = document.createElement("div");
  overlay.style.position = "absolute";
  overlay.style.left = `${x}px`;
  overlay.style.top = `${y}px`;
  overlay.style.width = `${w}px`;
  overlay.style.height = `${h}px`;
  overlay.style.border = "2px dashed #0b74de";
  overlay.style.background = "rgba(11, 116, 222, 0.15)";
  overlay.style.pointerEvents = "none";
  layer.appendChild(overlay);
}

function clear_overlay(layer) {
  if (layer) {
    layer.innerHTML = "";
  }
}

function apply_zoom($wrapper, state) {
  const preview_wrapper = $wrapper.find("[data-preview-wrapper]")[0];
  const label = $wrapper.find("[data-zoom-label]");
  if (preview_wrapper) {
    preview_wrapper.style.width = `${state.zoom * 100}%`;
  }
  if (label.length) {
    label.text(`${Math.round(state.zoom * 100)}%`);
  }
}

function save_mappings(frm, dialog, state) {
  const payload = state.mappings
    .filter((row) => row.target_field || row.region_json || row.source_label)
    .map((row) => ({
      source_label: row.source_label || "",
      target_field: row.target_field || "",
      page_number: Number(row.page_number || 1),
      region_json: row.region_json || "",
      value_hint: row.value_hint || "",
      required: Number(row.required || 0),
      notes: row.notes || "",
    }));

  frappe.call({
    method: "invoice_import.api.invoice_import.save_template_field_mappings",
    args: {
      template_name: frm.doc.name,
      field_mappings_json: JSON.stringify(payload),
    },
    freeze: true,
    callback() {
      dialog.hide();
      frm.reload_doc();
    },
  });
}

function open_sample_uploader(frm, dialog, state) {
  new frappe.ui.FileUploader({
    doctype: "Supplier Invoice Template",
    docname: frm.doc.name,
    fieldname: "sample_invoice_file",
    dialog_title: __("Upload Sample Invoice File"),
    restrictions: {
      allowed_file_types: [".pdf", ".jpg", ".jpeg", ".png"],
      max_number_of_files: 1,
    },
    on_success(file_doc) {
      const $wrapper = dialog.fields_dict.mapper_html.$wrapper;
      $wrapper.find("[data-sample-file]").text(`${__("Sample File")}: ${file_doc.file_url}`);
      state.pdfDoc = null;
      frm.reload_doc();
      load_pdf_preview_from_url(frm, dialog, state, file_doc.file_url);
    },
  });
}

function build_file_url(file_url) {
  if (!file_url) return "";
  if (file_url.startsWith("http://") || file_url.startsWith("https://")) return file_url;
  return file_url.startsWith("/") ? file_url : `/${file_url}`;
}

function ensure_pdfjs_loaded() {
  return Promise.resolve();
}

function clone_mappings(rows) {
  return (rows || []).map((row) => ({ ...row }));
}

function describe_region(region_json) {
  if (!region_json) return __("No region");
  try {
    const region = JSON.parse(region_json);
    return `p${region.page_number || 1}: x${region.x || 0}, y${region.y || 0}, w${region.width || 0}, h${region.height || 0}`;
  } catch {
    return region_json;
  }
}

function round(value) {
  return Math.round(value * 100) / 100;
}
