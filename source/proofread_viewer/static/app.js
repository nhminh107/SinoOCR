"use strict";

(function () {
  const datasetSelect = document.getElementById("dataset-select");
  const pageSelect = document.getElementById("page-select");
  const pageCounter = document.getElementById("page-counter");
  const prevPageBtn = document.getElementById("prev-page-btn");
  const nextPageBtn = document.getElementById("next-page-btn");

  const zoomOutBtn = document.getElementById("zoom-out-btn");
  const zoomFitBtn = document.getElementById("zoom-fit-btn");
  const zoomInBtn = document.getElementById("zoom-in-btn");
  const zoomLabel = document.getElementById("zoom-label");

  const fontDecBtn = document.getElementById("font-dec-btn");
  const fontIncBtn = document.getElementById("font-inc-btn");
  const reloadBtn = document.getElementById("reload-btn");
  const saveAllBtn = document.getElementById("save-all-btn");
  const globalStatus = document.getElementById("global-status");

  const workspace = document.getElementById("workspace");
  const imageViewport = document.getElementById("image-viewport");
  const imageStatus = document.getElementById("image-status");
  const pageImage = document.getElementById("page-image");
  const imageFilename = document.getElementById("image-filename");

  const hanEditor = document.getElementById("han-editor");
  const hanSourceBadge = document.getElementById("han-source-badge");
  const hanBadge = document.getElementById("han-badge");
  const hanMeta = document.getElementById("han-meta");
  const hanPathBar = document.getElementById("han-path-bar");
  const saveHanBtn = document.getElementById("save-han-btn");

  const vietEditor = document.getElementById("viet-editor");
  const vietSourceBadge = document.getElementById("viet-source-badge");
  const vietBadge = document.getElementById("viet-badge");
  const vietMeta = document.getElementById("viet-meta");
  const vietPathBar = document.getElementById("viet-path-bar");
  const saveVietBtn = document.getElementById("save-viet-btn");

  const pageNote = document.getElementById("page-note");
  const noteBadge = document.getElementById("note-badge");
  const notePath = document.getElementById("note-path");
  const saveNoteBtn = document.getElementById("save-note-btn");

  const state = {
    datasets: [],
    currentDataset: "",
    pages: [],
    currentPageIndex: -1,
    savedHan: "",
    savedViet: "",
    savedNote: "",
    zoomMode: "fit",
    zoomScale: 1,
    fontSize: 16,
    saving: false,
    requestToken: 0,
    colFractions: [1.15, 1, 1],
  };

  function setGlobalStatus(message, kind) {
    globalStatus.textContent = message || "";
    globalStatus.classList.remove("is-ok", "is-dirty", "is-error");
    if (kind === "ok") globalStatus.classList.add("is-ok");
    if (kind === "dirty") globalStatus.classList.add("is-dirty");
    if (kind === "error") globalStatus.classList.add("is-error");
  }

  function setSourceBadge(el, source) {
    el.classList.remove("is-03", "is-02", "is-missing");
    if (source === "03_text_sach") {
      el.textContent = "03_text_sach";
      el.classList.add("is-03");
    } else if (source === "02_ocr_tho") {
      el.textContent = "Từ 02_ocr_tho";
      el.classList.add("is-02");
    } else {
      el.textContent = "Chưa có file";
      el.classList.add("is-missing");
    }
  }

  function isHanDirty() {
    return hanEditor.value !== state.savedHan;
  }

  function isVietDirty() {
    return vietEditor.value !== state.savedViet;
  }

  function isNoteDirty() {
    return pageNote.value !== state.savedNote;
  }

  function hasUnsavedChanges() {
    return isHanDirty() || isVietDirty() || isNoteDirty();
  }

  function updateDirtyUI() {
    const hanDirty = isHanDirty();
    const vietDirty = isVietDirty();
    const noteDirty = isNoteDirty();
    hanBadge.hidden = !hanDirty;
    vietBadge.hidden = !vietDirty;
    noteBadge.hidden = !noteDirty;

    if (hanDirty || vietDirty || noteDirty) {
      const names = [];
      if (hanDirty) names.push("Hán");
      if (vietDirty) names.push("Việt");
      if (noteDirty) names.push("Note");
      setGlobalStatus(`Chưa lưu: ${names.join(", ")}`, "dirty");
    } else if (globalStatus.classList.contains("is-dirty")) {
      setGlobalStatus("", "");
    }
  }

  function updateEditorMeta(editorEl, metaEl) {
    const text = editorEl.value;
    const totalLines = text.length === 0 ? 0 : text.split("\n").length;
    const pos = editorEl.selectionStart || 0;
    const currentLine = text.slice(0, pos).split("\n").length;
    metaEl.textContent = `Dòng ${currentLine} / ${totalLines}`;
  }

  function applyFontSize() {
    document.documentElement.style.setProperty("--editor-font-size", `${state.fontSize}px`);
  }

  function applyZoom() {
    if (state.zoomMode === "fit" || !pageImage.naturalWidth) {
      imageViewport.classList.remove("zoomed");
      pageImage.classList.add("fit-mode");
      pageImage.style.width = "";
      pageImage.style.height = "";
      zoomLabel.textContent = "Vừa khung";
      return;
    }
    imageViewport.classList.add("zoomed");
    pageImage.classList.remove("fit-mode");
    const width = Math.max(80, Math.round(pageImage.naturalWidth * state.zoomScale));
    pageImage.style.width = `${width}px`;
    pageImage.style.height = "auto";
    zoomLabel.textContent = `${Math.round(state.zoomScale * 100)}%`;
  }

  function stepZoom(direction) {
    if (!pageImage.naturalWidth) return;
    if (state.zoomMode === "fit") {
      const renderedWidth = pageImage.getBoundingClientRect().width || pageImage.naturalWidth;
      state.zoomScale = renderedWidth / pageImage.naturalWidth;
      state.zoomMode = "manual";
    }
    const factor = direction > 0 ? 1.2 : 1 / 1.2;
    state.zoomScale = Math.min(5, Math.max(0.2, state.zoomScale * factor));
    applyZoom();
  }

  function formatPageOptionLabel(page) {
    const tags = [];
    if (page.has_han_03) tags.push("H");
    if (page.has_viet_03) tags.push("V");
    const parts = [page.filename];
    if (tags.length > 0) parts.push(`[03:${tags.join("+")}]`);
    if (page.has_note) parts.push("[Note]");
    return parts.join(" ");
  }

  function renderPageOptions() {
    pageSelect.innerHTML = "";
    state.pages.forEach((p, idx) => {
      const opt = document.createElement("option");
      opt.value = String(idx);
      opt.textContent = formatPageOptionLabel(p);
      pageSelect.appendChild(opt);
    });
    if (state.currentPageIndex >= 0 && state.currentPageIndex < state.pages.length) {
      pageSelect.value = String(state.currentPageIndex);
    }
  }

  async function loadPage(index, skipConfirm) {
    if (!skipConfirm && hasUnsavedChanges()) {
      const ok = window.confirm("Trang hiện tại có thay đổi chưa lưu. Chuyển trang sẽ bỏ các thay đổi này?");
      if (!ok) {
        pageSelect.value = String(state.currentPageIndex);
        return;
      }
    }

    if (index < 0 || index >= state.pages.length) {
      state.currentPageIndex = -1;
      pageImage.hidden = true;
      imageFilename.textContent = "Không có ảnh";
      pageCounter.textContent = "0 / 0";
      prevPageBtn.disabled = true;
      nextPageBtn.disabled = true;
      hanEditor.value = "";
      vietEditor.value = "";
      pageNote.value = "";
      state.savedHan = "";
      state.savedViet = "";
      state.savedNote = "";
      updateDirtyUI();
      return;
    }

    state.currentPageIndex = index;
    const page = state.pages[index];
    pageSelect.value = String(index);
    pageCounter.textContent = `${index + 1} / ${state.pages.length}`;
    imageFilename.textContent = page.filename;
    prevPageBtn.disabled = index <= 0;
    nextPageBtn.disabled = index >= state.pages.length - 1;

    imageStatus.textContent = "Đang tải ảnh...";
    imageStatus.hidden = false;

    const imgUrl = `/api/image?dataset=${encodeURIComponent(state.currentDataset)}&page=${encodeURIComponent(page.id)}`;
    pageImage.onload = function () {
      imageStatus.hidden = true;
      pageImage.hidden = false;
      applyZoom();
    };
    pageImage.onerror = function () {
      imageStatus.textContent = `Không tải được ảnh ${page.filename}`;
      imageStatus.hidden = false;
      pageImage.hidden = true;
    };
    pageImage.src = imgUrl;

    const token = ++state.requestToken;
    setGlobalStatus("Đang tải...", "");
    try {
      const res = await fetch(
        `/api/page?dataset=${encodeURIComponent(state.currentDataset)}&page=${encodeURIComponent(page.id)}`
      );
      const data = await res.json();
      if (token !== state.requestToken) return;
      if (!res.ok) {
        throw new Error(data.error || "Không thể tải nội dung trang.");
      }

      state.savedHan = data.han.text || "";
      state.savedViet = data.viet.text || "";
      state.savedNote = data.note || "";
      hanEditor.value = state.savedHan;
      vietEditor.value = state.savedViet;
      pageNote.value = state.savedNote;
      notePath.textContent = data.note_path || "nhat_ky_kiem_tra_han_viet.txt";

      setSourceBadge(hanSourceBadge, data.han.source);
      setSourceBadge(vietSourceBadge, data.viet.source);

      hanPathBar.textContent =
        data.han.source === "03_text_sach"
          ? data.han.target_path
          : `${data.han.target_path} (đang hiển thị từ ${data.han.source === "02_ocr_tho" ? "02_ocr_tho" : "trống"} — lưu sẽ ghi vào 03)`;
      vietPathBar.textContent =
        data.viet.source === "03_text_sach"
          ? data.viet.target_path
          : `${data.viet.target_path} (đang hiển thị từ ${data.viet.source === "02_ocr_tho" ? "02_ocr_tho" : "trống"} — lưu sẽ ghi vào 03)`;

      updateEditorMeta(hanEditor, hanMeta);
      updateEditorMeta(vietEditor, vietMeta);
      updateDirtyUI();
      setGlobalStatus("", "");
    } catch (err) {
      if (token !== state.requestToken) return;
      setGlobalStatus(err.message || "Lỗi tải trang", "error");
    }
  }

  async function loadDataset(datasetId, preservePageIndex) {
    setGlobalStatus("Đang tải tác phẩm...", "");
    try {
      const res = await fetch(`/api/pages?dataset=${encodeURIComponent(datasetId)}`);
      const data = await res.json();
      if (!res.ok) {
        throw new Error(data.error || "Không thể tải danh sách trang.");
      }

      state.currentDataset = data.dataset;
      state.pages = data.pages || [];
      renderPageOptions();

      if (state.pages.length > 0) {
        const targetIndex =
          typeof preservePageIndex === "number" && preservePageIndex >= 0 && preservePageIndex < state.pages.length
            ? preservePageIndex
            : 0;
        await loadPage(targetIndex, true);
      } else {
        await loadPage(-1, true);
      }
    } catch (err) {
      setGlobalStatus(err.message || "Lỗi tải tác phẩm", "error");
    }
  }

  async function saveCurrentPage(which) {
    if (!state.currentDataset || state.currentPageIndex < 0 || state.saving) return;
    const page = state.pages[state.currentPageIndex];

    const payload = { dataset: state.currentDataset, page: page.id };
    if (which === "han") {
      payload.han_text = hanEditor.value;
    } else if (which === "viet") {
      payload.viet_text = vietEditor.value;
    } else if (which === "note") {
      payload.note = pageNote.value;
    } else {
      const hanDirty = isHanDirty();
      const vietDirty = isVietDirty();
      const noteDirty = isNoteDirty();
      if (hanDirty || (!vietDirty && !noteDirty)) payload.han_text = hanEditor.value;
      if (vietDirty || (!hanDirty && !noteDirty)) payload.viet_text = vietEditor.value;
      if (noteDirty || (!hanDirty && !vietDirty)) payload.note = pageNote.value;
    }

    state.saving = true;
    saveAllBtn.disabled = true;
    saveHanBtn.disabled = true;
    saveVietBtn.disabled = true;
    saveNoteBtn.disabled = true;

    try {
      const res = await fetch("/api/save", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload),
      });
      const data = await res.json();
      if (!res.ok) {
        throw new Error(data.error || "Lưu thất bại.");
      }

      if (payload.han_text !== undefined) {
        state.savedHan = payload.han_text;
        page.has_han_03 = true;
        setSourceBadge(hanSourceBadge, "03_text_sach");
        hanPathBar.textContent = `submission/${state.currentDataset}/03_text_sach/han/${page.id}.txt`;
      }
      if (payload.viet_text !== undefined) {
        state.savedViet = payload.viet_text;
        page.has_viet_03 = true;
        setSourceBadge(vietSourceBadge, "03_text_sach");
        vietPathBar.textContent = `submission/${state.currentDataset}/03_text_sach/viet/${page.id}.txt`;
      }
      if (payload.note !== undefined) {
        state.savedNote = payload.note.trim();
        pageNote.value = state.savedNote;
        page.has_note = state.savedNote.length > 0;
      }

      renderPageOptions();
      updateDirtyUI();
      setGlobalStatus(`Đã lưu ${(data.saved || []).join(", ")} (${data.saved_at})`, "ok");
    } catch (err) {
      setGlobalStatus(err.message || "Lỗi khi lưu file", "error");
    } finally {
      state.saving = false;
      saveAllBtn.disabled = false;
      saveHanBtn.disabled = false;
      saveVietBtn.disabled = false;
      saveNoteBtn.disabled = false;
    }
  }

  // Pan image by dragging when zoomed
  let panActive = false;
  let panStartX = 0;
  let panStartY = 0;
  let panScrollLeft = 0;
  let panScrollTop = 0;

  imageViewport.addEventListener("mousedown", function (e) {
    if (state.zoomMode === "fit" || e.button !== 0) return;
    panActive = true;
    panStartX = e.clientX;
    panStartY = e.clientY;
    panScrollLeft = imageViewport.scrollLeft;
    panScrollTop = imageViewport.scrollTop;
    imageViewport.classList.add("is-panning");
  });

  window.addEventListener("mousemove", function (e) {
    if (!panActive) return;
    imageViewport.scrollLeft = panScrollLeft - (e.clientX - panStartX);
    imageViewport.scrollTop = panScrollTop - (e.clientY - panStartY);
  });

  window.addEventListener("mouseup", function () {
    if (!panActive) return;
    panActive = false;
    imageViewport.classList.remove("is-panning");
  });

  imageViewport.addEventListener(
    "wheel",
    function (e) {
      if (!e.ctrlKey) return;
      e.preventDefault();
      stepZoom(e.deltaY < 0 ? 1 : -1);
    },
    { passive: false }
  );

  // Resizable 3-column splitters
  function applyColumnTemplate() {
    const [a, b, c] = state.colFractions;
    workspace.style.gridTemplateColumns = `minmax(200px, ${a}fr) 6px minmax(200px, ${b}fr) 6px minmax(200px, ${c}fr)`;
  }

  document.querySelectorAll(".splitter").forEach((splitter) => {
    splitter.addEventListener("mousedown", function (e) {
      e.preventDefault();
      const idx = Number(splitter.dataset.splitter);
      const rect = workspace.getBoundingClientRect();
      const totalWidth = rect.width - 28;
      const startX = e.clientX;
      const startFractions = [...state.colFractions];
      const totalFr = startFractions[0] + startFractions[1] + startFractions[2];

      splitter.classList.add("is-dragging");

      function onMove(ev) {
        const dx = ev.clientX - startX;
        const deltaFr = (dx / totalWidth) * totalFr;
        const minFr = 0.35;
        let left = startFractions[idx] + deltaFr;
        let right = startFractions[idx + 1] - deltaFr;
        if (left < minFr) {
          right -= minFr - left;
          left = minFr;
        }
        if (right < minFr) {
          left -= minFr - right;
          right = minFr;
        }
        state.colFractions[idx] = left;
        state.colFractions[idx + 1] = right;
        applyColumnTemplate();
      }

      function onUp() {
        splitter.classList.remove("is-dragging");
        window.removeEventListener("mousemove", onMove);
        window.removeEventListener("mouseup", onUp);
      }

      window.addEventListener("mousemove", onMove);
      window.addEventListener("mouseup", onUp);
    });
  });

  // Event bindings
  datasetSelect.addEventListener("change", function () {
    const nextDs = datasetSelect.value;
    if (hasUnsavedChanges()) {
      const ok = window.confirm("Bạn có thay đổi chưa lưu. Chuyển tác phẩm sẽ bỏ các thay đổi này?");
      if (!ok) {
        datasetSelect.value = state.currentDataset;
        return;
      }
    }
    loadDataset(nextDs);
  });

  pageSelect.addEventListener("change", function () {
    loadPage(Number(pageSelect.value), false);
  });

  prevPageBtn.addEventListener("click", function () {
    if (state.currentPageIndex > 0) loadPage(state.currentPageIndex - 1, false);
  });

  nextPageBtn.addEventListener("click", function () {
    if (state.currentPageIndex < state.pages.length - 1) loadPage(state.currentPageIndex + 1, false);
  });

  zoomOutBtn.addEventListener("click", () => stepZoom(-1));
  zoomInBtn.addEventListener("click", () => stepZoom(1));
  zoomFitBtn.addEventListener("click", function () {
    state.zoomMode = "fit";
    applyZoom();
  });

  fontDecBtn.addEventListener("click", function () {
    state.fontSize = Math.max(12, state.fontSize - 1);
    applyFontSize();
  });

  fontIncBtn.addEventListener("click", function () {
    state.fontSize = Math.min(28, state.fontSize + 1);
    applyFontSize();
  });

  reloadBtn.addEventListener("click", function () {
    if (hasUnsavedChanges()) {
      const ok = window.confirm("Tải lại từ ổ đĩa sẽ ghi đè các thay đổi chưa lưu?");
      if (!ok) return;
    }
    if (state.currentPageIndex >= 0) {
      loadPage(state.currentPageIndex, true);
    }
  });

  saveAllBtn.addEventListener("click", () => saveCurrentPage("all"));
  saveHanBtn.addEventListener("click", () => saveCurrentPage("han"));
  saveVietBtn.addEventListener("click", () => saveCurrentPage("viet"));
  saveNoteBtn.addEventListener("click", () => saveCurrentPage("note"));
  pageNote.addEventListener("input", updateDirtyUI);

  hanEditor.addEventListener("input", function () {
    updateDirtyUI();
    updateEditorMeta(hanEditor, hanMeta);
  });
  hanEditor.addEventListener("click", () => updateEditorMeta(hanEditor, hanMeta));
  hanEditor.addEventListener("keyup", () => updateEditorMeta(hanEditor, hanMeta));

  vietEditor.addEventListener("input", function () {
    updateDirtyUI();
    updateEditorMeta(vietEditor, vietMeta);
  });
  vietEditor.addEventListener("click", () => updateEditorMeta(vietEditor, vietMeta));
  vietEditor.addEventListener("keyup", () => updateEditorMeta(vietEditor, vietMeta));

  window.addEventListener("keydown", function (e) {
    if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === "s") {
      e.preventDefault();
      saveCurrentPage("all");
      return;
    }
    if (e.altKey && e.key === "ArrowLeft") {
      e.preventDefault();
      if (state.currentPageIndex > 0) loadPage(state.currentPageIndex - 1, false);
      return;
    }
    if (e.altKey && e.key === "ArrowRight") {
      e.preventDefault();
      if (state.currentPageIndex < state.pages.length - 1) loadPage(state.currentPageIndex + 1, false);
    }
  });

  window.addEventListener("beforeunload", function (e) {
    if (hasUnsavedChanges()) {
      e.preventDefault();
      e.returnValue = "";
    }
  });

  async function init() {
    applyFontSize();
    try {
      const res = await fetch("/api/datasets");
      const data = await res.json();
      state.datasets = data.datasets || [];
      datasetSelect.innerHTML = "";
      state.datasets.forEach((ds) => {
        const opt = document.createElement("option");
        opt.value = ds.id;
        opt.textContent = `${ds.label} (${ds.page_count} trang)`;
        datasetSelect.appendChild(opt);
      });
      if (state.datasets.length > 0) {
        await loadDataset(state.datasets[0].id);
      } else {
        setGlobalStatus("Không tìm thấy tác phẩm trong submission/", "error");
      }
    } catch (err) {
      setGlobalStatus("Không kết nối được server", "error");
    }
  }

  init();
})();
