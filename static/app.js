const STATUS_LABELS = {
  idle: "空闲",
  queued: "排队中",
  running: "传输中",
  completed: "已完成",
  failed: "失败",
  stopped: "已停止",
  cancelled: "已取消",
  interrupted: "已中断",
};
const ACTIVE_STATUSES = new Set(["queued", "running"]);
const POLL_ACTIVE_MS = 1000;
const POLL_IDLE_MS = 10000;
const HEALTH_INTERVAL_MS = 60000;
const LOG_DOM_LIMIT = 400000;
const CONFIRM_ITEM_LIMIT = 50;
const SVG_NS = "http://www.w3.org/2000/svg";
const SIDES = ["left", "right"];
const OTHER_SIDE = { left: "right", right: "left" };

const $ = (id) => document.getElementById(id);
const els = {
  appTitle: $("appTitle"),
  taskLine: $("taskLine"),
  userBadge: $("userBadge"),
  showHidden: $("showHidden"),
  refreshAll: $("refreshAll"),
  openHistory: $("openHistory"),
  themeToggle: $("themeToggle"),
  copyRight: $("copyRight"),
  copyLeft: $("copyLeft"),
  swapSides: $("swapSides"),
  statusBadge: $("statusBadge"),
  statusTitle: $("statusTitle"),
  queueLine: $("queueLine"),
  stopTask: $("stopTask"),
  progress: $("progress"),
  progressBar: $("progressBar"),
  statusGrid: $("statusGrid"),
  statusError: $("statusError"),
  logDetails: $("logDetails"),
  logBox: $("logBox"),
  confirmDialog: $("confirmDialog"),
  cfFromName: $("cfFromName"),
  cfToName: $("cfToName"),
  cfToPath: $("cfToPath"),
  cfItems: $("cfItems"),
  cfPreview: $("cfPreview"),
  cfSkipExisting: $("cfSkipExisting"),
  cfStart: $("cfStart"),
  historyDialog: $("historyDialog"),
  historyList: $("historyList"),
  historyDetail: $("historyDetail"),
  toasts: $("toasts"),
  paneTemplate: $("paneTemplate"),
};

const state = {
  config: null,
  endpoints: new Map(),
  showHidden: false,
  panes: {},
  status: { active: null, queued: [], last: null },
  statusLoaded: false,
  health: {},
  log: { taskId: "", offset: null, busy: false, complete: false },
  confirm: null,
  history: { tasks: [], selected: "", seq: 0 },
  pollTimer: 0,
  healthTimer: 0,
};

// ---- small helpers ---------------------------------------------------------

function storageGet(key) {
  try {
    return localStorage.getItem(key);
  } catch (error) {
    return null;
  }
}

function storageSet(key, value) {
  try {
    localStorage.setItem(key, value);
  } catch (error) {
    // Preferences simply do not persist when storage is unavailable.
  }
}

function icon(name, className = "icon") {
  const svg = document.createElementNS(SVG_NS, "svg");
  svg.setAttribute("class", className);
  svg.setAttribute("aria-hidden", "true");
  const use = document.createElementNS(SVG_NS, "use");
  use.setAttribute("href", `#i-${name}`);
  svg.appendChild(use);
  return svg;
}

function el(tag, options = {}, ...children) {
  const node = document.createElement(tag);
  if (options.className) node.className = options.className;
  if (options.text !== undefined) node.textContent = options.text;
  for (const [name, value] of Object.entries(options.attrs || {})) {
    if (value !== undefined && value !== null && value !== false) node.setAttribute(name, value === true ? "" : value);
  }
  for (const child of children) {
    if (child !== null && child !== undefined) node.append(child);
  }
  return node;
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

function pad(value) {
  return String(value).padStart(2, "0");
}

function formatDate(date) {
  if (!date || Number.isNaN(date.getTime())) return "-";
  return `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())} ${pad(date.getHours())}:${pad(date.getMinutes())}`;
}

function parseIso(iso) {
  return iso ? new Date(iso) : null;
}

function relativeTime(iso) {
  const date = parseIso(iso);
  if (!date || Number.isNaN(date.getTime())) return "";
  const seconds = Math.round((Date.now() - date.getTime()) / 1000);
  if (seconds < 45) return "刚刚";
  if (seconds < 3600) return `${Math.round(seconds / 60)} 分钟前`;
  if (seconds < 86400) return `${Math.round(seconds / 3600)} 小时前`;
  if (seconds < 86400 * 7) return `${Math.round(seconds / 86400)} 天前`;
  return formatDate(date);
}

function formatDuration(startIso, endIso) {
  const start = parseIso(startIso);
  const end = endIso ? parseIso(endIso) : new Date();
  if (!start || !end) return "-";
  const total = Math.max(0, Math.round((end.getTime() - start.getTime()) / 1000));
  const hours = Math.floor(total / 3600);
  const minutes = Math.floor((total % 3600) / 60);
  const seconds = total % 60;
  if (hours) return `${hours} 小时 ${minutes} 分`;
  if (minutes) return `${minutes} 分 ${seconds} 秒`;
  return `${seconds} 秒`;
}

function basename(path) {
  const parts = (path || "").split("/");
  return parts[parts.length - 1] || path;
}

function parentPath(path) {
  if (!path) return "";
  const parts = path.split("/");
  parts.pop();
  return parts.join("/");
}

function toast(message, kind = "info", timeout = 6000) {
  const node = el("div", { className: `toast ${kind}`, text: message, attrs: { role: kind === "error" ? "alert" : "status" } });
  els.toasts.appendChild(node);
  setTimeout(() => node.remove(), timeout);
}

async function api(path, options = {}) {
  const init = { ...options, headers: { ...(options.headers || {}) } };
  if (init.method === "POST") {
    init.headers["Content-Type"] = "application/json";
    init.body = JSON.stringify(options.body || {});
  }
  let res;
  try {
    res = await fetch(path, init);
  } catch (error) {
    throw new Error("无法连接到服务。");
  }
  const body = await res.json().catch(() => ({}));
  if (!res.ok || body.ok === false) {
    throw new Error(body.error || `请求失败（HTTP ${res.status}）`);
  }
  return body;
}

// ---- endpoints -------------------------------------------------------------

function endpoint(id) {
  return state.endpoints.get(id);
}

function endpointName(id) {
  return endpoint(id)?.name || id || "?";
}

function displayPath(endpointId, rel) {
  const root = endpoint(endpointId)?.root || "";
  if (!rel) return root.endsWith("/") ? root : `${root}/`;
  return `${root.replace(/\/$/, "")}/${rel}`;
}

function canTransfer(fromId, toId) {
  return (state.config?.transferable?.[fromId] || []).includes(toId);
}

function itemsSummary(items) {
  if (!items?.length) return "";
  return items.length === 1 ? basename(items[0]) : `${basename(items[0])} 等 ${items.length} 项`;
}

function taskRoute(task) {
  return `${endpointName(task.from)} → ${endpointName(task.to)}`;
}

// ---- URL hash: which endpoint and directory each pane shows ---------------

function readHash() {
  const params = new URLSearchParams(location.hash.slice(1));
  return {
    left: params.get("left"),
    leftPath: params.get("lp") || "",
    right: params.get("right"),
    rightPath: params.get("rp") || "",
  };
}

function saveHash() {
  const { left, right } = state.panes;
  const params = new URLSearchParams({ left: left.endpoint, lp: left.path, right: right.endpoint, rp: right.path });
  history.replaceState(null, "", `#${params.toString()}`);
}

// ---- panes -------------------------------------------------------------------

function createPane(side, container) {
  container.appendChild(els.paneTemplate.content.cloneNode(true));
  const q = (selector) => container.querySelector(selector);
  const pane = {
    side,
    endpoint: "",
    path: "",
    entries: [],
    byPath: new Map(),
    visible: [],
    selected: new Set(),
    anchor: "",
    sort: { key: "name", dir: 1 },
    filter: "",
    seq: 0,
    loading: false,
    limited: false,
    error: "",
    els: {
      root: container,
      dot: q(".conn-dot"),
      name: q(".pane-name"),
      select: q(".endpoint-select"),
      host: q(".pane-host"),
      status: q(".pane-status"),
      up: q(".up-button"),
      refresh: q(".refresh-button"),
      crumbs: q(".breadcrumbs"),
      pathInput: q(".path-input"),
      editPath: q(".edit-path"),
      filter: q(".filter-input"),
      selectAll: q(".select-all"),
      thead: q("thead"),
      tbody: q("tbody"),
      count: q(".pane-count"),
      warning: q(".pane-warning"),
    },
  };
  const e = pane.els;

  e.select.addEventListener("change", () => setEndpoint(side, e.select.value));
  e.up.addEventListener("click", () => navigate(side, parentPath(pane.path)));
  e.refresh.addEventListener("click", () => loadPane(side, { keepSelection: true }));
  e.editPath.addEventListener("click", () => togglePathInput(pane, true));
  e.pathInput.addEventListener("keydown", (event) => {
    if (event.key === "Enter") {
      event.preventDefault();
      submitPathInput(pane);
    } else if (event.key === "Escape") {
      togglePathInput(pane, false);
    }
  });
  e.pathInput.addEventListener("blur", () => togglePathInput(pane, false));
  e.filter.addEventListener("input", () => {
    pane.filter = e.filter.value;
    renderRows(pane);
  });
  e.filter.addEventListener("keydown", (event) => {
    if (event.key === "Escape" && e.filter.value) {
      e.filter.value = "";
      pane.filter = "";
      renderRows(pane);
    }
  });
  e.selectAll.addEventListener("change", () => {
    const selectable = pane.visible.filter((entry) => entry.safe);
    if (e.selectAll.checked) selectable.forEach((entry) => pane.selected.add(entry.path));
    else selectable.forEach((entry) => pane.selected.delete(entry.path));
    syncSelection(pane);
  });
  e.thead.addEventListener("click", (event) => {
    const button = event.target.closest(".sort");
    if (!button) return;
    const key = button.dataset.key;
    pane.sort = pane.sort.key === key ? { key, dir: -pane.sort.dir } : { key, dir: key === "name" ? 1 : -1 };
    renderRows(pane);
  });
  e.tbody.addEventListener("click", (event) => onRowClick(pane, event));
  e.tbody.addEventListener("dblclick", (event) => {
    const entry = rowEntry(pane, event.target);
    if (entry?.type === "dir" && entry.safe && !event.target.closest("input, .name-link")) navigate(side, entry.path);
  });
  return pane;
}

// Entries whose name cannot be transferred come back without a path; key them
// by "/name", which can never collide with a valid (relative) path.
function entryKey(entry) {
  return entry.path || `/${entry.name}`;
}

function rowEntry(pane, target) {
  const row = target.closest("tr[data-path]");
  return row ? pane.byPath.get(row.dataset.path) : null;
}

function onRowClick(pane, event) {
  const action = event.target.closest("[data-action]");
  if (action) {
    if (action.dataset.action === "root") navigate(pane.side, "");
    if (action.dataset.action === "retry") loadPane(pane.side);
    return;
  }
  const entry = rowEntry(pane, event.target);
  if (!entry) return;
  if (event.target.closest(".name-link")) {
    navigate(pane.side, entry.path);
    return;
  }
  if (!entry.safe) return;
  if (event.shiftKey && pane.anchor && pane.anchor !== entry.path) {
    const paths = pane.visible.map((item) => item.path);
    const from = paths.indexOf(pane.anchor);
    const to = paths.indexOf(entry.path);
    if (from >= 0 && to >= 0) {
      const [start, end] = from < to ? [from, to] : [to, from];
      for (const item of pane.visible.slice(start, end + 1)) {
        if (item.safe) pane.selected.add(item.path);
      }
    }
  } else if (pane.selected.has(entry.path)) {
    pane.selected.delete(entry.path);
  } else {
    pane.selected.add(entry.path);
  }
  pane.anchor = entry.path;
  syncSelection(pane);
}

function togglePathInput(pane, show) {
  const e = pane.els;
  if (show) {
    e.pathInput.value = displayPath(pane.endpoint, pane.path);
    e.crumbs.hidden = true;
    e.pathInput.hidden = false;
    e.pathInput.focus();
    e.pathInput.select();
  } else {
    e.pathInput.hidden = true;
    e.crumbs.hidden = false;
  }
}

function submitPathInput(pane) {
  const root = (endpoint(pane.endpoint)?.root || "").replace(/\/$/, "");
  let value = pane.els.pathInput.value.trim().replace(/\/+$/, "");
  if (value.startsWith("/")) {
    if (value === root || value === "") {
      value = "";
    } else if (value.startsWith(`${root}/`)) {
      value = value.slice(root.length + 1);
    } else {
      toast(`只能访问 ${root}/ 之内的路径。`, "error");
      return;
    }
  }
  togglePathInput(pane, false);
  navigate(pane.side, value);
}

function sortedVisible(pane) {
  const needle = pane.filter.trim().toLowerCase();
  const { key, dir } = pane.sort;
  const collator = new Intl.Collator("zh-CN", { numeric: true, sensitivity: "base" });
  return pane.entries
    .filter((entry) => !needle || entry.name.toLowerCase().includes(needle))
    .sort((a, b) => {
      if ((a.type === "dir") !== (b.type === "dir")) return a.type === "dir" ? -1 : 1;
      let result = 0;
      if (key === "size") result = (a.type === "dir" ? 0 : a.size) - (b.type === "dir" ? 0 : b.size);
      else if (key === "mtime") result = a.mtime - b.mtime;
      if (result !== 0) return result * dir;
      const byName = collator.compare(a.name, b.name);
      return key === "name" ? byName * dir : byName;
    });
}

function renderPaneHead(pane) {
  const e = pane.els;
  const ep = endpoint(pane.endpoint);
  e.name.textContent = ep?.name || "";
  e.host.textContent = ep?.type === "ssh" ? ep.host : "本机";
  e.host.title = ep?.type === "ssh" ? `通过 SSH 连接 ${ep.host}` : "运行本服务的机器";
  const many = state.endpoints.size > 2;
  e.select.hidden = !many;
  e.name.hidden = many;
  if (many) {
    e.select.replaceChildren(...[...state.endpoints.values()].map((item) =>
      el("option", { text: item.name, attrs: { value: item.id, selected: item.id === pane.endpoint } })));
    e.select.value = pane.endpoint;
  }
  e.root.setAttribute("aria-label", `${ep?.name || ""}：${displayPath(pane.endpoint, pane.path)}`);
  e.up.disabled = !pane.path;
  renderBreadcrumbs(pane);
  renderDot(pane);
}

function renderBreadcrumbs(pane) {
  const nav = pane.els.crumbs;
  const root = (endpoint(pane.endpoint)?.root || "/").replace(/\/$/, "") || "/";
  const parts = pane.path ? pane.path.split("/") : [];
  const crumbs = [];
  const add = (label, path, current) => {
    const button = el("button", { className: "crumb", text: label, attrs: { type: "button", title: displayPath(pane.endpoint, path), "aria-current": current ? "page" : null } });
    button.addEventListener("click", () => navigate(pane.side, path));
    crumbs.push(button);
  };
  add(root, "", parts.length === 0);
  parts.forEach((part, index) => {
    crumbs.push(icon("chevron", "icon crumb-sep"));
    add(part, parts.slice(0, index + 1).join("/"), index === parts.length - 1);
  });
  nav.replaceChildren(...crumbs);
  nav.scrollLeft = nav.scrollWidth;
}

function renderRows(pane) {
  const e = pane.els;
  pane.visible = sortedVisible(pane);
  for (const th of e.thead.querySelectorAll("th[data-key]")) {
    const active = th.dataset.key === pane.sort.key;
    th.toggleAttribute("aria-sort", active);
    if (active) th.setAttribute("aria-sort", pane.sort.dir > 0 ? "ascending" : "descending");
    th.querySelector(".sort-ind").textContent = active ? (pane.sort.dir > 0 ? "▲" : "▼") : "";
  }

  const rows = [];
  if (pane.error) {
    const cell = el("td", { className: "message-cell error", attrs: { colspan: 4 } },
      el("div", { text: pane.error }),
      el("div", {},
        el("button", { text: "重试", attrs: { type: "button", "data-action": "retry" } }),
        " ",
        pane.path ? el("button", { text: "回到根目录", attrs: { type: "button", "data-action": "root" } }) : null));
    rows.push(el("tr", {}, cell));
  } else if (!pane.visible.length) {
    const text = pane.loading ? "加载中…" : pane.filter ? "没有匹配的项目" : "目录为空";
    rows.push(el("tr", {}, el("td", { className: "message-cell", text, attrs: { colspan: 4 } })));
  }

  for (const entry of pane.error ? [] : pane.visible) {
    const isDir = entry.type === "dir";
    const selected = pane.selected.has(entry.path);
    const checkbox = el("input", {
      attrs: { type: "checkbox", "aria-label": `选择 ${entry.name}`, disabled: !entry.safe },
    });
    checkbox.checked = selected;
    const nameNode = el("span", { className: "name", text: entry.name });
    const nameCell = el("div", { className: `name-cell${isDir ? " folder" : ""}` },
      icon(isDir ? "folder" : entry.type === "symlink" ? "link" : "file"),
      isDir && entry.safe
        ? el("button", { className: "name-link", attrs: { type: "button", title: `打开 ${entry.name}` } }, nameNode)
        : nameNode,
      entry.safe ? null : el("span", { className: "row-note", text: entry.type === "symlink" ? "符号链接" : "不可传输" }));
    const row = el("tr", {
      className: `file-row${selected ? " selected" : ""}${entry.safe ? "" : " disabled"}`,
      attrs: { "data-path": entryKey(entry), title: entry.safe ? null : entry.reason },
    },
    el("td", { className: "check-col" }, checkbox),
    el("td", { className: "name-col" }, nameCell),
    el("td", { className: "size-col", text: isDir ? "-" : formatSize(entry.size) }),
    el("td", { className: "time-col", text: formatDate(new Date(entry.mtime * 1000)), attrs: { title: new Date(entry.mtime * 1000).toLocaleString() } }));
    rows.push(row);
  }
  e.tbody.replaceChildren(...rows);
  syncSelection(pane);
}

function syncSelection(pane) {
  const e = pane.els;
  for (const row of e.tbody.querySelectorAll("tr[data-path]")) {
    const selected = pane.selected.has(row.dataset.path);
    row.classList.toggle("selected", selected);
    const checkbox = row.querySelector("input[type=checkbox]");
    if (checkbox) checkbox.checked = selected;
  }
  const selectable = pane.visible.filter((entry) => entry.safe);
  const selectedVisible = selectable.filter((entry) => pane.selected.has(entry.path)).length;
  e.selectAll.disabled = selectable.length === 0;
  e.selectAll.checked = selectable.length > 0 && selectedVisible === selectable.length;
  e.selectAll.indeterminate = selectedVisible > 0 && selectedVisible < selectable.length;
  renderFoot(pane);
  updateTransferButtons();
}

function renderFoot(pane) {
  const e = pane.els;
  const dirs = pane.entries.filter((entry) => entry.type === "dir").length;
  let text = pane.filter
    ? `显示 ${pane.visible.length} / ${pane.entries.length} 项`
    : `${pane.entries.length} 项${dirs ? `（${dirs} 个文件夹）` : ""}`;
  if (pane.selected.size) {
    const chosen = [...pane.selected].map((path) => pane.byPath.get(path)).filter(Boolean);
    const size = chosen.filter((entry) => entry.type !== "dir").reduce((sum, entry) => sum + entry.size, 0);
    const chosenDirs = chosen.filter((entry) => entry.type === "dir").length;
    text += ` · 已选 ${pane.selected.size} 项`;
    if (size) text += `，${formatSize(size)}`;
    if (chosenDirs) text += `${size ? " + " : "，含 "}${chosenDirs} 个文件夹`;
  }
  e.count.textContent = text;
  e.warning.hidden = !pane.limited;
  if (pane.limited) e.warning.lastElementChild.textContent = "目录过大，只显示了前 5000 项";
}

function renderPane(pane) {
  renderPaneHead(pane);
  renderRows(pane);
}

function setPaneStatus(pane, text, kind = "") {
  pane.els.status.textContent = text;
  pane.els.status.className = `pane-status ${kind}`.trim();
}

async function loadPane(side, { keepSelection = false } = {}) {
  const pane = state.panes[side];
  const seq = ++pane.seq;
  const requestEndpoint = pane.endpoint;
  const requestPath = pane.path;
  pane.loading = true;
  setPaneStatus(pane, "加载中…");
  if (!pane.entries.length && !pane.error) renderRows(pane);
  try {
    const params = new URLSearchParams({ endpoint: requestEndpoint, path: requestPath, showHidden: state.showHidden ? "1" : "0" });
    const data = await api(`/api/list?${params}`);
    if (seq !== pane.seq) return;
    pane.path = data.path || "";
    pane.entries = data.entries || [];
    pane.byPath = new Map(pane.entries.map((entry) => [entryKey(entry), entry]));
    pane.limited = Boolean(data.limited);
    pane.error = "";
    if (keepSelection) {
      for (const path of [...pane.selected]) if (!pane.byPath.get(path)?.safe) pane.selected.delete(path);
    } else {
      pane.selected.clear();
      pane.anchor = "";
    }
    setPaneStatus(pane, "");
    markHealth(requestEndpoint, true);
  } catch (error) {
    if (seq !== pane.seq) return;
    pane.entries = [];
    pane.byPath = new Map();
    pane.selected.clear();
    pane.error = error.message;
    setPaneStatus(pane, "加载失败", "error");
    if (/SSH|连接|主机|超时/.test(error.message)) markHealth(requestEndpoint, false, error.message);
  } finally {
    if (seq === pane.seq) {
      pane.loading = false;
      renderPane(pane);
    }
  }
}

function navigate(side, path) {
  const pane = state.panes[side];
  pane.path = path;
  pane.filter = "";
  pane.els.filter.value = "";
  pane.selected.clear();
  pane.anchor = "";
  pane.entries = [];
  pane.byPath = new Map();
  pane.error = "";
  saveHash();
  renderPaneHead(pane);
  loadPane(side);
}

function setEndpoint(side, endpointId) {
  if (endpointId === state.panes[OTHER_SIDE[side]].endpoint) {
    swapSides();
    return;
  }
  const pane = state.panes[side];
  pane.endpoint = endpointId;
  navigate(side, "");
}

function swapSides() {
  const { left, right } = state.panes;
  const keys = ["endpoint", "path", "entries", "byPath", "selected", "anchor", "limited", "error", "filter"];
  for (const key of keys) [left[key], right[key]] = [right[key], left[key]];
  for (const pane of [left, right]) {
    pane.seq += 1;
    pane.loading = false;
    pane.els.filter.value = pane.filter;
    setPaneStatus(pane, "");
    renderPane(pane);
  }
  saveHash();
}

// ---- transfer buttons and confirmation --------------------------------------

function updateTransferButtons() {
  if (!state.config) return;
  const buttons = [[els.copyRight, "left", "right"], [els.copyLeft, "right", "left"]];
  for (const [button, fromSide, toSide] of buttons) {
    const from = state.panes[fromSide];
    const to = state.panes[toSide];
    const count = from.selected.size;
    const allowed = canTransfer(from.endpoint, to.endpoint);
    button.querySelector(".transfer-label").textContent = `复制到${endpointName(to.endpoint)}`;
    button.querySelector(".transfer-sub").textContent = !allowed
      ? "不支持此方向"
      : count ? `已选 ${count} 项` : `先勾选${endpointName(from.endpoint)}的文件`;
    button.disabled = !allowed || count === 0;
    button.title = allowed
      ? `把${endpointName(from.endpoint)}选中的项目复制到 ${endpointName(to.endpoint)}:${displayPath(to.endpoint, to.path)}`
      : "rsync 不能直接在两个远端之间传输";
  }
}

function openConfirm(fromSide) {
  const from = state.panes[fromSide];
  const to = state.panes[OTHER_SIDE[fromSide]];
  const items = [...from.selected];
  if (!items.length) return;
  state.confirm = { from: from.endpoint, to: to.endpoint, items, destPath: to.path, fromSide, seq: 0 };
  els.cfFromName.textContent = endpointName(from.endpoint);
  els.cfToName.textContent = endpointName(to.endpoint);
  els.cfToPath.textContent = displayPath(to.endpoint, to.path);
  const shown = items.slice(0, CONFIRM_ITEM_LIMIT).map((path) => {
    const entry = from.byPath.get(path);
    return el("li", {}, icon(entry?.type === "dir" ? "folder" : "file"), el("span", { text: displayPath(from.endpoint, path), attrs: { title: displayPath(from.endpoint, path) } }));
  });
  if (items.length > CONFIRM_ITEM_LIMIT) shown.push(el("li", { className: "muted", text: `…以及另外 ${items.length - CONFIRM_ITEM_LIMIT} 项` }));
  els.cfItems.replaceChildren(...shown);
  els.cfSkipExisting.checked = false;
  els.cfStart.disabled = false;
  els.cfStart.textContent = state.status.active ? "加入队列" : "开始传输";
  els.confirmDialog.showModal();
  runPreview();
}

async function runPreview() {
  const confirm = state.confirm;
  if (!confirm) return;
  const seq = ++confirm.seq;
  els.cfPreview.className = "preview";
  els.cfPreview.replaceChildren(el("p", { className: "muted", text: "正在对比两侧文件…（可以不等预检直接开始）" }));
  try {
    const { preview } = await api("/api/preview", {
      method: "POST",
      body: { from: confirm.from, to: confirm.to, items: confirm.items, destPath: confirm.destPath, skipExisting: els.cfSkipExisting.checked },
    });
    if (state.confirm !== confirm || seq !== confirm.seq) return;
    renderPreview(preview);
  } catch (error) {
    if (state.confirm !== confirm || seq !== confirm.seq) return;
    els.cfPreview.className = "preview error";
    els.cfPreview.replaceChildren(el("p", { text: `预检失败：${error.message}` }));
  }
}

function renderPreview(preview) {
  const transferCount = preview.newFiles + preview.updatedCount;
  const nodes = [];
  if (!preview.destExists) nodes.push(el("p", { text: "目标目录还不存在，开始后会自动创建。" }));
  if (transferCount === 0) {
    nodes.push(el("p", { text: "没有需要传输的文件：目标端已经是最新的。" }));
  } else {
    nodes.push(el("p", {}, el("strong", { text: `将传输 ${transferCount} 个文件，共 ${formatSize(preview.transferSize)}` })));
    const parts = [];
    if (preview.newFiles) parts.push(`新增 ${preview.newFiles} 个文件`);
    if (preview.newDirs) parts.push(`新建 ${preview.newDirs} 个文件夹`);
    if (preview.updatedCount) parts.push(`覆盖 ${preview.updatedCount} 个已存在的文件`);
    nodes.push(el("p", { className: "muted", text: parts.join("，") }));
  }
  if (els.cfSkipExisting.checked) nodes.push(el("p", { className: "muted", text: "目标端已存在的文件会被跳过。" }));
  if (preview.updatedCount) {
    nodes.push(el("p", { text: "以下文件会被覆盖：" }));
    const list = el("ul", {}, ...preview.updated.map((path) => el("li", { text: path })));
    if (preview.updatedTruncated) list.append(el("li", { className: "muted", text: `…共 ${preview.updatedCount} 个` }));
    nodes.push(list);
  }
  els.cfPreview.className = `preview${preview.updatedCount ? " warn" : ""}`;
  els.cfPreview.replaceChildren(...nodes);
}

async function startTransfer() {
  const confirm = state.confirm;
  if (!confirm) return;
  els.cfStart.disabled = true;
  try {
    const wasBusy = Boolean(state.status.active);
    await api("/api/transfer", {
      method: "POST",
      body: { from: confirm.from, to: confirm.to, items: confirm.items, destPath: confirm.destPath, skipExisting: els.cfSkipExisting.checked },
    });
    state.confirm = null;
    els.confirmDialog.close();
    const pane = state.panes[confirm.fromSide];
    if (pane.endpoint === confirm.from) {
      pane.selected.clear();
      syncSelection(pane);
    }
    toast(wasBusy ? "已加入队列，会在当前任务结束后开始。" : "已开始传输。", "ok", 3500);
    pollStatus();
  } catch (error) {
    toast(error.message, "error");
    els.cfStart.disabled = false;
  }
}

// ---- task status ---------------------------------------------------------------

function statusLabel(status) {
  return STATUS_LABELS[status] || status;
}

function taskTitle(task) {
  return `${taskRoute(task)} · ${itemsSummary(task.items)} → ${displayPath(task.to, task.destPath)}`;
}

function gridItem(label, value, wide = false) {
  return el("div", { className: wide ? "wide" : "" }, el("dt", { text: label }), el("dd", { text: value || "-", attrs: { title: value || "" } }));
}

function renderStatus() {
  const { active, queued, last } = state.status;
  const task = active || last;
  const status = task ? task.status : "idle";
  const progress = task?.progress;

  // One-line summary under the title.
  let line = "空闲";
  let tone = "";
  if (active) {
    line = `正在传输：${taskRoute(active)} · ${progress ? `${progress.percent}%` : "准备中"}`;
    tone = "running";
  } else if (queued.length) {
    line = `${queued.length} 个任务排队中`;
    tone = "running";
  } else if (last) {
    line = `上次任务：${taskRoute(last)} · ${statusLabel(last.status)} · ${relativeTime(last.endedAt || last.createdAt)}`;
    tone = last.status === "failed" ? "bad" : "";
  }
  if (active && queued.length) line += ` · 另有 ${queued.length} 个排队`;
  els.taskLine.textContent = line;
  els.taskLine.dataset.tone = tone;
  els.taskLine.title = task ? taskTitle(task) : "";
  document.title = active && progress ? `${progress.percent}% · ${state.config?.title || "Transfer"}` : state.config?.title || "Transfer";

  els.statusBadge.textContent = statusLabel(status);
  els.statusBadge.dataset.status = status;
  els.statusTitle.textContent = task ? taskTitle(task) : "还没有传输任务";
  els.statusTitle.title = els.statusTitle.textContent;

  let percent = 0;
  if (status === "completed") percent = 100;
  else if (progress) percent = progress.percent;
  els.progressBar.style.width = `${percent}%`;
  els.progress.dataset.status = status;
  els.progress.setAttribute("aria-valuenow", String(percent));

  const items = [];
  if (active) {
    items.push(gridItem("进度", progress ? `${progress.percent}%` : "准备中"));
    items.push(gridItem("已传输", progress ? formatSize(progress.bytes) : "-"));
    items.push(gridItem("速度", progress?.speed));
    items.push(gridItem("剩余时间", progress?.eta));
    items.push(gridItem("当前文件", active.currentFile || (progress ? "" : "正在扫描文件列表…"), true));
  } else if (last) {
    const stats = last.stats || {};
    items.push(gridItem("文件", stats.files !== undefined ? `传输 ${stats.transferredFiles ?? 0} / 共 ${stats.files}` : "-"));
    items.push(gridItem("数据量", stats.transferredSize !== undefined ? formatSize(stats.transferredSize) : "-"));
    items.push(gridItem("耗时", last.startedAt ? formatDuration(last.startedAt, last.endedAt) : "-"));
    items.push(gridItem("结束于", formatDate(parseIso(last.endedAt))));
    items.push(gridItem("目标目录", displayPath(last.to, last.destPath), true));
  }
  els.statusGrid.replaceChildren(...items);
  els.statusGrid.hidden = !items.length;

  const error = task && !["completed", "running", "queued"].includes(task.status) ? task.error : "";
  els.statusError.hidden = !error;
  els.statusError.textContent = error;

  els.stopTask.hidden = !active;
  els.stopTask.disabled = false;
  els.queueLine.hidden = !queued.length;
  els.queueLine.textContent = `${queued.length} 个任务排队中`;
}

function schedulePoll() {
  clearTimeout(state.pollTimer);
  if (document.hidden) return;
  const busy = state.status.active || state.status.queued.length;
  state.pollTimer = setTimeout(pollStatus, busy ? POLL_ACTIVE_MS : POLL_IDLE_MS);
}

async function pollStatus() {
  clearTimeout(state.pollTimer);
  try {
    const data = await api("/api/status");
    const previous = state.status;
    state.status = { active: data.active, queued: data.queued || [], last: data.last };
    renderStatus();
    if (state.statusLoaded && previous.active && previous.active.id !== data.active?.id) {
      onTaskFinished(previous.active);
    }
    state.statusLoaded = true;
    if (els.logDetails.open) refreshLog();
  } catch (error) {
    els.taskLine.textContent = `无法获取任务状态：${error.message}`;
    els.taskLine.dataset.tone = "bad";
  }
  schedulePoll();
}

function onTaskFinished(task) {
  const finished = state.status.last?.id === task.id ? state.status.last : task;
  if (finished.status === "completed") toast(`传输完成：${taskRoute(finished)}`, "ok", 4000);
  else if (finished.status === "failed") toast(`传输失败：${finished.error || taskRoute(finished)}`, "error", 8000);
  for (const side of SIDES) {
    const pane = state.panes[side];
    if (pane.endpoint === finished.to || pane.endpoint === finished.from) loadPane(side, { keepSelection: true });
  }
}

async function stopActiveTask() {
  const active = state.status.active;
  if (!active) return;
  els.stopTask.disabled = true;
  try {
    const data = await api(`/api/tasks/${encodeURIComponent(active.id)}/cancel`, { method: "POST" });
    toast(data.message, "info", 3000);
  } catch (error) {
    toast(error.message, "error");
  }
  pollStatus();
}

async function refreshLog() {
  const task = state.status.active || state.status.last;
  if (!task) {
    els.logBox.textContent = "还没有日志。";
    return;
  }
  if (state.log.taskId !== task.id) {
    state.log = { taskId: task.id, offset: null, busy: false, complete: false };
    els.logBox.textContent = "";
  }
  if (state.log.busy || state.log.complete) return;
  state.log.busy = true;
  try {
    const query = state.log.offset === null ? "" : `?offset=${state.log.offset}`;
    const data = await api(`/api/tasks/${encodeURIComponent(task.id)}/log${query}`);
    if (state.log.taskId !== task.id) return;
    const box = els.logBox;
    const atBottom = box.scrollHeight - box.scrollTop - box.clientHeight < 32;
    if (state.log.offset === null && data.truncated) box.textContent = "…（日志较长，只显示末尾部分）\n";
    if (data.data) box.append(data.data);
    if (box.textContent.length > LOG_DOM_LIMIT) box.textContent = box.textContent.slice(-LOG_DOM_LIMIT);
    state.log.offset = data.offset;
    state.log.complete = !ACTIVE_STATUSES.has(task.status) && data.offset >= data.size;
    if (atBottom) box.scrollTop = box.scrollHeight;
  } catch (error) {
    els.logBox.textContent = error.message;
  } finally {
    state.log.busy = false;
  }
}

// ---- history ------------------------------------------------------------------

async function openHistory() {
  els.historyList.replaceChildren(el("li", { className: "muted", text: "加载中…" }));
  if (!els.historyDialog.open) els.historyDialog.showModal();
  try {
    const { tasks } = await api("/api/tasks?limit=200");
    state.history.tasks = tasks;
    renderHistoryList();
    const selected = tasks.find((task) => task.id === state.history.selected) || tasks[0];
    if (selected) showHistoryDetail(selected.id);
    else els.historyDetail.replaceChildren(el("p", { className: "muted", text: "还没有任何传输任务。" }));
  } catch (error) {
    els.historyList.replaceChildren(el("li", { className: "muted", text: error.message }));
  }
}

function renderHistoryList() {
  const items = state.history.tasks.map((task) => {
    const stats = task.stats || {};
    const meta = [itemsSummary(task.items), relativeTime(task.endedAt || task.startedAt || task.createdAt)];
    if (stats.transferredSize !== undefined) meta.push(formatSize(stats.transferredSize));
    const button = el("button", { className: "history-item", attrs: { type: "button", "aria-current": task.id === state.history.selected ? "true" : null } },
      el("span", { className: "line" },
        el("span", { className: "badge", text: statusLabel(task.status), attrs: { "data-status": task.status } }),
        el("span", { className: "title", text: taskRoute(task) })),
      el("span", { className: "meta", text: meta.filter(Boolean).join(" · ") }));
    button.addEventListener("click", () => showHistoryDetail(task.id));
    return el("li", {}, button);
  });
  els.historyList.replaceChildren(...items);
}

async function showHistoryDetail(taskId) {
  const task = state.history.tasks.find((item) => item.id === taskId);
  if (!task) return;
  state.history.selected = taskId;
  const seq = ++state.history.seq;
  renderHistoryList();
  const stats = task.stats || {};
  const rows = [
    ["状态", statusLabel(task.status)],
    ["从", `${endpointName(task.from)}：${task.items.map((item) => displayPath(task.from, item)).join("\n")}`],
    ["到", `${endpointName(task.to)}：${displayPath(task.to, task.destPath)}`],
    ["创建时间", formatDate(parseIso(task.createdAt))],
    ["耗时", task.startedAt ? formatDuration(task.startedAt, task.endedAt || null) : "-"],
  ];
  if (stats.files !== undefined) rows.push(["文件", `传输 ${stats.transferredFiles ?? 0} / 共 ${stats.files}`]);
  if (stats.transferredSize !== undefined) rows.push(["数据量", `${formatSize(stats.transferredSize)}（总大小 ${formatSize(stats.totalSize || 0)}）`]);
  if (task.skipExisting) rows.push(["选项", "跳过已存在的文件"]);
  if (task.error) rows.push(["错误", task.error]);
  rows.push(["任务 ID", task.id]);
  const grid = el("dl", { className: "detail-grid" });
  for (const [label, value] of rows) grid.append(el("dt", { text: label }), el("dd", { text: value }));
  const log = el("pre", { text: "正在加载日志…", attrs: { tabindex: "0" } });
  const nodes = [el("h3", { text: taskRoute(task) }), grid];
  if (ACTIVE_STATUSES.has(task.status)) {
    const cancel = el("button", { className: "danger", text: task.status === "queued" ? "取消排队" : "停止任务", attrs: { type: "button" } });
    cancel.addEventListener("click", async () => {
      cancel.disabled = true;
      try {
        const data = await api(`/api/tasks/${encodeURIComponent(task.id)}/cancel`, { method: "POST" });
        toast(data.message, "info", 3000);
        pollStatus();
        openHistory();
      } catch (error) {
        toast(error.message, "error");
        cancel.disabled = false;
      }
    });
    nodes.push(el("div", {}, cancel));
  }
  nodes.push(log);
  els.historyDetail.replaceChildren(...nodes);
  try {
    const data = await api(`/api/tasks/${encodeURIComponent(task.id)}/log`);
    if (seq !== state.history.seq) return;
    log.textContent = (data.truncated ? "…（日志较长，只显示末尾部分）\n" : "") + (data.data || "（没有日志：任务还没开始，或日志已按保留期限清理）");
    log.scrollTop = log.scrollHeight;
  } catch (error) {
    if (seq === state.history.seq) log.textContent = error.message;
  }
}

// ---- connection health -----------------------------------------------------------

function markHealth(endpointId, ok, error = "") {
  const previous = state.health[endpointId] || {};
  state.health[endpointId] = { ...previous, ok, error: ok ? "" : error || previous.error };
  for (const side of SIDES) renderDot(state.panes[side]);
}

function renderDot(pane) {
  if (!pane) return;
  const health = state.health[pane.endpoint];
  const dot = pane.els.dot;
  let label = "连接状态未知";
  dot.dataset.state = "unknown";
  if (health) {
    dot.dataset.state = health.ok ? "ok" : "bad";
    label = health.ok
      ? `已连接${health.latencyMs !== undefined ? `（${health.latencyMs} ms）` : ""}`
      : `连接异常：${health.error}`;
  }
  dot.setAttribute("aria-label", label);
  dot.title = label;
}

async function pollHealth(force = false) {
  clearTimeout(state.healthTimer);
  try {
    const { endpoints } = await api(`/api/health${force ? "?force=1" : ""}`);
    state.health = { ...state.health, ...endpoints };
    for (const side of SIDES) renderDot(state.panes[side]);
  } catch (error) {
    // The status line already reports when the service itself is unreachable.
  }
  if (!document.hidden) state.healthTimer = setTimeout(pollHealth, HEALTH_INTERVAL_MS);
}

// ---- theme and preferences ----------------------------------------------------------

function setTheme(theme, persist = true) {
  const light = theme === "light";
  document.documentElement.dataset.theme = light ? "light" : "dark";
  const label = light ? "切换到暗色主题" : "切换到亮色主题";
  els.themeToggle.setAttribute("aria-label", label);
  els.themeToggle.title = label;
  if (persist) storageSet("transfer-web-theme", light ? "light" : "dark");
}

function followSystemTheme(event) {
  const saved = storageGet("transfer-web-theme");
  if (saved === "light" || saved === "dark") return;
  setTheme(event.matches ? "light" : "dark", false);
}

// ---- startup ---------------------------------------------------------------------------

function wireGlobalEvents() {
  setTheme(document.documentElement.dataset.theme === "light" ? "light" : "dark", false);
  const systemTheme = window.matchMedia("(prefers-color-scheme: light)");
  systemTheme.addEventListener?.("change", followSystemTheme);
  els.themeToggle.addEventListener("click", () => setTheme(document.documentElement.dataset.theme === "light" ? "dark" : "light"));

  state.showHidden = storageGet("transfer-web-show-hidden") === "1";
  els.showHidden.checked = state.showHidden;
  els.showHidden.addEventListener("change", () => {
    state.showHidden = els.showHidden.checked;
    storageSet("transfer-web-show-hidden", state.showHidden ? "1" : "0");
    for (const side of SIDES) loadPane(side);
  });

  els.refreshAll.addEventListener("click", () => {
    for (const side of SIDES) loadPane(side, { keepSelection: true });
    pollStatus();
    pollHealth(true);
  });
  els.openHistory.addEventListener("click", openHistory);
  els.copyRight.addEventListener("click", () => openConfirm("left"));
  els.copyLeft.addEventListener("click", () => openConfirm("right"));
  els.swapSides.addEventListener("click", swapSides);
  els.stopTask.addEventListener("click", stopActiveTask);
  els.cfStart.addEventListener("click", startTransfer);
  els.cfSkipExisting.addEventListener("change", runPreview);
  els.confirmDialog.addEventListener("close", () => {
    state.confirm = null;
  });

  els.logDetails.open = storageGet("transfer-web-log-open") === "1";
  els.logDetails.addEventListener("toggle", () => {
    storageSet("transfer-web-log-open", els.logDetails.open ? "1" : "0");
    if (els.logDetails.open) {
      state.log.complete = false;
      refreshLog();
    }
  });

  for (const button of document.querySelectorAll("[data-close]")) {
    button.addEventListener("click", () => button.closest("dialog").close());
  }
  for (const dialog of [els.confirmDialog, els.historyDialog]) {
    // Clicking the dimmed backdrop closes the dialog.
    dialog.addEventListener("click", (event) => {
      if (event.target === dialog) dialog.close();
    });
  }

  document.addEventListener("visibilitychange", () => {
    if (document.hidden) {
      clearTimeout(state.pollTimer);
      clearTimeout(state.healthTimer);
    } else if (state.config) {
      pollStatus();
      pollHealth();
    }
  });
  window.addEventListener("hashchange", () => {
    if (!state.config) return;
    const hash = readHash();
    for (const side of SIDES) {
      const pane = state.panes[side];
      const wanted = hash[side];
      const wantedPath = hash[`${side}Path`];
      const otherWanted = hash[OTHER_SIDE[side]];
      if (wanted === otherWanted) continue;
      if (wanted && state.endpoints.has(wanted) && (wanted !== pane.endpoint || wantedPath !== pane.path)) {
        pane.endpoint = wanted;
        navigate(side, wantedPath);
      }
    }
  });
}

async function start() {
  let config;
  try {
    config = await api("/api/config");
  } catch (error) {
    els.taskLine.textContent = `无法加载配置：${error.message}（5 秒后重试）`;
    els.taskLine.dataset.tone = "bad";
    setTimeout(start, 5000);
    return;
  }
  state.config = config;
  state.endpoints = new Map(config.endpoints.map((item) => [item.id, item]));
  els.appTitle.textContent = config.title;
  document.title = config.title;
  if (config.user) {
    els.userBadge.hidden = false;
    els.userBadge.lastElementChild.textContent = config.user;
    els.userBadge.title = `Tailscale 账号：${config.user}`;
  }

  state.panes.left = createPane("left", $("paneLeft"));
  state.panes.right = createPane("right", $("paneRight"));
  const hash = readHash();
  let left = state.endpoints.has(hash.left) ? hash.left : config.layout.left;
  let right = state.endpoints.has(hash.right) ? hash.right : config.layout.right;
  if (left === right) [left, right] = [config.layout.left, config.layout.right];
  state.panes.left.endpoint = left;
  state.panes.right.endpoint = right;
  state.panes.left.path = left === hash.left ? hash.leftPath : "";
  state.panes.right.path = right === hash.right ? hash.rightPath : "";
  saveHash();
  for (const side of SIDES) {
    renderPane(state.panes[side]);
    loadPane(side);
  }
  updateTransferButtons();
  pollStatus();
  pollHealth();
}

wireGlobalEvents();
start();
