// PyCharm Projects Hub - frontend

const state = {
  hub: null,
  discovered: [],
  projects: [],
  selected: null,
  pollTimer: null,
  pollIntervalMs: 3500,
  logStream: null,
  logBuffer: "",
};

const $ = (id) => document.getElementById(id);
const el = (tag, attrs = {}, children = []) => {
  const node = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (k === "class") node.className = v;
    else if (k === "onclick") node.addEventListener("click", v);
    else if (k.startsWith("on") && typeof v === "function") node.addEventListener(k.slice(2).toLowerCase(), v);
    else if (k === "html") node.innerHTML = v;
    else if (v != null) node.setAttribute(k, v);
  }
  for (const c of children) {
    if (c == null) continue;
    node.appendChild(typeof c === "string" ? document.createTextNode(c) : c);
  }
  return node;
};

// ---------- API ----------

async function api(path, opts = {}) {
  try {
    const res = await fetch(path, opts);
    const ct = res.headers.get("content-type") || "";
    const body = ct.includes("application/json") ? await res.json() : await res.text();
    if (!res.ok) {
      const err = (body && body.error) || res.statusText || `HTTP ${res.status}`;
      throw new Error(err);
    }
    return body;
  } catch (err) {
    toast(err.message || String(err), "error");
    throw err;
  }
}

// ---------- Toast ----------

function toast(msg, type = "info", timeout = 3500) {
  const t = el("div", { class: `toast ${type}` }, [msg]);
  $("toastStack").appendChild(t);
  setTimeout(() => {
    t.style.opacity = "0";
    t.style.transition = "opacity 0.3s";
    setTimeout(() => t.remove(), 300);
  }, timeout);
}

// ---------- Theme ----------

function applyTheme(theme) {
  document.body.dataset.theme = theme;
  const btn = $("themeBtn");
  btn.querySelector(".theme-icon").textContent = theme === "dark" ? "🌙" : "☀️";
  btn.querySelector(".theme-label").textContent = theme === "dark" ? "Dark" : "Light";
  localStorage.setItem("hub-theme", theme);
}

function initTheme() {
  const saved = localStorage.getItem("hub-theme");
  applyTheme(saved || "dark");
  $("themeBtn").addEventListener("click", () => {
    applyTheme(document.body.dataset.theme === "dark" ? "light" : "dark");
  });
}

// ---------- Render ----------

function statusOf(p) { return p.status || {}; }

function renderSidebar() {
  const root = $("projectList");
  root.innerHTML = "";
  if (!state.projects.length) {
    root.appendChild(el("div", { class: "empty" }, ["No projects yet. Click the rescan button to detect them."]));
    return;
  }
  const filter = ($("searchInput").value || "").toLowerCase().trim();
  const filtered = state.projects.filter((p) => {
    if (!filter) return true;
    return p.name.toLowerCase().includes(filter) ||
           (p.description || "").toLowerCase().includes(filter) ||
           (p.tags || []).some((t) => t.toLowerCase().includes(filter));
  });
  for (const p of filtered) {
    const st = statusOf(p);
    const running = st.running;
    const card = el("div", {
      class: "project-card" + (state.selected === p.name ? " active" : ""),
      onclick: () => selectProject(p.name),
    }, [
      el("div", { class: "pc-icon", style: `background:${p.color || "linear-gradient(135deg,#10b981,#059669)"}` }, [p.icon || p.name.slice(0, 2).toUpperCase()]),
      el("div", { class: "pc-body" }, [
        el("div", { class: "pc-name" }, [p.name]),
        el("div", { class: "pc-meta" }, [
          el("span", { class: "pc-status" + (running ? " running" : (st.last_error ? " error" : "")) }),
          running ? "Running" : (st.last_error ? "Crashed" : "Stopped"),
          p.port ? ` · :${p.port}` : "",
          p.type ? ` · ${p.type}` : "",
        ]),
      ]),
    ]);
    root.appendChild(card);
  }
}

function renderMain() {
  const p = state.projects.find((x) => x.name === state.selected);
  if (!p) {
    $("emptyState").classList.remove("hidden");
    $("projectView").classList.add("hidden");
    return;
  }
  $("emptyState").classList.add("hidden");
  $("projectView").classList.remove("hidden");

  $("projectAvatar").textContent = p.icon || p.name.slice(0, 2).toUpperCase();
  $("projectAvatar").style.background = p.color || "linear-gradient(135deg,#10b981,#059669)";
  $("projectName").textContent = p.name;
  $("projectType").textContent = p.type || "web";
  $("projectDesc").textContent = p.description || "(no description)";
  $("projectPath").textContent = p.path || p.cwd || "—";
  $("projectCommand").textContent = p.command || "—";
  $("projectUrl").textContent = p.url || "—";
  $("projectUrl").href = p.url || "#";
  $("projectLog").textContent = "logs/" + (p.name.replace(/[^A-Za-z0-9_.-]/g, "_")) + ".log";

  const tags = $("projectTags");
  tags.innerHTML = "";
  for (const t of p.tags || []) tags.appendChild(el("span", { class: "tag" }, [t]));

  applyStatusToButtons(p);
  updatePreview(p);
}

function applyStatusToButtons(p) {
  const st = statusOf(p);
  const running = st.running;
  $("startBtn").disabled = running;
  $("stopBtn").disabled = !running;
  $("openBtn").disabled = !p.url;

  const dot = $("statusDot");
  const text = $("statusText");
  dot.classList.remove("running", "starting", "error");
  if (running) {
    dot.classList.add("running");
    text.textContent = "running";
  } else if (st.last_error) {
    dot.classList.add("error");
    text.textContent = "crashed";
  } else {
    text.textContent = "stopped";
  }
  $("portText").textContent = p.port ? `· port ${p.port}` : "";
  $("pidText").textContent = st.pid ? `· pid ${st.pid}` : "";
}

function updatePreview(p) {
  const frame = $("previewFrame");
  const placeholder = $("previewPlaceholder");
  const st = statusOf(p);
  const running = st.running;
  if (running && p.url) {
    if (frame.dataset.url !== p.url) {
      frame.src = p.url;
      frame.dataset.url = p.url;
    }
    frame.style.display = "block";
    placeholder.style.display = "none";
    $("previewInfo").textContent = `Running at ${p.url}`;
  } else {
    frame.src = "about:blank";
    frame.dataset.url = "";
    frame.style.display = "none";
    placeholder.style.display = "grid";
    $("previewInfo").textContent = st.last_error
      ? `Last error: ${st.last_error}`
      : "Project is not running yet.";
  }
}

// ---------- Tabs ----------

function initTabs() {
  const tabs = document.querySelectorAll(".tab");
  tabs.forEach((t) => {
    t.addEventListener("click", () => {
      tabs.forEach((x) => x.classList.remove("active"));
      t.classList.add("active");
      document.querySelectorAll(".tab-panel").forEach((p) => p.classList.add("hidden"));
      const panel = document.querySelector(`.tab-panel[data-panel="${t.dataset.tab}"]`);
      if (panel) panel.classList.remove("hidden");
      if (t.dataset.tab === "log" && state.selected) startLogStream();
      if (t.dataset.tab !== "log") stopLogStream();
    });
  });
}

// ---------- Log streaming ----------

function appendLogLine(text) {
  const view = $("logView");
  const lines = text.split(/\r?\n/);
  for (const line of lines) {
    if (!line) continue;
    let cls = "";
    const lower = line.toLowerCase();
    if (line.startsWith("[hub]")) cls = "hub";
    else if (lower.includes("error") || lower.includes("failed") || lower.includes("exception")) cls = "err";
    else if (lower.includes("warn")) cls = "warn";
    else if (lower.includes("info")) cls = "info";
    const span = el("span", { class: "log-line" + (cls ? " " + cls : "") }, [line]);
    view.appendChild(span);
    view.appendChild(document.createTextNode("\n"));
  }
  if ($("autoscrollChk").checked) view.scrollTop = view.scrollHeight;
  state.logBuffer += text;
  if (state.logBuffer.length > 200000) {
    state.logBuffer = state.logBuffer.slice(-100000);
    $("logView").innerHTML = "";
    appendLogLine(state.logBuffer);
  }
}

function stopLogStream() {
  if (state.logStream) {
    try { state.logStream.close(); } catch {}
    state.logStream = null;
  }
}

function startLogStream() {
  stopLogStream();
  if (!state.selected) return;
  $("logView").innerHTML = "";
  state.logBuffer = "";
  $("logInfo").textContent = "Connecting…";
  const es = new EventSource(`/api/projects/${encodeURIComponent(state.selected)}/log/stream`);
  state.logStream = es;
  es.addEventListener("hello", (ev) => {
    $("logInfo").textContent = `Streaming logs for ${state.selected}`;
  });
  es.onmessage = (ev) => {
    appendLogLine(ev.data);
  };
  es.onerror = () => {
    $("logInfo").textContent = "Stream disconnected. Reconnecting in 3s…";
    setTimeout(() => {
      if (state.selected && document.querySelector('.tab[data-tab="log"]').classList.contains("active")) {
        startLogStream();
      }
    }, 3000);
  };
}

// ---------- Selection / actions ----------

async function selectProject(name) {
  state.selected = name;
  renderSidebar();
  renderMain();
  stopLogStream();
  const tab = document.querySelector('.tab.active');
  if (tab && tab.dataset.tab === "log") startLogStream();
}

async function startProject(install = false) {
  if (!state.selected) return;
  const btn = install ? $("installBtn") : $("startBtn");
  btn.disabled = true;
  try {
    const q = install ? "?install=1" : "";
    const res = await api(`/api/projects/${encodeURIComponent(state.selected)}/start${q}`, { method: "POST" });
    if (res.ok) {
      toast(`${state.selected} started`, "info");
      await refresh();
    } else {
      toast(res.error || "Failed to start", "error");
    }
  } finally {
    setTimeout(() => { if (state.selected) applyStatusToButtons(state.projects.find((p) => p.name === state.selected)); }, 200);
  }
}

async function stopProject() {
  if (!state.selected) return;
  $("stopBtn").disabled = true;
  try {
    const res = await api(`/api/projects/${encodeURIComponent(state.selected)}/stop`, { method: "POST" });
    if (res.ok) {
      toast(`${state.selected} stopped`, "info");
      await refresh();
    } else {
      toast(res.error || "Failed to stop", "error");
    }
  } finally {
    setTimeout(() => { if (state.selected) applyStatusToButtons(state.projects.find((p) => p.name === state.selected)); }, 200);
  }
}

async function openProject() {
  if (!state.selected) return;
  const p = state.projects.find((x) => x.name === state.selected);
  if (!p) return;
  if (!statusOf(p).running) {
    const res = await api(`/api/projects/${encodeURIComponent(state.selected)}/start`, { method: "POST" });
    if (!res.ok) {
      toast(res.error || "Failed to start", "error");
      return;
    }
    await refresh();
  }
  const updated = state.projects.find((x) => x.name === state.selected);
  if (updated && updated.url) window.open(updated.url, "_blank", "noopener");
}

function openFullscreen() {
  const p = state.projects.find((x) => x.name === state.selected);
  if (!p || !p.url) {
    toast("Project has no URL configured", "warn");
    return;
  }
  $("fsTitle").textContent = `${p.name} — ${p.url}`;
  $("fsFrame").src = p.url;
  $("fsOpenExt").href = p.url;
  $("fullscreenModal").classList.remove("hidden");
}

function closeFullscreen() {
  $("fullscreenModal").classList.add("hidden");
  $("fsFrame").src = "about:blank";
}

function reloadIframe() {
  const f = $("previewFrame");
  if (f.dataset.url) f.src = f.dataset.url;
}

// ---------- Polling ----------

async function refresh() {
  try {
    const data = await api("/api/projects");
    state.hub = data.hub;
    state.discovered = data.discovered || [];
    state.projects = data.projects || [];
    $("scanRootLabel").textContent = data.hub && data.hub.scan_root ? data.hub.scan_root : "";
    $("hubInfo").textContent = `Hub :${data.hub ? data.hub.port : "?"}`;
    if (!state.selected && state.projects.length) state.selected = state.projects[0].name;
    renderSidebar();
    if (state.selected) renderMain();
  } catch (err) {
    console.error("refresh failed", err);
  }
}

function startPolling() {
  stopPolling();
  state.pollTimer = setInterval(refresh, state.pollIntervalMs);
}
function stopPolling() {
  if (state.pollTimer) clearInterval(state.pollTimer);
  state.pollTimer = null;
}

// ---------- Init ----------

function init() {
  initTheme();
  initTabs();
  $("searchInput").addEventListener("input", renderSidebar);
  $("refreshBtn").addEventListener("click", () => refresh().then(() => toast("Refreshed", "info", 1500)));
  $("scanBtn").addEventListener("click", async () => {
    await api("/api/scan", { method: "POST" });
    toast("Rescanning…", "info", 1500);
    setTimeout(refresh, 1500);
  });
  $("startBtn").addEventListener("click", () => startProject(false));
  $("installBtn").addEventListener("click", () => startProject(true));
  $("stopBtn").addEventListener("click", stopProject);
  $("openBtn").addEventListener("click", openProject);
  $("reloadIframeBtn").addEventListener("click", reloadIframe);
  $("fullscreenBtn").addEventListener("click", openFullscreen);
  $("fsCloseBtn").addEventListener("click", closeFullscreen);
  $("fsReloadBtn").addEventListener("click", () => { $("fsFrame").src = $("fsFrame").src; });
  $("clearLogBtn").addEventListener("click", () => { $("logView").innerHTML = ""; state.logBuffer = ""; });
  $("openLogFileBtn").addEventListener("click", () => toast($("projectLog").textContent, "info", 5000));
  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape") closeFullscreen();
    if (e.key === "/" && document.activeElement.tagName !== "INPUT") {
      e.preventDefault();
      $("searchInput").focus();
    }
  });

  refresh().then(startPolling);
  setInterval(refresh, state.pollIntervalMs);
}

document.addEventListener("DOMContentLoaded", init);
