export function backend(name, ...args) {
  return new Promise((resolve, reject) => {
    if (!window.eel || typeof window.eel[name] !== "function") {
      reject(new Error("Python bridge is not available. Restart the desktop app."));
      return;
    }
    try {
      window.eel[name](...args)(resolve);
    } catch (error) {
      reject(error);
    }
  });
}

export function formatNumber(value) {
  return Number(value || 0).toLocaleString();
}

export function formatDate(value) {
  if (!value) return "—";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return value;
  return date.toLocaleString(undefined, { year: "numeric", month: "short", day: "2-digit", hour: "2-digit", minute: "2-digit" });
}

export function toast(message, type = "info") {
  const stack = document.getElementById("toast-stack");
  const item = document.createElement("div");
  item.className = `toast ${type}`;
  item.textContent = message;
  stack.appendChild(item);
  window.setTimeout(() => item.remove(), 4600);
}

export function setFormMessage(message, type = "") {
  const element = document.getElementById("form-message");
  if (!element) return;
  element.textContent = message || "";
  element.className = `form-message ${type}`.trim();
}

export function setText(id, value) {
  const node = document.getElementById(id);
  if (node) node.textContent = value;
}

export function escapeHtml(value) {
  return String(value).replace(/[&<>"']/g, (char) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[char]));
}
