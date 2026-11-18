"use strict";

(function () {
  const datasetSelect = document.getElementById("dataset-select");
  const imageSelect = document.getElementById("image-select");
  const pageCounter = document.getElementById("page-counter");
  const prevBtn = document.getElementById("prev-btn");
  const nextBtn = document.getElementById("next-btn");

  const zoomOutBtn = document.getElementById("zoom-out-btn");
  const zoomFitBtn = document.getElementById("zoom-fit-btn");
  const zoomInBtn = document.getElementById("zoom-in-btn");
  const zoomLabel = document.getElementById("zoom-label");

  const reviewStatus = document.getElementById("review-status");
  const reviewNote = document.getElementById("review-note");
  const saveReviewBtn = document.getElementById("save-review-btn");
  const reviewFeedback = document.getElementById("review-feedback");

  const imageViewport = document.getElementById("image-viewport");
  const imageStatus = document.getElementById("image-status");
  const pageImage = document.getElementById("page-image");
  const imageFilename = document.getElementById("image-filename");

  const hanMeta = document.getElementById("han-meta");
  const hanStatus = document.getElementById("han-status");
  const hanText = document.getElementById("han-text");

  const vietMeta = document.getElementById("viet-meta");
  const vietStatus = document.getElementById("viet-status");
  const vietText = document.getElementById("viet-text");

  const state = {
    datasets: [],
    images: [],
    currentDataset: "",
    currentIndex: -1,
    zoomMode: "fit",
    zoomScale: 1,
    requestToken: 0,
    ocrController: null,
    savedHanText: "",
    savedVietText: "",
    savedReview: { status: "unreviewed", note: "" },
    isDirty: false,
    pageLoaded: false,
    saving: false,
  };

  function setStatus(el, message, isMissing) {
    if (!message) {
      el.hidden = true;
      el.textContent = "";
      el.classList.remove("is-missing");
      return;
    }
    el.hidden = false;
    el.textContent = message;
    el.classList.toggle("is-missing", Boolean(isMissing));
  }

  function setReviewFeedback(message, kind) {
    reviewFeedback.textContent = message || "";
    reviewFeedback.classList.remove("is-ok", "is-error", "is-dirty");
    if (kind === "ok") {
      reviewFeedback.classList.add("is-ok");
    } else if (kind === "error") {
      reviewFeedback.classList.add("is-error");
    } else if (kind === "dirty") {
      reviewFeedback.classList.add("is-dirty");
    }
  }

  function setEditControlsDisabled(disabled) {
    reviewStatus.disabled = disabled;
    reviewNote.disabled = disabled;
    hanText.disabled = disabled;
    vietText.disabled = disabled;
    saveReviewBtn.disabled = disabled;
  }

  function isHanDirty() {
    return hanText.value !== state.savedHanText;
  }

  function isVietDirty() {
    return vietText.value !== state.savedVietText;
  }

  function isReviewDirty() {
    return (
      reviewStatus.value !== state.savedReview.status ||
      reviewNote.value !== state.savedReview.note
    );
  }

  function updateDirtyState() {
    if (!state.pageLoaded) {
      state.isDirty = false;
      return;
    }
    const dirty = isHanDirty() || isVietDirty() || isReviewDirty();
    state.isDirty = dirty;
    if (dirty) {
      setReviewFeedback("Chưa lưu thay đổi", "dirty");
    } else if (reviewFeedback.classList.contains("is-dirty")) {
      setReviewFeedback("", "");
    }
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
    const targetWidth = Math.max(80, Math.round(pageImage.naturalWidth * state.zoomScale));
    pageImage.style.width = targetWidth + "px";
    pageImage.style.height = "auto";
    zoomLabel.textContent = Math.round(state.zoomScale * 100) + "%";
  }

  function stepZoom(delta) {
    if (!pageImage.naturalWidth) {
      return;
    }
    if (state.zoomMode === "fit") {
      const renderedWidth = pageImage.getBoundingClientRect().width || pageImage.naturalWidth;
      state.zoomScale = Math.max(0.2, renderedWidth / pageImage.naturalWidth);
      state.zoomMode = "manual";
    }
    state.zoomScale = Math.min(4, Math.max(0.2, +(state.zoomScale + delta).toFixed(2)));
    applyZoom();
  }

  function resetFitZoom() {
    state.zoomMode = "fit";
    state.zoomScale = 1;
    applyZoom();
  }

  function updateNavControls() {
    const total = state.images.length;
    const idx = state.currentIndex;
    if (total === 0 || idx < 0) {
      pageCounter.textContent = "0 / 0";
      prevBtn.disabled = true;
      nextBtn.disabled = true;
      return;
    }
    pageCounter.textContent = (idx + 1) + " / " + total;
    prevBtn.disabled = idx <= 0;
    nextBtn.disabled = idx >= total - 1;
    imageSelect.value = state.images[idx].id;
  }

  function clearForLoading(filename) {
    imageFilename.textContent = filename || "—";
    setStatus(imageStatus, "Đang tải ảnh...", false);
    pageImage.hidden = true;
    pageImage.removeAttribute("src");

    hanMeta.textContent = "Đang tải...";
    setStatus(hanStatus, "Đang tải OCR Hán–Nôm...", false);
    hanText.value = "";

    vietMeta.textContent = "Đang tải...";
    setStatus(vietStatus, "Đang tải OCR Việt...", false);
    vietText.value = "";

    state.pageLoaded = false;
    state.isDirty = false;
    setEditControlsDisabled(true);
    setReviewFeedback("Đang tải dữ liệu...", "");
  }

  async function saveCurrentAll() {
    if (state.currentIndex < 0 || !state.currentDataset || !state.pageLoaded) {
      return true;
    }
    const item = state.images[state.currentIndex];
    if (!item) {
      return true;
    }

    const statusToSave = reviewStatus.value;
    const noteToSave = reviewNote.value;
    const hanToSave = isHanDirty() ? hanText.value : null;
    const vietToSave = isVietDirty() ? vietText.value : null;

    state.saving = true;
    saveReviewBtn.disabled = true;
    setReviewFeedback("Đang lưu...", "");

    try {
      const resp = await fetch("/api/save", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          dataset: state.currentDataset,
          id: item.id,
          status: statusToSave,
          note: noteToSave,
          han_text: hanToSave,
          viet_text: vietToSave,
        }),
      });
      const data = await resp.json().catch(function () {
        return {};
      });
      if (!resp.ok) {
        setReviewFeedback(data.error || "Lưu thất bại (HTTP " + resp.status + ").", "error");
        return false;
      }

      if (hanToSave !== null) {
        state.savedHanText = hanToSave;
        hanMeta.textContent = item.id + ".txt";
        setStatus(hanStatus, "", false);
      }
      if (vietToSave !== null) {
        state.savedVietText = vietToSave;
        vietMeta.textContent = item.id + ".txt";
        setStatus(vietStatus, "", false);
      }

      state.savedReview = {
        status: data.review && data.review.status ? data.review.status : statusToSave,
        note: data.review && typeof data.review.note === "string" ? data.review.note : noteToSave,
      };
      state.isDirty = false;
      const updated = data.review && data.review.updated_at ? data.review.updated_at.slice(11, 19) : "";
      setReviewFeedback(updated ? "Đã lưu (" + updated + ")" : "Đã lưu", "ok");
      return true;
    } catch (_err) {
      setReviewFeedback("Lỗi kết nối khi lưu.", "error");
      return false;
    } finally {
      state.saving = false;
      saveReviewBtn.disabled = !state.pageLoaded;
    }
  }

  async function ensureSavedBeforeLeave() {
    if (!state.isDirty) {
      return true;
    }
    const ok = await saveCurrentAll();
    if (!ok) {
      window.alert("Lưu thay đổi thất bại. Hệ thống giữ nguyên nội dung đang sửa để tránh mất dữ liệu.");
    }
    return ok;
  }

  async function selectImage(index, skipDirtyCheck) {
    if (index < 0 || index >= state.images.length) {
      return;
    }
    if (!skipDirtyCheck && state.currentIndex >= 0 && index !== state.currentIndex) {
      const canLeave = await ensureSavedBeforeLeave();
      if (!canLeave) {
        updateNavControls();
        return;
      }
    }

    state.currentIndex = index;
    updateNavControls();

    const item = state.images[index];
    const token = ++state.requestToken;

    if (state.ocrController) {
      state.ocrController.abort();
    }
    const controller = new AbortController();
    state.ocrController = controller;

    clearForLoading(item.filename);

    const ds = encodeURIComponent(state.currentDataset);
    const id = encodeURIComponent(item.id);
    const imageUrl = "/api/image?dataset=" + ds + "&id=" + id;
    const ocrUrl = "/api/ocr?dataset=" + ds + "&id=" + id;

    pageImage.onload = function () {
      if (token !== state.requestToken) {
        return;
      }
      setStatus(imageStatus, "", false);
      pageImage.hidden = false;
      applyZoom();
    };
    pageImage.onerror = function () {
      if (token !== state.requestToken) {
        return;
      }
      pageImage.hidden = true;
      setStatus(imageStatus, "Không thể tải ảnh cho trang này.", true);
    };
    pageImage.src = imageUrl;

    try {
      const resp = await fetch(ocrUrl, { signal: controller.signal });
      if (token !== state.requestToken) {
        return;
      }
      if (!resp.ok) {
        throw new Error("HTTP " + resp.status);
      }
      const data = await resp.json();
      if (token !== state.requestToken) {
        return;
      }

      if (data.han && data.han.exists) {
        hanMeta.textContent = item.id + ".txt";
        setStatus(hanStatus, "", false);
        hanText.value = data.han.text || "";
      } else {
        hanMeta.textContent = "Chưa có file (nhập để tạo)";
        setStatus(hanStatus, "Chưa có file OCR Hán–Nôm. Bạn có thể nhập nội dung và bấm Lưu để tạo file.", false);
        hanText.value = "";
      }
      state.savedHanText = hanText.value;

      if (data.viet && data.viet.exists) {
        vietMeta.textContent = item.id + ".txt";
        setStatus(vietStatus, "", false);
        vietText.value = data.viet.text || "";
      } else {
        vietMeta.textContent = "Chưa có file (nhập để tạo)";
        setStatus(vietStatus, "Chưa có OCR tiếng Việt. Bạn có thể nhập nội dung và bấm Lưu để tạo file.", false);
        vietText.value = "";
      }
      state.savedVietText = vietText.value;

      const rev = data.review || { status: "unreviewed", note: "", updated_at: "" };
      reviewStatus.value = rev.status || "unreviewed";
      reviewNote.value = typeof rev.note === "string" ? rev.note : "";
      state.savedReview = {
        status: reviewStatus.value,
        note: reviewNote.value,
      };
      state.pageLoaded = true;
      state.isDirty = false;
      setEditControlsDisabled(false);

      if (data.review_error) {
        setReviewFeedback(data.review_error, "error");
      } else if (rev.updated_at) {
        setReviewFeedback("Đã lưu (" + rev.updated_at.slice(11, 19) + ")", "ok");
      } else {
        setReviewFeedback("", "");
      }
    } catch (err) {
      if (err && err.name === "AbortError") {
        return;
      }
      if (token !== state.requestToken) {
        return;
      }
      hanMeta.textContent = "Lỗi tải";
      setStatus(hanStatus, "Không thể tải OCR Hán–Nôm.", true);
      hanText.value = "";

      vietMeta.textContent = "Lỗi tải";
      setStatus(vietStatus, "Không thể tải OCR Việt.", true);
      vietText.value = "";

      setEditControlsDisabled(true);
      setReviewFeedback("Không thể tải dữ liệu trang.", "error");
    }
  }

  async function loadDataset(datasetId) {
    if (state.currentDataset && datasetId !== state.currentDataset) {
      const canLeave = await ensureSavedBeforeLeave();
      if (!canLeave) {
        datasetSelect.value = state.currentDataset;
        return;
      }
    }

    state.currentDataset = datasetId;
    state.images = [];
    state.currentIndex = -1;
    imageSelect.innerHTML = "";
    updateNavControls();
    clearForLoading("—");

    const resp = await fetch("/api/images?dataset=" + encodeURIComponent(datasetId));
    if (!resp.ok) {
      setStatus(imageStatus, "Không thể tải danh sách ảnh.", true);
      return;
    }
    const data = await resp.json();
    state.images = data.images || [];

    const frag = document.createDocumentFragment();
    for (let i = 0; i < state.images.length; i++) {
      const opt = document.createElement("option");
      opt.value = state.images[i].id;
      opt.textContent = state.images[i].filename;
      frag.appendChild(opt);
    }
    imageSelect.appendChild(frag);

    if (state.images.length > 0) {
      selectImage(0, true);
    } else {
      setStatus(imageStatus, "Tác phẩm này chưa có ảnh PNG.", true);
      setStatus(hanStatus, "Không có dữ liệu.", false);
      setStatus(vietStatus, "Không có dữ liệu.", false);
      setReviewFeedback("", "");
    }
  }

  async function init() {
    setEditControlsDisabled(true);
    try {
      const resp = await fetch("/api/datasets");
      if (!resp.ok) {
        throw new Error("HTTP " + resp.status);
      }
      const data = await resp.json();
      state.datasets = data.datasets || [];

      datasetSelect.innerHTML = "";
      for (let i = 0; i < state.datasets.length; i++) {
        const ds = state.datasets[i];
        const opt = document.createElement("option");
        opt.value = ds.id;
        opt.textContent = ds.label + " (" + ds.count + ")";
        datasetSelect.appendChild(opt);
      }

      if (state.datasets.length > 0) {
        await loadDataset(state.datasets[0].id);
      } else {
        setStatus(imageStatus, "Không tìm thấy thư mục tác phẩm nào.", true);
        setReviewFeedback("", "");
      }
    } catch (_err) {
      setStatus(imageStatus, "Không thể kết nối máy chủ cục bộ.", true);
      setReviewFeedback("", "");
    }
  }

  datasetSelect.addEventListener("change", function () {
    loadDataset(datasetSelect.value);
  });

  imageSelect.addEventListener("change", function () {
    const idx = imageSelect.selectedIndex;
    if (idx >= 0) {
      selectImage(idx, false);
    }
  });

  prevBtn.addEventListener("click", function () {
    if (state.currentIndex > 0) {
      selectImage(state.currentIndex - 1, false);
    }
  });

  nextBtn.addEventListener("click", function () {
    if (state.currentIndex < state.images.length - 1) {
      selectImage(state.currentIndex + 1, false);
    }
  });

  zoomOutBtn.addEventListener("click", function () {
    stepZoom(-0.2);
  });

  zoomInBtn.addEventListener("click", function () {
    stepZoom(0.2);
  });

  zoomFitBtn.addEventListener("click", resetFitZoom);

  reviewStatus.addEventListener("change", updateDirtyState);
  reviewNote.addEventListener("input", updateDirtyState);
  hanText.addEventListener("input", updateDirtyState);
  vietText.addEventListener("input", updateDirtyState);

  saveReviewBtn.addEventListener("click", function () {
    saveCurrentAll();
  });

  document.addEventListener("keydown", function (event) {
    if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === "s") {
      event.preventDefault();
      saveCurrentAll();
      return;
    }
    if (event.key === "PageUp") {
      event.preventDefault();
      if (state.currentIndex > 0) {
        selectImage(state.currentIndex - 1, false);
      }
    } else if (event.key === "PageDown") {
      event.preventDefault();
      if (state.currentIndex < state.images.length - 1) {
        selectImage(state.currentIndex + 1, false);
      }
    }
  });

  window.addEventListener("beforeunload", function (event) {
    if (state.isDirty) {
      event.preventDefault();
      event.returnValue = "";
    }
  });

  init();
})();
