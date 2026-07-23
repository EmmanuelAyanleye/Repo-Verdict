const PAGE_SIZE = 7;

const form = document.getElementById("search-form");
const submitBtn = document.getElementById("submit-btn");
const spinner = submitBtn.querySelector(".spinner");
const btnText = submitBtn.querySelector(".btn-text");
const loadingPanel = document.getElementById("loading");
const loadingMessage = document.getElementById("loading-message");
const loadingQuery = document.getElementById("loading-query");
const searchProgress = document.getElementById("search-progress");
const errorSection = document.getElementById("error");
const errorMessage = document.getElementById("error-message");
const resultsSection = document.getElementById("results");
const resultCount = document.getElementById("result-count");
const searchQuery = document.getElementById("search-query");
const searchResults = document.getElementById("search-results");
const pagination = document.getElementById("pagination");

let currentRepos = [];
let currentPage = 1;

function getSelectedValues(container) {
  if (!container) return [];
  return Array.from(container.querySelectorAll("input:checked")).map((cb) => cb.value);
}

function getSelectedOptions(select) {
  return Array.from(select.selectedOptions).map((opt) => opt.value);
}

function formatNumber(num) {
  return new Intl.NumberFormat().format(num);
}

function formatSize(kb) {
  if (kb == null) return "Unknown";
  const mb = Math.round(kb / 1024);
  return `${mb} MB`;
}

function toNumber(value) {
  if (typeof value === "number") return Number.isFinite(value) ? value : null;
  if (typeof value === "string" && value.trim() !== "") {
    const parsed = Number(value);
    return Number.isFinite(parsed) ? parsed : null;
  }
  return null;
}

function escapeHtml(text) {
  const div = document.createElement("div");
  div.textContent = text == null ? "" : String(text);
  return div.innerHTML;
}

function inferCoupling(s, language) {
  const fileCount = toNumber(s.source_file_count) || 0;
  const dirCount = toNumber(s.source_dir_count) || 0;
  const maxDepth = toNumber(s.max_depth) || 0;
  if (s.coupling_sentence && s.coupling_sentence !== "unverified") return s.coupling_sentence;
  if (dirCount >= 20 && maxDepth >= 3) {
    return `Likely cross-component: ${formatNumber(fileCount)} ${language || "source"} files across ${formatNumber(dirCount)} directories with depth ${maxDepth}.`;
  }
  if (dirCount >= 5) {
    return `Moderate component spread: ${formatNumber(fileCount)} ${language || "source"} files across ${formatNumber(dirCount)} directories.`;
  }
  if (dirCount > 0) {
    return `Limited component spread in fast scan: ${formatNumber(dirCount)} source directories detected.`;
  }
  return "Directory coupling could not be derived from the returned metadata.";
}

function inferRisks(repo, s, cloneSize) {
  if (Array.isArray(s.reliability_risks) && s.reliability_risks.length) {
    const generic = s.reliability_risks.length === 1 && /not deeply checked/i.test(s.reliability_risks[0]);
    if (!generic) return s.reliability_risks;
  }

  const risks = [];
  const dirCount = toNumber(s.source_dir_count) || 0;
  const maxDepth = toNumber(s.max_depth) || 0;
  if (cloneSize !== null && cloneSize > 45) risks.push(`Repo is near the 60MB ceiling at about ${cloneSize} MB.`);
  if (dirCount > 80) risks.push(`Large tree breadth: ${formatNumber(dirCount)} source directories may make scoping harder.`);
  if (maxDepth > 6) risks.push(`Deep tree depth (${maxDepth}) may add setup/navigation complexity.`);
  if (!repo.description) risks.push("Thin GitHub description; inspect docs before selecting.");
  if (repo.archived) risks.push("Repository is archived.");
  if (!risks.length) risks.push("No obvious metadata-level risk; still inspect CI and build docs before final selection.");
  return risks;
}

function inferGaps(repo, s) {
  const dirCount = toNumber(s.source_dir_count) || 0;
  const fileCount = toNumber(s.source_file_count) || 0;
  const text = `${repo.full_name || ""} ${repo.description || ""} ${(repo.topics || []).join(" ")}`.toLowerCase();
  if (text.match(/compiler|interpreter|parser|type/)) {
    return ["Parser/type-system edge cases may be worth investigating.", "Look for diagnostics, incremental analysis, or language-feature issues."];
  }
  if (text.match(/query|sql|database|storage|warehouse/)) {
    return ["Planner/optimizer behavior gaps may be worth investigating.", "Look for schema, transaction, or serialization edge cases."];
  }
  if (text.match(/agent|llm|workflow|chain/)) {
    return ["Agent state, tool-call, or memory behavior gaps may be worth investigating.", "Look for retry, streaming, or structured-output issues."];
  }
  if (text.match(/distributed|consensus|cache|queue|broker/)) {
    return ["Consistency, ordering, retry, or failover behavior gaps may be worth investigating.", "Look for race-condition or recovery issues."];
  }
  if (text.match(/schema|serial|validat|protobuf|json|yaml|parser/)) {
    return ["Schema, validation, or serialization invariants may be worth investigating.", "Look for round-trip, compatibility, and nested-data edge cases."];
  }
  if (dirCount >= 20 || fileCount >= 200) {
    return [
      `Look for bugs crossing the repo's ${formatNumber(dirCount)} source directories rather than isolated one-file issues.`,
      "Prioritize issues involving internal contracts, state transitions, or data conversions.",
    ];
  }
  return ["Use as a lower-confidence volume candidate until issues/discussions are reviewed.", "Look for behavior bugs that affect more than one module."];
}

function renderFitPanel(fit) {
  if (!fit || !Array.isArray(fit.sections)) return "";
  const total = Math.round(toNumber(fit.total) ?? 0);
  const decision = escapeHtml(fit.decision || "REVIEW");
  const decisionClass = (fit.decision || "REVIEW").toLowerCase();
  const blockers = Array.isArray(fit.blockers) ? fit.blockers : [];
  const cautions = Array.isArray(fit.cautions) ? fit.cautions : [];
  const statusItems = [
    { text: decision, className: `fit-${decisionClass}` },
    { text: `${blockers.length} blockers`, className: "" },
    { text: `${cautions.length} cautions`, className: "" },
  ];
  const statusHtml = statusItems.map((item) => (
    `<span class="fit-pill ${item.className}">${escapeHtml(item.text)}</span>`
  )).join("");
  const rows = fit.sections.map((section) => {
    const score = Math.max(0, toNumber(section.score) ?? 0);
    const max = Math.max(1, toNumber(section.max) ?? 1);
    const pct = Math.min(100, Math.round((score / max) * 100));
    return `
      <div class="fit-metric">
        <div class="fit-meter" style="--pct: ${pct}%">
          <strong>${Math.round(score)}</strong>
          <span>/${Math.round(max)}</span>
        </div>
        <div class="fit-copy">
          <h4>${escapeHtml(section.name)}</h4>
          <p>${escapeHtml(section.detail || "")}</p>
        </div>
      </div>
    `;
  }).join("");
  return `
    <div class="fit-panel">
      <div class="fit-header">
        <div>
          <span class="repo-stat-label">Long-horizon fit</span>
        </div>
        <div class="fit-score">
          <div class="fit-score-number"><strong>${total}</strong><span>/100</span></div>
          <div class="fit-score-pills">${statusHtml}</div>
        </div>
      </div>
      <div class="fit-bars">${rows}</div>
      ${fit.target_check ? `<p class="fit-target">${escapeHtml(fit.target_check)}</p>` : ""}
    </div>
  `;
}

function renderRepo(repo) {
  const license = repo.license?.spdx_id || repo.license?.name || "Unknown";
  const updated = repo.pushed_at ? new Date(repo.pushed_at).toLocaleDateString() : "Unknown";
  const s = repo.suitability || {};
  const locValue = toNumber(s.primary_loc);
  const loc = locValue && locValue > 0 ? formatNumber(locValue) : "Unknown";
  const dirCount = toNumber(s.source_dir_count);
  const maxDepth = toNumber(s.max_depth);
  const cloneSize = toNumber(s.approx_clone_size_mb) ?? (toNumber(repo.size) ? Math.round((toNumber(repo.size) / 1024) * 10) / 10 : null);
  const cloneSizeLabel = cloneSize !== null ? `${cloneSize} MB` : "Unknown";
  const coupling = inferCoupling(s, repo.language);
  const risks = inferRisks(repo, s, cloneSize);
  const gaps = Array.isArray(s.feature_gap_ideas) && s.feature_gap_ideas.length
    && !/skim open issues for candidate behavior gaps/i.test(s.feature_gap_ideas[0])
    ? s.feature_gap_ideas
    : inferGaps(repo, s);
  const fitPanel = renderFitPanel(s.long_horizon_fit);
  const fullName = escapeHtml(repo.full_name || repo.name || "Unknown repository");
  const repoUrl = escapeHtml(repo.html_url || "#");
  return `
    <article class="repo-card">
      <div class="repo-header">
        <a href="${repoUrl}" target="_blank" rel="noopener" class="repo-name">${fullName}</a>
      </div>
      <p class="repo-description">${escapeHtml(repo.description || "No description.")}</p>
      <div class="repo-stats">
        <div class="repo-stat">
          <span class="repo-stat-label">Language</span>
          <span class="repo-stat-value">${escapeHtml(repo.language || "Unknown")}</span>
        </div>
        <div class="repo-stat">
          <span class="repo-stat-label">LOC</span>
          <span class="repo-stat-value">${loc}</span>
        </div>
        <div class="repo-stat">
          <span class="repo-stat-label">License</span>
          <span class="repo-stat-value">${escapeHtml(license)}</span>
        </div>
        <div class="repo-stat">
          <span class="repo-stat-label">Stars</span>
          <span class="repo-stat-value">★ ${formatNumber(repo.stargazers_count || 0)}</span>
        </div>
        <div class="repo-stat">
          <span class="repo-stat-label">Clone size</span>
          <span class="repo-stat-value">${cloneSizeLabel}</span>
        </div>
        <div class="repo-stat">
          <span class="repo-stat-label">Last commit</span>
          <span class="repo-stat-value">${updated}</span>
        </div>
      </div>
      ${fitPanel}
      <div class="repo-evaluation">
        <div>
          <span class="repo-stat-label">Cross-component coupling</span>
          <p>${escapeHtml(coupling)}</p>
        </div>
        <div>
          <span class="repo-stat-label">Reliability risks</span>
          <ul>${risks.map((risk) => `<li>${escapeHtml(risk)}</li>`).join("")}</ul>
        </div>
        <div>
          <span class="repo-stat-label">Feature gap leads</span>
          <ul>${gaps.map((gap) => `<li>${escapeHtml(gap)}</li>`).join("")}</ul>
        </div>
      </div>
    </article>
  `;
}

function getPageNumbers(current, total) {
  const pages = [];
  if (total <= 7) {
    for (let i = 1; i <= total; i++) pages.push(i);
    return pages;
  }
  pages.push(1);
  if (current > 4) pages.push("...");
  const start = Math.max(2, current - 2);
  const end = Math.min(total - 1, current + 2);
  for (let i = start; i <= end; i++) pages.push(i);
  if (current < total - 3) pages.push("...");
  pages.push(total);
  return pages;
}

function renderPagination() {
  const totalPages = Math.ceil(currentRepos.length / PAGE_SIZE) || 1;
  if (totalPages <= 1) {
    pagination.style.display = "none";
    return;
  }

  const pages = getPageNumbers(currentPage, totalPages);
  let html = `
    <button class="btn btn-ghost pagination-btn" data-page="prev" ${currentPage === 1 ? "disabled" : ""}>← Prev</button>
  `;
  for (const p of pages) {
    if (p === "...") {
      html += `<span class="pagination-ellipsis">...</span>`;
    } else {
      const active = p === currentPage ? "pagination-active" : "";
      html += `<button class="btn btn-ghost pagination-btn pagination-number ${active}" data-page="${p}">${p}</button>`;
    }
  }
  html += `
    <button class="btn btn-ghost pagination-btn" data-page="next" ${currentPage === totalPages ? "disabled" : ""}>Next →</button>
  `;
  pagination.innerHTML = html;
  pagination.style.display = "flex";

  pagination.querySelectorAll(".pagination-btn").forEach((btn) => {
    btn.addEventListener("click", () => {
      const dir = btn.dataset.page;
      const total = Math.ceil(currentRepos.length / PAGE_SIZE);
      if (dir === "prev" && currentPage > 1) currentPage--;
      else if (dir === "next" && currentPage < total) currentPage++;
      else if (!isNaN(parseInt(dir, 10))) currentPage = parseInt(dir, 10);
      displayPage();
    });
  });
}

function displayPage() {
  const start = (currentPage - 1) * PAGE_SIZE;
  const pageItems = currentRepos.slice(start, start + PAGE_SIZE);
  searchResults.innerHTML = pageItems.length
    ? pageItems.map(renderRepo).join("")
    : `<p class="empty-state">No repositories matched the criteria.</p>`;
  renderPagination();
  resultsSection.scrollIntoView({ behavior: "smooth" });
}

function parseCustomLicenses() {
  const raw = document.getElementById("custom_license")?.value || "";
  if (!raw) return [];
  return raw.split(",").map((s) => s.trim()).filter(Boolean);
}

form.addEventListener("submit", async (e) => {
  e.preventDefault();

  const languages = getSelectedValues(document.getElementById("languages"));
  const selectedLicenses = getSelectedValues(document.getElementById("licenses"));
  const customLicenses = parseCustomLicenses();
  const licenses = [...new Set([...selectedLicenses, ...customLicenses])];
  const visibility = document.getElementById("visibility")?.value || "public";
  const minStars = parseInt(document.getElementById("min_stars")?.value || "500", 10) || 500;
  const maxStarsRaw = document.getElementById("max_stars")?.value || "";
  const maxStars = maxStarsRaw ? parseInt(maxStarsRaw, 10) : null;
  const maxSizeRaw = document.getElementById("max_size_mb")?.value || "";
  const maxSizeMb = maxSizeRaw ? parseInt(maxSizeRaw, 10) : null;
  const resultLimit = parseInt(document.getElementById("result_limit")?.value || "20", 10) || 20;
  const minLoc = parseInt(document.getElementById("min_loc")?.value || "30000", 10);
  const domainIdeas = (document.getElementById("domain_ideas")?.value || "").trim();
  const requireArchitecture = document.getElementById("require_architecture")?.checked ?? true;
  const allowSurface = document.getElementById("allow_surface")?.checked ?? false;

  if (languages.length === 0 || licenses.length === 0) {
    errorMessage.textContent = "Select at least one language and one license.";
    errorSection.style.display = "block";
    resultsSection.style.display = "none";
    return;
  }

  submitBtn.disabled = true;
  spinner.style.display = "inline-block";
  btnText.textContent = "Searching...";
  loadingPanel.style.display = "flex";
  loadingMessage.textContent = "Searching GitHub repositories...";
  if (loadingQuery) loadingQuery.textContent = "";
  if (searchProgress) searchProgress.style.width = "0%";
  errorSection.style.display = "none";
  resultsSection.style.display = "none";

  try {
    const response = await fetch("/api/search/", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        visibility: visibility || null,
        languages,
        licenses,
        min_stars: minStars,
        max_stars: maxStars,
        max_size_mb: maxSizeMb,
        result_limit: Math.min(resultLimit, 100),
        min_loc: isNaN(minLoc) ? 0 : minLoc,
        require_architecture: requireArchitecture,
        allow_surface: allowSurface,
        domain_ideas: domainIdeas,
        shipd_mode: true,
      }),
    });

    const contentType = response.headers.get("content-type") || "";
    let data;
    if (contentType.includes("application/json")) {
      data = await response.json();
    } else {
      const text = await response.text();
      throw new Error(text ? `Server returned an error page. ${text.slice(0, 200)}` : "Server returned a non-JSON response.");
    }

    if (!response.ok || data.error) {
      throw new Error(data.error || "Search failed.");
    }

    currentRepos = (data.repositories || []).slice(0, resultLimit);
    currentPage = 1;

    resultCount.textContent = `${formatNumber(currentRepos.length)} repositories`;
    searchQuery.textContent = data.summary || data.query || "Searched GitHub for Shipd-fit public repositories.";
    displayPage();
    resultsSection.style.display = "flex";
  } catch (err) {
    errorMessage.textContent = err.message;
    errorSection.style.display = "block";
  } finally {
    submitBtn.disabled = false;
    spinner.style.display = "none";
    btnText.textContent = "Search repositories";
    loadingPanel.style.display = "none";
  }
});
