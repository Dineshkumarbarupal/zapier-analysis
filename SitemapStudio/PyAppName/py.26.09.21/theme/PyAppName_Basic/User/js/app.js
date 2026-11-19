import { backend, formatNumber, formatDate, toast, setFormMessage, setText, escapeHtml } from "/js/api.js";
import { appendLog, clearLog } from "/js/activity.js";

const SAMPLE_SITEMAPS = [
  "https://zapier.com/uag6x9zaeydnbfy75xsggcsb37h8cgzf-two-service-sitemap.xml",
  "https://zapier.com/uag6x9zaeydnbfy75xsggcsb37h8cgzf-one-service-sitemap.xml",
  "https://zapier.com/sitemaps/zap-templates/LZB2XiWzbJFggRYdnRGwcoAgEkTnHHEh",
  "https://zapier.com/blog/sitemap.xml",
  "https://zapier.com/uag6x9zaeydnbfy75xsggcsb37h8cgzf-sitemap.xml",
  "https://zapier.com/uag6x9zaeydnbfy75xsggcsb37h8cgzf-static-sitemap.xml",
  "https://zapier.com/agents/sitemap.xml",
  "https://zapier.com/automation/sitemap.xml",
  "https://zapier.com/marketing/sitemap.xml",
  "https://zapier.com/automations/sitemap-automations.xml"
].join("\n");

const state = {
  page: "dashboard",
  logEntries: 0,
  pollBusy: false,
  currentJobId: null,
  lastStatus: null
};

async function mountPartial(elementId, path) {
  const response = await fetch(path, { cache: "no-store" });
  if (!response.ok) throw new Error(`Could not load ${path}`);
  document.getElementById(elementId).innerHTML = await response.text();
}

function parseForm() {
  const urls = document.getElementById("sitemap-urls").value.trim();
  return {
    sitemap_urls: urls,
    repository_url: document.getElementById("repository-url").value.trim(),
    branch: document.getElementById("branch-name").value.trim() || "main",
    github_token: document.getElementById("github-token").value,
    batch_size: Number(document.getElementById("batch-size").value),
    delay_seconds: Number(document.getElementById("delay-seconds").value),
    max_urls: Number(document.getElementById("max-urls").value || 0),
    settle_ms: Number(document.getElementById("settle-ms").value),
    screenshot_quality: Number(document.getElementById("quality").value),
    max_full_page_height: Number(document.getElementById("max-height").value),
    retry_failed: document.getElementById("retry-failed").checked
  };
}

function setBusy(busy) {
  const start = document.getElementById("start-button");
  const validate = document.getElementById("validate-button");
  const stop = document.getElementById("stop-button");
  if (start) start.disabled = busy;
  if (validate) validate.disabled = busy;
  if (stop) stop.classList.toggle("hidden", !busy);
  const connection = document.getElementById("connection-pill");
  if (connection) connection.innerHTML = `<span class="status-dot"></span><span>${busy ? "Scan running" : "Desktop ready"}</span>`;
}

async function startPage(page) {
  state.page = page;
  const file = page === "history" ? "/template/history.html" : (page === "help" ? "/template/help.html" : "/template/dashboard.html");
  await mountPartial("main-slot", file);
  document.querySelectorAll(".nav-item").forEach((button) => button.classList.toggle("active", button.dataset.page === page));
  const title = document.getElementById("topbar-title");
  if (title) title.textContent = page === "history" ? "Run history" : page === "help" ? "Documentation" : "Scanner";

  if (page === "dashboard") {
    wireDashboard();
    await refreshDashboard();
  } else if (page === "history") {
    document.getElementById("history-refresh")?.addEventListener("click", refreshHistory);
    await refreshHistory();
  }
}

function wireDashboard() {
  document.getElementById("scan-form")?.addEventListener("submit", async (event) => {
    event.preventDefault();
    await startScan();
  });
  document.getElementById("validate-button")?.addEventListener("click", validateGitHub);
  document.getElementById("stop-button")?.addEventListener("click", stopScan);
  document.getElementById("toggle-token")?.addEventListener("click", () => {
    const input = document.getElementById("github-token");
    const button = document.getElementById("toggle-token");
    const show = input.type === "password";
    input.type = show ? "text" : "password";
    button.textContent = show ? "Hide" : "Show";
  });
  document.getElementById("load-zapier-samples")?.addEventListener("click", () => {
    document.getElementById("sitemap-urls").value = SAMPLE_SITEMAPS;
    toast("Loaded the 10 Zapier sitemap examples.", "success");
  });
  document.getElementById("toggle-advanced")?.addEventListener("click", () => {
    const advanced = document.getElementById("advanced-settings");
    advanced.classList.toggle("hidden");
    document.getElementById("advanced-chevron").textContent = advanced.classList.contains("hidden") ? "⌄" : "⌃";
  });
  document.getElementById("clear-log")?.addEventListener("click", clearLog);
  document.getElementById("refresh-status")?.addEventListener("click", refreshDashboard);
  document.getElementById("help-button")?.addEventListener("click", () => startPage("help"));
}

async function validateGitHub() {
  const button = document.getElementById("validate-button");
  const payload = parseForm();
  if (!payload.github_token) {
    setFormMessage("Enter a GitHub token first.", "error");
    return;
  }
  button.disabled = true;
  button.textContent = "Testing connection…";
  setFormMessage("Testing repository and branch access…");
  try {
    const result = await backend("validate_connection", payload);
    if (result.ok) {
      setFormMessage(result.message, "success");
      toast("GitHub connection validated.", "success");
      appendLog(result.message, "connected");
    } else {
      setFormMessage(result.error || "Connection test failed.", "error");
    }
  } catch (error) {
    setFormMessage(error.message || String(error), "error");
  } finally {
    // Keep the token in the password field after a read-only connection test so
    // the user can start the scan without pasting it twice. Start clears it.
    button.disabled = false;
    button.innerHTML = `<span class="button-icon">⌁</span> Test GitHub connection`;
  }
}

async function startScan() {
  const button = document.getElementById("start-button");
  const payload = parseForm();
  if (!payload.sitemap_urls) return setFormMessage("Add at least one sitemap URL.", "error");
  if (!payload.repository_url) return setFormMessage("Enter the GitHub repository URL.", "error");
  if (!payload.github_token) return setFormMessage("Enter a GitHub token. The token is not saved by this app.", "error");
  if (payload.max_urls === 0) {
    const proceed = window.confirm(
      "Unlimited scan selected. Some sitemaps contain hundreds of thousands of URLs. Capturing every page can take a long time, consume GitHub API quota, and create a very large repository. Continue?"
    );
    if (!proceed) return;
  }
  button.disabled = true;
  button.querySelector("span:first-child").textContent = "Starting…";
  setFormMessage("Validating settings and starting the scan…");
  try {
    const result = await backend("start_job", payload);
    document.getElementById("github-token").value = "";
    if (!result.ok) {
      setFormMessage(result.error || "Could not start scan.", "error");
      toast(result.error || "Could not start scan.", "error");
      return;
    }
    state.currentJobId = result.job_id;
    clearLog();
    appendLog(`Job ${result.job_id} created. Initializing browser and GitHub connection…`, "info");
    setFormMessage(`Scan started. Job ID: ${result.job_id}`, "success");
    toast("Sitemap scan started.", "success");
    await refreshStatus();
  } catch (error) {
    document.getElementById("github-token").value = "";
    setFormMessage(error.message || String(error), "error");
  } finally {
    button.disabled = Boolean(state.lastStatus?.running);
    button.querySelector("span:first-child").textContent = "Start scanning";
  }
}

async function stopScan() {
  const button = document.getElementById("stop-button");
  button.disabled = true;
  button.textContent = "Stopping…";
  try {
    const result = await backend("stop_job");
    toast(result.message || "Stop request sent.");
  } catch (error) {
    toast(error.message || String(error), "error");
  } finally {
    button.disabled = false;
    button.textContent = "Stop scan";
  }
}

function statusClass(status) {
  return String(status || "unknown").toLowerCase().replace(/[^a-z0-9_-]/g, "_");
}

function updateDashboard(status) {
  state.lastStatus = status;
  const job = status?.job;
  const counts = status?.counts || {};
  const metrics = status?.metrics || {};
  const running = Boolean(status?.running);
  const tracked = Object.values(counts).reduce((a, b) => a + Number(b || 0), 0);
  const uploaded = Number(counts.uploaded || 0);
  const skipped = Number(counts.skipped_robots || 0);
  const failed = Number(counts.failed || 0);
  const captured = Number(counts.captured || 0);
  const total = Number(job?.total_urls || 0);
  const done = uploaded + skipped + failed;
  const percent = total > 0 ? Math.min(100, Math.round((done / total) * 100)) : 0;

  setText("metric-tracked", formatNumber(metrics.tracked_pages || tracked));
  setText("metric-uploaded", formatNumber(metrics.uploaded_screenshots || uploaded));
  setText("metric-jobs", formatNumber(metrics.jobs || 0));
  setText("metric-engine", running ? "Working" : (job?.status === "completed" ? "Complete" : "Ready"));
  setText("metric-engine-detail", running ? (status.runtime?.message || "Processing pages") : (job?.message || "Waiting for a scan"));
  setText("scan-state", running ? capitalize(status.runtime?.stage || "running") : capitalize(job?.status || "idle"));
  setText("progress-label", total ? `${formatNumber(done)} of ${formatNumber(total)} pages resolved · ${formatNumber(captured)} waiting to upload` : "No active scan");
  setText("progress-percent", `${percent}%`);
  setText("progress-uploaded", formatNumber(uploaded));
  setText("progress-skipped", formatNumber(skipped));
  setText("progress-failed", formatNumber(failed));
  setText("activity-footer-text", running ? (status.runtime?.message || "Engine is working") : "Engine is ready for a new job");
  const bar = document.getElementById("progress-fill");
  if (bar) bar.style.width = `${percent}%`;
  const stateIcon = document.getElementById("scan-state-icon");
  if (stateIcon) stateIcon.textContent = running ? "◌" : (job?.status === "completed" ? "✓" : "Ⅱ");
  setBusy(running);
}

function capitalize(value) {
  const text = String(value || "");
  return text ? text.charAt(0).toUpperCase() + text.slice(1).replaceAll("_", " ") : "Ready";
}

async function refreshStatus() {
  try {
    const status = await backend("get_status");
    updateDashboard(status);
  } catch (error) {
    appendLog(`Could not refresh status: ${error.message || error}`, "warning");
  }
}

async function refreshDashboard() {
  await refreshStatus();
  try {
    const info = await backend("get_app_info");
    setText("metric-tracked", formatNumber(info.metrics?.tracked_pages || 0));
    setText("metric-uploaded", formatNumber(info.metrics?.uploaded_screenshots || 0));
    setText("metric-jobs", formatNumber(info.metrics?.jobs || 0));
  } catch (error) {
    appendLog(`App metrics unavailable: ${error.message || error}`, "warning");
  }
  await refreshInventory();
}

async function refreshInventory() {
  const body = document.getElementById("inventory-body");
  if (!body) return;
  try {
    const result = await backend("get_latest_inventory", 100);
    if (!result.job || !result.pages?.length) {
      body.innerHTML = `<tr><td colspan="4" class="table-empty">No pages discovered yet. Configure a sitemap to begin.</td></tr>`;
      setText("inventory-caption", "No inventory available");
      return;
    }
    body.replaceChildren();
    for (const page of result.pages) {
      const row = document.createElement("tr");
      const urlCell = document.createElement("td");
      const url = document.createElement("span");
      url.className = "table-url";
      url.title = page.url;
      url.textContent = page.title || page.url;
      const sub = document.createElement("span");
      sub.className = "table-secondary";
      sub.textContent = page.url;
      urlCell.append(url, sub);
      const sourceCell = document.createElement("td");
      sourceCell.textContent = page.source_sitemap || "—";
      sourceCell.title = page.source_sitemap || "";
      const statusCell = document.createElement("td");
      const badge = document.createElement("span");
      badge.className = `status-badge ${statusClass(page.status)}`;
      badge.textContent = page.status || "unknown";
      statusCell.appendChild(badge);
      const pathCell = document.createElement("td");
      pathCell.textContent = page.github_path || page.error || "—";
      pathCell.title = page.github_path || page.error || "";
      row.append(urlCell, sourceCell, statusCell, pathCell);
      body.appendChild(row);
    }
    setText("inventory-caption", `Showing ${result.pages.length} of ${formatNumber(result.job.total_urls)} URLs from the latest job`);
  } catch (error) {
    body.innerHTML = `<tr><td colspan="4" class="table-empty">Could not load inventory: ${escapeHtml(error.message || String(error))}</td></tr>`;
  }
}

async function refreshHistory() {
  const body = document.getElementById("history-body");
  if (!body) return;
  body.innerHTML = `<tr><td colspan="6" class="table-empty">Loading job history…</td></tr>`;
  try {
    const jobs = await backend("get_jobs");
    body.replaceChildren();
    if (!jobs.length) {
      body.innerHTML = `<tr><td colspan="6" class="table-empty">No scan history yet.</td></tr>`;
      return;
    }
    for (const job of jobs) {
      const row = document.createElement("tr");
      const values = [
        formatDate(job.created_at),
        job.config?.repository || "—",
        job.config?.branch || "main",
        job.status || "unknown",
        formatNumber(job.total_urls || 0),
        formatNumber(job.uploaded || 0)
      ];
      values.forEach((value, index) => {
        const cell = document.createElement("td");
        if (index === 3) {
          const badge = document.createElement("span");
          badge.className = `status-badge ${statusClass(value)}`;
          badge.textContent = value;
          cell.appendChild(badge);
        } else {
          cell.textContent = value;
        }
        row.appendChild(cell);
      });
      body.appendChild(row);
    }
  } catch (error) {
    body.innerHTML = `<tr><td colspan="6" class="table-empty">Could not load job history: ${escapeHtml(error.message || String(error))}</td></tr>`;
  }
}

async function pollLoop() {
  if (state.pollBusy) return;
  state.pollBusy = true;
  try {
    const events = await backend("poll_events");
    if (state.page === "dashboard") {
      for (const event of events) appendLog(event.message, event.type, event.timestamp);
    }
    if (events.some((event) => ["uploaded", "completed", "stopped", "fatal", "reports_uploaded"].includes(event.type))) {
      if (state.page === "dashboard") await refreshInventory();
      if (state.page === "history") await refreshHistory();
    }
    const status = await backend("get_status");
    if (state.page === "dashboard") updateDashboard(status);
  } catch (error) {
    console.warn("Status polling error", error);
  } finally {
    state.pollBusy = false;
  }
}

async function initialize() {
  try {
    await mountPartial("sidebar-slot", "/component/sidebar.html");
    await mountPartial("topbar-slot", "/component/topbar.html");
    document.querySelectorAll(".nav-item").forEach((button) => {
      button.addEventListener("click", () => startPage(button.dataset.page).catch((error) => toast(error.message, "error")));
    });
    document.getElementById("help-button")?.addEventListener("click", () => startPage("help"));
    await startPage("dashboard");
    window.setInterval(pollLoop, 1100);
  } catch (error) {
    document.getElementById("main-slot").innerHTML = `<div class="panel" style="padding:24px"><h2>Application UI could not load</h2><p>${escapeHtml(error.message || String(error))}</p></div>`;
  }
}

document.addEventListener("DOMContentLoaded", initialize);
