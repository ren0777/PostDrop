"use strict";

const state = {
  platforms: [],
  accountSections: {},
  sharedSections: {},
  ratings: {},
  lastRatings: {},
  files: [],
  selected: new Set(),
  lastPostId: null,
  postImages: [],
  done: new Set(),
  lastGenerated: {},
  caption: null,
  styles: {},
  posting: false,
  lazy: null,
  queue: null,
};

const $ = (selector) => document.querySelector(selector);

/* ------------------------------------------------------------------ utils */

function escapeHtml(value) {
  return String(value ?? "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#39;");
}

async function api(path, options = {}) {
  const response = await fetch(path, {
    ...options,
    headers: { "X-ArtDrop": "1", ...(options.headers || {}) },
  });
  let data = {};
  try { data = await response.json(); } catch (error) { data = {}; }
  if (!response.ok && !data.error) data.error = `HTTP ${response.status}`;
  return data;
}

function toast(message, ms = 4000) {
  const node = $("#toast");
  node.textContent = message;
  node.hidden = false;
  clearTimeout(toast.timer);
  toast.timer = setTimeout(() => { node.hidden = true; }, ms);
}

function parseTagInput(raw) {
  return String(raw || "")
    .split(/[,\s]+/)
    .map((tag) => tag.trim().replace(/^#+/, ""))
    .filter(Boolean);
}

const STYLE_LABELS = [
  ["ai", "AI caption"],
  ["name_series", "Name + series"],
  ["none", "Images only"],
];

function styleFor(platformId) {
  return state.styles[platformId] || "ai";
}

function styleOptions(current) {
  return STYLE_LABELS.map(([value, label]) =>
    `<option value="${value}" ${value === current ? "selected" : ""}>${label}</option>`).join("");
}

async function loadSettings() {
  const data = await api("/api/settings");
  state.styles = data.caption_styles || {};
}

async function saveStyle(platformId, style) {
  const response = await api("/api/settings", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ caption_styles: { [platformId]: style } }),
  });
  if (response.caption_styles) state.styles = response.caption_styles;
}

function buildCaption(caption, tags, limit) {
  caption = String(caption || "").trim();
  let list = (tags || []).filter(Boolean);
  if (limit && caption.length > limit) {
    caption = caption.slice(0, Math.max(0, limit - 1)).replace(/\s+$/, "") + "…";
  }
  let tagLine = list.map((tag) => "#" + tag).join(" ");
  if (!tagLine) return caption;
  if (caption) {
    while (list.length && caption.length + 2 + tagLine.length > limit) {
      list = list.slice(0, -1);
      tagLine = list.map((tag) => "#" + tag).join(" ");
    }
    return caption + (tagLine ? "\n\n" + tagLine : "");
  }
  while (list.length && tagLine.length > limit) {
    list = list.slice(0, -1);
    tagLine = list.map((tag) => "#" + tag).join(" ");
  }
  return tagLine;
}

/* ------------------------------------------------------------------- tabs */

function showTab(name) {
  document.querySelectorAll(".tab").forEach((button) => {
    button.classList.toggle("active", button.dataset.tab === name);
  });
  document.querySelectorAll(".tab-panel").forEach((panel) => {
    panel.hidden = panel.id !== "tab-" + name;
  });
  if (name === "history") loadHistory();
  if (name === "queue") loadQueue();
}

/* -------------------------------------------------------------- platforms */

async function loadPlatforms() {
  const data = await api("/api/platforms");
  state.platforms = data.platforms || [];
  state.accountSections = data.account_sections || {};
}

function renderCards() {
  const root = $("#cards");
  root.innerHTML = state.platforms.map((platform) => `
    <article class="card" id="card-${platform.id}">
      <header>
        <label class="check">
          <input type="checkbox" class="select" data-id="${platform.id}" ${state.selected.has(platform.id) ? "checked" : ""}>
          <span class="name">${escapeHtml(platform.label)}</span>
        </label>
        <span class="badge ${platform.configured ? "ok" : "miss"}">${platform.configured ? "set up" : "not set up"}</span>
        <select class="style" id="style-${platform.id}" data-id="${platform.id}" title="Caption style">
          ${styleOptions(styleFor(platform.id))}
        </select>
        <span class="limit" id="limit-${platform.id}"></span>
      </header>
      <div class="preview" id="preview-${platform.id}" hidden></div>
      <details>
        <summary>Text for ${escapeHtml(platform.label)} (optional)</summary>
        <div class="caption-fields" id="caption-fields-${platform.id}">
          <textarea id="override-${platform.id}" rows="3" name="artdrop-override-${platform.id}" autocomplete="off" placeholder="Caption for ${escapeHtml(platform.label)}"></textarea>
          <label class="mini-label">Tags
            <input id="tags-${platform.id}" type="text" name="artdrop-tags-${platform.id}" autocomplete="off" placeholder="only for ${escapeHtml(platform.label)}">
          </label>
        </div>
        ${(platform.option_fields || []).map((field) => optionFieldHtml(platform, field)).join("")}
      </details>
      <div class="status" id="status-${platform.id}"></div>
      <div class="row"><button id="retry-${platform.id}" data-id="${platform.id}" hidden>Retry</button></div>
    </article>
  `).join("");

  root.querySelectorAll(".select").forEach((box) => {
    box.addEventListener("change", () => {
      if (box.checked) state.selected.add(box.dataset.id);
      else state.selected.delete(box.dataset.id);
      savePrefs();
    });
  });
  root.querySelectorAll("select.style").forEach((select) => {
    select.addEventListener("change", () => {
      state.styles[select.dataset.id] = select.value;
      saveStyle(select.dataset.id, select.value);
      updateLimits();
    });
  });
  root.querySelectorAll("#cards input, #cards textarea, #cards select").forEach((node) => {
    node.addEventListener("input", updateLimits);
  });
  root.querySelectorAll("input[data-invite]").forEach((node) => {
    node.addEventListener("input", () => {
      const line = document.getElementById("invite-" + node.dataset.invite);
      if (line) line.textContent = inviteText(node.value);
    });
  });
  root.querySelectorAll("button.rating").forEach((button) => {
    button.addEventListener("click", () => {
      state.ratings[button.dataset.card] = button.dataset.rating;
      saveRating(button.dataset.card, button.dataset.rating);
      paintRating(button.dataset.card);
    });
  });
  state.platforms.forEach((platform) => paintRating(platform.id));
  root.querySelectorAll("button[data-id]").forEach((button) => {
    button.addEventListener("click", () => {
      const platform = state.platforms.find((item) => item.id === button.dataset.id);
      if (!platform) return;
      if (state.lastPostId) publishOne(platform, state.lastPostId, false);
      else doPost();
    });
  });
  updateLimits();
}

function setCard(platformId, options) {
  const card = document.getElementById("card-" + platformId);
  if (!card) return;
  const { kind, text, url, retry, lock } = options;
  card.classList.remove("pending", "done", "failed");
  if (kind) card.classList.add(kind);

  const status = document.getElementById("status-" + platformId);
  if (status) {
    status.innerHTML = escapeHtml(text || "");
    if (url) {
      status.innerHTML += ` · <a href="${escapeHtml(url)}" target="_blank" rel="noopener">open post</a>`;
    }
  }
  const retryButton = document.getElementById("retry-" + platformId);
  if (retryButton) retryButton.hidden = !retry;
  if (lock) {
    card.querySelectorAll("input, textarea").forEach((node) => { node.disabled = true; });
  }
}

function siteText(platform) {
  const style = styleFor(platform.id);
  if (style === "none") return "";
  if (style === "name_series") {
    const name = $("#character").value.trim();
    const series = $("#series").value.trim();
    if (!name) return "";
    return series ? `${name} — ${series}` : name;
  }
  const override = document.getElementById("override-" + platform.id);
  const tagsOverride = document.getElementById("tags-" + platform.id);
  const caption = (override && override.value.trim()) || $("#caption").value;
  const tags = (tagsOverride && tagsOverride.value.trim())
    ? parseTagInput(tagsOverride.value)
    : parseTagInput($("#tags").value);
  const useTags = platform.tags_in_caption === false ? [] : tags;
  return buildCaption(caption, useTags, platform.caption_limit);
}

function updateLimits() {
  state.platforms.forEach((platform) => {
    const style = styleFor(platform.id);
    const text = siteText(platform);

    const fieldsNode = document.getElementById("caption-fields-" + platform.id);
    if (fieldsNode) fieldsNode.hidden = style !== "ai";

    const previewNode = document.getElementById("preview-" + platform.id);
    if (previewNode) {
      previewNode.hidden = style === "ai";
      if (style !== "ai") {
        previewNode.textContent = style === "none"
          ? "Images only — no text"
          : (text || "(type a character above)");
      }
    }

    const limitNode = document.getElementById("limit-" + platform.id);
    if (limitNode) {
      if (style === "ai") {
        limitNode.textContent = `${text.length} / ${platform.caption_limit}`;
        limitNode.classList.toggle("over", text.length > platform.caption_limit);
      } else {
        limitNode.textContent = "";
        limitNode.classList.remove("over");
      }
    }
  });
}

function collaboratorNames(raw) {
  return String(raw || "")
    .split(/[,\s]+/)
    .map((name) => name.replace(/^@+/, "").trim())
    .filter(Boolean);
}

function inviteText(raw) {
  const names = collaboratorNames(raw);
  if (!names.length) return "No collaborators on this post.";
  return "Will invite: " + names.map((name) => "@" + name).join(", ");
}

function optionFieldHtml(platform, field) {
  const id = `opt-${platform.id}-${field.key}`;
  const help = field.help ? `<p class="help">${escapeHtml(field.help)}</p>` : "";
  const flags = `data-opt="1" data-sticky="${field.sticky ? "1" : "0"}"`;
  if (field.kind === "checkbox") {
    return `<label class="mini-check"><input type="checkbox" id="${id}" ${field.value ? "checked" : ""}> ${escapeHtml(field.label)}</label>${help}`;
  }
  if (field.kind === "select") {
    const options = (field.choices || []).map((choice) =>
      `<option value="${escapeHtml(choice)}">${escapeHtml(choice)}</option>`).join("");
    return `<label class="mini-label">${escapeHtml(field.label)}
      <select id="${id}" ${flags}>${options}</select>
    </label>${help}`;
  }
  if (field.kind === "rating") {
    const buttons = (field.choices || []).map((choice) => {
      const value = Array.isArray(choice) ? choice[0] : choice;
      const label = Array.isArray(choice) ? choice[1] : choice;
      return `<button type="button" class="rating" data-card="${escapeHtml(platform.id)}" data-rating="${escapeHtml(value)}">${escapeHtml(label)}</button>`;
    }).join("");
    return `<div class="mini-label">${escapeHtml(field.label)}
      <div class="rating-row" id="rating-${escapeHtml(platform.id)}">${buttons}</div>
      <p class="help" id="rating-hint-${escapeHtml(platform.id)}"></p>
    </div>${help}`;
  }
  const invite = field.key === "collaborators" ? ` data-invite="${escapeHtml(platform.id)}"` : "";
  const inviteLine = field.key === "collaborators"
    ? `<p class="help" id="invite-${platform.id}">${escapeHtml(inviteText(field.value || ""))}</p>`
    : "";
  return `<label class="mini-label">${escapeHtml(field.label)}
    <input id="${id}" type="text" ${flags}${invite} name="artdrop-${platform.id}-${field.key}"
           autocomplete="off" value="${escapeHtml(field.value || "")}"
           placeholder="${escapeHtml(field.placeholder || "")}">
  </label>${help}${inviteLine}`;
}

/* ------------------------------------------------------------ drop / files */

function renderThumbs() {
  const root = $("#thumbs");
  if (state.files.length) {
    root.innerHTML = state.files.map((file, index) => `
      <div class="thumb" title="${escapeHtml(file.name)}">
        <img src="${URL.createObjectURL(file)}" alt="">
        <button data-index="${index}" title="Remove">×</button>
      </div>
    `).join("");
  } else if (state.postImages.length && state.lastPostId) {
    root.innerHTML = state.postImages.map((image, index) => `
      <div class="thumb" title="${escapeHtml(image.name)}">
        <img src="/api/posts/${escapeHtml(state.lastPostId)}/image/${index}" alt="">
        <button data-clear="1" title="Remove">×</button>
      </div>
    `).join("");
  } else {
    root.innerHTML = "";
  }
  root.querySelectorAll("button[data-index]").forEach((button) => {
    button.addEventListener("click", () => {
      state.files.splice(Number(button.dataset.index), 1);
      resetPostState();
      renderThumbs();
    });
  });
  root.querySelectorAll("button[data-clear]").forEach((button) => {
    button.addEventListener("click", () => newPost());
  });
}

async function addFiles(fileList) {
  let added = 0;
  for (const file of fileList) {
    if (/\.txt$/i.test(file.name)) {
      await applySidecar(file);
      continue;
    }
    if (!/^image\//.test(file.type)) continue;
    state.files.push(file);
    added += 1;
  }
  if (added) {
    resetPostState();
    renderThumbs();
  }
}

async function applySidecar(file) {
  const lines = (await file.text()).split(/\r?\n/);
  const caption = [];
  const tags = [];
  for (const line of lines) {
    const match = line.match(/^\s*tags?\s*:\s*(.*)$/i);
    if (match) tags.push(...match[1].split(/[,\s]+/));
    else caption.push(line);
  }
  $("#caption").value = caption.join("\n").trim();
  $("#tags").value = tags.map((tag) => tag.replace(/^#+/, "")).filter(Boolean).join(" ");
  updateLimits();
  toast(`Loaded caption and tags from ${file.name}`);
}

function bindDropZone() {
  const drop = $("#drop");
  ["dragenter", "dragover"].forEach((type) => {
    drop.addEventListener(type, (event) => {
      event.preventDefault();
      drop.classList.add("over");
    });
  });
  ["dragleave", "drop"].forEach((type) => {
    drop.addEventListener(type, (event) => {
      event.preventDefault();
      drop.classList.remove("over");
    });
  });
  drop.addEventListener("drop", (event) => {
    if (event.dataTransfer && event.dataTransfer.files.length) addFiles(event.dataTransfer.files);
  });
  $("#file-input").addEventListener("change", (event) => {
    addFiles(event.target.files);
    event.target.value = "";
  });
  document.addEventListener("dragover", (event) => event.preventDefault());
  document.addEventListener("drop", (event) => event.preventDefault());
}

/* ------------------------------------------------------------- post state */

function resetCards() {
  state.platforms.forEach((platform) => {
    const card = document.getElementById("card-" + platform.id);
    if (!card) return;
    card.classList.remove("pending", "done", "failed");
    card.querySelectorAll("input, textarea").forEach((node) => { node.disabled = false; });
    card.querySelectorAll("input[type=checkbox]:not(.select)").forEach((node) => { node.checked = false; });
    card.querySelectorAll("[data-opt='1'][data-sticky='0']").forEach((node) => {
      if (node.tagName === "SELECT") node.selectedIndex = 0;
      else node.value = "";
    });
    const override = card.querySelector("textarea");
    if (override) override.value = "";
    const tags = document.getElementById("tags-" + platform.id);
    if (tags) tags.value = "";
    const status = document.getElementById("status-" + platform.id);
    if (status) status.textContent = "";
    const retry = document.getElementById("retry-" + platform.id);
    if (retry) retry.hidden = true;
  });
}

/** The uploaded batch changed: the old post_id (and its guard) no longer applies. */
function resetPostState() {
  state.lastPostId = null;
  state.mixed = null;  // a mixed set from the queue: its names, for the AI caption
  state.done = new Set();
  state.lastGenerated = {};
  resetCards();
}

function newPost() {
  state.files = [];
  state.postImages = [];
  $("#caption").value = "";
  $("#tags").value = "";
  $("#alt").value = "";
  resetPostState();
  renderThumbs();
  updateLimits();
  toast("Cleared — ready for the next piece");
}

/* ------------------------------------------------------- publishing */

async function doPost() {
  if (state.posting) return;
  const selected = state.platforms.filter((platform) => state.selected.has(platform.id));
  if (!state.files.length && !state.lastPostId) {
    return toast("Add at least one image first");
  }
  if (!selected.length) return toast("Tick at least one site");

  const dryRun = $("#dry-run").checked;
  // Sites that already have this post stay locked; only the rest are published.
  const targets = dryRun
    ? selected
    : selected.filter((platform) => !state.done.has(platform.id));
  if (!targets.length) {
    return toast("Every ticked site already has this post — press “New post” to start over");
  }

  state.posting = true;
  const button = $("#post");
  button.disabled = true;
  button.textContent = "Posting…";

  try {
    // One upload per batch of images: pressing Post again reuses the same
    // post_id, so the server's double-post guard still sees it.
    let postId = state.lastPostId;
    if (!postId) {
      const form = new FormData();
      state.files.forEach((file) => form.append("images", file));
      const upload = await api("/api/upload", { method: "POST", body: form });
      if (!upload.post_id) throw new Error(upload.error || "Upload failed");
      postId = upload.post_id;
      state.lastPostId = postId;
    }
    targets.forEach((platform) =>
      setCard(platform.id, { kind: "pending", text: dryRun ? "checking…" : "posting…" }));
    await Promise.all(targets.map((platform) => publishOne(platform, postId, dryRun)));
  } catch (error) {
    targets.forEach((platform) =>
      setCard(platform.id, { kind: "failed", text: error.message, retry: true }));
    toast(error.message);
  }

  state.posting = false;
  button.disabled = false;
  button.textContent = "Post";
}

async function publishOne(platform, postId, dryRun) {
  const browserFlow = (platform.option_fields || []).some((field) => field.kind === "rating");
  const pending = dryRun ? "checking…" : (browserFlow ? "waiting for the Pixiv window…" : "posting…");
  setCard(platform.id, { kind: "pending", text: pending });

  const options = {};
  const overrideNode = document.getElementById("override-" + platform.id);
  const override = overrideNode ? overrideNode.value.trim() : "";
  const tagsNode = document.getElementById("tags-" + platform.id);
  const tagsOverride = tagsNode ? tagsNode.value.trim() : "";
  const site = { style: styleFor(platform.id) };
  if (override) site.caption = override;
  if (tagsOverride) site.tags = tagsOverride;
  (platform.option_fields || []).forEach((field) => {
    if (field.kind === "rating") {
      // The rating is a row of buttons, not an input: it has no `opt-…` node,
      // so it must be read before the node lookup below.
      site[field.key] = state.ratings[platform.id] || "";
      return;
    }
    const node = document.getElementById(`opt-${platform.id}-${field.key}`);
    if (!node) return;
    if (field.kind === "checkbox") {
      if (node.checked) site[field.key] = true;
    } else if (field.kind === "select") {
      if (node.value) site[field.key] = node.value;
    } else {
      // Always send the key: an empty box means "nothing for this post", and
      // only a missing key falls back to the account default.
      site[field.key] = node.value.trim();
    }
  });
  options[platform.id] = site;
  if ($("#mature").checked) options.mature = true;

  const body = {
    post_id: postId,
    platform: platform.id,
    caption: $("#caption").value,
    tags: $("#tags").value,
    alt_text: $("#alt").value,
    character: $("#character").value.trim(),
    series: $("#series").value.trim(),
    options,
    dry_run: dryRun,
  };
  const result = await api("/api/publish", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });

  if (result.already) {
    state.done.add(platform.id);
    setCard(platform.id, { kind: "done", text: "already posted", url: result.url, lock: true });
    return result;
  }
  if (result.ok && dryRun) {
    const preview = result.preview || {};
    const sizes = (preview.images || [])
      .map((image) => `${image.kb} KB · ${image.width}×${image.height}`)
      .join(", ");
    const head = preview.ready
      ? "Dry run — ready to post"
      : "Dry run — missing: " + (preview.missing || []).join(", ");
    const note = preview.note ? ` — ${preview.note}` : "";
    const invite = (preview.collaborators || []).length
      ? ` — will invite ${preview.collaborators.map((name) => "@" + name).join(", ")}`
      : "";
    const extra = [];
    if (preview.rating) extra.push(`rating: ${preview.rating}`);
    if (preview.warning) extra.push(`⚠ ${preview.warning}`);
    const extraText = extra.length ? ` — ${extra.join(" — ")}` : "";
    setCard(platform.id, {
      kind: preview.ready ? "pending" : "failed",
      text: `${head} — ${sizes}${invite}${extraText}${note}`,
    });
  } else if (result.ok) {
    state.done.add(platform.id);
    const note = result.note ? ` — ${result.note}` : "";
    setCard(platform.id, { kind: "done", text: `posted${note}`, url: result.url, lock: true });
  } else {
    setCard(platform.id, { kind: "failed", text: result.error || "failed", retry: true });
  }
  return result;
}

/* -------------------------------------------------------------- caption AI */

async function generate() {
  const character = $("#character").value.trim();
  if (!character && !state.mixed) return toast("Type the character name first");
  const selected = state.platforms.filter((platform) => state.selected.has(platform.id));
  if (!selected.length) return toast("Tick at least one site first");
  const aiSites = selected.filter((platform) => styleFor(platform.id) === "ai");
  if (!aiSites.length) {
    return toast("No ticked site uses AI captions — type the character and series by hand");
  }

  const button = $("#generate");
  const status = $("#generate-status");
  button.disabled = true;
  status.textContent = "writing…";
  try {
    // Reuse the uploaded batch when there is one, like Post does.
    let postId = state.lastPostId;
    if (!postId && state.files.length) {
      const form = new FormData();
      state.files.forEach((file) => form.append("images", file));
      const upload = await api("/api/upload", { method: "POST", body: form });
      if (!upload.post_id) throw new Error(upload.error || "Upload failed");
      postId = upload.post_id;
      state.lastPostId = postId;
    }
    const result = await api("/api/caption", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        post_id: postId || null,
        character,
        mixed: state.mixed || null,
        series: $("#series").value.trim(),
        notes: $("#notes").value.trim(),
        mature: $("#mature").checked,
        caption: $("#caption").value.trim(),
        caption_exact: $("#caption-exact").checked,
        platforms: aiSites.map((platform) => platform.id),
      }),
    });
    if (!result.ok) throw new Error(result.error || "Caption AI failed");
    applyCaptionResult(result);
    if (result.series && !$("#series").value.trim()) $("#series").value = result.series;
    updateLimits();
    status.textContent = result.fallback
      ? `written from name only (${result.used_model})`
      : `written by ${result.used_model}`;
  } catch (error) {
    status.textContent = "";
    toast(error.message);
  }
  button.disabled = false;
}

function applyCaptionResult(result) {
  const writes = [];
  if (result.alt_text) writes.push({ id: "alt", value: result.alt_text });
  // Every card with a title box gets the site's own title, or the English one.
  const answers = result.platforms || {};
  state.platforms.forEach((platform) => {
    const hasTitle = (platform.option_fields || []).some((field) => field.key === "title");
    if (!hasTitle) return;
    const value = (answers[platform.id] || {}).title || result.title || "";
    if (value) writes.push({ id: `opt-${platform.id}-title`, value });
  });
  Object.entries(result.platforms || {}).forEach(([platformId, entry]) => {
    if (entry.caption) writes.push({ id: `override-${platformId}`, value: entry.caption });
    if (entry.tags && entry.tags.length) {
      writes.push({ id: `tags-${platformId}`, value: entry.tags.join(" ") });
    }
  });
  const edited = writes.some(({ id, value }) => {
    const node = document.getElementById(id);
    if (!node) return false;
    const current = node.value.trim();
    return current && current !== value && current !== (state.lastGenerated[id] || "");
  });
  if (edited && !window.confirm("Replace your edited boxes with the new suggestions?")) return;
  writes.forEach(({ id, value }) => {
    const node = document.getElementById(id);
    if (node) {
      node.value = value;
      state.lastGenerated[id] = value;
    }
  });
  updateLimits();
}

/* --------------------------------------------------------------- accounts */

function fieldHtml(platform, field) {
  const id = `acc-${platform.id}-${field.key}`;
  const placeholder = field.secret && field.saved
    ? "saved — leave blank to keep"
    : escapeHtml(field.placeholder || "");
  return `
    <div class="field">
      <label for="${id}">${escapeHtml(field.label)}${field.required ? "" : " (optional)"}</label>
      <input id="${id}" data-key="${field.key}" type="${field.secret ? "password" : "text"}"
             name="artdrop-${platform.id}-${field.key}" autocomplete="off"
             placeholder="${placeholder}" value="${field.secret ? "" : escapeHtml(field.value)}">
      ${field.help ? `<p class="help">${escapeHtml(field.help)}</p>` : ""}
    </div>`;
}

function renderAccounts() {
  const visible = state.platforms.filter((platform) => !platform.account_of);
  $("#accounts").innerHTML = visible.map((platform) => `
    <article class="card account" id="account-${platform.id}">
      <header>
        <span class="name">${escapeHtml(platform.label)}</span>
        <span class="badge ${platform.configured ? "ok" : "miss"}">${platform.configured ? "set up" : "not set up"}</span>
      </header>
      ${platform.fields.map((field) => fieldHtml(platform, field)).join("")}
      <div class="row">
        <button class="primary" data-action="save" data-id="${platform.id}">Save</button>
        <button data-action="test" data-id="${platform.id}">Test connection</button>
        <button class="danger" data-action="remove" data-id="${platform.id}">Remove</button>
      </div>
      ${platform.is_oauth ? oauthBlockHtml(platform) : ""}
      <p class="result" id="result-${platform.id}"></p>
      ${platform.setup_help ? `<details><summary>How to set this up</summary><p class="help">${escapeHtml(platform.setup_help)}</p></details>` : ""}
    </article>
  `).join("");

  renderAccountSections();

  $("#accounts").querySelectorAll("button[data-action]").forEach((button) => {
    button.addEventListener("click", () => accountAction(button.dataset.action, button.dataset.id));
  });
  $("#accounts").querySelectorAll("button[data-oauth-action]").forEach((button) => {
    button.addEventListener("click", () => oauthAction(button.dataset.oauthAction, button.dataset.id));
  });
  $("#accounts").querySelectorAll("button[data-acct-action]").forEach((button) => {
    button.addEventListener("click", () => accountRowAction(button.dataset.acctAction, button.dataset.id));
  });
  renderCaptionCard();
}

/** Multiple-account sites (Instagram): shared settings plus one row per account. */
function renderAccountSections() {
  state.sharedSections = {};
  Object.values(state.accountSections || {}).forEach((section) => {
    const shared = {
      id: section.id,
      label: section.label,
      fields: section.fields,
      configured: section.configured,
      setup_help: section.setup_help,
    };
    state.sharedSections[section.id] = shared;

    const rows = (section.accounts || []).map((account) => `
      <article class="card account" id="account-${account.id}">
        <header>
          <span class="name">${escapeHtml(account.name || account.key)}</span>
          <span class="badge ${account.connected ? "ok" : "miss"}">${account.connected
            ? (section.flow === "browser" ? "logged in" : "connected")
            : (section.flow === "browser" ? "not logged in" : "not connected")}</span>
          ${account.username ? `<span class="muted">@${escapeHtml(account.username)}</span>` : ""}
        </header>
        <label class="mini-label">Name
          <input id="acct-name-${account.id}" type="text" name="artdrop-name-${account.id}" autocomplete="off" value="${escapeHtml(account.name)}">
        </label>
        ${(account.settings || []).map((field) => optionFieldHtml({ id: account.id }, field)).join("")}
        <div class="row">
          <button class="primary" data-acct-action="save" data-id="${account.id}">Save</button>
          <button data-acct-action="connect" data-id="${account.id}">${section.flow === "browser" ? "Log in" : "Connect"}</button>
          <button data-acct-action="test" data-id="${account.id}">${section.flow === "browser" ? "Check login" : "Test connection"}</button>
          <button class="danger" data-acct-action="disconnect" data-id="${account.id}">${section.flow === "browser" ? "Forget login" : "Disconnect"}</button>
          <button class="danger" data-acct-action="remove" data-id="${account.id}">Remove</button>
        </div>
        <p class="result" id="result-${account.id}"></p>
      </article>`).join("");

    $("#accounts").insertAdjacentHTML("beforeend", `
      <h3 class="section-title">${escapeHtml(section.label)}</h3>
      <article class="card account" id="account-${section.id}">
        <header>
          <span class="name">Shared settings</span>
          <span class="badge ${section.configured ? "ok" : "miss"}">${section.configured ? "set up" : "not set up"}</span>
        </header>
        ${section.fields.map((field) => fieldHtml(shared, field)).join("")}
        <div class="row"><button class="primary" data-action="save" data-id="${section.id}">Save</button></div>
        <p class="result" id="result-${section.id}"></p>
        ${section.oauth_help ? `<details><summary>How to connect an account</summary><p class="help">${escapeHtml(section.oauth_help)}</p></details>` : ""}
      </article>
      <div class="row"><button id="add-${section.id}">Add ${escapeHtml(section.label.replace(" settings", ""))} account</button></div>
      ${rows}
    `);
    const addButton = document.getElementById("add-" + section.id);
    if (addButton) addButton.addEventListener("click", () => addAccount(section.id));
  });
}

async function addAccount(sectionId) {
  const name = window.prompt(`Name for the new ${sectionId} account? (e.g. Solo)`);
  if (name === null) return;
  const response = await api(`/api/${sectionId}/accounts`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ name: (name || "").trim() || "Instagram" }),
  });
  if (!response.ok) {
    toast(response.error || "Could not add the account.");
    return;
  }
  await loadPlatforms();
  renderAccounts();
  renderCards();
  await accountRowAction("connect", response.id);
}

async function accountRowAction(action, cardId) {
  const [site, key] = cardId.split(":");
  const section = (state.accountSections || {})[site] || {};
  const account = (section.accounts || []).find((item) => item.id === cardId);
  let resultNode = document.getElementById("result-" + cardId);

  if (action === "save") {
    const body = {};
    const name = document.getElementById("acct-name-" + cardId);
    if (name) body.name = name.value;
    (account ? account.settings || [] : []).forEach((field) => {
      const node = document.getElementById(`opt-${cardId}-${field.key}`);
      if (!node) return;
      body[field.key] = field.kind === "checkbox" ? node.checked : node.value;
    });
    const response = await api(`/api/${site}/accounts/${key}`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    await loadPlatforms();
    renderAccounts();
    renderCards();
    resultNode = document.getElementById("result-" + cardId);
    if (resultNode) {
      resultNode.textContent = response.ok ? "Saved." : (response.error || "Could not save.");
      resultNode.className = response.ok ? "result ok" : "result err";
    }
    return;
  }

  if (action === "remove") {
    if (!window.confirm("Remove this account? The posts themselves stay online.")) return;
    await api(`/api/${site}/accounts/${key}`, { method: "DELETE" });
    await loadPlatforms();
    renderAccounts();
    renderCards();
    return;
  }

  if (action === "test") {
    // Per-account rows have their own test path: /api/test/<site:key> also
    // refreshes Pixiv's browser login (a window opens) and Instagram's token.
    if (resultNode) {
      resultNode.textContent = section.flow === "browser"
        ? "Checking… (a Pixiv window opens for a moment)"
        : "Testing…";
      resultNode.className = "result";
    }
    const response = await api(`/api/test/${cardId}`, { method: "POST" });
    await loadPlatforms();
    renderAccounts();
    renderCards();
    resultNode = document.getElementById("result-" + cardId);
    if (resultNode) {
      resultNode.textContent = response.ok
        ? (response.detail || "Connected.")
        : (response.error || "Failed.");
      resultNode.className = response.ok ? "result ok" : "result err";
    }
    return;
  }

  if (action === "connect" && section.flow === "browser") {
    if (resultNode) {
      resultNode.textContent = "Opening Pixiv's login page in your Chrome…";
      resultNode.className = "result";
    }
    const response = await api(`/api/browser-login/${cardId}`, { method: "POST" });
    await loadPlatforms();
    renderAccounts();
    renderCards();
    resultNode = document.getElementById("result-" + cardId);
    if (resultNode) {
      resultNode.textContent = response.ok
        ? (response.detail || "Logged in.")
        : (response.error || "Login failed.");
      resultNode.className = response.ok ? "result ok" : "result err";
    }
    return;
  }

  if (action === "disconnect" && section.flow === "browser") {
    if (!window.confirm("Forget this login? The browser profile is deleted; you can log in again.")) return;
    const response = await api(`/api/browser-logout/${cardId}`, { method: "POST" });
    await loadPlatforms();
    renderAccounts();
    renderCards();
    resultNode = document.getElementById("result-" + cardId);
    if (resultNode) {
      resultNode.textContent = response.ok ? (response.detail || "Logged out.") : (response.error || "Failed.");
      resultNode.className = response.ok ? "result ok" : "result err";
    }
    return;
  }

  if (section.flow === "browser") {
    // a browser account has no other actions; say so instead of doing nothing
    console.warn("ArtDrop: unknown action", action, "for", cardId);
    return;
  }

  // everything else (connect/finish/disconnect) works like any other OAuth card
  await oauthAction(action, cardId);
}

function platformById(platformId) {
  const found = state.platforms.find((platform) => platform.id === platformId);
  if (found) return found;
  const sections = state.sharedSections || {};
  if (sections[platformId]) {
    const shared = sections[platformId];
    return { ...shared, is_oauth: false, fields: shared.fields };
  }
  return null;
}

function oauthBlockHtml(platform) {
  return `
    <div class="oauth-box">
      <div class="row">
        <button class="primary" data-oauth-action="connect" data-id="${platform.id}">Connect</button>
        <span class="badge ${platform.connected ? "ok" : "miss"}">${platform.connected ? "connected" : "not connected"}</span>
      </div>
      <p class="help">${escapeHtml(platform.oauth_help || "")}</p>
      <label class="mini-label">…or paste the code from the address bar
        <input id="oauth-code-${platform.id}" type="text" placeholder="the code, or the whole redirected URL">
      </label>
      <div class="row">
        <button data-oauth-action="finish" data-id="${platform.id}">Finish connection</button>
        <button data-oauth-action="disconnect" data-id="${platform.id}">Disconnect</button>
      </div>
    </div>`;
}

async function oauthAction(action, platformId) {
  let resultNode = document.getElementById("result-" + platformId);
  const platform = state.platforms.find((item) => item.id === platformId);
  const label = platform ? platform.label : platformId;

  if (action === "connect") {
    const response = await api(`/api/oauth/${platformId}/start`, { method: "POST" });
    if (!response.ok) {
      if (resultNode) {
        resultNode.textContent = response.error || "Could not start the connection.";
        resultNode.className = "result err";
      }
      return;
    }
    window.open(response.url, "_blank", "noopener");
    if (resultNode) {
      resultNode.textContent =
        `Authorize ${label} in the new tab. It shows "connected ✓" when it works — then press ` +
        `Test connection. If that tab ends on a blank or error page instead, copy the code from ` +
        `its address bar into the box above and press Finish connection.`;
      resultNode.className = "result";
    }
    return;
  }

  if (action === "finish") {
    const input = document.getElementById("oauth-code-" + platformId);
    const code = input ? input.value.trim() : "";
    if (!code) {
      if (resultNode) { resultNode.textContent = "Paste the code first."; resultNode.className = "result err"; }
      return;
    }
    if (resultNode) { resultNode.textContent = "Exchanging the code…"; resultNode.className = "result"; }
    const response = await api(`/api/oauth/${platformId}/code`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ code }),
    });
    await loadPlatforms();
    renderAccounts();
    renderCards();
    resultNode = document.getElementById("result-" + platformId);
    if (resultNode) {
      resultNode.textContent = response.ok
        ? "Connected ✓ — now press Test connection."
        : (response.error || "Could not connect.");
      resultNode.className = response.ok ? "result ok" : "result err";
    }
    return;
  }

  if (action === "disconnect") {
    if (!window.confirm(`Disconnect ${label}?`)) return;
    await api(`/api/oauth/${platformId}/disconnect`, { method: "POST" });
    await loadPlatforms();
    renderAccounts();
    renderCards();
    resultNode = document.getElementById("result-" + platformId);
    if (resultNode) { resultNode.textContent = "Disconnected."; resultNode.className = "result"; }
    return;
  }

  console.warn("ArtDrop: unknown action", action, "for", platformId);
}

async function loadCaptionSettings() {
  state.caption = await api("/api/caption/settings");
}

function renderCaptionCard() {
  const cfg = state.caption || {};
  $("#accounts").insertAdjacentHTML("beforeend", `
    <article class="card account" id="account-caption">
      <header>
        <span class="name">✨ Caption AI</span>
        <span class="badge ${cfg.configured ? "ok" : "miss"}">${cfg.configured ? "set up" : "not set up"}</span>
      </header>
      <div class="field">
        <label for="caption-api-key">OpenCode Go API key</label>
        <input id="caption-api-key" type="password" placeholder="${cfg.api_key_saved ? "saved — leave blank to keep" : "paste your key"}">
        <p class="help">From opencode.ai → Console → Go. Stored in Windows Credential Manager, never on disk.</p>
      </div>
      <div class="field">
        <label for="caption-vision-model">Vision model</label>
        <input id="caption-vision-model" type="text" value="${escapeHtml(cfg.vision_model || "")}">
        <p class="help">Reads the artwork first. Default: deepseek-v4.1-flash</p>
      </div>
      <div class="field">
        <label for="caption-backup-model">Backup model</label>
        <input id="caption-backup-model" type="text" value="${escapeHtml(cfg.backup_model || "")}">
        <p class="help">Takes over when the vision model fails. Default: mimo-v2.6-flash</p>
      </div>
      <div class="field">
        <label for="caption-strong-model">Strong model (optional)</label>
        <input id="caption-strong-model" type="text" value="${escapeHtml(cfg.strong_model || "")}"
               placeholder="e.g. kimi-k3">
        <p class="help">A third, stronger model for characters the first two are unsure about
          (new gacha/anime characters). Empty = off. It also double-checks a confident answer:
          if two models name different characters, the set waits for you instead of guessing.</p>
      </div>
      <label class="mini-check"><input type="checkbox" id="caption-add-ai-tag" ${cfg.add_ai_tag === false ? "" : "checked"}> Always add an AI tag (aiart)</label>
      <div class="row">
        <button class="primary" data-caption-action="save">Save</button>
        <button data-caption-action="test">Test connection</button>
        <button class="danger" data-caption-action="remove">Remove</button>
      </div>
      <p class="result" id="caption-result"></p>
    </article>
  `);
  document.getElementById("account-caption")
    .querySelectorAll("button[data-caption-action]")
    .forEach((button) => {
      button.addEventListener("click", () => captionAction(button.dataset.captionAction));
    });
}

async function captionAction(action) {
  let resultNode = document.getElementById("caption-result");

  if (action === "save") {
    const response = await api("/api/caption/settings", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        api_key: document.getElementById("caption-api-key").value,
        vision_model: document.getElementById("caption-vision-model").value,
        backup_model: document.getElementById("caption-backup-model").value,
        strong_model: document.getElementById("caption-strong-model").value,
        add_ai_tag: document.getElementById("caption-add-ai-tag").checked,
      }),
    });
    await loadCaptionSettings();
    renderAccounts();
    resultNode = document.getElementById("caption-result");
    if (resultNode) {
      resultNode.textContent = response.ok ? "Saved." : (response.error || "Could not save.");
      resultNode.className = response.ok ? "result ok" : "result err";
    }
    return;
  }

  if (action === "test") {
    if (resultNode) { resultNode.textContent = "Testing both models…"; resultNode.className = "result"; }
    const response = await api("/api/caption/test", { method: "POST" });
    if (!resultNode) return;
    if (response.ok) {
      resultNode.textContent = response.detail || "OK";
      resultNode.className = "result ok";
    } else {
      resultNode.textContent = response.error || "Failed.";
      resultNode.className = "result err";
    }
    return;
  }

  if (action === "remove") {
    if (!window.confirm("Remove the Caption AI settings?")) return;
    await api("/api/caption/settings", { method: "DELETE" });
    await loadCaptionSettings();
    renderAccounts();
  }
}

async function accountAction(action, platformId) {
  const platform = platformById(platformId);
  if (!platform) return;
  let resultNode = document.getElementById("result-" + platformId);

  if (action === "save") {
    const body = {};
    platform.fields.forEach((field) => {
      const input = document.getElementById(`acc-${platformId}-${field.key}`);
      if (input) body[field.key] = input.value;
    });
    const response = await api(`/api/accounts/${platformId}`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    await loadPlatforms();
    renderAccounts();
    renderCards();
    resultNode = document.getElementById("result-" + platformId);
    if (resultNode) {
      if (response.ok) {
        resultNode.textContent = response.warning ? `Saved. ${response.warning}` : "Saved.";
        resultNode.className = response.warning ? "result warn" : "result ok";
      } else {
        resultNode.textContent = response.error || "Could not save.";
        resultNode.className = "result err";
      }
    }
    return;
  }

  if (action === "test") {
    if (resultNode) { resultNode.textContent = "Testing…"; resultNode.className = "result"; }
    const response = await api(`/api/test/${platformId}`, { method: "POST" });
    if (!resultNode) return;
    if (response.ok) {
      resultNode.textContent = response.detail || "OK";
      resultNode.className = "result ok";
    } else {
      resultNode.textContent = response.error || "Failed.";
      resultNode.className = "result err";
    }
    return;
  }

  if (action === "remove") {
    if (!window.confirm(`Remove the saved settings for ${platform.label}?`)) return;
    await api(`/api/accounts/${platformId}`, { method: "DELETE" });
    await loadPlatforms();
    renderAccounts();
    renderCards();
    return;
  }

  console.warn("ArtDrop: unknown action", action, "for", platformId);
}

/* --------------------------------------------------------- lazy mode (M6) */

async function loadLazy() {
  state.lazy = await api("/api/lazy");
  renderLazyBanner();
}

async function loadQueue() {
  state.queue = await api("/api/queue");
  renderLazySettings();
  renderQueueList();
  renderLazyBanner();
}

function renderLazyBanner() {
  const banner = $("#lazy-banner");
  if (!banner) return;
  const settings = state.lazy ? state.lazy.settings : null;
  const counts = state.queue ? state.queue.queue.counts : null;
  banner.hidden = !(settings && settings.enabled);
  if (banner.hidden) {
    const badge = $("#queue-badge");
    if (badge && counts) {
      badge.hidden = !counts.needs_you;
      badge.textContent = String(counts.needs_you || "");
    }
    return;
  }
  const pause = $("#lazy-pause");
  pause.textContent = settings.paused ? "Resume" : "Pause";
  const parts = [];
  if (settings.paused) parts.push("paused");
  if (counts) {
    parts.push(`${counts.pending} waiting`);
    if (counts.failed) parts.push(`${counts.failed} failed`);
    if (counts.missing) parts.push(`${counts.missing} missing files`);
    if (counts.needs_you) parts.push(`${counts.needs_you} need you`);
  }
  $("#lazy-counts").textContent = parts.join(" · ");
  const badge = $("#queue-badge");
  if (badge) {
    badge.hidden = !(counts && counts.needs_you);
    badge.textContent = String(counts ? counts.needs_you : "");
  }
}

function renderLazySettings() {
  const data = state.lazy;
  const root = $("#lazy-settings");
  if (!data || !root) return;
  const settings = data.settings;
  const folders = settings.folders.map((folder) => `
    <div class="lazy-folder" data-folder="${escapeHtml(folder.key)}">
      <label class="mini-label">Folder
        <input type="text" data-lazy-path="${escapeHtml(folder.key)}"
               value="${escapeHtml(folder.path)}" autocomplete="off">
      </label>
      <p class="help">${escapeHtml(folder.resolved)}${folder.exists ? "" : " (will be created)"}</p>
      <label class="mini-check"><input type="checkbox" data-lazy-mature="${escapeHtml(folder.key)}"
        ${folder.mature ? "checked" : ""}> Treat these pieces as mature</label>
      <label class="mini-check"><input type="checkbox" data-lazy-pixiv="${escapeHtml(folder.key)}"
        ${folder.pixiv === false ? "" : "checked"}> Also prepare for Pixiv</label>
      <div class="lazy-cards">
        ${data.cards.map((card) => `
          <label class="mini-check"><input type="checkbox" data-lazy-card="${escapeHtml(card.id)}"
            data-folder-key="${escapeHtml(folder.key)}"
            ${folder.cards.includes(card.id) ? "checked" : ""}> ${escapeHtml(card.label)}</label>
        `).join("")}
      </div>
    </div>`).join("");
  const sites = Object.entries(data.sites).map(([site, limits]) => `
    <label class="mini-label">${escapeHtml(site)}
      <span class="lazy-limits">
        <input type="number" min="1" max="30" data-lazy-per="${escapeHtml(site)}"
               value="${limits.per_post}"> images / post ·
        <input type="number" min="${data.min_spacing_minutes}" step="15"
               data-lazy-spacing="${escapeHtml(site)}" value="${limits.spacing_minutes}"> min apart
      </span>
    </label>`).join("");
  root.innerHTML = `
    <article class="card">
      <header><span class="name">Lazy mode</span>
        <span class="badge ${settings.enabled ? "ok" : "miss"}">${settings.enabled ? "on" : "off"}</span>
      </header>
      <label class="mini-check"><input type="checkbox" id="lazy-enabled"
        ${settings.enabled ? "checked" : ""}> Post from my outbox folders automatically</label>
      <p class="help">Drop images into a folder and they queue themselves. Pixiv always waits for
        you (rating + Post) unless "Also prepare for Pixiv" is off for that folder. Nothing posts
        more often than the spacing below, and the minimum is ${data.min_spacing_minutes} minutes.</p>
      ${folders}
      <details><summary>Per-site images and spacing</summary>${sites}</details>
      <label class="mini-check"><input type="checkbox" id="lazy-quiet"
        ${settings.quiet.enabled ? "checked" : ""}> Quiet hours
        <input type="number" id="lazy-quiet-start" min="0" max="23" value="${settings.quiet.start}">
        to <input type="number" id="lazy-quiet-end" min="0" max="24" value="${settings.quiet.end}">
      </label>
      <div class="row">
        <button class="primary" id="lazy-save">Save</button>
        <button id="lazy-scan">Scan folders now</button>
        <span id="lazy-result" class="result"></span>
      </div>
      <p class="help">Folders live in ${escapeHtml(data.root)}. Finished pieces move to
        <code>posted\\&lt;date&gt;\\</code> with a <code>links.json</code>.</p>
    </article>`;

  $("#lazy-save").addEventListener("click", saveLazySettings);
  $("#lazy-scan").addEventListener("click", scanLazy);
}

function saveLazySettings() {
  const folders = (state.lazy.settings.folders || []).map((folder) => ({
    key: folder.key,
    path: document.querySelector(`[data-lazy-path="${folder.key}"]`).value,
    mature: document.querySelector(`[data-lazy-mature="${folder.key}"]`).checked,
    pixiv: document.querySelector(`[data-lazy-pixiv="${folder.key}"]`).checked,
    cards: Array.from(document.querySelectorAll(`[data-folder-key="${folder.key}"]`))
      .filter((node) => node.checked).map((node) => node.dataset.lazyCard),
  }));
  const sites = {};
  Object.keys(state.lazy.sites).forEach((site) => {
    const per = document.querySelector(`[data-lazy-per="${site}"]`);
    const spacing = document.querySelector(`[data-lazy-spacing="${site}"]`);
    sites[site] = { per_post: Number(per.value), spacing_minutes: Number(spacing.value) };
  });
  api("/api/lazy", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      enabled: $("#lazy-enabled").checked,
      folders,
      sites,
      quiet: {
        enabled: $("#lazy-quiet").checked,
        start: Number($("#lazy-quiet-start").value),
        end: Number($("#lazy-quiet-end").value),
      },
    }),
  }).then((response) => {
    const result = $("#lazy-result");
    result.textContent = response.ok ? "Saved." : (response.error || "Could not save.");
    result.className = response.ok ? "result ok" : "result err";
    if (response.ok) { state.lazy = response; renderLazySettings(); renderLazyBanner(); }
  });
}

async function scanLazy() {
  const result = $("#lazy-result");
  result.textContent = "Looking…";
  const response = await api("/api/lazy/scan", { method: "POST" });
  if (!response.ok) {
    result.textContent = response.error || "Scan failed.";
    result.className = "result err";
    return;
  }
  const summary = response.summary || {};
  result.textContent = `Queued ${summary.queued || 0}, needing you: ${summary.needs_you || 0}` +
    (summary.reason ? ` (${summary.reason})` : "");
  result.className = "result ok";
  state.lazy = response;
  await loadQueue();
}

function whenText(stamp) {
  if (!stamp) return "";
  const date = new Date(stamp * 1000);
  const now = Date.now() / 1000;
  const minutes = Math.round((stamp - now) / 60);
  if (Math.abs(minutes) < 60) {
    return minutes <= 0 ? "now" : `in ${minutes} min`;
  }
  return date.toLocaleString([], { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" });
}

function renderQueueList() {
  const root = $("#queue-list");
  const data = state.queue;
  if (!data || !root) return;
  const labels = {};
  (data.cards || []).forEach((card) => { labels[card.id] = card.label; });
  const groups = (data.queue.cards || []).map((group) => {
    const sets = [];
    const byKey = {};
    group.items.forEach((item) => {
      const key = item.batch_key || `item-${item.id}`;
      if (!byKey[key]) {
        byKey[key] = { key, items: [] };
        sets.push(byKey[key]);
      }
      byKey[key].items.push(item);
    });
    return `
    <article class="card queue-card">
      <header><span class="name">${escapeHtml(labels[group.card_id] || group.card_id)}</span>
        <span class="badge ${group.pending ? "ok" : "miss"}">${group.pending} waiting</span>
        <span class="muted">${escapeHtml(whenText(group.next_due))}</span>
      </header>
      ${sets.map((set) => {
        const first = set.items[0];
        const who = [first.character, first.series].filter(Boolean).join(" — ");
        const actionable = set.items.some((item) => item.status === "pending"
          || item.status === "failed");
        return `
        <div class="queue-set">
          ${who ? `<p class="set-title">${escapeHtml(who)}
            <span class="muted">· ${set.items.reduce((sum, item) => sum + item.images.length, 0)}
            image${set.items.reduce((sum, item) => sum + item.images.length, 0) === 1 ? "" : "s"}</span>
            ${actionable ? `<button data-back="${escapeHtml(set.key)}">Wrong? Move to Needs you</button>` : ""}
          </p>` : ""}
          ${set.items.map((item) => `
            <div class="queue-item ${item.status}">
              <img src="/api/queue/${item.id}/thumb" alt="">
              <div>
                <p class="muted">${escapeHtml(whenText(item.due_at))} ·
                  ${item.images.length} image${item.images.length === 1 ? "" : "s"} ·
                  ${escapeHtml(item.status)}</p>
                <p class="queue-caption">${escapeHtml(item.caption || "(no caption yet)")}</p>
                ${item.error ? `<p class="help warn-text">${escapeHtml(item.error)}</p>` : ""}
                ${item.url ? `<p class="help"><a href="${escapeHtml(item.url)}" target="_blank" rel="noopener">open post</a></p>` : ""}
              </div>
              <div class="queue-actions">
                ${item.status === "pending" ? `<button data-queue="now" data-id="${item.id}">Post now</button>` : ""}
                ${item.status === "failed" ? `<button data-queue="retry" data-id="${item.id}">Retry</button>` : ""}
                <button data-queue="edit" data-id="${item.id}">Edit caption</button>
                <button data-queue="skip" data-id="${item.id}">Skip</button>
                <button class="danger" data-queue="remove" data-id="${item.id}">Remove</button>
              </div>
            </div>`).join("")}
        </div>`;
      }).join("")}
    </article>`;
  }).join("");

  const needs = (data.needs_you || []).map((entry) => `
    <div class="queue-item needs-you">
      <img src="/api/needs-you/${entry.id}/thumb" alt="">
      <div>
        <p class="muted">${escapeHtml(entry.kind === "pixiv" ? "Needs you: Pixiv" : "Needs you: unknown character")}
          · ${entry.images.length} image${entry.images.length === 1 ? "" : "s"}</p>
        <p class="queue-caption">${escapeHtml(entry.caption || "(the AI was not sure — write it yourself)")}</p>
      </div>
      <div class="queue-actions">
        <button class="primary" data-needs="${entry.id}">Open in the Post tab</button>
        ${entry.note ? `<button data-needs-retry="${entry.id}">Retry AI</button>` : ""}
        <button data-needs-skip="${entry.id}">Dismiss</button>
      </div>
      ${entry.note ? `<p class="help warn-text">${escapeHtml(entry.note)}</p>` : ""}
      ${entry.waiting_pixiv ? `<p class="help">Waiting for your Pixiv decision — everything
        else is done, and the files stay in the outbox until you post it or dismiss this.</p>` : ""}
    </div>`).join("");

  const stuck = (data.stuck || []).map((entry) => `
    <div class="queue-item">
      <div>
        <p class="muted">Stuck · ${escapeHtml(entry.folder)}</p>
        <p class="queue-caption">${escapeHtml(entry.file)}</p>
        <p class="help">Not an image ArtDrop can post — move it out of the folder or convert it.</p>
      </div>
    </div>`).join("");

  root.innerHTML = `
    ${needs ? `<h3 class="section-title">Needs you</h3><article class="card">${needs}</article>` : ""}
    ${stuck ? `<h3 class="section-title">Stuck</h3><article class="card">${stuck}</article>` : ""}
    <h3 class="section-title">Queue</h3>
    ${groups || `<article class="card"><p class="muted">Nothing queued yet — drop images into
      ${escapeHtml((state.lazy && state.lazy.root) || "Pictures\\ArtDrop")} and press
      “Scan folders now”.</p></article>`}`;

  root.querySelectorAll("button[data-queue]").forEach((button) => {
    button.addEventListener("click", () =>
      queueAction(button.dataset.queue, Number(button.dataset.id)));
  });
  root.querySelectorAll("button[data-back]").forEach((button) => {
    button.addEventListener("click", () => pullBackSet(button.dataset.back));
  });
  root.querySelectorAll("button[data-needs]").forEach((button) => {
    button.addEventListener("click", () => useNeedsYou(Number(button.dataset.needs)));
  });
  root.querySelectorAll("button[data-needs-retry]").forEach((button) => {
    button.addEventListener("click", () => retryNeedsYouAI(button.dataset.needsRetry));
  });
  root.querySelectorAll("button[data-needs-skip]").forEach((button) => {
    button.addEventListener("click", async () => {
      await api(`/api/needs-you/${button.dataset.needsSkip}/dismiss`, { method: "POST" });
      await loadQueue();
    });
  });
}

async function queueAction(action, itemId) {
  if (action === "edit") {
    const entry = findQueueItem(itemId);
    const caption = window.prompt("Caption for this post", entry ? entry.caption : "");
    if (caption === null) return;
    await api(`/api/queue/${itemId}/caption`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ caption }),
    });
    await loadQueue();
    return;
  }
  const response = await api(`/api/queue/${itemId}/${action}`, { method: "POST" });
  if (!response.ok) toast(response.error || "That did not work");
  await loadQueue();
  await loadHistory();
}

async function retryNeedsYouAI(entryId) {
  const response = await api(`/api/needs-you/${entryId}/retry-ai`, { method: "POST" });
  if (!response.ok) return toast(response.error || "Could not retry that one");
  toast("Trying the AI again — watch the queue");
  await loadQueue();
  await loadHistory();
}

async function pullBackSet(batchKey) {
  const response = await api("/api/lazy/pull-back", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ batch_key: batchKey }),
  });
  if (!response.ok) return toast(response.error || "Could not pull that set back");
  toast("Moved to Needs you — open it from there when you are ready");
  await loadQueue();
}

function findQueueItem(itemId) {  const groups = (state.queue && state.queue.queue.cards) || [];
  for (const group of groups) {
    const found = group.items.find((item) => item.id === itemId);
    if (found) return found;
  }
  return null;
}

async function useNeedsYou(entryId) {
  const response = await api(`/api/needs-you/${entryId}/use`, { method: "POST" });
  if (!response.ok) return toast(response.error || "Could not open that batch");
  state.lastPostId = response.post_id;
  state.postImages = response.images || [];
  state.files = [];
  $("#caption").value = response.caption || "";
  $("#tags").value = (response.tags || []).join(", ");
  $("#alt").value = response.alt_text || "";
  $("#character").value = response.character || "";
  $("#series").value = response.series || "";
  state.mixed = response.mixed || null;
  $("#mature").checked = !!response.mature;  // the folder's default travels with the batch
  state.selected = new Set();
  if (response.kind === "pixiv") {
    state.platforms.filter((platform) => platform.id.startsWith("pixiv:"))
      .forEach((platform) => state.selected.add(platform.id));
  } else {
    // the folder's cards, whether or not the AI wrote something for each of them
    (response.cards || []).forEach((cardId) => {
      if (platformById(cardId)) state.selected.add(cardId);
    });
  }
  renderCards();  // rebuild the cards first: the boxes below have to exist after this
  // The saved text is per site ("pixiv"); the boxes are per card ("pixiv:main").
  Object.entries(response.platforms || {}).forEach(([site, text]) => {
    if (!text) return;
    state.platforms
      .filter((platform) => platform.id === site || platform.id.startsWith(site + ":"))
      .forEach((platform) => {
        const override = document.getElementById("override-" + platform.id);
        if (override && text.caption) override.value = text.caption;
        const tags = document.getElementById("tags-" + platform.id);
        if (tags && text.tags) tags.value = (text.tags || []).join(", ");
        const title = document.getElementById(`opt-${platform.id}-title`);
        if (title && text.title) title.value = text.title;
      });
  });
  renderThumbs();
  updateLimits();
  state.platforms.forEach((platform) => paintRating(platform.id));
  showTab("post");
  toast(response.kind === "pixiv"
    ? "Pick the Pixiv rating, then press Post"
    : "Batch loaded — check the caption and press Post");
}

/* ---------------------------------------------------------------- history */

async function loadHistory() {
  const data = await api("/api/history");
  const items = data.items || [];
  const root = $("#history");
  if (!items.length) {
    root.innerHTML = '<p class="muted">Nothing yet.</p>';
    return;
  }
  root.innerHTML = `
    <table>
      <thead><tr><th>When</th><th>Site</th><th>Result</th><th>Post</th><th>Info</th></tr></thead>
      <tbody>
        ${items.map((row) => {
          const when = new Date(row.ts * 1000).toLocaleString();
          const found = state.platforms.find((platform) => platform.id === row.platform);
          const label = found ? found.label : row.platform;
          const result = row.ok ? (row.dry_run ? "dry run ✓" : "posted ✓") : "failed ✗";
          const link = row.url
            ? `<a href="${escapeHtml(row.url)}" target="_blank" rel="noopener">open</a>`
            : "";
          const info = row.error
            ? escapeHtml(row.error)
            : [row.note, (row.caption || "").slice(0, 90)]
                .filter(Boolean).map(escapeHtml).join(" — ");
          return `<tr><td>${escapeHtml(when)}</td><td>${escapeHtml(label)}</td><td>${result}</td><td>${link}</td><td>${info}</td></tr>`;
        }).join("")}
      </tbody>
    </table>`;
}

/* ------------------------------------------------------------------ prefs */

function savePrefs() {
  try {
    localStorage.setItem("artdrop.selected", JSON.stringify([...state.selected]));
    localStorage.setItem("artdrop.dry", $("#dry-run").checked ? "1" : "0");
  } catch (error) { /* storage may be blocked — not important */ }
}

function loadPrefs() {
  try {
    const stored = JSON.parse(localStorage.getItem("artdrop.selected") || "[]");
    state.selected = new Set(
      stored.filter((id) => state.platforms.some((platform) => platform.id === id))
    );
    $("#dry-run").checked = localStorage.getItem("artdrop.dry") === "1";
    state.lastRatings = JSON.parse(localStorage.getItem("artdrop.ratings") || "{}");
  } catch (error) { /* ignore */ }
}

/* ---------------------------------------------------------------- ratings */

function saveRating(cardId, value) {
  state.lastRatings[cardId] = value;
  try {
    const stored = JSON.parse(localStorage.getItem("artdrop.ratings") || "{}");
    stored[cardId] = value;
    localStorage.setItem("artdrop.ratings", JSON.stringify(stored));
  } catch (error) { /* storage may be blocked — not important */ }
}

function paintRating(cardId) {
  const group = document.getElementById("rating-" + cardId);
  if (!group) return;
  const chosen = state.ratings[cardId] || "";
  const last = state.lastRatings[cardId] || "";
  group.querySelectorAll("button.rating").forEach((button) => {
    button.classList.toggle("selected", button.dataset.rating === chosen);
    button.classList.toggle("last", !chosen && button.dataset.rating === last);
  });
  const hint = document.getElementById("rating-hint-" + cardId);
  if (!hint) return;
  if (chosen === "all" && $("#mature").checked) {
    hint.textContent = "Are you sure? Pixiv requires R-18 for sexual content.";
    hint.className = "help warn-text";
  } else if (!chosen && last) {
    const previous = Array.from(group.querySelectorAll("button.rating"))
      .find((button) => button.dataset.rating === last);
    hint.textContent = `Last time: ${previous ? previous.textContent.trim() : last} — choose again`;
    hint.className = "help";
  } else {
    hint.textContent = "Pixiv requires a rating on every post.";
    hint.className = "help";
  }
}

/* ------------------------------------------------------------------- init */

async function init() {
  document.querySelectorAll(".tab").forEach((button) => {
    button.addEventListener("click", () => showTab(button.dataset.tab));
  });
  $("#refresh-history").addEventListener("click", loadHistory);
  $("#post").addEventListener("click", doPost);
  $("#new-post").addEventListener("click", newPost);
  $("#generate").addEventListener("click", generate);
  $("#caption").addEventListener("input", updateLimits);
  $("#tags").addEventListener("input", updateLimits);
  $("#character").addEventListener("input", updateLimits);
  $("#series").addEventListener("input", updateLimits);
  $("#dry-run").addEventListener("change", savePrefs);
  $("#lazy-pause").addEventListener("click", async () => {
    const paused = !(state.lazy && state.lazy.settings.paused);
    const response = await api("/api/lazy/pause", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ paused }),
    });
    if (response.ok) state.lazy = response;
    renderLazyBanner();
    if (state.queue) await loadQueue();
    toast(paused ? "Lazy mode paused" : "Lazy mode running again");
  });
  $("#mature").addEventListener("change", () => {
    state.platforms.forEach((platform) => paintRating(platform.id));
  });
  bindDropZone();

  await loadPlatforms();
  await loadCaptionSettings();
  await loadSettings();
  await loadLazy();
  await loadQueue();
  loadPrefs();
  renderCards();
  renderAccounts();
  await loadHistory();
}

init();
