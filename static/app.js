const state = {
  us: { path: "", entries: [], selected: new Set(), loadSeq: 0, loading: false, queuedLoad: false, loadedOnce: false, error: "" },
  kr: { path: "", entries: [], selected: new Set(), loadSeq: 0, loading: false, queuedLoad: false, loadedOnce: false, error: "" },
  showHidden: false,
  lastTaskId: "",
  lastTaskStatus: "",
};

const els = {
  usList: document.getElementById("usList"),
  krList: document.getElementById("krList"),
  usPathLabel: document.getElementById("usPathLabel"),
  krPathLabel: document.getElementById("krPathLabel"),
  usUp: document.getElementById("usUp"),
  krUp: document.getElementById("krUp"),
  usRefresh: document.getElementById("usRefresh"),
  krRefresh: document.getElementById("krRefresh"),
  usPaneStatus: document.getElementById("usPaneStatus"),
  krPaneStatus: document.getElementById("krPaneStatus"),
  refreshAll: document.getElementById("refreshAll"),
  themeToggle: document.getElementById("themeToggle"),
  showHidden: document.getElementById("showHidden"),
  copyToKr: document.getElementById("copyToKr"),
  copyToUs: document.getElementById("copyToUs"),
  stopTask: document.getElementById("stopTask"),
  taskLine: document.getElementById("taskLine"),
  statusText: document.getElementById("statusText"),
  currentItem: document.getElementById("currentItem"),
  taskId: document.getElementById("taskId"),
  progressBar: document.getElementById("progressBar"),
  logBox: document.getElementById("logBox"),
};

function api(path, options = {}) {
  return fetch(path, {
    ...options,
    headers: {
      "Content-Type": "application/json",
      ...(options.headers || {}),
    },
  }).then(async (res) => {
    const body = await res.json().catch(() => ({}));
    if (!res.ok || body.ok === false) {
      throw new Error(body.error || `HTTP ${res.status}`);
    }
    return body;
  });
}

function setTheme(theme, persist = true) {
  const light = theme === "light";
  document.documentElement.dataset.theme = light ? "light" : "dark";
  const nextThemeLabel = light ? "切换到暗色主题" : "切换到亮色主题";
  els.themeToggle.setAttribute("aria-label", nextThemeLabel);
  els.themeToggle.title = nextThemeLabel;
  if (persist) {
    try {
      localStorage.setItem("transfer-web-theme", light ? "light" : "dark");
    } catch (error) {
      // The theme still applies for this page when browser storage is unavailable.
    }
  }
}

function toggleTheme() {
  setTheme(document.documentElement.dataset.theme === "light" ? "dark" : "light");
}

function fullPath(side) {
  const base = side === "us" ? "/home/ubuntu" : "/home/azureuser";
  return state[side].path ? `${base}/${state[side].path}` : `${base}/`;
}

function parentPath(path) {
  if (!path) return "";
  const parts = path.split("/");
  parts.pop();
  return parts.join("/");
}

function formatSize(bytes) {
  if (!Number.isFinite(bytes)) return "-";
  const units = ["B", "KB", "MB", "GB", "TB"];
  let value = bytes;
  let index = 0;
  while (value >= 1024 && index < units.length - 1) {
    value /= 1024;
    index += 1;
  }
  return `${value.toFixed(index === 0 ? 0 : 1)} ${units[index]}`;
}

function formatTime(seconds) {
  if (!seconds) return "-";
  return new Date(seconds * 1000).toLocaleString();
}

function iconFor(entry) {
  if (entry.type === "dir") return "▸";
  if (entry.type === "symlink") return "↪";
  return "•";
}

function isHiddenEntry(entry) {
  return entry.name !== ".." && entry.name.startsWith(".");
}

function render(side) {
  const data = state[side];
  const list = side === "us" ? els.usList : els.krList;
  const pathLabel = side === "us" ? els.usPathLabel : els.krPathLabel;
  const upButton = side === "us" ? els.usUp : els.krUp;

  pathLabel.textContent = data.path;
  pathLabel.title = fullPath(side);
  upButton.disabled = !data.path;
  list.innerHTML = "";

  const visibleEntries = data.entries.filter((entry) => state.showHidden || !isHiddenEntry(entry));

  if (visibleEntries.length === 0) {
    const row = document.createElement("tr");
    const cell = document.createElement("td");
    cell.colSpan = 4;
    cell.className = "muted";
    cell.textContent = data.loading && !data.loadedOnce ? "加载中..." : "目录为空";
    row.appendChild(cell);
    list.appendChild(row);
  }

  for (const entry of visibleEntries) {
    const row = document.createElement("tr");
    row.className = `file-row${entry.safe ? "" : " disabled"}`;
    row.classList.toggle("selected", data.selected.has(entry.path));
    row.title = entry.safe ? entry.path : entry.reason || "不可选择";

    const checkCell = document.createElement("td");
    checkCell.className = "check-col";
    const checkbox = document.createElement("input");
    checkbox.type = "checkbox";
    checkbox.disabled = !entry.safe;
    checkbox.checked = data.selected.has(entry.path);
    checkbox.addEventListener("click", (event) => event.stopPropagation());
    checkbox.addEventListener("change", () => {
      if (checkbox.checked) data.selected.add(entry.path);
      else data.selected.delete(entry.path);
      row.classList.toggle("selected", checkbox.checked);
      updateTransferButtons();
    });
    checkCell.appendChild(checkbox);

    const nameCell = document.createElement("td");
    const wrap = document.createElement("div");
    wrap.className = `name-cell${entry.type === "dir" ? " folder" : ""}`;
    const icon = document.createElement("span");
    icon.className = "file-icon";
    icon.textContent = iconFor(entry);
    const name = document.createElement("span");
    name.className = "name";
    name.textContent = entry.name;
    wrap.append(icon, name);
    nameCell.appendChild(wrap);

    if (entry.type === "dir") {
      row.addEventListener("dblclick", () => openDir(side, entry.path));
      nameCell.addEventListener("click", () => openDir(side, entry.path));
    }

    const sizeCell = document.createElement("td");
    sizeCell.className = "size-col";
    sizeCell.textContent = entry.type === "dir" ? "-" : formatSize(entry.size);

    const timeCell = document.createElement("td");
    timeCell.className = "time-col";
    timeCell.textContent = formatTime(entry.mtime);

    row.append(checkCell, nameCell, sizeCell, timeCell);
    list.appendChild(row);
  }

  updateTransferButtons();
}

function paneStatus(side) {
  return side === "us" ? els.usPaneStatus : els.krPaneStatus;
}

function setPaneStatus(side, text, kind = "") {
  const el = paneStatus(side);
  el.textContent = text;
  el.className = `pane-status ${kind}`.trim();
}

async function load(side) {
  const dataState = state[side];
  const list = side === "us" ? els.usList : els.krList;
  if (dataState.loading) {
    dataState.queuedLoad = true;
    setPaneStatus(side, "等待刷新", "loading");
    return;
  }

  const requestPath = dataState.path;
  const requestSeq = dataState.loadSeq + 1;
  dataState.loadSeq = requestSeq;
  dataState.loading = true;
  dataState.queuedLoad = false;
  dataState.error = "";
  setPaneStatus(side, "刷新中", "loading");
  if (!dataState.loadedOnce && dataState.entries.length === 0) render(side);

  try {
    const data = await api(`/api/list?side=${encodeURIComponent(side)}&path=${encodeURIComponent(requestPath)}&showHidden=${state.showHidden ? "1" : "0"}`);
    if (requestSeq !== dataState.loadSeq || requestPath !== dataState.path) return;
    dataState.path = data.path || "";
    dataState.entries = data.entries || [];
    dataState.selected.clear();
    dataState.loadedOnce = true;
    dataState.error = "";
    render(side);
    setPaneStatus(side, "");
  } catch (error) {
    if (requestSeq !== dataState.loadSeq || requestPath !== dataState.path) return;
    dataState.error = error.message;
    setPaneStatus(side, error.message, "error");
    if (!dataState.loadedOnce && dataState.entries.length === 0) {
      list.innerHTML = `<tr><td colspan="4" class="muted">${error.message}</td></tr>`;
    }
  } finally {
    if (requestSeq === dataState.loadSeq) {
      dataState.loading = false;
      if (dataState.queuedLoad) {
        dataState.queuedLoad = false;
        load(side);
      }
    }
  }
}

function openDir(side, path) {
  state[side].path = path;
  load(side);
}

function up(side) {
  state[side].path = parentPath(state[side].path);
  load(side);
}

function toggleHiddenFiles() {
  state.showHidden = els.showHidden.checked;
  for (const side of ["us", "kr"]) {
    state[side].selected.clear();
    render(side);
  }
  Promise.all([load("us"), load("kr")]);
}

async function startTransfer(direction) {
  const sourceSide = direction === "us_to_kr" ? "us" : "kr";
  const destSide = direction === "us_to_kr" ? "kr" : "us";
  const items = Array.from(state[sourceSide].selected);
  if (!items.length) return;

  els.copyToKr.disabled = true;
  els.copyToUs.disabled = true;
  try {
    const body = { direction, items, destPath: state[destSide].path };
    const result = await api("/api/transfer", {
      method: "POST",
      body: JSON.stringify(body),
    });
    state.lastTaskId = result.task.id;
    state.lastTaskStatus = result.task.status || "running";
    state[sourceSide].selected.clear();
    render(sourceSide);
    await pollStatus();
  } catch (error) {
    els.taskLine.textContent = error.message;
  } finally {
    updateTransferButtons();
  }
}

function updateTransferButtons() {
  const running = els.statusText.dataset.running === "1";
  els.copyToKr.disabled = running || state.us.selected.size === 0;
  els.copyToUs.disabled = running || state.kr.selected.size === 0;
}

function renderStatus(task) {
  if (!task) {
    els.statusText.textContent = "空闲";
    els.statusText.dataset.running = "0";
    els.currentItem.textContent = "-";
    els.taskId.textContent = "-";
    els.progressBar.style.width = "0%";
    els.stopTask.disabled = true;
    els.taskLine.textContent = "就绪";
    updateTransferButtons();
    return;
  }

  const running = task.status === "running";
  els.statusText.textContent = task.error ? `${task.status}: ${task.error}` : task.status;
  els.statusText.dataset.running = running ? "1" : "0";
  els.currentItem.textContent = task.currentItem || "-";
  els.taskId.textContent = task.id || "-";
  els.progressBar.style.width = `${Math.max(0, Math.min(100, Number(task.progress) || 0))}%`;
  els.stopTask.disabled = !running;
  els.taskLine.textContent = task.direction ? `${task.direction} · ${task.status}` : task.status;
  if (task.id) state.lastTaskId = task.id;
  updateTransferButtons();
}

async function pollStatus() {
  try {
    const data = await api("/api/status");
    const previousTaskId = state.lastTaskId;
    const previousStatus = state.lastTaskStatus;
    renderStatus(data.task);
    if (data.task?.id) await loadLog(data.task.id);

    const taskFinished =
      data.task &&
      data.task.id === previousTaskId &&
      previousStatus === "running" &&
      data.task.status !== "running";

    state.lastTaskStatus = data.task?.status || "";

    if (taskFinished) {
      await Promise.all([load("us"), load("kr")]);
    }
  } catch (error) {
    els.taskLine.textContent = error.message;
  }
}

async function loadLog(taskId = state.lastTaskId) {
  try {
    const data = await api(`/api/logs?task=${encodeURIComponent(taskId || "")}`);
    els.logBox.textContent = data.log || "";
    els.logBox.scrollTop = els.logBox.scrollHeight;
  } catch (error) {
    els.logBox.textContent = error.message;
  }
}

async function stopTask() {
  try {
    const data = await api("/api/stop", { method: "POST", body: "{}" });
    els.taskLine.textContent = data.message || "已请求停止";
    await pollStatus();
  } catch (error) {
    els.taskLine.textContent = error.message;
  }
}

els.usUp.addEventListener("click", () => up("us"));
els.krUp.addEventListener("click", () => up("kr"));
els.usRefresh.addEventListener("click", () => load("us"));
els.krRefresh.addEventListener("click", () => load("kr"));
els.refreshAll.addEventListener("click", () => Promise.all([load("us"), load("kr"), pollStatus()]));
els.themeToggle.addEventListener("click", toggleTheme);
els.showHidden.addEventListener("change", toggleHiddenFiles);
els.copyToKr.addEventListener("click", () => startTransfer("us_to_kr"));
els.copyToUs.addEventListener("click", () => startTransfer("kr_to_us"));
els.stopTask.addEventListener("click", stopTask);

setTheme(document.documentElement.dataset.theme === "light" ? "light" : "dark", false);
const systemTheme = window.matchMedia("(prefers-color-scheme: light)");
function followSystemTheme(event) {
  try {
    const savedTheme = localStorage.getItem("transfer-web-theme");
    if (savedTheme === "light" || savedTheme === "dark") return;
  } catch (error) {
    // Continue following the system theme when browser storage is unavailable.
  }
  setTheme(event.matches ? "light" : "dark", false);
}
if (systemTheme.addEventListener) systemTheme.addEventListener("change", followSystemTheme);
else systemTheme.addListener?.(followSystemTheme);
Promise.all([load("us"), load("kr"), pollStatus()]);
setInterval(pollStatus, 2000);
