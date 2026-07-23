const SAMPLE_REQUEST = {
  repo_url: "https://github.com/FoundationAgents/OpenManus",
  commit_hash: "52a13f2a57d8c7f6737eefb02ccf569594d44273",
  feature_title: "Adaptive Memory Compaction for Token Budget Management",
  feature_category: "Feature Request",
  exploration_angle: "Edge Case & Robustness",
  feature_description:
    "OpenManus agents currently fail with TokenLimitExceeded or silently drop old messages through Memory.max_messages truncation, which breaks long-running tasks and corrupts tool-call chains. This feature introduces configurable memory compaction strategies that shrink the active context while preserving essential tool-call context, system prompts, and the most recent working memory. Users can set a global or per-agent token budget and choose a strategy (e.g., sliding-window, summarization), allowing agents to execute longer multi-step tasks without manual intervention or sudden crashes.",
  technical_scope:
    "New subsystem: app/memory/compaction.py — MemoryCompactor, TokenBudget, and pluggable compaction strategies. Schema changes: schema.py — add MemoryCompactionStrategy enum and CompactionResult; extend Memory to host compaction state. LLM integration: llm.py — hook token-budget checks before TokenLimitExceeded. Agent loop integration: base.py and toolcall.py — attach a compactor and trigger compaction between steps. Configuration: config.py and config.example.toml. Flow integration: planning.py.",
};

const form = document.getElementById("analyze-form");
const submitBtn = document.getElementById("submit-btn");
const btnText = submitBtn.querySelector(".btn-text");
const spinner = submitBtn.querySelector(".spinner");
const errorMessage = document.getElementById("error-message");
const loadingMessage = document.getElementById("loading-message");
const loadingStatus = document.getElementById("loading-status");
const progressBar = document.getElementById("progress-bar");
const resultsSection = document.getElementById("results");
const loadExampleBtn = document.getElementById("load-example");
const downloadPdfBtn = document.getElementById("download-pdf");

const verdictTitle = document.getElementById("verdict-title");
const verdictSummary = document.getElementById("verdict-summary");
const verdictDetails = document.getElementById("verdict-details");
const aiReview = document.getElementById("ai-review");
const confidenceValue = document.getElementById("confidence-value");
const confidenceStroke = document.getElementById("confidence-stroke");
const keywordsList = document.getElementById("keywords-list");
const evidenceFilter = document.getElementById("evidence-filter");

const tabs = document.querySelectorAll(".tab");
const panels = document.querySelectorAll(".evidence-panel");

let lastEvidenceData = {};

downloadPdfBtn.addEventListener("click", () => {
  window.print();
});

function clearResults() {
  hideError();
  resultsSection.hidden = true;
  lastEvidenceData = {};
  verdictTitle.textContent = "";
  verdictTitle.className = "verdict-title";
  verdictSummary.textContent = "";
  verdictDetails.innerHTML = "";
  if (aiReview) {
    aiReview.hidden = true;
    aiReview.innerHTML = "";
  }
  confidenceValue.textContent = "0%";
  if (confidenceStroke) confidenceStroke.setAttribute("stroke-dasharray", "0, 100");
  keywordsList.innerHTML = "";
  if (evidenceFilter) evidenceFilter.value = "";
  ["issues", "pull_requests", "commits", "branches"].forEach((key) => {
    renderEvidence(key, []);
  });
  activateTab("issues");
}

loadExampleBtn.addEventListener("click", () => {
  document.getElementById("repo_url").value = SAMPLE_REQUEST.repo_url;
  document.getElementById("commit_hash").value = SAMPLE_REQUEST.commit_hash;
  document.getElementById("feature_title").value = SAMPLE_REQUEST.feature_title;
  document.getElementById("feature_category").value = SAMPLE_REQUEST.feature_category;
  document.getElementById("exploration_angle").value = SAMPLE_REQUEST.exploration_angle;
  document.getElementById("feature_description").value = SAMPLE_REQUEST.feature_description;
  document.getElementById("technical_scope").value = SAMPLE_REQUEST.technical_scope;
  clearResults();
});

const textInputIds = [
  "repo_url",
  "commit_hash",
  "feature_title",
  "exploration_angle",
  "feature_description",
  "technical_scope",
];
textInputIds.forEach((id) => {
  const el = document.getElementById(id);
  if (el) el.addEventListener("input", clearResults);
});

const categoryEl = document.getElementById("feature_category");
if (categoryEl) categoryEl.addEventListener("change", clearResults);

function activateTab(key) {
  tabs.forEach((t) => t.classList.remove("active"));
  panels.forEach((p) => p.classList.remove("active"));
  const activeTab = document.querySelector(`.tab[data-tab="${key}"]`);
  if (activeTab) activeTab.classList.add("active");
  const activePanel = document.getElementById(`panel-${key}`);
  if (activePanel) activePanel.classList.add("active");
}

tabs.forEach((tab) => {
  tab.addEventListener("click", () => activateTab(tab.dataset.tab));
});

if (evidenceFilter) {
  evidenceFilter.addEventListener("input", (e) => {
    const term = e.target.value.trim().toLowerCase();
    const keys = ["issues", "pull_requests", "commits", "branches"];
    const nonEmptyKey = keys.find((key) => {
      const items = lastEvidenceData[key] || [];
      if (!term) return items.length > 0;
      return items.some((i) => {
        const title = (i.title || "").toLowerCase();
        const repository = (i.repository || "").toLowerCase();
        const number = i.number ? String(i.number) : "";
        return title.includes(term) || repository.includes(term) || number.includes(term);
      });
    });
    keys.forEach((key) => {
      const items = lastEvidenceData[key] || [];
      const filtered = term
        ? items.filter((i) => {
            const title = (i.title || "").toLowerCase();
            const repository = (i.repository || "").toLowerCase();
            const number = i.number ? String(i.number) : "";
            return title.includes(term) || repository.includes(term) || number.includes(term);
          })
        : items;
      renderEvidence(key, filtered);
    });
    // Switch to the first tab that has matching results, or back to Issues when cleared.
    if (nonEmptyKey) {
      activateTab(nonEmptyKey);
    } else if (!term) {
      activateTab("issues");
    }
  });
}

form.addEventListener("submit", async (event) => {
  event.preventDefault();
  setLoading(true);
  hideError();
  resultsSection.hidden = true;
  showLoading();

  const payload = {
    repo_url: document.getElementById("repo_url").value.trim(),
    commit_hash: document.getElementById("commit_hash").value.trim(),
    feature: {
      title: document.getElementById("feature_title").value.trim(),
      category: document.getElementById("feature_category").value,
      exploration_angle: document.getElementById("exploration_angle").value.trim(),
      description: document.getElementById("feature_description").value.trim(),
      technical_scope: document.getElementById("technical_scope").value.trim(),
    },
  };

  try {
    const response = await fetch("/api/analyze/", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });

    const data = await parseJsonResponse(response);

    if (!response.ok) {
      showError(data.error || `Request failed (${response.status})`);
      return;
    }

    renderResults(data);
    resultsSection.hidden = false;
    resultsSection.scrollIntoView({ behavior: "smooth", block: "start" });
  } catch (err) {
    showError(err.message || "Network error. Please try again.");
  } finally {
    setLoading(false);
    hideLoading();
  }
});

async function parseJsonResponse(response) {
  const contentType = response.headers.get("content-type") || "";
  if (contentType.includes("application/json")) {
    return response.json();
  }

  const text = await response.text();
  const fallback = text.replace(/<[^>]*>/g, " ").replace(/\s+/g, " ").trim();
  return {
    error: fallback
      ? `Server returned a non-JSON response: ${fallback.slice(0, 240)}`
      : "Server returned a non-JSON response.",
  };
}

function setLoading(isLoading) {
  submitBtn.disabled = isLoading;
  btnText.textContent = isLoading ? "Analyzing..." : "Analyze repository";
  spinner.hidden = !isLoading;
}

function showLoading() {
  loadingMessage.hidden = false;
  loadingMessage.scrollIntoView({ behavior: "smooth", block: "nearest" });
  setProgress(10, "Fetching issues and pull requests from GitHub…");

  // Simulate progress steps since we can't stream real server progress.
  window._loadingSteps = [
    { pct: 25, msg: "Reading repository metadata…" },
    { pct: 45, msg: "Searching old issues and pull requests…" },
    { pct: 65, msg: "Checking related repositories for prior art…" },
    { pct: 85, msg: "Computing verdict and confidence…" },
  ];
  window._loadingInterval = setInterval(() => {
    const step = window._loadingSteps.shift();
    if (step) setProgress(step.pct, step.msg);
  }, 3500);
}

function hideLoading() {
  loadingMessage.hidden = true;
  if (window._loadingInterval) {
    clearInterval(window._loadingInterval);
    window._loadingInterval = null;
  }
}

function setProgress(percent, message) {
  if (progressBar) progressBar.style.width = `${percent}%`;
  if (loadingStatus) loadingStatus.textContent = message;
}

function showError(message) {
  errorMessage.innerHTML = `<strong>Analysis failed</strong><br>${escapeHtml(message)}`;
  errorMessage.hidden = false;
  errorMessage.scrollIntoView({ behavior: "smooth", block: "nearest" });
}

function hideError() {
  errorMessage.hidden = true;
  errorMessage.textContent = "";
}

function renderResults(data) {
  verdictTitle.textContent = data.verdict;
  verdictTitle.className = `verdict-title ${data.verdict}`;
  verdictSummary.textContent = data.summary;

  const percent = Math.round((data.confidence || 0) * 100);
  confidenceValue.textContent = `${percent}%`;
  confidenceStroke.setAttribute("stroke-dasharray", `${percent}, 100`);

  verdictDetails.innerHTML = "";
  (data.details || []).forEach((detail) => {
    const li = document.createElement("li");
    li.textContent = detail;
    verdictDetails.appendChild(li);
  });
  renderAiReview(data.ai_review || null);

  lastEvidenceData = data.evidence || {};
  renderEvidence("issues", data.evidence.issues || []);
  renderEvidence("pull_requests", data.evidence.pull_requests || []);
  renderEvidence("commits", data.evidence.commits || []);
  renderEvidence("branches", data.evidence.branches || []);

  if (evidenceFilter) evidenceFilter.value = "";

  keywordsList.innerHTML = "";
  (data.feature_keywords || []).forEach((keyword) => {
    const span = document.createElement("span");
    span.className = "keyword-pill";
    span.textContent = keyword;
    keywordsList.appendChild(span);
  });

  // Reset to first tab
  activateTab("issues");
}

function renderAiReview(review) {
  if (!aiReview) return;
  if (!review || !review.opinion) {
    aiReview.hidden = true;
    aiReview.innerHTML = "";
    return;
  }

  const leads = Array.isArray(review.automated_leads) ? review.automated_leads : [];
  const leadHtml = leads.length
    ? leads.map((lead) => {
        const title = escapeHtml(lead.title || "Automated lead");
        const url = lead.url ? `<a href="${escapeHtml(lead.url)}" target="_blank" rel="noopener">${title}</a>` : title;
        return `
          <article class="ai-lead">
            <span class="ai-lead-type">${escapeHtml(lead.type || "lead")}</span>
            <h4>${url}</h4>
            ${lead.note ? `<p>${escapeHtml(lead.note)}</p>` : ""}
            ${lead.metric ? `<small>${escapeHtml(lead.metric)}</small>` : ""}
          </article>
        `;
      }).join("")
    : `<p class="ai-empty">No automated leads were confirmed; retained for transparency.</p>`;

  aiReview.innerHTML = `
    <div class="ai-review-header">
      <span class="verdict-label">AI Review</span>
      <span class="ai-source">${escapeHtml(review.source || "analysis")}</span>
    </div>
    <p class="ai-opinion">${escapeHtml(review.opinion)}</p>
    <div class="ai-review-grid">
      <div class="ai-review-card">
        <span class="repo-stat-label">Overlap</span>
        <strong>${escapeHtml(review.overlap_level || "Low")}</strong>
        <p>${escapeHtml(review.overlap || "")}</p>
      </div>
      <div class="ai-review-card">
        <span class="repo-stat-label">Off-scope risk</span>
        <strong>${escapeHtml(review.scope_level || "Low")}</strong>
        <p>${escapeHtml(review.scope || "")}</p>
      </div>
    </div>
    <div class="ai-leads">
      <span class="repo-stat-label">Automated leads (${leads.length})</span>
      ${leadHtml}
    </div>
  `;
  aiReview.hidden = false;
}

function renderEvidence(key, items) {
  const panel = document.getElementById(`panel-${key}`);
  panel.innerHTML = "";

  if (items.length === 0) {
    panel.innerHTML = `<div class="empty-state">No relevant ${formatKey(key)} found.</div>`;
    return;
  }

  items.forEach((item) => {
    const el = document.createElement("div");
    el.className = "evidence-item";

    const title = item.title || "Untitled";
    const link = item.url ? `<a href="${escapeHtml(item.url)}" target="_blank" rel="noopener">${escapeHtml(title)}</a>` : escapeHtml(title);
    const numberBadge = item.number ? `<span class="badge badge-committed">#${item.number}</span>` : "";
    const repoBadge = item.repository ? `<span class="badge">${escapeHtml(item.repository)}</span>` : "";
    const stateClass = getStateClass(item.state, item.is_rejection);
    const stateLabel = item.is_rejection ? "rejected" : item.state;
    const relevance = Math.round((item.relevance || 0) * 100);

    el.innerHTML = `
      <div class="evidence-header">
        <h4 class="evidence-title">${link}</h4>
        ${numberBadge}
      </div>
      <div class="evidence-meta">
        <span class="badge ${stateClass}">${stateLabel}</span>
        ${repoBadge}
        ${item.author ? `<span class="badge">@${escapeHtml(item.author)}</span>` : ""}
        ${(item.labels || []).map((l) => `<span class="badge">${escapeHtml(l)}</span>`).join("")}
      </div>
      ${item.body ? `<p class="evidence-body">${escapeHtml(item.body)}</p>` : ""}
      <p class="evidence-reason">${escapeHtml(item.reason)}</p>
      <div class="relevance-bar" aria-label="Relevance ${relevance}%">
        <div class="relevance-fill" style="width: ${relevance}%"></div>
      </div>
    `;
    panel.appendChild(el);
  });
}

function formatKey(key) {
  const map = {
    issues: "issues",
    pull_requests: "pull requests",
    commits: "commits",
    branches: "branches",
  };
  return map[key] || key;
}

function getStateClass(state, isRejection) {
  if (isRejection) return "badge-rejection";
  const map = {
    open: "badge-open",
    closed: "badge-closed",
    merged: "badge-merged",
    committed: "badge-committed",
    active: "badge-active",
  };
  return map[state] || "badge-closed";
}

function escapeHtml(text) {
  const div = document.createElement("div");
  div.textContent = text;
  return div.innerHTML;
}
