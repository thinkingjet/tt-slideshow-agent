const state = {
  jobs: [],
  clips: [],
  accounts: [],
  scheduledPosts: [],
  postbridgeConfigured: false,
  researchConfigured: false,
  supabase: { configured: false, url: null, anonKey: null },
  session: JSON.parse(localStorage.getItem("clip_creator_supabase_session") || "null"),
  savedProducts: [],
  researchStep: "input",
  researchInput: null,
  productResearch: null,
  slideshowResult: null,
  slideshowDraft: null,
  slideshowDraftResult: null,
  slideshowSelectedSlide: 0,
  activeView: "product-research",
  selectedJobId: null,
  clipFilter: "all",
};

const $ = (selector) => document.querySelector(selector);

const viewMeta = {
  "product-research": {
    eyebrow: "Product Research",
    title: "Turn product context into TikTok angles.",
  },
  "cta-generator": {
    eyebrow: "CTA Generator",
    title: "Generate 10s CTA videos for testing.",
  },
  "slideshows": {
    eyebrow: "Slideshows",
    title: "Generate TikTok slideshows from research.",
  },
  "clip-creator": {
    eyebrow: "Clip Creator",
    title: "Upload CTA. Paste a Shorts channel. Generate clips.",
  },
};

function setMessage(element, text, isError = false) {
  element.textContent = text || "";
  element.classList.toggle("error", isError);
}

function authHeaders() {
  return state.session?.access_token
    ? { Authorization: `Bearer ${state.session.access_token}` }
    : {};
}

function saveSession(session) {
  state.session = session;
  if (session) {
    localStorage.setItem("clip_creator_supabase_session", JSON.stringify(session));
  } else {
    localStorage.removeItem("clip_creator_supabase_session");
  }
  renderAuthState();
}

function setActiveView(view) {
  state.activeView = view;
  document.querySelectorAll("[data-view-panel]").forEach((panel) => {
    panel.classList.toggle("active", panel.dataset.viewPanel === view);
  });
  document.querySelectorAll(".sidebar-tab").forEach((tab) => {
    tab.classList.toggle("active", tab.dataset.view === view);
  });
  $("#view-eyebrow").textContent = viewMeta[view]?.eyebrow || "";
  $("#view-title").textContent = viewMeta[view]?.title || "";

  if (view === "cta-generator" || view === "slideshows") {
    // Keep dropdown fresh when navigating to CTA Generator.
    loadSavedProducts().catch(() => {});
  }
}

function setResearchStep(step) {
  state.researchStep = step;
  document.querySelectorAll("[data-research-step]").forEach((panel) => {
    panel.classList.toggle("active", panel.dataset.researchStep === step);
  });
}

async function api(path, options = {}) {
  const headers = {
    ...(options.headers || {}),
    ...authHeaders(),
  };
  const response = await fetch(path, { ...options, headers });
  if (!response.ok) {
    const raw = await response.text().catch(() => "");
    try {
      const payload = raw ? JSON.parse(raw) : {};
      const message = payload.detail || payload.message || JSON.stringify(payload) || response.statusText;
      // If the stored Supabase session is expired/invalid, clear it so the UI returns to sign-in state.
      if (response.status === 401 && String(message).toLowerCase().includes("supabase session")) {
        saveSession(null);
      }
      throw new Error(message);
    } catch (_error) {
      if (response.status === 401 && raw.toLowerCase().includes("supabase session")) {
        saveSession(null);
      }
      throw new Error(raw || response.statusText);
    }
  }
  const raw = await response.text();
  return raw ? JSON.parse(raw) : {};
}

async function supabaseAuth(path, body) {
  if (!state.supabase.configured) {
    throw new Error("Supabase is not configured yet.");
  }
  const response = await fetch(`${state.supabase.url}${path}`, {
    method: "POST",
    headers: {
      apikey: state.supabase.anonKey,
      "Content-Type": "application/json",
    },
    body: JSON.stringify(body),
  });
  const payload = await response.json().catch(() => ({}));
  if (!response.ok) {
    throw new Error(payload.msg || payload.message || payload.error_description || "Supabase auth failed.");
  }
  return payload;
}

function formatDate(value) {
  if (!value) return "Unscheduled";
  return new Date(value).toLocaleString([], {
    month: "short",
    day: "numeric",
    hour: "numeric",
    minute: "2-digit",
  });
}

function selectedClipIds() {
  return [...document.querySelectorAll(".clip-select:checked")].map((input) => input.value);
}

function selectedAccountIds() {
  return [...document.querySelectorAll(".account-select:checked")].map((input) => Number(input.value));
}

function escapeHtml(value) {
  return String(value || "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#039;");
}

function asArray(value) {
  return Array.isArray(value) ? value : [];
}

function listHtml(items) {
  const values = asArray(items).filter(Boolean);
  if (!values.length) return '<p class="muted">Nothing returned.</p>';
  return `<ul>${values.map((item) => `<li>${escapeHtml(item)}</li>`).join("")}</ul>`;
}

function hashtagsHtml(items) {
  return asArray(items)
    .filter(Boolean)
    .map((tag) => `<span class="mini-pill">#${escapeHtml(String(tag).replace(/^#/, ""))}</span>`)
    .join("");
}

function researchBundleText(plan) {
  return [
    `Product: ${plan.product?.name || ""}`,
    "",
    "Positioning:",
    plan.product?.positioning || "",
    "",
    "Hooks:",
    ...asArray(plan.hookBank).map((hook) => `- ${hook}`),
    "",
    "Captions:",
    ...asArray(plan.captions).map((caption) => `- ${caption}`),
    "",
    "CTAs:",
    ...asArray(plan.ctas).map((cta) => `- ${cta}`),
    "",
    "Hashtags:",
    asArray(plan.hashtags).map((tag) => `#${String(tag).replace(/^#/, "")}`).join(" "),
  ].join("\n");
}

function researchInputFromForm(form) {
  const formData = new FormData(form);
  const files = [...(form.querySelector('[name="files"]')?.files || [])].map((file) => file.name);
  return {
    product_name: formData.get("product_name") || "",
    product_url: formData.get("product_url") || "",
    audience: formData.get("audience") || "",
    product_context: formData.get("product_context") || "",
    files,
  };
}

function researchInputFromResult(result) {
  return {
    product_name: result?.product_name || result?.data?.product?.name || "",
    product_url: result?.product_url || "",
    audience: result?.audience || "",
    product_context: result?.product_context || "",
    files: asArray(result?.files).map((file) => file.file_name).filter(Boolean),
  };
}

function renderReadonlyInputs(input) {
  const container = $("#readonly-inputs");
  const fileText = input.files?.length ? input.files.join(", ") : "No files uploaded";
  container.innerHTML = `
    <div class="readonly-item">
      <span>Product name</span>
      <strong>${escapeHtml(input.product_name || "Not provided")}</strong>
    </div>
    <div class="readonly-item">
      <span>Product URL</span>
      <strong>${escapeHtml(input.product_url || "Not provided")}</strong>
    </div>
    <div class="readonly-item">
      <span>Audience</span>
      <strong>${escapeHtml(input.audience || "Not provided")}</strong>
    </div>
    <div class="readonly-item">
      <span>Context</span>
      <p>${escapeHtml(input.product_context || "No written context provided.")}</p>
    </div>
    <div class="readonly-item">
      <span>Files</span>
      <p>${escapeHtml(fileText)}</p>
    </div>
  `;
}

function jobProgress(job) {
  return Math.min(job.progress_percent || 0, 100);
}

function jobTotal(job) {
  return job.total_videos || job.requested_limit || 0;
}

function activeJob() {
  const running = state.jobs.find((job) => ["queued", "processing"].includes(job.status));
  if (running) return running;
  return state.jobs.find((job) => job.id === state.selectedJobId) || state.jobs[0] || null;
}

function visibleClips() {
  let clips = state.clips;
  if (state.selectedJobId) {
    clips = clips.filter((clip) => clip.job_id === state.selectedJobId);
  }
  if (state.clipFilter === "completed") {
    clips = clips.filter((clip) => clip.status === "completed");
  }
  if (state.clipFilter === "processing") {
    clips = clips.filter((clip) => ["pending", "downloading", "downloaded", "processing"].includes(clip.status));
  }
  if (state.clipFilter === "failed") {
    clips = clips.filter((clip) => clip.status === "failed");
  }
  return clips;
}

async function loadSettings() {
  const settings = await api("/api/settings");
  state.postbridgeConfigured = settings.postbridge_configured;
  state.researchConfigured = settings.research_configured;
  state.supabase = {
    configured: Boolean(settings.supabase?.configured),
    url: settings.supabase?.url,
    anonKey: settings.supabase?.anon_key,
  };
  const badge = $("#postbridge-status");
  badge.textContent = settings.postbridge_configured
    ? "PostBridge connected"
    : "PostBridge key missing";
  badge.classList.toggle("ok", settings.postbridge_configured);
  badge.classList.toggle("warn", !settings.postbridge_configured);

  const researchBadge = $("#research-status");
  researchBadge.textContent = settings.research_configured
    ? "Research engine ready"
    : "Research engine offline";
  researchBadge.classList.toggle("ok", settings.research_configured);
  researchBadge.classList.toggle("warn", !settings.research_configured);
  renderAuthState();
}

async function loadJobs() {
  const payload = await api("/api/jobs");
  state.jobs = payload.data || [];
  if (!state.selectedJobId && state.jobs.length) {
    state.selectedJobId = state.jobs[0].id;
  }
  renderActiveJob();
  renderJobs();
}

async function loadClips() {
  const payload = await api("/api/clips");
  state.clips = payload.data || [];
  renderClips();
}

async function loadAccounts() {
  const container = $("#accounts-list");
  container.textContent = "Loading accounts...";
  try {
    const payload = await api("/api/postbridge/accounts?platform=tiktok&platform=instagram&platform=youtube");
    state.accounts = payload.data || [];
    renderAccounts();
  } catch (error) {
    container.textContent = error.message;
  }
}

async function loadScheduledPosts() {
  const payload = await api("/api/scheduled-posts");
  state.scheduledPosts = payload.data || [];
  renderCalendar();
}

function renderJobs() {
  const container = $("#jobs-list");
  if (!state.jobs.length) {
    container.innerHTML = '<p class="muted">No batches yet. Upload a CTA and create your first batch.</p>';
    return;
  }
  container.innerHTML = state.jobs
    .map(
      (job) => `
        <article class="job-card ${job.id === state.selectedJobId ? "selected" : ""}" data-job-id="${job.id}">
          <div>
            <strong>${escapeHtml(job.source_url)}</strong>
            <p class="muted">${job.completed_videos}/${jobTotal(job)} ready · ${job.active_videos || 0} active · ${job.failed_videos} failed</p>
            <div class="mini-progress"><span style="width: ${jobProgress(job)}%"></span></div>
          </div>
          <span class="badge ${job.status}">${job.status}</span>
        </article>
      `,
    )
    .join("");
  container.querySelectorAll(".job-card").forEach((card) => {
    card.addEventListener("click", () => {
      state.selectedJobId = card.dataset.jobId;
      renderActiveJob();
      renderJobs();
      renderClips();
    });
  });
}

function renderActiveJob() {
  const container = $("#active-job");
  const job = activeJob();
  if (!job) {
    container.innerHTML = `
      <div class="empty-state">
        <strong>No active batch</strong>
        <span>Submit the form to start scraping Shorts and stitching your CTA.</span>
      </div>
    `;
    return;
  }
  const total = jobTotal(job);
  const progress = jobProgress(job);
  const currentStep =
    job.status === "queued"
      ? "Waiting to start"
      : job.status === "processing"
        ? "Downloading Shorts and stitching clips"
        : job.status === "completed"
          ? "Batch complete"
          : "Batch failed";
  container.innerHTML = `
    <article class="progress-card">
      <div class="progress-copy">
        <span class="badge ${job.status}">${job.status}</span>
        <h3>${currentStep}</h3>
        <p>${job.completed_videos}/${total} clips ready · ${job.pending_videos || 0} waiting · ${job.active_videos || 0} processing · ${job.failed_videos} failed</p>
      </div>
      <div class="progress-meter" aria-label="${progress}% complete">
        <span style="width: ${progress}%"></span>
      </div>
      <div class="progress-meta">
        <span>${progress}% complete</span>
        <span>${job.hook_seconds}s hook + CTA</span>
      </div>
      ${job.error ? `<p class="message error">${escapeHtml(job.error)}</p>` : ""}
    </article>
  `;
}

function renderClips() {
  const container = $("#clips-grid");
  const clips = visibleClips();
  if (!clips.length) {
    container.innerHTML = '<p class="muted">Clips for this batch will show here as soon as YouTube discovery starts.</p>';
    return;
  }
  container.innerHTML = clips
    .map(
      (clip) => `
        <article class="clip-card ${clip.status}">
          ${
            clip.output_url
              ? `<video src="${clip.output_url}" controls playsinline preload="metadata"></video>`
              : `<div class="clip-placeholder"><span>${clip.status}</span></div>`
          }
          <div class="clip-body">
            <p class="clip-title">${escapeHtml(clip.title || clip.youtube_id)}</p>
            <div class="clip-actions">
              <label>
                <input class="clip-select" type="checkbox" value="${clip.id}" ${clip.status !== "completed" ? "disabled" : ""} />
                Select
              </label>
              ${clip.output_url ? `<a href="${clip.output_url}" download>Download</a>` : "<span class=\"muted\">Not ready</span>"}
            </div>
            <span class="badge ${clip.status}">${clip.status}</span>
            ${clip.error ? `<p class="message error">${escapeHtml(clip.error)}</p>` : ""}
          </div>
        </article>
      `,
    )
    .join("");
}

function renderAccounts() {
  const container = $("#accounts-list");
  if (!state.accounts.length) {
    container.innerHTML = '<p class="muted">No TikTok, Instagram, or YouTube accounts returned.</p>';
    return;
  }
  container.innerHTML = state.accounts
    .map(
      (account) => `
        <label class="account-option">
          <input class="account-select" type="checkbox" value="${account.id}" />
          <span>${escapeHtml(account.platform)} · @${escapeHtml(account.username || account.id)}</span>
        </label>
      `,
    )
    .join("");
}

function renderCalendar() {
  const container = $("#calendar");
  if (!state.scheduledPosts.length) {
    container.innerHTML = '<p class="muted">Scheduled clips will appear here.</p>';
    return;
  }

  const grouped = new Map();
  for (const post of state.scheduledPosts) {
    const day = post.scheduled_at
      ? new Date(post.scheduled_at).toLocaleDateString([], { month: "short", day: "numeric" })
      : "Unscheduled";
    grouped.set(day, [...(grouped.get(day) || []), post]);
  }

  container.innerHTML = [...grouped.entries()]
    .map(
      ([day, posts]) => `
        <div class="calendar-day">
          <strong>${day}</strong>
          ${posts
            .map(
              (post) => `
                <div class="calendar-item">
                  ${formatDate(post.scheduled_at)}<br />
                  ${escapeHtml(post.caption || "No caption")}<br />
                  <span class="badge ${post.status}">${post.status}</span>
                </div>
              `,
            )
            .join("")}
        </div>
      `,
    )
    .join("");
}

function renderAuthState() {
  const authForm = $("#auth-form");
  const signoutButton = $("#signout-button");
  const authUser = $("#auth-user");
  const userMenu = $("#user-menu");
  const userAvatar = $("#user-avatar");
  if (!authForm || !signoutButton || !authUser || !userMenu || !userAvatar) return;

  if (!state.supabase.configured) {
    $("#auth-message").textContent = "Saved products are not configured.";
    authForm.classList.add("hidden");
    userMenu.classList.add("hidden");
    return;
  }

  if (state.session?.user?.email) {
    const email = state.session.user.email;
    authUser.textContent = email;
    userAvatar.textContent = email.slice(0, 1).toUpperCase();
    authForm.classList.add("hidden");
    userMenu.classList.remove("hidden");
  } else {
    authForm.classList.remove("hidden");
    userMenu.classList.add("hidden");
  }
}

async function loadSavedProducts() {
  const container = $("#saved-products-list");
  if (!state.session?.access_token) {
    container.textContent = "Sign in to load saved products.";
    state.savedProducts = [];
    renderCtaProjectsSelect();
    renderSlideshowProjectsSelect();
    return;
  }
  container.textContent = "Loading saved products...";
  try {
    const payload = await api("/api/product-research/projects");
    state.savedProducts = payload.data || [];
    renderSavedProducts();
    renderCtaProjectsSelect();
    renderSlideshowProjectsSelect();
  } catch (error) {
    container.textContent = error.message;
  }
}

function renderSavedProducts() {
  const container = $("#saved-products-list");
  if (!state.savedProducts.length) {
    container.innerHTML = '<p class="muted">No saved products yet. Generate one to save it here.</p>';
    return;
  }
  container.innerHTML = state.savedProducts
    .map((project) => {
      const summary = project.plan?.product?.summary || "Saved product research";
      return `
        <article class="saved-product-card" data-project-id="${project.id}">
          <strong>${escapeHtml(project.product_name)}</strong>
          <span>${escapeHtml(summary)}</span>
          <span>${formatDate(project.created_at)}</span>
          <div class="saved-product-actions">
            <button class="secondary load-product" type="button">Open</button>
            <button class="secondary delete-product" type="button">Delete</button>
          </div>
        </article>
      `;
    })
    .join("");

  container.querySelectorAll(".load-product").forEach((button) => {
    button.addEventListener("click", () => {
      loadSavedProduct(button.closest(".saved-product-card").dataset.projectId);
    });
  });
  container.querySelectorAll(".delete-product").forEach((button) => {
    button.addEventListener("click", () => {
      deleteSavedProduct(button.closest(".saved-product-card").dataset.projectId);
    });
  });
}

async function loadSavedProduct(projectId) {
  const message = $("#research-message");
  setMessage(message, "Loading saved product...");
  try {
    const result = await api(`/api/product-research/projects/${projectId}`);
    state.productResearch = result;
    state.researchInput = researchInputFromResult(result);
    renderReadonlyInputs(state.researchInput);
    renderResearchResults(result);
    setResearchStep("results");
    setMessage(message, "Saved product loaded.");
  } catch (error) {
    setMessage(message, error.message, true);
  }
}

async function deleteSavedProduct(projectId) {
  if (!window.confirm("Delete this saved product research?")) return;
  const message = $("#research-message");
  setMessage(message, "Deleting saved product...");
  try {
    await api(`/api/product-research/projects/${projectId}`, { method: "DELETE" });
    if (state.productResearch?.id === projectId) {
      state.productResearch = null;
      renderResearchResults(null);
    }
    await loadSavedProducts();
    setMessage(message, "Saved product deleted.");
  } catch (error) {
    setMessage(message, error.message, true);
  }
}

function renderResearchResults(result) {
  const container = $("#research-results");
  if (!result?.data) {
    container.innerHTML = "";
    return;
  }

  const plan = result.data;
  const product = plan.product || {};
  const sources = result.sources || [];
  container.innerHTML = `
    <div class="research-result-header">
      <div>
        <span class="mini-pill">AI research</span>
        <h3>${escapeHtml(product.name || "Product plan")}</h3>
        <p>${escapeHtml(product.summary || "")}</p>
      </div>
      <button class="secondary small-button" id="copy-research-plan">Copy plan</button>
    </div>

    <div class="research-card full">
      <span class="card-label">Positioning</span>
      <p>${escapeHtml(product.positioning || "")}</p>
    </div>

    <div class="research-mini-grid">
      <div class="research-card">
        <span class="card-label">Target Customers</span>
        ${listHtml(product.targetCustomers)}
      </div>
      <div class="research-card">
        <span class="card-label">Buying Triggers</span>
        ${listHtml(product.buyingTriggers)}
      </div>
      <div class="research-card">
        <span class="card-label">Objections</span>
        ${listHtml(product.objections)}
      </div>
    </div>

    <div class="research-section-title">Content angles</div>
    <div class="angle-list">
      ${asArray(plan.contentAngles)
        .map(
          (angle, index) => `
            <article class="angle-card">
              <span class="mini-pill">Angle ${index + 1}</span>
              <h4>${escapeHtml(angle.title)}</h4>
              <p>${escapeHtml(angle.insight)}</p>
              <div class="angle-grid">
                <div>
                  <span class="card-label">Hooks</span>
                  ${listHtml(angle.hookIdeas)}
                </div>
                <div>
                  <span class="card-label">Video</span>
                  <p>${escapeHtml(angle.videoConcept)}</p>
                  <p><strong>Caption:</strong> ${escapeHtml(angle.caption)}</p>
                  <p><strong>CTA:</strong> ${escapeHtml(angle.cta)}</p>
                </div>
              </div>
              <div class="tag-row">${hashtagsHtml(angle.hashtags)}</div>
            </article>
          `,
        )
        .join("")}
    </div>

    <div class="research-section-title">Hook bank</div>
    <div class="research-card full">${listHtml(plan.hookBank)}</div>

    <div class="research-mini-grid">
      <div class="research-card">
        <span class="card-label">Captions</span>
        ${listHtml(plan.captions)}
      </div>
      <div class="research-card">
        <span class="card-label">CTAs</span>
        ${listHtml(plan.ctas)}
      </div>
      <div class="research-card">
        <span class="card-label">Hashtags</span>
        <div class="tag-row">${hashtagsHtml(plan.hashtags)}</div>
      </div>
    </div>

    <div class="research-section-title">Posting plan</div>
    <div class="posting-plan">
      ${asArray(plan.postingPlan)
        .map(
          (item) => `
            <div class="posting-item">
              <span>${escapeHtml(item.day)}</span>
              <strong>${escapeHtml(item.angle)}</strong>
              <p>${escapeHtml(item.format)} · ${escapeHtml(item.hook)}</p>
              <p>${escapeHtml(item.caption)}</p>
              <p>${escapeHtml(item.cta)}</p>
            </div>
          `,
        )
        .join("")}
    </div>

    <details class="sources-block">
      <summary>Research notes + sources</summary>
      <div class="research-card full">
        ${listHtml(plan.researchNotes)}
      </div>
      <div class="source-list">
        ${
          sources.length
            ? sources
                .map(
                  (source) => `
                    <a href="${escapeHtml(source.url)}" target="_blank" rel="noopener noreferrer">
                      <strong>${escapeHtml(source.title)}</strong>
                      <span>${escapeHtml(source.url)}</span>
                    </a>
                  `,
                )
                .join("")
            : '<p class="muted">No sources returned.</p>'
        }
      </div>
    </details>
  `;

  $("#copy-research-plan")?.addEventListener("click", async () => {
    await navigator.clipboard.writeText(researchBundleText(plan));
    $("#copy-research-plan").textContent = "Copied";
    setTimeout(() => {
      $("#copy-research-plan").textContent = "Copy plan";
    }, 1400);
  });
}

async function refreshAll() {
  await Promise.all([loadSettings(), loadJobs(), loadClips(), loadScheduledPosts()]);
}

function syncCtaHintsFromResearch(form) {
  const productNameInput = form.querySelector('[name="product_name"]');
  const ctaHint1Input = form.querySelector('[name="cta_hint_1"]');
  const ctaHint2Input = form.querySelector('[name="cta_hint_2"]');
  if (!productNameInput || !ctaHint1Input || !ctaHint2Input) return;

  const plan = state.productResearch?.data;
  const productName = state.productResearch?.product_name || plan?.product?.name || "";
  const ctas = asArray(plan?.ctas);

  productNameInput.value = productName || "";
  ctaHint1Input.value = ctas[0] || "";
  ctaHint2Input.value = ctas[1] || ctas[0] || "";
}

function syncCtaGeneratorFromResearch(form) {
  // No-op now: CTA generator runs fully automated from saved project id.
  void form;
}

function renderCtaProjectsSelect() {
  const select = $("#cta-project-select");
  if (!select) return;
  const countLabel = $("#cta-products-count");
  const existing = select.value;
  const projects = Array.isArray(state.savedProducts) ? state.savedProducts : [];
  if (!state.session?.access_token) {
    select.innerHTML = '<option value="">Sign in to load saved products...</option>';
    select.value = "";
    if (countLabel) countLabel.textContent = "";
    return;
  }
  if (countLabel) countLabel.textContent = `${projects.length} saved product${projects.length === 1 ? "" : "s"} loaded`;

  if (!projects.length) {
    select.innerHTML = '<option value="">No saved products found (generate one in Product Research)</option>';
    select.value = "";
    return;
  }
  select.innerHTML = [
    '<option value="">Select a saved product...</option>',
    ...projects.map((p) => {
      const label = p.product_name || p.plan?.product?.name || "Saved product";
      const created = p.created_at ? new Date(p.created_at).toLocaleDateString() : "";
      return `<option value="${escapeHtml(p.id)}">${escapeHtml(label)}${created ? ` · ${escapeHtml(created)}` : ""}</option>`;
    }),
  ].join("");
  if (existing) select.value = existing;
}

function renderSlideshowProjectsSelect() {
  const select = $("#slideshow-project-select");
  if (!select) return;
  const countLabel = $("#slideshow-products-count");
  const existing = select.value;
  const projects = Array.isArray(state.savedProducts) ? state.savedProducts : [];
  if (!state.session?.access_token) {
    select.innerHTML = '<option value="">Sign in to load saved products...</option>';
    select.value = "";
    if (countLabel) countLabel.textContent = "";
    return;
  }
  if (countLabel) countLabel.textContent = `${projects.length} saved product${projects.length === 1 ? "" : "s"} loaded`;

  if (!projects.length) {
    select.innerHTML = '<option value="">No saved products found (generate one in Product Research)</option>';
    select.value = "";
    return;
  }
  select.innerHTML = [
    '<option value="">Select a saved product...</option>',
    ...projects.map((p) => {
      const label = p.product_name || p.plan?.product?.name || "Saved product";
      const created = p.created_at ? new Date(p.created_at).toLocaleDateString() : "";
      return `<option value="${escapeHtml(p.id)}">${escapeHtml(label)}${created ? ` · ${escapeHtml(created)}` : ""}</option>`;
    }),
  ].join("");
  if (existing) select.value = existing;
}

function slideTextLines(slide) {
  const textElements = asArray(slide?.text_elements || slide?.textElements).filter((el) => el && el.text);
  if (textElements.length) {
    return textElements.map((el) => String(el.text || "").trim()).filter(Boolean);
  }
  if (slide?.text) {
    return [String(slide.text).trim()];
  }
  return [];
}

function slideImageUrl(slide) {
  const candidates = [
    slide?.imageUrl,
    slide?.image_url,
    slide?.image?.url,
    slide?.image?.imageUrl,
    slide?.image?.image_url,
    slide?.asset?.signed_url,
    slide?.asset?.url,
  ];
  const first = candidates.find((value) => typeof value === "string" && value.trim());
  return first ? String(first).trim() : "";
}

function slideFallbackImageUrl(slide) {
  const candidates = [
    slide?.fallbackImageUrl,
    slide?.fallback_image_url,
  ];
  const first = candidates.find((value) => typeof value === "string" && value.trim());
  return first ? String(first).trim() : "";
}

function createSlideshowDraft(result) {
  return {
    hook: result?.hook ? String(result.hook).trim() : "",
    slides: asArray(result?.slides).map((slide, index) => ({
      id: slide?.id || `slide_${index + 1}`,
      imageUrl: slideImageUrl(slide),
      fallbackImageUrl: slideFallbackImageUrl(slide),
      text: slideTextLines(slide).join(" ").trim(),
    })),
  };
}

function slideshowPayloadFromDraft(result, draft) {
  const base = result?.slideshow && typeof result.slideshow === "object" ? result.slideshow : {};
  return {
    ...base,
    hook: draft.hook,
    slides: asArray(draft.slides).map((slide, index) => ({
      id: slide.id || `slide_${index + 1}`,
      order: index + 1,
      duration_s: 3,
      imageUrl: slide.imageUrl,
      fallbackImageUrl: slide.fallbackImageUrl || "",
      text_elements: [
        {
          id: `text_${index + 1}_1`,
          text: slide.text,
          position: { x: 20, y: index === 0 ? 160 : 180 },
          font_size: index === 0 ? 25 : 18,
          font_color: "#ffffff",
          width: 344,
          height: 140,
          text_align: "center",
        },
      ],
    })),
  };
}

function slideshowPlainText(result) {
  const slides = asArray(result?.slides);
  const hook = result?.hook ? String(result.hook).trim() : "";
  const lines = [];
  if (hook) {
    lines.push(`Hook: ${hook}`, "");
  }
  slides.forEach((slide, index) => {
    const textLines = slideTextLines(slide);
    lines.push(`Slide ${index + 1}: ${textLines.join(" ")}`.trim());
  });
  return lines.join("\n");
}

function renderSlideshowOutput(result) {
  const output = $("#slideshow-output");
  if (!output) return;
  if (!result) {
    output.innerHTML = "";
    state.slideshowDraft = null;
    state.slideshowDraftResult = null;
    state.slideshowSelectedSlide = 0;
    return;
  }

  if (state.slideshowDraftResult !== result) {
    state.slideshowDraft = createSlideshowDraft(result);
    state.slideshowDraftResult = result;
    state.slideshowSelectedSlide = 0;
  }

  const draft = state.slideshowDraft || createSlideshowDraft(result);
  const slides = asArray(draft.slides);
  const hook = draft.hook ? String(draft.hook).trim() : "";
  const selectedSlideIndex = Math.max(0, Math.min(Number(state.slideshowSelectedSlide) || 0, Math.max(slides.length - 1, 0)));
  state.slideshowSelectedSlide = selectedSlideIndex;
  const selectedSlide = slides[selectedSlideIndex] || null;
  const draftPayload = slideshowPayloadFromDraft(result, draft);
  const jsonPayload = JSON.stringify(draftPayload, null, 2);

  const selectedSlideHtml = selectedSlide
    ? (() => {
        const content = selectedSlide?.text ? escapeHtml(selectedSlide.text) : "No text returned.";
        const imageUrl = selectedSlide?.imageUrl || "";
        const fallbackImageUrl = selectedSlide?.fallbackImageUrl || "";
        const imageHtml = imageUrl
          ? `<img class="slideshow-image" src="${escapeHtml(imageUrl)}" alt="Slide ${selectedSlideIndex + 1}" loading="lazy" onerror="${fallbackImageUrl ? `this.onerror=null;this.src='${escapeHtml(fallbackImageUrl)}';` : "this.style.display='none';"}" />`
          : '<div class="slideshow-image slideshow-image-empty">No image</div>';
        return `
          <div class="slideshow-slide">
            <span class="mini-pill">Slide ${selectedSlideIndex + 1}</span>
            ${imageHtml}
            <p>${content}</p>
          </div>
        `;
      })()
    : '<p class="muted">No slides returned.</p>';

  output.innerHTML = `
    <div class="slideshow-card">
      <div class="slideshow-summary">
        <span class="mini-pill">Hook</span>
        <textarea id="slideshow-hook-input" rows="2" placeholder="Hook text">${escapeHtml(hook || "")}</textarea>
      </div>
      <div class="slideshow-editor">
        <div class="slideshow-editor-preview">
          <div class="phone-preview small slideshow-preview-phone">
            ${
              selectedSlide?.imageUrl
                ? `<img id="slideshow-selected-preview-image" class="phone-video" src="${escapeHtml(selectedSlide.imageUrl)}" alt="Selected slide preview" onerror="${selectedSlide?.fallbackImageUrl ? `this.onerror=null;this.src='${escapeHtml(selectedSlide.fallbackImageUrl)}';` : "this.style.display='none';"}" />`
                : '<div class="phone-video slideshow-image-empty">No image</div>'
            }
            <div id="slideshow-selected-preview-text" class="slideshow-preview-text">${escapeHtml(selectedSlide?.text || "")}</div>
          </div>
          <div class="slideshow-editor-thumbs">
            <button class="secondary small-button slideshow-thumb" type="button" id="slideshow-prev" ${selectedSlideIndex <= 0 ? "disabled" : ""}>Prev</button>
            ${slides
              .map(
                (_slide, index) => `
              <button
                class="secondary small-button slideshow-thumb ${index === selectedSlideIndex ? "active" : ""}"
                type="button"
                data-slide-index="${index}"
              >
                Slide ${index + 1}
              </button>
            `,
              )
              .join("")}
            <button class="secondary small-button slideshow-thumb" type="button" id="slideshow-next" ${selectedSlideIndex >= slides.length - 1 ? "disabled" : ""}>Next</button>
          </div>
        </div>
        <div class="slideshow-editor-fields">
          <label>
            Slide text
            <textarea id="slideshow-slide-text-input" rows="4" placeholder="Write slide text...">${escapeHtml(selectedSlide?.text || "")}</textarea>
          </label>
          <label>
            Slide image URL
            <input id="slideshow-slide-image-input" type="url" placeholder="https://..." value="${escapeHtml(selectedSlide?.imageUrl || "")}" />
          </label>
        </div>
      </div>
      <div class="slideshow-slides">
        ${selectedSlideHtml}
      </div>
      <div class="slideshow-actions">
        <button class="secondary small-button" type="button" id="slideshow-copy-json">Copy JSON</button>
        <button class="secondary small-button" type="button" id="slideshow-copy-text">Copy Text</button>
      </div>
    </div>
  `;

  const copyJsonButton = $("#slideshow-copy-json");
  if (copyJsonButton) {
    copyJsonButton.addEventListener("click", async () => {
      await navigator.clipboard.writeText(jsonPayload);
      copyJsonButton.textContent = "Copied";
      setTimeout(() => {
        copyJsonButton.textContent = "Copy JSON";
      }, 1200);
    });
  }

  const copyTextButton = $("#slideshow-copy-text");
  if (copyTextButton) {
    copyTextButton.addEventListener("click", async () => {
      await navigator.clipboard.writeText(slideshowPlainText({ hook: draft.hook, slides: draft.slides }));
      copyTextButton.textContent = "Copied";
      setTimeout(() => {
        copyTextButton.textContent = "Copy Text";
      }, 1200);
    });
  }

  output.querySelectorAll(".slideshow-thumb").forEach((button) => {
    if (!button.dataset.slideIndex) return;
    button.addEventListener("click", () => {
      state.slideshowSelectedSlide = Number(button.dataset.slideIndex || 0);
      renderSlideshowOutput(result);
    });
  });

  const prevButton = $("#slideshow-prev");
  if (prevButton) {
    prevButton.addEventListener("click", () => {
      state.slideshowSelectedSlide = Math.max(0, selectedSlideIndex - 1);
      renderSlideshowOutput(result);
    });
  }

  const nextButton = $("#slideshow-next");
  if (nextButton) {
    nextButton.addEventListener("click", () => {
      state.slideshowSelectedSlide = Math.min(slides.length - 1, selectedSlideIndex + 1);
      renderSlideshowOutput(result);
    });
  }

  const hookInput = $("#slideshow-hook-input");
  if (hookInput) {
    hookInput.addEventListener("input", () => {
      draft.hook = hookInput.value;
    });
  }

  const slideTextInput = $("#slideshow-slide-text-input");
  if (slideTextInput && selectedSlide) {
    slideTextInput.addEventListener("input", () => {
      selectedSlide.text = slideTextInput.value;
      const previewText = $("#slideshow-selected-preview-text");
      if (previewText) previewText.textContent = slideTextInput.value;
      const slideCards = output.querySelectorAll(".slideshow-slide p");
      const activeCard = slideCards[selectedSlideIndex];
      if (activeCard) activeCard.textContent = slideTextInput.value || "No text returned.";
    });
  }

  const slideImageInput = $("#slideshow-slide-image-input");
  if (slideImageInput && selectedSlide) {
    slideImageInput.addEventListener("input", () => {
      selectedSlide.imageUrl = slideImageInput.value.trim();
      const previewImage = $("#slideshow-selected-preview-image");
      if (previewImage) previewImage.src = selectedSlide.imageUrl;
    });
    slideImageInput.addEventListener("blur", () => {
      renderSlideshowOutput(result);
    });
  }
}

$("#job-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const form = event.currentTarget;
  const button = form.querySelector("button");
  const message = $("#job-message");
  button.disabled = true;

  const generateCta = Boolean(form.querySelector('[name="generate_cta"]')?.checked);
  syncCtaHintsFromResearch(form);

  const ctaFile = form.querySelector('[name="cta"]')?.files?.[0] || null;

  if (generateCta) {
    if (!state.productResearch?.data) {
      setMessage(message, "Run Product Research first so we can generate your CTA.", true);
      button.disabled = false;
      return;
    }
  } else if (!ctaFile) {
    setMessage(message, "Upload a CTA video, or enable 'Generate CTA automatically'.", true);
    button.disabled = false;
    return;
  }

  setMessage(message, generateCta ? "Generating CTA and creating scrape job..." : "Uploading CTA and creating scrape job...");
  try {
    const formData = new FormData(form);
    formData.set("generate_cta", generateCta ? "true" : "false");

    const job = await api("/api/jobs", { method: "POST", body: formData });
    state.selectedJobId = job.id;
    setMessage(message, `Batch started. Scraping ${job.requested_limit} Shorts and stitching your CTA.`);
    form.reset();
    // Keep the CTA auto-generation default after reset.
    form.querySelector('[name="generate_cta"]') && (form.querySelector('[name="generate_cta"]').checked = true);
    form.querySelector('[name="hook_seconds"]').value = "3";
    await refreshAll();
  } catch (error) {
    setMessage(message, error.message, true);
  } finally {
    button.disabled = false;
  }
});

$("#cta-form")?.addEventListener("submit", async (event) => {
  event.preventDefault();
  const form = event.currentTarget;
  const button = $("#cta-generate-button");
  const message = $("#cta-message");
  const output = $("#cta-output");
  if (!form || !button || !message || !output) return;

  button.disabled = true;
  output.innerHTML = "";
  setMessage(message, "Generating CTA (this can take ~30-120s)...");
  try {
    const formData = new FormData(form);
    const projectId = formData.get("project_id");
    if (!projectId) {
      throw new Error("Select a saved product first.");
    }
    const result = await api("/api/cta/generate-from-project", { method: "POST", body: formData });
    setMessage(message, "CTA generated.");
    console.log("[CTA] generate-from-project result", result);
    output.innerHTML = `
      <div class="message">
        <strong>Frame 1:</strong> ${escapeHtml(result.frame1_text || "")}<br />
        <strong>Frame 2:</strong> ${escapeHtml(result.frame2_text || "")}
      </div>
      <div class="phone-preview">
        <video class="phone-video" src="${escapeHtml(result.media_url)}" controls playsinline preload="metadata"></video>
      </div>
      <a class="secondary" href="${escapeHtml(result.media_url)}" download>Download CTA</a>
    `;
    await loadCtaGallery(String(projectId));
  } catch (error) {
    setMessage(message, error.message, true);
  } finally {
    button.disabled = false;
  }
});

async function loadCtaGallery(projectId) {
  const gallery = $("#cta-gallery");
  if (!gallery) return;
  if (!state.session?.access_token) {
    gallery.textContent = "Sign in to load generated CTAs.";
    return;
  }
  if (!projectId) {
    gallery.textContent = "Select a saved product to load generated CTAs.";
    return;
  }
  gallery.textContent = "Loading generated CTAs...";
  const result = await api(`/api/cta/list?project_id=${encodeURIComponent(projectId)}`);
  const items = Array.isArray(result.items) ? result.items : [];
  if (!items.length) {
    gallery.textContent = "No CTAs generated yet for this product.";
    return;
  }
  gallery.classList.remove("muted");
  gallery.innerHTML = items
    .map((item) => {
      const url = item.signed_url || "";
      const id = item.cta_id || "";
      return `
        <div class="cta-card">
          <div class="phone-preview small">
            <video class="phone-video" src="${escapeHtml(url)}" controls playsinline preload="metadata"></video>
          </div>
          <div class="cta-meta">
            <strong>${escapeHtml(id)}</strong>
            <a class="secondary small-button" href="${escapeHtml(url)}" download>Download</a>
          </div>
        </div>
      `;
    })
    .join("");
}

$("#cta-project-select")?.addEventListener("change", async (event) => {
  const projectId = event.currentTarget?.value || "";
  try {
    await loadCtaGallery(projectId);
  } catch (_) {
    // ignore
  }
});

$("#cta-refresh-gallery")?.addEventListener("click", async () => {
  const select = $("#cta-project-select");
  const projectId = select?.value || "";
  await loadCtaGallery(projectId);
});

$("#cta-refresh-products")?.addEventListener("click", async () => {
  const message = $("#cta-message");
  setMessage(message, "Loading saved products...");
  try {
    await loadSavedProducts();
    renderCtaProjectsSelect();
    setMessage(message, "Saved products loaded.");
  } catch (error) {
    setMessage(message, error.message, true);
  }
});

$("#slideshow-form")?.addEventListener("submit", async (event) => {
  event.preventDefault();
  const form = event.currentTarget;
  const button = $("#slideshow-generate-button");
  const message = $("#slideshow-message");
  const output = $("#slideshow-output");
  if (!form || !button || !message || !output) return;

  button.disabled = true;
  setMessage(message, "Generating slideshow...");
  state.slideshowDraft = null;
  state.slideshowDraftResult = null;
  state.slideshowSelectedSlide = 0;
  renderSlideshowOutput(null);

  try {
    const formData = new FormData(form);
    const projectId = formData.get("project_id");
    if (!projectId) {
      throw new Error("Select a saved product first.");
    }
    const result = await api("/api/slideshows/generate-from-project", { method: "POST", body: formData });
    state.slideshowResult = result;
    renderSlideshowOutput(result);
    setMessage(message, "Slideshow generated.");
  } catch (error) {
    setMessage(message, error.message, true);
  } finally {
    button.disabled = false;
  }
});

$("#slideshow-refresh-products")?.addEventListener("click", async () => {
  const message = $("#slideshow-message");
  setMessage(message, "Loading saved products...");
  try {
    await loadSavedProducts();
    renderSlideshowProjectsSelect();
    setMessage(message, "Saved products loaded.");
  } catch (error) {
    setMessage(message, error.message, true);
  }
});

$("#research-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const form = event.currentTarget;
  const button = $("#research-button");
  const message = $("#research-message");
  if (!state.session?.access_token) {
    setMessage(message, "Sign in first so this product can be saved.", true);
    return;
  }
  button.disabled = true;
  state.researchInput = researchInputFromForm(form);
  renderReadonlyInputs(state.researchInput);
  setResearchStep("generating");
  setMessage(message, "Researching product and building TikTok assets...");
  try {
    const result = await api("/api/product-research", {
      method: "POST",
      body: new FormData(form),
    });
    state.productResearch = result;
    state.researchInput = researchInputFromResult(result);
    renderReadonlyInputs(state.researchInput);
    renderResearchResults(result);
    await loadSavedProducts();
    setResearchStep("results");
    setMessage(message, "Done. Your TikTok plan is ready.");
  } catch (error) {
    setResearchStep("input");
    setMessage(message, error.message, true);
  } finally {
    button.disabled = false;
  }
});

$("#auth-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const form = event.currentTarget;
  const message = $("#auth-message");
  setMessage(message, "Signing in...");
  try {
    const formData = new FormData(form);
    const payload = await supabaseAuth("/auth/v1/token?grant_type=password", {
      email: formData.get("email"),
      password: formData.get("password"),
    });
    saveSession(payload);
    setMessage(message, "Signed in.");
    form.reset();
    await loadSavedProducts();
  } catch (error) {
    setMessage(message, error.message, true);
  }
});

$("#signup-button").addEventListener("click", async () => {
  const form = $("#auth-form");
  const message = $("#auth-message");
  const formData = new FormData(form);
  setMessage(message, "Creating account...");
  try {
    const payload = await supabaseAuth("/auth/v1/signup", {
      email: formData.get("email"),
      password: formData.get("password"),
    });

    // Some Supabase error shapes can be returned in JSON even when we get a response.
    // We defensively detect "duplicate user" strings and show a friendly message.
    const payloadStr = JSON.stringify(payload || {}).toLowerCase();
    if (
      payloadStr.includes("already registered") ||
      payloadStr.includes("user already registered") ||
      payloadStr.includes("email already") ||
      payloadStr.includes("email already in use") ||
      payloadStr.includes("already exists") ||
      payloadStr.includes("already in use") ||
      payloadStr.includes("duplicate key") ||
      payloadStr.includes("unique constraint") ||
      payloadStr.includes("conflict")
    ) {
      setMessage(message, "An account with this email already exists. Please sign in instead.", true);
      return;
    }

    if (payload.access_token) {
      saveSession(payload);
      await loadSavedProducts();
      setMessage(message, "Account created and signed in.");
    } else {
      // When email confirmation is enabled, Supabase often returns 200 with no access_token.
      // We avoid misleading "created" messaging and instead prompt returning users to sign in.
      setMessage(
        message,
        "Check your email for a confirmation link. If you already have an account, please sign in instead.",
      );
    }
  } catch (error) {
    const rawMessage = error?.message ? String(error.message) : "";
    const lower = rawMessage.toLowerCase();
    // Supabase/GOTRUE typically responds with "User already registered" for duplicate emails.
    if (
      lower.includes("already registered") ||
      lower.includes("user already registered") ||
      (lower.includes("already") && lower.includes("account")) ||
      (lower.includes("duplicate") && lower.includes("email"))
    ) {
      setMessage(message, "An account with this email already exists. Please sign in instead.", true);
      return;
    }
    setMessage(message, rawMessage || "Sign up failed.", true);
  }
});

$("#signout-button").addEventListener("click", () => {
  saveSession(null);
  state.savedProducts = [];
  loadSavedProducts();
  setMessage($("#auth-message"), "Signed out.");
});

$("#schedule-button").addEventListener("click", async () => {
  const message = $("#schedule-message");
  const button = $("#schedule-button");
  const videoIds = selectedClipIds();
  const socialAccountIds = selectedAccountIds();
  if (!videoIds.length) {
    setMessage(message, "Select at least one generated clip.", true);
    return;
  }
  if (!socialAccountIds.length) {
    setMessage(message, "Select at least one PostBridge account.", true);
    return;
  }

  button.disabled = true;
  setMessage(message, `Scheduling ${videoIds.length} clips...`);
  try {
    const startAt = $("#start-at").value ? new Date($("#start-at").value).toISOString() : null;
    const payload = {
      video_ids: videoIds,
      social_account_ids: socialAccountIds,
      caption: $("#caption").value,
      start_at: startAt,
      interval_minutes: Number($("#interval").value || 0),
    };
    const result = await api("/api/schedule", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
    setMessage(message, `Scheduled ${result.data.length} posts. Check the calendar.`);
    await Promise.all([loadScheduledPosts(), loadClips()]);
  } catch (error) {
    setMessage(message, error.message, true);
  } finally {
    button.disabled = false;
  }
});

$("#refresh-button").addEventListener("click", refreshAll);
$("#load-accounts").addEventListener("click", loadAccounts);
$("#refresh-calendar").addEventListener("click", loadScheduledPosts);
$("#refresh-products").addEventListener("click", loadSavedProducts);
$("#new-research-button").addEventListener("click", () => {
  state.productResearch = null;
  state.researchInput = null;
  renderResearchResults(null);
  $("#research-form").reset();
  $("#research-files-preview").textContent = "Images, PDFs, text, markdown, or CSV. Max 8 files.";
  setMessage($("#research-message"), "");
  setResearchStep("input");
});

document.querySelectorAll(".sidebar-tab").forEach((tab) => {
  tab.addEventListener("click", () => {
    setActiveView(tab.dataset.view);
  });
});

document.querySelector('[name="cta"]').addEventListener("change", (event) => {
  const file = event.target.files?.[0];
  $("#cta-preview").textContent = file
    ? `${file.name} · ${(file.size / 1024 / 1024).toFixed(1)} MB`
    : "MP4 or MOV. The app normalizes it for Shorts/Reels/TikTok.";
});

// If auto-generation is enabled, CTA upload is optional (and we disable the file picker).
document.querySelector('[name="generate_cta"]')?.addEventListener("change", (event) => {
  const generateEnabled = Boolean(event.currentTarget.checked);
  const ctaInput = document.querySelector('[name="cta"]');
  if (!ctaInput) return;
  ctaInput.disabled = generateEnabled;
  if (generateEnabled) {
    ctaInput.value = "";
    $("#cta-preview").textContent = "CTA upload disabled; generating automatically.";
  } else {
    $("#cta-preview").textContent = "MP4 or MOV. If provided, it will be used when auto-generation is off.";
  }
});

// Apply initial toggle state.
(() => {
  const generateCheckbox = document.querySelector('[name="generate_cta"]');
  const ctaInput = document.querySelector('[name="cta"]');
  if (!generateCheckbox || !ctaInput) return;
  ctaInput.disabled = Boolean(generateCheckbox.checked);
})();

document.querySelector('[name="files"]').addEventListener("change", (event) => {
  const files = [...(event.target.files || [])];
  $("#research-files-preview").textContent = files.length
    ? `${files.length} file${files.length === 1 ? "" : "s"} selected · ${files.map((file) => file.name).join(", ")}`
    : "Images, PDFs, text, markdown, or CSV. Max 8 files.";
});

document.querySelectorAll(".tab").forEach((tab) => {
  tab.addEventListener("click", () => {
    state.clipFilter = tab.dataset.filter;
    document.querySelectorAll(".tab").forEach((button) => button.classList.remove("active"));
    tab.classList.add("active");
    renderClips();
  });
});

refreshAll()
  .then(loadSavedProducts)
  .then(() => {
    renderCtaProjectsSelect();
    renderSlideshowProjectsSelect();
  })
  .catch(() => {});
setActiveView(state.activeView);
setResearchStep(state.researchStep);
setInterval(refreshAll, 3000);
