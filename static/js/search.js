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

function renderRepo(repo) {
  const license = repo.license?.spdx_id || repo.license?.name || "Unknown";
  const updated = repo.pushed_at ? new Date(repo.pushed_at).toLocaleDateString() : "Unknown";
  const s = repo.suitability || {};
  const locValue = toNumber(s.primary_loc);
  const loc = locValue && locValue > 0 ? formatNumber(locValue) : "Unknown";
  const scoreValue = toNumber(s.suitability_score);
  const scoreMode = s.score_mode === "basic" ? "Basic score" : "Score";
  const scoreTitle = s.score_reason || `Suitability score: ${scoreValue ?? "unknown"}/100`;
  const scoreBadge = scoreValue !== null
    ? `<span class="badge badge-score" title="${escapeHtml(scoreTitle)}">${scoreMode} ${Math.round(scoreValue)}</span>`
    : `<span class="badge badge-muted" title="Repository could not be scored.">Unscored</span>`;
  const dirCount = toNumber(s.source_dir_count);
  const maxDepth = toNumber(s.max_depth);
  const archMeta = dirCount && dirCount > 0
    ? `${formatNumber(dirCount)} dirs · depth ${maxDepth ?? 0}`
    : (s.score_mode === "basic" ? "Not inspected" : "—");
  const fullName = escapeHtml(repo.full_name || repo.name || "Unknown repository");
  const repoUrl = escapeHtml(repo.html_url || "#");
  return `
    <article class="repo-card">
      <div class="repo-header">
        <a href="${repoUrl}" target="_blank" rel="noopener" class="repo-name">${fullName}</a>
        ${scoreBadge}
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
          <span class="repo-stat-label">Architecture</span>
          <span class="repo-stat-value">${archMeta || "—"}</span>
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
          <span class="repo-stat-label">Size</span>
          <span class="repo-stat-value">${formatSize(repo.size)}</span>
        </div>
        <div class="repo-stat">
          <span class="repo-stat-label">Last commit</span>
          <span class="repo-stat-value">${updated}</span>
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
  const raw = document.getElementById("custom_license").value;
  if (!raw) return [];
  return raw.split(",").map((s) => s.trim()).filter(Boolean);
}

form.addEventListener("submit", async (e) => {
  e.preventDefault();

  const languages = getSelectedValues(document.getElementById("languages"));
  const selectedLicenses = getSelectedValues(document.getElementById("licenses"));
  const customLicenses = parseCustomLicenses();
  const licenses = [...new Set([...selectedLicenses, ...customLicenses])];
  const visibility = document.getElementById("visibility").value;
  const minStars = parseInt(document.getElementById("min_stars").value, 10) || 0;
  const maxStarsRaw = document.getElementById("max_stars").value;
  const maxStars = maxStarsRaw ? parseInt(maxStarsRaw, 10) : null;
  const maxSizeRaw = document.getElementById("max_size_mb").value;
  const maxSizeMb = maxSizeRaw ? parseInt(maxSizeRaw, 10) : null;
  const resultLimit = parseInt(document.getElementById("result_limit").value, 10) || 30;
  const minLoc = parseInt(document.getElementById("min_loc").value, 10);
  const requireArchitecture = document.getElementById("require_architecture").checked;
  const allowSurface = document.getElementById("allow_surface").checked;

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
    searchQuery.textContent = data.query || "Searched GitHub for active, permissive repositories.";
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
