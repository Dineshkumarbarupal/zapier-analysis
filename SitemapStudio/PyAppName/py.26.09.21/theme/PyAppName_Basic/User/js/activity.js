let logEntries = 0;

export function appendLog(message, type = "info", timestamp = Date.now() / 1000) {
  const container = document.getElementById("activity-log");
  if (!container) return;
  const empty = container.querySelector(".empty-log");
  if (empty) empty.remove();
  const row = document.createElement("div");
  const normalizedType = ["uploaded", "captured", "completed", "reports_uploaded", "connected"].includes(type)
    ? "success"
    : (["fatal", "error"].includes(type) ? "error" : (["warning", "skipped", "stopped"].includes(type) ? "warning" : ""));
  row.className = `log-row ${normalizedType}`;
  const time = document.createElement("span");
  time.className = "log-time";
  time.textContent = new Date(timestamp * 1000).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit" });
  const text = document.createElement("span");
  text.className = "log-message";
  text.textContent = message;
  row.append(time, text);
  container.appendChild(row);
  container.scrollTop = container.scrollHeight;
  logEntries += 1;
  const count = document.getElementById("log-count");
  if (count) count.textContent = `${logEntries} events`;
  while (container.querySelectorAll(".log-row").length > 500) container.querySelector(".log-row")?.remove();
}

export function clearLog() {
  const container = document.getElementById("activity-log");
  if (!container) return;
  container.innerHTML = `<div class="empty-log"><span class="empty-log-icon">⌁</span><strong>No activity yet</strong><p>New discovery and upload events will appear here.</p></div>`;
  logEntries = 0;
  const count = document.getElementById("log-count");
  if (count) count.textContent = "0 events";
}
