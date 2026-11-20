const state = {
  allPages: [],
  filteredPages: [],
  currentIndex: -1,
  currentDetail: null,
  zoom: 1,
  naturalWidth: 0,
  naturalHeight: 0,
  mode: "pan", // "pan" | "draw"
  bboxes: [], // [{ error_index, row_index, dong, bbox: [x1,y1,x2,y2], note }]
  regionHints: [], // [{ part, region_index, bbox, is_primary, is_neighbor, matched_lines }]
  selectedBboxIdx: -1,
  activeRowIndex: null,
};

const els = {
  summary: document.getElementById("summary-badge"),
  filterDataset: document.getElementById("filter-dataset"),
  filterStatus: document.getElementById("filter-status"),
  pageList: document.getElementById("page-list"),
  currentTitle: document.getElementById("current-title"),
  viewport: document.getElementById("image-viewport"),
  stage: document.getElementById("image-stage"),
  img: document.getElementById("page-image"),
  bboxLayer: document.getElementById("bbox-layer"),
  zoomLabel: document.getElementById("zoom-label"),
  rowsContainer: document.getElementById("rows-container"),
  bboxCount: document.getElementById("bbox-count"),
  bboxList: document.getElementById("bbox-list"),
  btnClearBboxes: document.getElementById("btn-clear-bboxes"),
  saveToast: document.getElementById("save-toast"),
  chkApplyTxt: document.getElementById("chk-apply-txt"),
  chkShowHints: document.getElementById("chk-show-hints"),
  btnModePan: document.getElementById("btn-mode-pan"),
  btnModeDraw: document.getElementById("btn-mode-draw"),
  btnPrev: document.getElementById("btn-prev"),
  btnNext: document.getElementById("btn-next"),
  btnSave: document.getElementById("btn-save"),
  btnZoomIn: document.getElementById("btn-zoom-in"),
  btnZoomOut: document.getElementById("btn-zoom-out"),
  btnZoomFit: document.getElementById("btn-zoom-fit"),
};

function escapeHtml(s) {
  return String(s)
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;");
}

function highlightSquares(text) {
  return escapeHtml(text).replace(/□/g, '<span class="sq-mark">□</span>');
}

function showToast(msg, isError = false) {
  els.saveToast.textContent = msg;
  els.saveToast.style.color = isError ? "#f87171" : "#6ee7b7";
  clearTimeout(showToast._t);
  showToast._t = setTimeout(() => {
    els.saveToast.textContent = "";
  }, 2600);
}

function setMode(mode) {
  state.mode = mode;
  els.btnModePan.classList.toggle("active", mode === "pan");
  els.btnModeDraw.classList.toggle("active", mode === "draw");
  els.viewport.classList.toggle("draw-mode", mode === "draw");
}

function applyZoom(newZoom) {
  state.zoom = Math.max(0.15, Math.min(4.0, newZoom));
  if (state.naturalWidth > 0) {
    els.img.style.width = `${Math.round(state.naturalWidth * state.zoom)}px`;
    els.img.style.height = `${Math.round(state.naturalHeight * state.zoom)}px`;
  }
  els.zoomLabel.textContent = `${Math.round(state.zoom * 100)}%`;
  renderBboxOverlay();
}

function fitImage() {
  if (!state.naturalWidth) return;
  const avail = Math.max(200, els.viewport.clientWidth - 28);
  applyZoom(avail / state.naturalWidth);
}

function populateDatasetFilter(pages) {
  const current = els.filterDataset.value;
  const datasets = [...new Set(pages.map((p) => p.tac_pham))];
  els.filterDataset.innerHTML =
    '<option value="ALL">Tất cả tác phẩm</option>' +
    datasets.map((d) => `<option value="${escapeHtml(d)}">${escapeHtml(d)}</option>`).join("");
  if (datasets.includes(current)) {
    els.filterDataset.value = current;
  }
}

function renderList(keepId = null) {
  const dsFilter = els.filterDataset.value;
  const stFilter = els.filterStatus.value;

  state.filteredPages = state.allPages.filter((p) => {
    if (dsFilter !== "ALL" && p.tac_pham !== dsFilter) return false;
    if (stFilter === "PENDING" && p.pending_count === 0) return false;
    if (stFilter === "DONE" && p.pending_count > 0) return false;
    return true;
  });

  els.pageList.innerHTML = "";
  state.filteredPages.forEach((p, idx) => {
    const div = document.createElement("div");
    div.className = "page-item";
    div.dataset.id = p.id;
    const isDone = p.pending_count === 0;
    const bboxBadge = p.bbox_count ? ` · ${p.bbox_count} box` : "";
    div.innerHTML = `
      <div class="page-item-top">
        <span>${escapeHtml(p.trang)} (${escapeHtml(p.ngon_ngu)})</span>
        <span class="pill ${isDone ? "done" : "pending"}">${isDone ? "Xong" : `${p.pending_count} dòng`}</span>
      </div>
      <div class="page-item-sub">
        <span>${escapeHtml(p.tac_pham)}</span>
        <span>${p.square_count} □${bboxBadge}</span>
      </div>
    `;
    div.addEventListener("click", () => selectIndex(idx));
    els.pageList.appendChild(div);
  });

  if (keepId) {
    const found = state.filteredPages.findIndex((p) => p.id === keepId);
    if (found >= 0) {
      state.currentIndex = found;
      highlightActiveListItem();
      return;
    }
  }
  if (state.filteredPages.length > 0 && state.currentIndex < 0) {
    selectIndex(0);
  }
}

function highlightActiveListItem() {
  const active = state.filteredPages[state.currentIndex];
  const activeId = active ? active.id : null;
  for (const el of els.pageList.children) {
    el.classList.toggle("active", el.dataset.id === activeId);
  }
}

function renumberBboxes() {
  state.bboxes.forEach((b, i) => {
    b.error_index = i + 1;
  });
}

function renderBboxOverlay(draftRect = null) {
  els.bboxLayer.innerHTML = "";
  const z = state.zoom;

  // 1. Render column region hints from packed_maxrects manifest
  if (els.chkShowHints && els.chkShowHints.checked) {
    const activeRow =
      state.currentDetail &&
      (state.currentDetail.rows || []).find(
        (r) => Number(r.row_index) === Number(state.activeRowIndex)
      );
    const activeDong = activeRow ? Number(activeRow.dong) : null;

    state.regionHints.forEach((h) => {
      const [x1, y1, x2, y2] = h.bbox;
      const matchesActive = activeDong !== null && (h.matched_lines || []).includes(activeDong);
      const cls = matchesActive || h.is_primary
        ? "primary"
        : h.is_neighbor
        ? "neighbor"
        : "";
      const div = document.createElement("div");
      div.className = `hint-rect ${cls}`.trim();
      div.style.left = `${x1 * z}px`;
      div.style.top = `${y1 * z}px`;
      div.style.width = `${(x2 - x1) * z}px`;
      div.style.height = `${(y2 - y1) * z}px`;

      const tag = document.createElement("div");
      tag.className = "hint-tag";
      const linesInfo =
        h.matched_lines && h.matched_lines.length > 0
          ? ` → D${h.matched_lines.join(",")}`
          : "";
      tag.textContent = `${h.part === "full" ? "" : h.part + " "}Cột ${h.region_index}${linesInfo}`;
      div.appendChild(tag);
      els.bboxLayer.appendChild(div);
    });
  }

  // 2. Render user-drawn error bboxes
  state.bboxes.forEach((b, idx) => {
    const [x1, y1, x2, y2] = b.bbox;
    const div = document.createElement("div");
    div.className = "bbox-rect" + (idx === state.selectedBboxIdx ? " selected" : "");
    div.style.left = `${x1 * z}px`;
    div.style.top = `${y1 * z}px`;
    div.style.width = `${(x2 - x1) * z}px`;
    div.style.height = `${(y2 - y1) * z}px`;

    const tag = document.createElement("div");
    tag.className = "bbox-tag";
    const dongLabel = b.dong ? ` · D${b.dong}` : "";
    tag.textContent = `#${b.error_index}${dongLabel}`;
    div.appendChild(tag);

    div.addEventListener("mousedown", (e) => {
      if (state.mode === "draw") return;
      e.stopPropagation();
      state.selectedBboxIdx = idx;
      renderBboxOverlay();
      renderBboxList();
    });

    els.bboxLayer.appendChild(div);
  });

  if (draftRect) {
    const [x1, y1, x2, y2] = draftRect;
    const draft = document.createElement("div");
    draft.className = "bbox-rect draft";
    draft.style.left = `${x1 * z}px`;
    draft.style.top = `${y1 * z}px`;
    draft.style.width = `${(x2 - x1) * z}px`;
    draft.style.height = `${(y2 - y1) * z}px`;
    els.bboxLayer.appendChild(draft);
  }
}

function renderBboxList() {
  renumberBboxes();
  els.bboxCount.textContent = String(state.bboxes.length);
  els.bboxList.innerHTML = "";

  const rows = (state.currentDetail && state.currentDetail.rows) || [];

  state.bboxes.forEach((b, idx) => {
    const item = document.createElement("div");
    item.className = "bbox-item" + (idx === state.selectedBboxIdx ? " selected" : "");

    const rowOptions = [
      `<option value="">-- Không gắn dòng --</option>`,
      ...rows.map(
        (r) =>
          `<option value="${r.row_index}" ${Number(b.row_index) === Number(r.row_index) ? "selected" : ""}>Dòng ${r.dong}</option>`
      ),
    ].join("");

    item.innerHTML = `
      <div class="bbox-item-info">
        <div><strong>Lỗi #${b.error_index}</strong> <span class="bbox-item-meta">[${b.bbox.join(", ")}]</span></div>
        <select class="bbox-row-link">${rowOptions}</select>
      </div>
      <button type="button" class="btn-del-bbox" title="Xóa bbox này">Xóa</button>
    `;

    item.addEventListener("click", () => {
      state.selectedBboxIdx = idx;
      renderBboxOverlay();
      renderBboxList();
    });

    const sel = item.querySelector(".bbox-row-link");
    sel.addEventListener("click", (e) => e.stopPropagation());
    sel.addEventListener("change", () => {
      if (sel.value === "") {
        b.row_index = null;
        b.dong = null;
      } else {
        const rIdx = Number(sel.value);
        const found = rows.find((r) => Number(r.row_index) === rIdx);
        b.row_index = rIdx;
        b.dong = found ? Number(found.dong) : null;
      }
      renderBboxOverlay();
    });

    item.querySelector(".btn-del-bbox").addEventListener("click", (e) => {
      e.stopPropagation();
      state.bboxes.splice(idx, 1);
      if (state.selectedBboxIdx === idx) state.selectedBboxIdx = -1;
      else if (state.selectedBboxIdx > idx) state.selectedBboxIdx--;
      renumberBboxes();
      renderBboxOverlay();
      renderBboxList();
    });

    els.bboxList.appendChild(item);
  });
}

async function selectIndex(idx) {
  if (idx < 0 || idx >= state.filteredPages.length) return;
  state.currentIndex = idx;
  state.selectedBboxIdx = -1;
  highlightActiveListItem();

  const item = state.filteredPages[idx];
  els.currentTitle.textContent = `${item.tac_pham} / ${item.ngon_ngu} / ${item.trang}.png`;

  const res = await fetch(
    `/api/page?tac_pham=${encodeURIComponent(item.tac_pham)}&ngon_ngu=${encodeURIComponent(item.ngon_ngu)}&trang=${encodeURIComponent(item.trang)}`
  );
  const detail = await res.json();
  state.currentDetail = detail;
  state.regionHints = detail.region_hints || [];
  state.bboxes = (detail.bboxes || []).map((b, i) => ({
    error_index: b.error_index || i + 1,
    row_index: b.row_index ?? null,
    dong: b.dong ?? null,
    bbox: b.bbox,
    note: b.note || "",
  }));
  state.activeRowIndex = detail.rows && detail.rows.length > 0 ? Number(detail.rows[0].row_index) : null;

  if (detail.has_image) {
    els.img.onload = () => {
      state.naturalWidth = els.img.naturalWidth;
      state.naturalHeight = els.img.naturalHeight;
      fitImage();
    };
    els.img.src = detail.image_url;
  } else {
    els.img.removeAttribute("src");
  }

  renderRows(detail.rows || []);
  renderBboxList();
}

function renderRows(rows) {
  els.rowsContainer.innerHTML = "";
  rows.forEach((row) => {
    const card = document.createElement("div");
    const isActive = Number(row.row_index) === Number(state.activeRowIndex);
    card.className = `row-card status-${row.trang_thai}` + (isActive ? " active-target" : "");
    card.dataset.rowIndex = row.row_index;

    const ctxHtml = (row.context || [])
      .map(
        (c) => `
          <div class="ctx-line ${c.is_target ? "target" : ""}">
            <span class="ctx-no">${c.line_no}</span>
            <span>${highlightSquares(c.text)}</span>
          </div>`
      )
      .join("");

    card.innerHTML = `
      <div class="row-header">
        <strong>Dòng ${row.dong} (${row.so_dau_vuong} dấu □)</strong>
        <span>Gốc: ${highlightSquares(row.text_ocr)}</span>
      </div>
      <div class="context-box">${ctxHtml}</div>
      <input type="text" class="edit-input" value="${escapeHtml(row.text_sua)}" />
      <div class="card-actions">
        <select class="status-select">
          <option value="CAN_XEM_LAI" ${row.trang_thai === "CAN_XEM_LAI" ? "selected" : ""}>Cần xem lại</option>
          <option value="DA_SUA" ${row.trang_thai === "DA_SUA" ? "selected" : ""}>Đã sửa</option>
          <option value="GIU_NGUYEN_MO" ${row.trang_thai === "GIU_NGUYEN_MO" ? "selected" : ""}>Giữ nguyên (ảnh mờ)</option>
        </select>
        <input type="text" class="note-input" placeholder="Ghi chú (VD: mất nét, nhòe mực...)" value="${escapeHtml(row.ghi_chu || "")}" />
        <button type="button" class="btn-draw-for-row" title="Chuyển sang vẽ bbox gắn với dòng này">Đánh dấu BBox</button>
        <button type="button" class="btn-mark-blur" title="Giữ nguyên □ vì ảnh gốc mờ không đọc được">Ảnh mờ</button>
      </div>
    `;

    const input = card.querySelector(".edit-input");
    const statusSel = card.querySelector(".status-select");
    const noteInput = card.querySelector(".note-input");
    const btnBlur = card.querySelector(".btn-mark-blur");
    const btnDrawRow = card.querySelector(".btn-draw-for-row");

    const setActiveRow = () => {
      state.activeRowIndex = Number(row.row_index);
      for (const el of els.rowsContainer.querySelectorAll(".row-card")) {
        el.classList.toggle("active-target", Number(el.dataset.rowIndex) === state.activeRowIndex);
      }
      renderBboxOverlay();
    };

    card.addEventListener("click", setActiveRow);

    btnDrawRow.addEventListener("click", (e) => {
      e.stopPropagation();
      setActiveRow();
      setMode("draw");
      showToast(`Đang vẽ BBox cho Dòng ${row.dong}`);
    });

    const syncBorder = () => {
      const activeCls = Number(row.row_index) === Number(state.activeRowIndex) ? " active-target" : "";
      card.className = `row-card status-${statusSel.value}${activeCls}`;
    };

    input.addEventListener("input", () => {
      if (!input.value.includes("□") && statusSel.value === "CAN_XEM_LAI") {
        statusSel.value = "DA_SUA";
        syncBorder();
      }
    });

    statusSel.addEventListener("change", syncBorder);

    btnBlur.addEventListener("click", () => {
      statusSel.value = "GIU_NGUYEN_MO";
      if (!noteInput.value.trim()) {
        noteInput.value = "Ảnh gốc mờ/mất nét, giữ nguyên □ theo Guideline";
      }
      syncBorder();
    });

    els.rowsContainer.appendChild(card);
  });
}

async function saveCurrent() {
  if (!state.currentDetail) return;
  const updates = [];
  for (const card of els.rowsContainer.querySelectorAll(".row-card")) {
    updates.push({
      row_index: Number(card.dataset.rowIndex),
      text_sua: card.querySelector(".edit-input").value,
      trang_thai: card.querySelector(".status-select").value,
      ghi_chu: card.querySelector(".note-input").value,
    });
  }
  renumberBboxes();

  const res = await fetch("/api/save", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      tac_pham: state.currentDetail.tac_pham,
      ngon_ngu: state.currentDetail.ngon_ngu,
      trang: state.currentDetail.trang,
      apply_to_txt: els.chkApplyTxt.checked,
      updates,
      bboxes: state.bboxes,
    }),
  });
  const data = await res.json();
  if (!res.ok || data.error) {
    showToast(`Lỗi lưu: ${data.error || res.status}`, true);
    return;
  }

  const keepId = `${state.currentDetail.tac_pham}/${state.currentDetail.ngon_ngu}/${state.currentDetail.trang}`;
  updateSummary(data.list.summary);
  state.allPages = data.list.pages;
  state.bboxes = (data.detail.bboxes || []).map((b, i) => ({
    error_index: b.error_index || i + 1,
    row_index: b.row_index ?? null,
    dong: b.dong ?? null,
    bbox: b.bbox,
    note: b.note || "",
  }));
  renderList(keepId);
  renderRows(data.detail.rows || []);
  renderBboxOverlay();
  renderBboxList();

  const parts = ["Đã lưu CSV"];
  if (data.bbox_saved) parts.push("JSON BBox");
  if (data.txt_updated) parts.push("03_text_sach");
  showToast(parts.join(" + "));
}

function updateSummary(summary) {
  els.summary.textContent = `${summary.done_rows}/${summary.total_rows} dòng đã kiểm (${summary.total_pages} trang)`;
}

async function init() {
  const res = await fetch("/api/pages");
  const data = await res.json();
  state.allPages = data.pages || [];
  updateSummary(data.summary);
  populateDatasetFilter(state.allPages);
  renderList();
}

// Convert mouse event to natural image pixel coordinates
function getImagePoint(e) {
  const rect = els.img.getBoundingClientRect();
  const x = Math.max(0, Math.min(state.naturalWidth, (e.clientX - rect.left) / state.zoom));
  const y = Math.max(0, Math.min(state.naturalHeight, (e.clientY - rect.top) / state.zoom));
  return { x: Math.round(x), y: Math.round(y) };
}

// Panning and Drawing handlers
let panning = false;
let panStart = { x: 0, y: 0, left: 0, top: 0 };
let drawing = false;
let drawStart = null;
let drawCurrent = null;

els.viewport.addEventListener("mousedown", (e) => {
  if (e.button !== 0) return;
  if (state.mode === "draw" && state.naturalWidth > 0) {
    e.preventDefault();
    drawing = true;
    drawStart = getImagePoint(e);
    drawCurrent = drawStart;
    return;
  }
  panning = true;
  panStart = {
    x: e.clientX,
    y: e.clientY,
    left: els.viewport.scrollLeft,
    top: els.viewport.scrollTop,
  };
});

window.addEventListener("mousemove", (e) => {
  if (drawing && drawStart) {
    drawCurrent = getImagePoint(e);
    const x1 = Math.min(drawStart.x, drawCurrent.x);
    const y1 = Math.min(drawStart.y, drawCurrent.y);
    const x2 = Math.max(drawStart.x, drawCurrent.x);
    const y2 = Math.max(drawStart.y, drawCurrent.y);
    renderBboxOverlay([x1, y1, x2, y2]);
    return;
  }
  if (!panning) return;
  els.viewport.scrollLeft = panStart.left - (e.clientX - panStart.x);
  els.viewport.scrollTop = panStart.top - (e.clientY - panStart.y);
});

window.addEventListener("mouseup", () => {
  if (drawing && drawStart && drawCurrent) {
    const x1 = Math.min(drawStart.x, drawCurrent.x);
    const y1 = Math.min(drawStart.y, drawCurrent.y);
    const x2 = Math.max(drawStart.x, drawCurrent.x);
    const y2 = Math.max(drawStart.y, drawCurrent.y);
    drawing = false;
    drawStart = null;
    drawCurrent = null;

    if (x2 - x1 >= 8 && y2 - y1 >= 8) {
      const rows = (state.currentDetail && state.currentDetail.rows) || [];
      const linked = rows.find((r) => Number(r.row_index) === Number(state.activeRowIndex));
      state.bboxes.push({
        error_index: state.bboxes.length + 1,
        row_index: linked ? Number(linked.row_index) : null,
        dong: linked ? Number(linked.dong) : null,
        bbox: [x1, y1, x2, y2],
        note: "",
      });
      state.selectedBboxIdx = state.bboxes.length - 1;
      renumberBboxes();
      renderBboxList();
    }
    renderBboxOverlay();
    return;
  }
  panning = false;
});

els.viewport.addEventListener(
  "wheel",
  (e) => {
    if (e.ctrlKey) {
      e.preventDefault();
      applyZoom(state.zoom * (e.deltaY < 0 ? 1.15 : 0.87));
    }
  },
  { passive: false }
);

els.btnModePan.addEventListener("click", () => setMode("pan"));
els.btnModeDraw.addEventListener("click", () => setMode("draw"));
els.chkShowHints.addEventListener("change", () => renderBboxOverlay());
els.btnZoomIn.addEventListener("click", () => applyZoom(state.zoom * 1.2));
els.btnZoomOut.addEventListener("click", () => applyZoom(state.zoom / 1.2));
els.btnZoomFit.addEventListener("click", fitImage);
els.btnPrev.addEventListener("click", () => selectIndex(state.currentIndex - 1));
els.btnNext.addEventListener("click", () => selectIndex(state.currentIndex + 1));
els.btnSave.addEventListener("click", saveCurrent);
els.btnClearBboxes.addEventListener("click", () => {
  state.bboxes = [];
  state.selectedBboxIdx = -1;
  renderBboxOverlay();
  renderBboxList();
});

els.filterDataset.addEventListener("change", () => {
  state.currentIndex = -1;
  renderList();
});
els.filterStatus.addEventListener("change", () => {
  state.currentIndex = -1;
  renderList();
});

window.addEventListener("keydown", (e) => {
  const tag = (e.target && e.target.tagName) || "";
  const typing = tag === "INPUT" || tag === "TEXTAREA" || tag === "SELECT";

  if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === "s") {
    e.preventDefault();
    saveCurrent();
    return;
  }
  if (e.altKey && e.key === "ArrowLeft") {
    e.preventDefault();
    selectIndex(state.currentIndex - 1);
    return;
  }
  if (e.altKey && e.key === "ArrowRight") {
    e.preventDefault();
    selectIndex(state.currentIndex + 1);
    return;
  }
  if (typing) return;

  if (e.key.toLowerCase() === "b") {
    setMode("draw");
  } else if (e.key.toLowerCase() === "v") {
    setMode("pan");
  } else if ((e.key === "Delete" || e.key === "Backspace") && state.selectedBboxIdx >= 0) {
    state.bboxes.splice(state.selectedBboxIdx, 1);
    state.selectedBboxIdx = -1;
    renumberBboxes();
    renderBboxOverlay();
    renderBboxList();
  }
});

init();
