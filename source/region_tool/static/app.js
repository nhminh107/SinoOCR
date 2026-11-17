(() => {
  "use strict";

  const LABEL_VI = {
    body: "Nội dung",
    title: "Tiêu đề",
    header: "Đầu trang",
    footer: "Chân trang",
    ignore: "Bỏ qua",
  };
  const LABEL_COLORS = {
    body: "#0f766e",
    title: "#b45309",
    header: "#0369a1",
    footer: "#4338ca",
    ignore: "#64748b",
  };
  const HANDLE_NAMES = ["nw", "n", "ne", "e", "se", "s", "sw", "w"];
  const HANDLE_CURSORS = {
    nw: "nwse-resize",
    se: "nwse-resize",
    ne: "nesw-resize",
    sw: "nesw-resize",
    n: "ns-resize",
    s: "ns-resize",
    e: "ew-resize",
    w: "ew-resize",
  };

  const state = {
    images: [],
    currentId: null,
    config: { model_name: "PP-OCRv6_medium_det", device: "gpu:0", model_dir: "" },
    outputDir: "",
    annotation: null,
    imageEl: null,
    selectedIds: new Set(),
    reviewCache: new Map(),
    mode: "select",
    zoom: 1,
    panX: 0,
    panY: 0,
    dirty: false,
    corruptLocked: false,
    revision: 0,
    loadSeq: 0,
    savingPromise: null,
    autosaveTimer: null,
    detecting: false,
    actionBusy: false,
    loading: false,
    undoStack: [],
    redoStack: [],
    drag: null,
    rafId: 0,
  };

  const $ = (id) => document.getElementById(id);
  const canvas = $("region-canvas");
  const ctx = canvas.getContext("2d");

  // ---------------------------------------------------------------------------
  // Coordinate helpers (original pixels <-> clockwise rotated view)
  // ---------------------------------------------------------------------------

  function viewDims(W, H, ori) {
    return ori === 90 || ori === 270 ? { w: H, h: W } : { w: W, h: H };
  }

  function origToViewPoint(x, y, W, H, ori) {
    if (ori === 90) return { x: H - y, y: x };
    if (ori === 180) return { x: W - x, y: H - y };
    if (ori === 270) return { x: y, y: W - x };
    return { x, y };
  }

  function viewToOrigPoint(vx, vy, W, H, ori) {
    if (ori === 90) return { x: vy, y: H - vx };
    if (ori === 180) return { x: W - vx, y: H - vy };
    if (ori === 270) return { x: W - vy, y: vx };
    return { x: vx, y: vy };
  }

  function origToViewBox(bbox, W, H, ori) {
    const [x1, y1, x2, y2] = bbox;
    const p1 = origToViewPoint(x1, y1, W, H, ori);
    const p2 = origToViewPoint(x2, y2, W, H, ori);
    return [Math.min(p1.x, p2.x), Math.min(p1.y, p2.y), Math.max(p1.x, p2.x), Math.max(p1.y, p2.y)];
  }

  function viewToOrigBox(vbox, W, H, ori) {
    const [vx1, vy1, vx2, vy2] = vbox;
    const p1 = viewToOrigPoint(vx1, vy1, W, H, ori);
    const p2 = viewToOrigPoint(vx2, vy2, W, H, ori);
    return [Math.min(p1.x, p2.x), Math.min(p1.y, p2.y), Math.max(p1.x, p2.x), Math.max(p1.y, p2.y)];
  }

  function clampOrigBox(box, W, H) {
    const x1 = Math.max(0, Math.min(W, Math.min(box[0], box[2])));
    const y1 = Math.max(0, Math.min(H, Math.min(box[1], box[3])));
    const x2 = Math.max(0, Math.min(W, Math.max(box[0], box[2])));
    const y2 = Math.max(0, Math.min(H, Math.max(box[1], box[3])));
    return [Math.round(x1), Math.round(y1), Math.round(x2), Math.round(y2)];
  }

  function clampMoveOrigBox(box, W, H) {
    const bw = box[2] - box[0];
    const bh = box[3] - box[1];
    const x1 = Math.max(0, Math.min(W - bw, box[0]));
    const y1 = Math.max(0, Math.min(H - bh, box[1]));
    return [Math.round(x1), Math.round(y1), Math.round(x1 + bw), Math.round(y1 + bh)];
  }

  function viewToCss(vx, vy) {
    return { x: state.panX + vx * state.zoom, y: state.panY + vy * state.zoom };
  }

  function cssToView(cx, cy) {
    return { x: (cx - state.panX) / state.zoom, y: (cy - state.panY) / state.zoom };
  }

  function getHandleCoords(vx1, vy1, vx2, vy2) {
    const cx = 0.5 * (vx1 + vx2);
    const cy = 0.5 * (vy1 + vy2);
    return {
      nw: [vx1, vy1],
      n: [cx, vy1],
      ne: [vx2, vy1],
      e: [vx2, cy],
      se: [vx2, vy2],
      s: [cx, vy2],
      sw: [vx1, vy2],
      w: [vx1, cy],
    };
  }

  // ---------------------------------------------------------------------------
  // UI Notifications & Status
  // ---------------------------------------------------------------------------

  function showToast(msg, type = "info") {
    const el = document.createElement("div");
    el.className = "toast";
    el.dataset.type = type;
    el.textContent = msg;
    $("toast-container").appendChild(el);
    setTimeout(() => el.remove(), 4000);
  }

  function showBanner(msg) {
    $("viewer-banner-text").textContent = msg;
    $("viewer-banner").hidden = false;
  }

  function hideBanner() {
    $("viewer-banner").hidden = true;
    $("viewer-banner-text").textContent = "";
  }

  function setLoading(active, text = "Đang tải...") {
    $("canvas-loading-text").textContent = text;
    $("canvas-loading").hidden = !active;
  }

  function editingLocked() {
    return state.detecting || state.actionBusy || state.loading || state.corruptLocked;
  }

  function updateSaveStatus(mode) {
    const pill = $("save-status");
    pill.dataset.state = mode;
    if (mode === "saved") pill.textContent = "Đã lưu";
    else if (mode === "dirty") pill.textContent = "Chưa lưu";
    else if (mode === "saving") pill.textContent = "Đang lưu...";
    else if (mode === "error") pill.textContent = "Lỗi lưu / JSON";
  }

  function markDirty() {
    state.revision += 1;
    state.dirty = true;
    updateSaveStatus("dirty");
    if (state.autosaveTimer) clearTimeout(state.autosaveTimer);
    if (!state.corruptLocked) {
      state.autosaveTimer = setTimeout(() => {
        saveCurrent();
      }, 800);
    }
  }

  // ---------------------------------------------------------------------------
  // API Helpers
  // ---------------------------------------------------------------------------

  async function apiJson(url, options = {}) {
    const res = await fetch(url, {
      headers: { "Content-Type": "application/json" },
      ...options,
    });
    let data = null;
    try {
      data = await res.json();
    } catch (_) {
      data = null;
    }
    if (!res.ok) {
      const err = new Error((data && data.error) || `HTTP ${res.status}`);
      err.status = res.status;
      throw err;
    }
    return data;
  }

  function loadImageElement(url) {
    return new Promise((resolve, reject) => {
      const img = new Image();
      img.onload = () => resolve(img);
      img.onerror = () => reject(new Error("Không tải được ảnh nguồn."));
      img.src = url;
    });
  }

  // ---------------------------------------------------------------------------
  // Undo / Redo & Region Mutations
  // ---------------------------------------------------------------------------

  function cloneRegions(regions) {
    return regions.map((r) => ({ ...r, bbox: [...r.bbox] }));
  }

  function pushUndoSnapshot() {
    if (!state.annotation) return;
    state.undoStack.push(cloneRegions(state.annotation.regions));
    if (state.undoStack.length > 60) state.undoStack.shift();
    state.redoStack = [];
    updateUndoButtons();
  }

  function undo() {
    if (!state.annotation || state.undoStack.length === 0 || editingLocked()) return;
    state.redoStack.push(cloneRegions(state.annotation.regions));
    state.annotation.regions = state.undoStack.pop();
    const valid = new Set(state.annotation.regions.map((r) => r.id));
    state.selectedIds = new Set([...state.selectedIds].filter((id) => valid.has(id)));
    markDirty();
    syncAfterRegionChange();
  }

  function redo() {
    if (!state.annotation || state.redoStack.length === 0 || editingLocked()) return;
    state.undoStack.push(cloneRegions(state.annotation.regions));
    state.annotation.regions = state.redoStack.pop();
    const valid = new Set(state.annotation.regions.map((r) => r.id));
    state.selectedIds = new Set([...state.selectedIds].filter((id) => valid.has(id)));
    markDirty();
    syncAfterRegionChange();
  }

  function updateUndoButtons() {
    $("btn-undo").disabled = editingLocked() || state.undoStack.length === 0;
    $("btn-redo").disabled = editingLocked() || state.redoStack.length === 0;
  }

  function syncAfterRegionChange() {
    updateUndoButtons();
    renderRegionList();
    syncInspectorFromSelection();
    updateBottomInfo();
    scheduleRender();
  }

  // ---------------------------------------------------------------------------
  // Bootstrap & Document Explorer
  // ---------------------------------------------------------------------------

  async function init() {
    bindEvents();
    const compactPanels = window.matchMedia("(max-width: 1100px)");
    const collapsePanels = () => {
      $("sidebar").classList.toggle("collapsed", compactPanels.matches);
      $("inspector").classList.toggle("collapsed", compactPanels.matches);
    };
    collapsePanels();
    compactPanels.addEventListener("change", collapsePanels);
    observeCanvasResize();
    try {
      setLoading(true, "Đang khởi tạo danh sách tài liệu...");
      const boot = await apiJson("/api/bootstrap");
      state.images = boot.images || [];
      if (boot.config) {
        state.config = boot.config;
        $("cfg-device").value = boot.config.device || "gpu:0";
        $("cfg-model-dir").value = boot.config.model_dir || "";
        $("gpu-chip").textContent = `${(boot.config.device || "gpu:0").toUpperCase()} · ${boot.config.model_name || "PP-OCRv6"}`;
      }
      state.outputDir = boot.output_dir || "";
      $("doc-total-badge").textContent = String(state.images.length);
      renderDocTree();

      const targetId =
        boot.current_id != null
          ? boot.current_id
          : state.images.length > 0
          ? state.images[0].id
          : null;

      if (targetId != null) {
        await selectImage(targetId, { skipSave: true });
      } else {
        $("canvas-empty").hidden = false;
      }
    } catch (err) {
      showBanner(`Không thể khởi tạo ứng dụng: ${err.message}`);
    } finally {
      setLoading(false);
    }
  }

  function renderDocTree() {
    const container = $("doc-tree");
    container.textContent = "";
    const q = $("doc-search").value.trim().toLowerCase();

    const groups = new Map();
    for (const img of state.images) {
      if (q) {
        const hay = `${img.dataset} ${img.filename} page${img.page_index ?? ""}`.toLowerCase();
        if (!hay.includes(q)) continue;
      }
      if (!groups.has(img.dataset)) groups.set(img.dataset, []);
      groups.get(img.dataset).push(img);
    }

    if (groups.size === 0) {
      const empty = document.createElement("p");
      empty.className = "hint-text";
      empty.textContent = "Không tìm thấy ảnh phù hợp.";
      container.appendChild(empty);
      return;
    }

    for (const [dataset, items] of groups.entries()) {
      const grp = document.createElement("div");
      grp.className = "dataset-group";

      const header = document.createElement("button");
      header.type = "button";
      header.className = "dataset-header";

      const nameSpan = document.createElement("span");
      nameSpan.className = "dataset-name";
      nameSpan.textContent = dataset;

      const countSpan = document.createElement("span");
      countSpan.className = "count-badge tabular";
      countSpan.textContent = String(items.length);

      header.append(nameSpan, countSpan);

      const listWrap = document.createElement("div");
      header.addEventListener("click", () => {
        listWrap.hidden = !listWrap.hidden;
      });

      for (const item of items) {
        const btn = document.createElement("button");
        btn.type = "button";
        btn.className = "doc-item";
        btn.dataset.id = String(item.id);
        if (item.id === state.currentId) btn.classList.add("active");

        const dot = document.createElement("span");
        dot.className = "rev-dot";
        dot.dataset.state = state.reviewCache.get(item.id) || "pending";

        const fname = document.createElement("span");
        fname.className = "doc-item-name";
        fname.textContent = item.filename;

        btn.append(dot, fname);
        if (item.image_part != null) {
          const part = document.createElement("span");
          part.className = "doc-item-part tabular";
          part.textContent = `#${item.image_part}`;
          btn.appendChild(part);
        }

        btn.addEventListener("click", () => {
          if (item.id !== state.currentId) selectImage(item.id);
        });
        listWrap.appendChild(btn);
      }

      grp.append(header, listWrap);
      container.appendChild(grp);
    }
  }

  function updateTreeSelectionAndDot(imageId, reviewState) {
    if (reviewState) state.reviewCache.set(imageId, reviewState);
    const items = $("doc-tree").querySelectorAll(".doc-item");
    for (const el of items) {
      const id = Number(el.dataset.id);
      el.classList.toggle("active", id === state.currentId);
      const dot = el.querySelector(".rev-dot");
      if (dot && state.reviewCache.has(id)) {
        dot.dataset.state = state.reviewCache.get(id);
      }
      if (id === state.currentId) {
        el.scrollIntoView({ block: "nearest" });
      }
    }
  }

  // ---------------------------------------------------------------------------
  // Load & Save Page Annotation
  // ---------------------------------------------------------------------------

  async function selectImage(imageId, opts = {}) {
    if (state.detecting || state.actionBusy || state.loading) {
      showToast("Đang xử lý, vui lòng đợi.", "info");
      return;
    }

    while (!opts.skipSave && state.dirty && !state.corruptLocked && state.annotation) {
      const ok = await saveCurrent();
      if (!ok) {
        showToast("Lưu thất bại; giữ nguyên trang hiện tại.", "error");
        return;
      }
    }

    if (state.loading || state.actionBusy) return;
    state.loading = true;
    setDetectingState(false);
    const seq = ++state.loadSeq;
    hideBanner();
    setLoading(true, "Đang tải trang và annotation...");

    try {
      const [annRes, imgEl] = await Promise.all([
        fetch(`/api/pages/${imageId}`).then(async (r) => {
          const data = await r.json().catch(() => null);
          if (!r.ok) {
            const err = new Error((data && data.error) || `HTTP ${r.status}`);
            err.status = r.status;
            throw err;
          }
          return data;
        }),
        loadImageElement(`/api/images/${imageId}`),
      ]);

      if (seq !== state.loadSeq) return;

      state.currentId = imageId;
      state.annotation = annRes;
      state.imageEl = imgEl;
      state.selectedIds.clear();
      state.undoStack = [];
      state.redoStack = [];
      state.dirty = false;
      state.corruptLocked = false;
      updateSaveStatus("saved");
      updateTreeSelectionAndDot(imageId, annRes.page_review_state);
      populatePageMetadata();
      fitView();
      syncAfterRegionChange();
    } catch (err) {
      if (seq !== state.loadSeq) return;
      if (err.status === 409) {
        state.currentId = imageId;
        state.corruptLocked = true;
        state.dirty = false;
        updateSaveStatus("error");
        showBanner(
          `Lỗi dữ liệu trang (HTTP 409): ${err.message}. Ứng dụng KHÔNG tự ghi đè tệp hiện có.`
        );
        try {
          const imgEl = await loadImageElement(`/api/images/${imageId}`);
          if (seq === state.loadSeq) {
            const meta = state.images.find((i) => i.id === imageId);
            state.imageEl = imgEl;
            state.annotation = {
              schema_version: 1,
              dataset: meta ? meta.dataset : "",
              filename: meta ? meta.filename : "",
              image_width: imgEl.naturalWidth,
              image_height: imgEl.naturalHeight,
              orientation: 0,
              source: meta || {},
              page_review_state: "pending",
              page_notes: "",
              raw_detections: [],
              regions: [],
            };
            populatePageMetadata();
            fitView();
            syncAfterRegionChange();
          }
        } catch (_) {}
      } else {
        showBanner(`Không tải được trang: ${err.message}`);
      }
    } finally {
      if (seq === state.loadSeq) {
        state.loading = false;
        setLoading(false);
        setDetectingState(false);
        if (window.matchMedia("(max-width: 1100px)").matches) $("sidebar").classList.add("collapsed");
      }
    }
  }

  async function saveCurrent(forceOverwrite = false) {
    if (!state.annotation || state.currentId == null) return true;
    if (state.corruptLocked) {
      showToast("Annotation bị lỗi; hãy kiểm tra tệp JSON trước khi chỉnh sửa hoặc lưu.", "error");
      return false;
    }

    if (state.autosaveTimer) {
      clearTimeout(state.autosaveTimer);
      state.autosaveTimer = null;
    }

    if (state.savingPromise) {
      await state.savingPromise;
      if (!state.dirty) return true;
    }

    const targetId = state.currentId;
    const revAtStart = state.revision;
    const payload = JSON.parse(JSON.stringify(state.annotation));

    updateSaveStatus("saving");
    const runSave = async () => {
      try {
        const res = await apiJson(`/api/pages/${targetId}`, {
          method: "PUT",
          body: JSON.stringify(payload),
        });
        if (state.currentId === targetId && res.annotation) {
          // Preserve edits made mid-flight
          if (state.revision === revAtStart) {
            state.annotation = res.annotation;
            state.dirty = false;
            state.corruptLocked = false;
            updateSaveStatus("saved");
          } else {
            updateSaveStatus("dirty");
          }
          updateTreeSelectionAndDot(targetId, res.annotation.page_review_state);
        }
        return true;
      } catch (err) {
        if (state.currentId === targetId) updateSaveStatus("error");
        showBanner(`Lưu thất bại (giữ nguyên chỉnh sửa): ${err.message}`);
        return false;
      } finally {
        state.savingPromise = null;
      }
    };

    state.savingPromise = runSave();
    return state.savingPromise;
  }

  // ---------------------------------------------------------------------------
  // Metadata, Inspector & Region List
  // ---------------------------------------------------------------------------

  function populatePageMetadata() {
    const ann = state.annotation;
    if (!ann) return;
    const meta = state.images.find((i) => i.id === state.currentId) || {};
    const idx = state.images.findIndex((i) => i.id === state.currentId);

    $("crumb-dataset").textContent = ann.dataset || meta.dataset || "—";
    $("crumb-filename").textContent = ann.filename || meta.filename || "—";
    const partText =
      ann.source && ann.source.image_part != null
        ? `Mảnh _${ann.source.image_part}`
        : "Ảnh đơn";
    $("crumb-meta").textContent = `${ann.image_width}×${ann.image_height} · ${partText} · ${ann.orientation}°`;
    $("crumb-meta").hidden = false;

    $("page-counter").textContent = `${idx + 1} / ${state.images.length}`;
    $("btn-prev").disabled = editingLocked() || idx <= 0;
    $("btn-next").disabled = editingLocked() || idx >= state.images.length - 1;

    $("meta-dataset").textContent = ann.dataset || "—";
    $("meta-filename").textContent = ann.filename || "—";
    $("meta-dims").textContent = `${ann.image_width} × ${ann.image_height} px`;
    $("meta-orientation").textContent = `${ann.orientation}°`;
    const pIdx = ann.source && ann.source.page_index != null ? `page${ann.source.page_index}` : "—";
    $("meta-part").textContent = `${pIdx} (${partText})`;
    $("meta-pdf").textContent = (ann.source && ann.source.pdf_basename) || "Không có";
    $("meta-pdf-page").textContent =
      ann.source && ann.source.pdf_page_index != null
        ? `Trang PDF ${ann.source.pdf_page_index}`
        : "—";

    $("page-review-state").value = ann.page_review_state || "pending";
    $("page-notes").value = ann.page_notes || "";
  }

  function updateBottomInfo() {
    const ann = state.annotation;
    if (!ann) return;
    $("bottom-info").textContent =
      `Vùng: ${ann.regions.length} · Raw OCR: ${ann.raw_detections.length} · Đã chọn: ${state.selectedIds.size} · Xoay: ${ann.orientation}°`;
  }

  function renderRegionList() {
    const list = $("region-list");
    list.textContent = "";
    const ann = state.annotation;
    if (!ann) {
      $("region-total-badge").textContent = "0";
      return;
    }

    const sorted = [...ann.regions].sort(
      (a, b) => (a.reading_order || 9999) - (b.reading_order || 9999)
    );
    $("region-total-badge").textContent = String(sorted.length);

    if (sorted.length === 0) {
      const empty = document.createElement("p");
      empty.className = "hint-text";
      empty.style.padding = "8px";
      empty.textContent = "Chưa có vùng nào. Bấm 'Nhận diện vùng' hoặc dùng công cụ Vẽ (N).";
      list.appendChild(empty);
      return;
    }

    for (const reg of sorted) {
      const row = document.createElement("button");
      row.type = "button";
      row.className = "region-row";
      if (state.selectedIds.has(reg.id)) row.classList.add("selected");

      const ord = document.createElement("span");
      ord.className = "reg-order-tag tabular";
      ord.textContent = `#${reg.reading_order}`;

      const meta = document.createElement("span");
      meta.className = "reg-meta";
      meta.textContent = `${LABEL_VI[reg.label] || reg.label} · ${reg.language} · ${reg.direction === "vertical" ? "Dọc" : "Ngang"}`;

      row.append(ord, meta);
      row.addEventListener("click", (ev) => {
        if (ev.shiftKey) {
          if (state.selectedIds.has(reg.id)) state.selectedIds.delete(reg.id);
          else state.selectedIds.add(reg.id);
        } else {
          state.selectedIds = new Set([reg.id]);
        }
        syncAfterRegionChange();
      });
      list.appendChild(row);
    }
  }

  function syncInspectorFromSelection() {
    const ann = state.annotation;
    const selected = ann ? ann.regions.filter((r) => state.selectedIds.has(r.id)) : [];
    $("sel-count-badge").textContent = String(selected.length);

    const fields = ["prop-label", "prop-language", "prop-role", "prop-direction", "prop-review"];
    const keys = ["label", "language", "content_role", "direction", "review_state"];

    const disabled = selected.length === 0 || editingLocked();
    for (const id of fields) $(id).disabled = disabled;
    $("btn-apply-props").disabled = disabled;

    const single = selected.length === 1 && !editingLocked();
    $("prop-order").disabled = !single;
    $("prop-notes").disabled = !single;

    if (selected.length === 0) {
      for (const id of fields) $(id).value = "";
      $("prop-order").value = "";
      $("prop-notes").value = "";
      return;
    }

    keys.forEach((key, idx) => {
      const vals = new Set(selected.map((r) => r[key]));
      $(fields[idx]).value = vals.size === 1 ? [...vals][0] : "";
    });

    if (selected.length === 1) {
      $("prop-order").value = String(selected[0].reading_order || 1);
      $("prop-notes").value = selected[0].notes || "";
    } else {
      $("prop-order").value = "";
      $("prop-notes").value = "";
    }
  }

  function applyInspectorProps() {
    if (!state.annotation || state.selectedIds.size === 0 || editingLocked()) return;
    pushUndoSnapshot();
    const selected = state.annotation.regions.filter((r) => state.selectedIds.has(r.id));
    const single = selected.length === 1;

    const label = $("prop-label").value;
    const lang = $("prop-language").value;
    const role = $("prop-role").value;
    const dir = $("prop-direction").value;
    const rev = $("prop-review").value;

    for (const reg of selected) {
      if (label) reg.label = label;
      if (lang) reg.language = lang;
      if (role) reg.content_role = role;
      if (dir) reg.direction = dir;
      if (rev) reg.review_state = rev;
      if (single) {
        const orderVal = parseInt($("prop-order").value, 10);
        if (Number.isFinite(orderVal) && orderVal >= 1) reg.reading_order = orderVal;
        reg.notes = $("prop-notes").value;
      }
    }

    markDirty();
    syncAfterRegionChange();
  }

  // ---------------------------------------------------------------------------
  // Backend Actions (group, number, merge, split) & Detection / Export
  // ---------------------------------------------------------------------------

  function currentActionSettings() {
    return {
      mode: $("grp-mode").value,
      min_score: parseFloat($("grp-min-score").value) || 0.25,
      overlap_threshold: parseFloat($("grp-overlap").value) || 0.35,
      reading_order: $("num-order-mode").value,
      start_index: Math.max(1, parseInt($("num-start").value, 10) || 1),
      exclude_non_content: $("num-exclude-non-content").checked,
    };
  }

  async function runServerAction(action, extra = {}) {
    if (!state.annotation || state.currentId == null || editingLocked()) return;
    pushUndoSnapshot();
    state.actionBusy = true;
    setDetectingState(state.detecting);
    try {
      const res = await apiJson("/api/action", {
        method: "POST",
        body: JSON.stringify({
          image_id: state.currentId,
          action,
          annotation: state.annotation,
          selected_ids: [...state.selectedIds],
          settings: currentActionSettings(),
          ...extra,
        }),
      });
      state.annotation = res.annotation;
      if (Array.isArray(res.selected_ids)) {
        state.selectedIds = new Set(res.selected_ids);
      }
      markDirty();
      syncAfterRegionChange();
    } catch (err) {
      state.undoStack.pop();
      updateUndoButtons();
      showToast(`Thao tác '${action}' lỗi: ${err.message}`, "error");
    } finally {
      state.actionBusy = false;
      setDetectingState(state.detecting);
    }
  }

  function setDetectingState(active) {
    state.detecting = active;
    const lockIds = [
      "btn-detect",
      "btn-save",
      "btn-prev",
      "btn-next",
      "btn-undo",
      "btn-redo",
      "btn-merge",
      "btn-split-h-mid",
      "btn-split-v-mid",
      "btn-delete",
      "btn-regroup",
      "btn-number-selected",
      "btn-number-all",
      "btn-apply-props",
      "btn-export-selected",
      "btn-export-content",
      "btn-rot-l",
      "btn-rot-r",
    ];
    lockIds.push("page-review-state", "page-notes");
    for (const id of lockIds) $(id).disabled = editingLocked();
    if (!editingLocked()) {
      populatePageMetadata();
      syncInspectorFromSelection();
      updateUndoButtons();
    }
  }

  async function runDetection() {
    if (!state.annotation || state.currentId == null || editingLocked()) return;
    const jobImageId = state.currentId;
    setDetectingState(true);
    setLoading(true, "Đang chạy PP-OCRv6 trên GPU...");
    hideBanner();

    try {
      const { job_id } = await apiJson(`/api/detect/${jobImageId}`, {
        method: "POST",
        body: JSON.stringify({
          orientation: state.annotation.orientation || 0,
          device: $("cfg-device").value.trim() || "gpu:0",
          model_dir: $("cfg-model-dir").value.trim() || state.config.model_dir,
        }),
      });

      while (true) {
        await new Promise((r) => setTimeout(r, 400));
        if (state.currentId !== jobImageId) return;
        const job = await apiJson(`/api/jobs/${job_id}`);
        if (job.status === "running") continue;
        if (job.status === "error") throw new Error(job.error || "Nhận diện thất bại.");
        if (job.status === "done") {
          const rawList = job.raw_detections || [];
          setDetectingState(false);
          setLoading(false);

          if (state.annotation.regions.length > 0) {
            const choice = await askReplaceModal(rawList.length, state.annotation.regions.length);
            if (choice === "cancel") return;
            state.annotation.raw_detections = rawList;
            if (choice === "keep") {
              markDirty();
              syncAfterRegionChange();
              showToast(`Đã cập nhật ${rawList.length} raw box, giữ vùng đã sửa.`, "success");
              return;
            }
          } else {
            state.annotation.raw_detections = rawList;
          }

          await runServerAction("group");
          showToast(`Đã nhận diện ${rawList.length} raw box.`, "success");
          return;
        }
      }
    } catch (err) {
      showBanner(`Lỗi PP-OCRv6 (giữ nguyên chỉnh sửa): ${err.message}`);
    } finally {
      setDetectingState(false);
      setLoading(false);
    }
  }

  function askReplaceModal(rawCount, existingCount) {
    return new Promise((resolve) => {
      const modal = $("detect-modal");
      $("detect-modal-desc").textContent =
        `Đã phát hiện ${rawCount} raw box. Trang hiện có ${existingCount} vùng đã gán nhãn. Bạn muốn xử lý thế nào?`;
      modal.hidden = false;

      const cleanup = (val) => {
        modal.hidden = true;
        modal.onkeydown = null;
        $("modal-btn-cancel").onclick = null;
        $("modal-btn-keep").onclick = null;
        $("modal-btn-replace").onclick = null;
        resolve(val);
      };

      $("modal-btn-cancel").onclick = () => cleanup("cancel");
      $("modal-btn-keep").onclick = () => cleanup("keep");
      $("modal-btn-replace").onclick = () => cleanup("replace");
      modal.onkeydown = (event) => {
        event.stopPropagation();
        if (event.key === "Escape") cleanup("cancel");
        if (event.key === "Tab") {
          const buttons = [$("modal-btn-replace"), $("modal-btn-keep"), $("modal-btn-cancel")];
          const current = buttons.indexOf(document.activeElement);
          const next = (current + (event.shiftKey ? -1 : 1) + buttons.length) % buttons.length;
          event.preventDefault();
          buttons[next].focus();
        }
      };
      $("modal-btn-replace").focus();
    });
  }

  async function runExport(selectedOnly) {
    if (!state.annotation || state.currentId == null || editingLocked()) return;
    try {
      const res = await apiJson(`/api/export/${state.currentId}`, {
        method: "POST",
        body: JSON.stringify({
          annotation: state.annotation,
          selected_ids: selectedOnly ? [...state.selectedIds] : null,
        }),
      });
      const box = $("export-result");
      box.textContent = `Đã xuất ${res.crop_count} ảnh crop → ${res.manifest_path}`;
      box.hidden = false;
      showToast(`Đã xuất ${res.crop_count} crop.`, "success");
    } catch (err) {
      showToast(`Xuất crop lỗi: ${err.message}`, "error");
    }
  }

  // ---------------------------------------------------------------------------
  // Canvas Rendering & Hit Testing
  // ---------------------------------------------------------------------------

  function observeCanvasResize() {
    const ro = new ResizeObserver(() => {
      const rect = canvas.getBoundingClientRect();
      const dpr = window.devicePixelRatio || 1;
      const w = Math.max(1, Math.round(rect.width * dpr));
      const h = Math.max(1, Math.round(rect.height * dpr));
      if (canvas.width !== w || canvas.height !== h) {
        canvas.width = w;
        canvas.height = h;
      }
      scheduleRender();
    });
    ro.observe($("canvas-stage"));
  }

  function fitView() {
    if (!state.annotation || !state.imageEl) return;
    const rect = canvas.getBoundingClientRect();
    if (rect.width < 10 || rect.height < 10) return;
    const vd = viewDims(
      state.annotation.image_width,
      state.annotation.image_height,
      state.annotation.orientation
    );
    state.zoom = Math.min((rect.width - 24) / vd.w, (rect.height - 24) / vd.h);
    state.zoom = Math.max(0.05, Math.min(8, state.zoom));
    state.panX = (rect.width - vd.w * state.zoom) / 2;
    state.panY = (rect.height - vd.h * state.zoom) / 2;
    $("zoom-readout").textContent = `${Math.round(state.zoom * 100)}%`;
    scheduleRender();
  }

  function zoomAt(factor, cssX, cssY) {
    const next = Math.max(0.05, Math.min(12, state.zoom * factor));
    const ratio = next / state.zoom;
    state.panX = cssX - (cssX - state.panX) * ratio;
    state.panY = cssY - (cssY - state.panY) * ratio;
    state.zoom = next;
    $("zoom-readout").textContent = `${Math.round(state.zoom * 100)}%`;
    scheduleRender();
  }

  function scheduleRender() {
    if (state.rafId) return;
    state.rafId = requestAnimationFrame(() => {
      state.rafId = 0;
      render();
    });
  }

  function render() {
    const dpr = window.devicePixelRatio || 1;
    const cssW = canvas.width / dpr;
    const cssH = canvas.height / dpr;

    ctx.save();
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    ctx.clearRect(0, 0, cssW, cssH);

    const ann = state.annotation;
    const img = state.imageEl;
    if (!ann || !img) {
      ctx.restore();
      return;
    }

    const W = ann.image_width;
    const H = ann.image_height;
    const ori = ann.orientation || 0;

    // 1. Draw scan with transform (no large resized bitmap allocation)
    ctx.save();
    ctx.translate(state.panX, state.panY);
    ctx.scale(state.zoom, state.zoom);
    if (ori === 90) {
      ctx.translate(H, 0);
      ctx.rotate(Math.PI / 2);
    } else if (ori === 180) {
      ctx.translate(W, H);
      ctx.rotate(Math.PI);
    } else if (ori === 270) {
      ctx.translate(0, W);
      ctx.rotate(-Math.PI / 2);
    }
    ctx.drawImage(img, 0, 0);
    ctx.restore();

    // 2. Raw OCR overlays (cyan dashed)
    if ($("chk-show-raw").checked) {
      ctx.save();
      ctx.strokeStyle = "#0891b2";
      ctx.lineWidth = 1.25;
      ctx.setLineDash([4, 3]);
      for (const det of ann.raw_detections) {
        if (det.poly && det.poly.length >= 3) {
          ctx.beginPath();
          det.poly.forEach((pt, idx) => {
            const vp = origToViewPoint(pt[0], pt[1], W, H, ori);
            const cp = viewToCss(vp.x, vp.y);
            if (idx === 0) ctx.moveTo(cp.x, cp.y);
            else ctx.lineTo(cp.x, cp.y);
          });
          ctx.closePath();
          ctx.stroke();
        } else if (det.bbox) {
          const vb = origToViewBox(det.bbox, W, H, ori);
          const p1 = viewToCss(vb[0], vb[1]);
          const p2 = viewToCss(vb[2], vb[3]);
          ctx.strokeRect(p1.x, p1.y, p2.x - p1.x, p2.y - p1.y);
        }
      }
      ctx.restore();
    }

    // 3. Corrected regions (teal / label color; amber when selected)
    ctx.font = '600 11px "Segoe UI", system-ui, sans-serif';
    ctx.textBaseline = "middle";
    for (const reg of ann.regions) {
      const selected = state.selectedIds.has(reg.id);
      const vb = origToViewBox(reg.bbox, W, H, ori);
      const p1 = viewToCss(vb[0], vb[1]);
      const p2 = viewToCss(vb[2], vb[3]);
      const rw = p2.x - p1.x;
      const rh = p2.y - p1.y;

      const color = selected ? "#d97706" : LABEL_COLORS[reg.label] || "#0f766e";
      ctx.fillStyle = selected ? "rgba(217, 119, 6, 0.16)" : "rgba(15, 118, 110, 0.08)";
      ctx.fillRect(p1.x, p1.y, rw, rh);
      ctx.strokeStyle = color;
      ctx.lineWidth = selected ? 2.5 : 1.75;
      ctx.strokeRect(p1.x, p1.y, rw, rh);

      const badge = selected || rw >= 120
        ? `#${reg.reading_order} ${LABEL_VI[reg.label] || reg.label}`
        : `#${reg.reading_order}`;
      const tw = ctx.measureText(badge).width + 10;
      const th = 18;
      const by = p1.y >= th + 2 ? p1.y - th : p1.y;
      ctx.fillStyle = color;
      ctx.fillRect(p1.x, by, tw, th);
      ctx.fillStyle = "#ffffff";
      ctx.fillText(badge, p1.x + 5, by + th / 2);

      if (selected && state.selectedIds.size === 1) {
        const handles = getHandleCoords(vb[0], vb[1], vb[2], vb[3]);
        for (const hName of HANDLE_NAMES) {
          const [hx, hy] = handles[hName];
          const ch = viewToCss(hx, hy);
          ctx.fillStyle = "#ffffff";
          ctx.strokeStyle = "#d97706";
          ctx.lineWidth = 1.5;
          ctx.fillRect(ch.x - 4, ch.y - 4, 8, 8);
          ctx.strokeRect(ch.x - 4, ch.y - 4, 8, 8);
        }
      }
    }

    // 4. Rubber-band marquee / draw preview
    if (state.drag && (state.drag.type === "draw" || state.drag.type === "marquee" || state.drag.type === "bulk_number")) {
      const x = Math.min(state.drag.cx0, state.drag.cx1);
      const y = Math.min(state.drag.cy0, state.drag.cy1);
      const w = Math.abs(state.drag.cx1 - state.drag.cx0);
      const h = Math.abs(state.drag.cy1 - state.drag.cy0);
      ctx.save();
      ctx.strokeStyle = state.drag.type === "bulk_number" ? "#d97706" : "#0f766e";
      ctx.fillStyle = state.drag.type === "bulk_number" ? "rgba(217, 119, 6, 0.12)" : "rgba(15, 118, 110, 0.12)";
      ctx.lineWidth = 1.5;
      ctx.setLineDash([5, 3]);
      ctx.fillRect(x, y, w, h);
      ctx.strokeRect(x, y, w, h);
      ctx.restore();
    }

    ctx.restore();
  }

  function hitTestHandle(cssX, cssY) {
    if (!state.annotation || state.selectedIds.size !== 1) return null;
    const reg = state.annotation.regions.find((r) => state.selectedIds.has(r.id));
    if (!reg) return null;
    const vb = origToViewBox(
      reg.bbox,
      state.annotation.image_width,
      state.annotation.image_height,
      state.annotation.orientation
    );
    const handles = getHandleCoords(vb[0], vb[1], vb[2], vb[3]);
    for (const hName of HANDLE_NAMES) {
      const ch = viewToCss(handles[hName][0], handles[hName][1]);
      if (Math.abs(cssX - ch.x) <= 7 && Math.abs(cssY - ch.y) <= 7) return hName;
    }
    return null;
  }

  function hitTestRegion(vx, vy) {
    if (!state.annotation) return null;
    const W = state.annotation.image_width;
    const H = state.annotation.image_height;
    const ori = state.annotation.orientation;
    for (let i = state.annotation.regions.length - 1; i >= 0; i--) {
      const reg = state.annotation.regions[i];
      const vb = origToViewBox(reg.bbox, W, H, ori);
      if (vx >= vb[0] && vx <= vb[2] && vy >= vb[1] && vy <= vb[3]) return reg;
    }
    return null;
  }

  // ---------------------------------------------------------------------------
  // Pointer Handlers
  // ---------------------------------------------------------------------------

  function onPointerDown(ev) {
    canvas.focus();
    const rect = canvas.getBoundingClientRect();
    const cx = ev.clientX - rect.left;
    const cy = ev.clientY - rect.top;

    if (ev.button === 1 || (ev.button === 0 && state.mode === "pan")) {
      canvas.setPointerCapture(ev.pointerId);
      state.drag = { type: "pan", cx0: cx, cy0: cy, panX0: state.panX, panY0: state.panY };
      canvas.style.cursor = "grabbing";
      return;
    }

    if (ev.button !== 0 || !state.annotation || editingLocked()) return;
    const vp = cssToView(cx, cy);

    if (state.mode === "split_h" || state.mode === "split_v") {
      const hit = hitTestRegion(vp.x, vp.y);
      if (!hit) {
        showToast("Click vào bên trong một vùng để chia.", "info");
        return;
      }
      const W = state.annotation.image_width;
      const H = state.annotation.image_height;
      const ori = state.annotation.orientation;
      const origPt = viewToOrigPoint(vp.x, vp.y, W, H, ori);
      const viewAxis = state.mode === "split_h" ? "horizontal" : "vertical";
      const origAxis =
        ori === 90 || ori === 270
          ? viewAxis === "horizontal"
            ? "vertical"
            : "horizontal"
          : viewAxis;
      const splitCoord = origAxis === "horizontal" ? origPt.y : origPt.x;
      state.selectedIds = new Set([hit.id]);
      runServerAction("split", { axis: origAxis, split_coord: splitCoord });
      return;
    }

    canvas.setPointerCapture(ev.pointerId);

    if (state.mode === "draw" || state.mode === "bulk_number") {
      state.drag = { type: state.mode, cx0: cx, cy0: cy, cx1: cx, cy1: cy, vx0: vp.x, vy0: vp.y };
      return;
    }

    const handle = hitTestHandle(cx, cy);
    if (handle) {
      const reg = state.annotation.regions.find((r) => state.selectedIds.has(r.id));
      const vb = origToViewBox(
        reg.bbox,
        state.annotation.image_width,
        state.annotation.image_height,
        state.annotation.orientation
      );
      state.drag = {
        type: "resize",
        role: handle,
        regId: reg.id,
        vx0: vp.x,
        vy0: vp.y,
        startVb: vb,
        snapshot: cloneRegions(state.annotation.regions),
        changed: false,
      };
      return;
    }

    const hit = hitTestRegion(vp.x, vp.y);
    if (hit) {
      if (ev.shiftKey) {
        if (state.selectedIds.has(hit.id)) state.selectedIds.delete(hit.id);
        else state.selectedIds.add(hit.id);
        syncAfterRegionChange();
        return;
      }
      if (!state.selectedIds.has(hit.id)) {
        state.selectedIds = new Set([hit.id]);
        syncAfterRegionChange();
      }
      const vb = origToViewBox(
        hit.bbox,
        state.annotation.image_width,
        state.annotation.image_height,
        state.annotation.orientation
      );
      state.drag = {
        type: "move",
        regId: hit.id,
        vx0: vp.x,
        vy0: vp.y,
        startVb: vb,
        snapshot: cloneRegions(state.annotation.regions),
        changed: false,
      };
      return;
    }

    if (!ev.shiftKey) {
      state.selectedIds.clear();
      syncAfterRegionChange();
    }
    state.drag = {
      type: "marquee",
      cx0: cx,
      cy0: cy,
      cx1: cx,
      cy1: cy,
      vx0: vp.x,
      vy0: vp.y,
      shift: ev.shiftKey,
    };
  }

  function onPointerMove(ev) {
    const rect = canvas.getBoundingClientRect();
    const cx = ev.clientX - rect.left;
    const cy = ev.clientY - rect.top;

    if (!state.drag) {
      if (state.mode === "pan") canvas.style.cursor = "grab";
      else if (state.mode !== "select") canvas.style.cursor = "crosshair";
      else {
        const h = hitTestHandle(cx, cy);
        canvas.style.cursor = h ? HANDLE_CURSORS[h] : "default";
      }
      return;
    }

    if (state.drag.type === "pan") {
      state.panX = state.drag.panX0 + (cx - state.drag.cx0);
      state.panY = state.drag.panY0 + (cy - state.drag.cy0);
      scheduleRender();
      return;
    }

    if (state.drag.type === "draw" || state.drag.type === "marquee" || state.drag.type === "bulk_number") {
      state.drag.cx1 = cx;
      state.drag.cy1 = cy;
      scheduleRender();
      return;
    }

    if (!state.annotation) return;
    const vp = cssToView(cx, cy);
    const dx = vp.x - state.drag.vx0;
    const dy = vp.y - state.drag.vy0;
    const [sx1, sy1, sx2, sy2] = state.drag.startVb;
    const W = state.annotation.image_width;
    const H = state.annotation.image_height;
    const ori = state.annotation.orientation;

    let newVb;
    if (state.drag.type === "move") {
      newVb = [sx1 + dx, sy1 + dy, sx2 + dx, sy2 + dy];
      const origBox = clampMoveOrigBox(viewToOrigBox(newVb, W, H, ori), W, H);
      const reg = state.annotation.regions.find((r) => r.id === state.drag.regId);
      if (reg) reg.bbox = origBox;
    } else if (state.drag.type === "resize") {
      let [x1, y1, x2, y2] = [sx1, sy1, sx2, sy2];
      const role = state.drag.role;
      if (role.includes("w")) x1 = Math.min(x2 - 6, x1 + dx);
      if (role.includes("e")) x2 = Math.max(x1 + 6, x2 + dx);
      if (role.includes("n")) y1 = Math.min(y2 - 6, y1 + dy);
      if (role.includes("s")) y2 = Math.max(y1 + 6, y2 + dy);
      const origBox = clampOrigBox(viewToOrigBox([x1, y1, x2, y2], W, H, ori), W, H);
      const reg = state.annotation.regions.find((r) => r.id === state.drag.regId);
      if (reg) reg.bbox = origBox;
    }

    state.drag.changed = true;
    scheduleRender();
  }

  function onPointerUp(ev) {
    if (!state.drag) return;
    const drag = state.drag;
    state.drag = null;
    if (canvas.hasPointerCapture(ev.pointerId)) {
      canvas.releasePointerCapture(ev.pointerId);
    }

    if (drag.type === "pan") {
      canvas.style.cursor = state.mode === "pan" ? "grab" : "default";
      return;
    }

    if (!state.annotation) return;
    const rect = canvas.getBoundingClientRect();
    const cx = ev.clientX - rect.left;
    const cy = ev.clientY - rect.top;
    const vp = cssToView(cx, cy);
    const vrect = [
      Math.min(drag.vx0, vp.x),
      Math.min(drag.vy0, vp.y),
      Math.max(drag.vx0, vp.x),
      Math.max(drag.vy0, vp.y),
    ];

    if (drag.type === "draw") {
      if (Math.abs(drag.cx1 - drag.cx0) >= 6 && Math.abs(drag.cy1 - drag.cy0) >= 6) {
        const W = state.annotation.image_width;
        const H = state.annotation.image_height;
        const origBox = clampOrigBox(viewToOrigBox(vrect, W, H, state.annotation.orientation), W, H);
        if (origBox[2] - origBox[0] >= 4 && origBox[3] - origBox[1] >= 4) {
          pushUndoSnapshot();
          const maxOrder = state.annotation.regions.reduce((m, r) => Math.max(m, r.reading_order || 0), 0);
          const newReg = {
            id: crypto.randomUUID(),
            bbox: origBox,
            label: $("prop-label").value || "body",
            language: $("prop-language").value || "han",
            content_role: $("prop-role").value || "original",
            direction: $("prop-direction").value || ($("grp-mode").value === "horizontal_lines" ? "horizontal" : "vertical"),
            reading_order: maxOrder + 1,
            review_state: "pending",
            notes: "",
          };
          state.annotation.regions.push(newReg);
          state.selectedIds = new Set([newReg.id]);
          markDirty();
          syncAfterRegionChange();
        }
      }
      scheduleRender();
      return;
    }

    if (drag.type === "marquee" || drag.type === "bulk_number") {
      const W = state.annotation.image_width;
      const H = state.annotation.image_height;
      const ori = state.annotation.orientation;
      const hits = new Set();
      for (const reg of state.annotation.regions) {
        const vb = origToViewBox(reg.bbox, W, H, ori);
        if (!(vb[2] < vrect[0] || vb[0] > vrect[2] || vb[3] < vrect[1] || vb[1] > vrect[3])) {
          hits.add(reg.id);
        }
      }
      if (drag.type === "marquee") {
        state.selectedIds = drag.shift ? new Set([...state.selectedIds, ...hits]) : hits;
        syncAfterRegionChange();
      } else {
        state.selectedIds = hits;
        syncAfterRegionChange();
        if (hits.size > 0) runServerAction("number");
      }
      return;
    }

    if ((drag.type === "move" || drag.type === "resize") && drag.changed) {
      state.undoStack.push(drag.snapshot);
      state.redoStack = [];
      markDirty();
      syncAfterRegionChange();
    }
  }

  // ---------------------------------------------------------------------------
  // Event Wiring & Shortcuts
  // ---------------------------------------------------------------------------

  function setMode(mode) {
    state.mode = mode;
    document.querySelectorAll(".btn-tool").forEach((b) => {
      b.classList.toggle("active", b.dataset.mode === mode);
    });
    canvas.style.cursor = mode === "pan" ? "grab" : mode === "select" ? "default" : "crosshair";
  }

  function rotateView(delta) {
    if (!state.annotation || editingLocked()) return;
    state.annotation.orientation = (((state.annotation.orientation || 0) + delta) % 360 + 360) % 360;
    markDirty();
    populatePageMetadata();
    fitView();
    updateBottomInfo();
  }

  function deleteSelected() {
    if (!state.annotation || state.selectedIds.size === 0 || editingLocked()) return;
    pushUndoSnapshot();
    state.annotation.regions = state.annotation.regions.filter((r) => !state.selectedIds.has(r.id));
    state.selectedIds.clear();
    markDirty();
    syncAfterRegionChange();
  }

  function navigateBy(delta) {
    if (state.images.length === 0 || state.currentId == null) return;
    const idx = state.images.findIndex((i) => i.id === state.currentId);
    const next = Math.max(0, Math.min(state.images.length - 1, idx + delta));
    if (next !== idx) selectImage(state.images[next].id);
  }

  function bindEvents() {
    $("doc-search").addEventListener("input", renderDocTree);
    $("viewer-banner-close").addEventListener("click", hideBanner);

    $("btn-toggle-sidebar").addEventListener("click", () => $("sidebar").classList.toggle("collapsed"));
    $("btn-toggle-inspector").addEventListener("click", () => $("inspector").classList.toggle("collapsed"));

    document.querySelectorAll(".tab-btn").forEach((btn) => {
      btn.addEventListener("click", () => {
        const tab = btn.dataset.tab;
        document.querySelectorAll(".tab-btn").forEach((b) => {
          const active = b.dataset.tab === tab;
          b.classList.toggle("active", active);
          b.setAttribute("aria-selected", String(active));
        });
        document.querySelectorAll(".tab-pane").forEach((p) => {
          const active = p.dataset.pane === tab;
          p.classList.toggle("active", active);
          p.hidden = !active;
        });
      });
    });

    document.querySelectorAll(".btn-tool").forEach((btn) => {
      btn.addEventListener("click", () => setMode(btn.dataset.mode));
    });

    $("btn-save").addEventListener("click", () => saveCurrent(true));
    $("btn-detect").addEventListener("click", runDetection);
    $("btn-prev").addEventListener("click", () => navigateBy(-1));
    $("btn-next").addEventListener("click", () => navigateBy(1));
    $("btn-undo").addEventListener("click", undo);
    $("btn-redo").addEventListener("click", redo);
    $("btn-merge").addEventListener("click", () => {
      if (state.selectedIds.size < 2) {
        showToast("Chọn ít nhất 2 vùng để gộp.", "info");
        return;
      }
      runServerAction("merge");
    });
    $("btn-split-h-mid").addEventListener("click", () => {
      if (state.selectedIds.size !== 1 || !state.annotation) {
        showToast("Chọn đúng 1 vùng để cắt ngang.", "info");
        return;
      }
      const ori = state.annotation.orientation;
      const origAxis = ori === 90 || ori === 270 ? "vertical" : "horizontal";
      runServerAction("split", { axis: origAxis });
    });
    $("btn-split-v-mid").addEventListener("click", () => {
      if (state.selectedIds.size !== 1 || !state.annotation) {
        showToast("Chọn đúng 1 vùng để cắt dọc.", "info");
        return;
      }
      const ori = state.annotation.orientation;
      const origAxis = ori === 90 || ori === 270 ? "horizontal" : "vertical";
      runServerAction("split", { axis: origAxis });
    });
    $("btn-delete").addEventListener("click", deleteSelected);
    $("btn-rot-l").addEventListener("click", () => rotateView(-90));
    $("btn-rot-r").addEventListener("click", () => rotateView(90));
    $("btn-fit").addEventListener("click", fitView);
    $("btn-zoom-in").addEventListener("click", () => {
      const r = canvas.getBoundingClientRect();
      zoomAt(1.25, r.width / 2, r.height / 2);
    });
    $("btn-zoom-out").addEventListener("click", () => {
      const r = canvas.getBoundingClientRect();
      zoomAt(0.8, r.width / 2, r.height / 2);
    });
    $("chk-show-raw").addEventListener("change", scheduleRender);

    $("btn-apply-props").addEventListener("click", applyInspectorProps);
    $("btn-number-selected").addEventListener("click", () => {
      if (state.selectedIds.size === 0) {
        showToast("Chưa chọn vùng nào để đánh số.", "info");
        return;
      }
      runServerAction("number");
    });
    $("btn-number-all").addEventListener("click", () => {
      if (!state.annotation) return;
      state.selectedIds = new Set(state.annotation.regions.map((r) => r.id));
      syncAfterRegionChange();
      runServerAction("number");
    });
    $("btn-regroup").addEventListener("click", () => {
      if (!state.annotation || state.annotation.raw_detections.length === 0) {
        showToast("Chưa có raw detections để gom lại.", "info");
        return;
      }
      runServerAction("group");
    });

    $("page-review-state").addEventListener("change", () => {
      if (!state.annotation || editingLocked()) return;
      state.annotation.page_review_state = $("page-review-state").value;
      updateTreeSelectionAndDot(state.currentId, state.annotation.page_review_state);
      markDirty();
    });
    $("page-notes").addEventListener("input", () => {
      if (!state.annotation || editingLocked()) return;
      state.annotation.page_notes = $("page-notes").value;
      markDirty();
    });

    $("btn-export-selected").addEventListener("click", () => {
      if (state.selectedIds.size === 0) {
        showToast("Chưa chọn vùng nào để xuất.", "info");
        return;
      }
      runExport(true);
    });
    $("btn-export-content").addEventListener("click", () => runExport(false));

    $("btn-help").addEventListener("click", () => ($("help-modal").hidden = false));
    $("help-modal-close").addEventListener("click", () => ($("help-modal").hidden = true));

    canvas.addEventListener("pointerdown", onPointerDown);
    canvas.addEventListener("pointermove", onPointerMove);
    canvas.addEventListener("pointerup", onPointerUp);
    canvas.addEventListener("pointercancel", onPointerUp);
    canvas.addEventListener(
      "wheel",
      (ev) => {
        ev.preventDefault();
        const rect = canvas.getBoundingClientRect();
        zoomAt(ev.deltaY < 0 ? 1.15 : 0.87, ev.clientX - rect.left, ev.clientY - rect.top);
      },
      { passive: false }
    );

    window.addEventListener("keydown", (ev) => {
      if (ev.ctrlKey && ev.key.toLowerCase() === "s") {
        ev.preventDefault();
        saveCurrent(true);
        return;
      }
      const tag = ev.target && ev.target.tagName;
      if (tag === "INPUT" || tag === "TEXTAREA" || tag === "SELECT") return;

      if (ev.ctrlKey && ev.key.toLowerCase() === "z" && !ev.shiftKey) {
        ev.preventDefault();
        undo();
      } else if (ev.ctrlKey && (ev.key.toLowerCase() === "y" || (ev.shiftKey && ev.key.toLowerCase() === "z"))) {
        ev.preventDefault();
        redo();
      } else if (ev.key === "PageUp") {
        ev.preventDefault();
        navigateBy(-1);
      } else if (ev.key === "PageDown") {
        ev.preventDefault();
        navigateBy(1);
      } else if (ev.key === "Delete") {
        ev.preventDefault();
        deleteSelected();
      } else if (ev.key === "v" || ev.key === "V") setMode("select");
      else if (ev.key === "n" || ev.key === "N") setMode("draw");
      else if (ev.key === "b" || ev.key === "B") setMode("bulk_number");
      else if (ev.key === "m" || ev.key === "M") $("btn-merge").click();
      else if (ev.key === "f" || ev.key === "F") fitView();
      else if (ev.key === "[") rotateView(-90);
      else if (ev.key === "]") rotateView(90);
      else if (ev.key === "+" || ev.key === "=") $("btn-zoom-in").click();
      else if (ev.key === "-") $("btn-zoom-out").click();
      else if (ev.key === "?") $("help-modal").hidden = !$("help-modal").hidden;
      else if (ev.key === "Escape") $("help-modal").hidden = true;
    });

    window.addEventListener("beforeunload", (ev) => {
      if (state.dirty && !state.corruptLocked) {
        ev.preventDefault();
        ev.returnValue = "";
      }
    });
  }

  init();
})();
