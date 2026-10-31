const MAX_FILE_SIZE = 20 * 1024 * 1024;

const dropzone = document.getElementById("dropzone");
const fileInput = document.getElementById("fileInput");
const uploadState = document.getElementById("uploadState");
const uploadJobs = document.getElementById("uploadJobs");
const docList = document.getElementById("docList");
const docCount = document.getElementById("docCount");
const statusDot = document.getElementById("statusDot");
const statusText = document.getElementById("statusText");
const modelLine = document.getElementById("modelLine");
const messages = document.getElementById("messages");
const composer = document.getElementById("composer");
const question = document.getElementById("question");
const sendBtn = document.getElementById("sendBtn");
const clearBtn = document.getElementById("clearBtn");
const toasts = document.getElementById("toasts");
const sessionList = document.getElementById("sessionList");
const newChatSideBtn = document.getElementById("newChatSideBtn");
const tabButtons = [...document.querySelectorAll(".tab")];
const panelDocs = document.getElementById("panel-docs");
const panelHistory = document.getElementById("panel-history");

const userAvatar = document.getElementById("userAvatar");
const userName = document.getElementById("userName");
const userDept = document.getElementById("userDept");
const adminBtn = document.getElementById("adminBtn");
const logoutBtn = document.getElementById("logoutBtn");
const uploadDept = document.getElementById("uploadDept");
const deptOptions = document.getElementById("deptOptions");
const scopeBadge = document.getElementById("scopeBadge");

const userModal = document.getElementById("userModal");
const closeUserModal = document.getElementById("closeUserModal");
const userList = document.getElementById("userList");
const createUserForm = document.getElementById("createUserForm");
const newUsername = document.getElementById("newUsername");
const newPassword = document.getElementById("newPassword");
const newDepartment = document.getElementById("newDepartment");
const newRole = document.getElementById("newRole");
const loadAuditBtn = document.getElementById("loadAuditBtn");
const auditList = document.getElementById("auditList");

const passwordBtn = document.getElementById("passwordBtn");
const passwordModal = document.getElementById("passwordModal");
const closePasswordModal = document.getElementById("closePasswordModal");
const passwordForm = document.getElementById("passwordForm");
const oldPassword = document.getElementById("oldPassword");
const newPasswordInput = document.getElementById("newPasswordInput");

const welcomeHTML = messages.innerHTML;
const state = { busy: false, sessionId: null, sessions: [], user: null };

/* ---------- 工具函数 ---------- */
function escapeHtml(value) {
  return String(value ?? "")
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&#39;");
}

function escapeAttr(value) {
  return escapeHtml(value).replace(/\n/g, " ");
}

async function api(url, options = {}) {
  const res = await fetch(url, options);
  const type = res.headers.get("content-type") || "";
  const data = type.includes("application/json") ? await res.json() : null;
  if (res.status === 401) {
    if (!window.location.pathname.startsWith("/login")) {
      window.location.href = "/login";
    }
  }
  if (!res.ok) {
    const error = new Error((data && data.error) || `请求失败（HTTP ${res.status}）`);
    error.data = data;
    throw error;
  }
  return data;
}

function toast(message, type = "") {
  const el = document.createElement("div");
  el.className = `toast ${type}`;
  el.textContent = message;
  el.style.transition = "opacity .2s, transform .2s";
  toasts.appendChild(el);
  setTimeout(() => {
    el.style.opacity = "0";
    el.style.transform = "translateY(4px)";
    setTimeout(() => el.remove(), 220);
  }, 3800);
}

function formatSize(bytes) {
  if (!bytes && bytes !== 0) return "";
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  return `${(bytes / 1024 / 1024).toFixed(1)} MB`;
}

function scrollBottom() {
  requestAnimationFrame(() => {
    messages.scrollTop = messages.scrollHeight;
  });
}

function autoGrow() {
  question.style.height = "auto";
  question.style.height = `${Math.min(question.scrollHeight, 160)}px`;
}

/* ---------- 状态与资料列表 ---------- */
async function loadHealth() {
  try {
    const info = await api("/api/health");
    statusDot.className = `status-dot ${info.ok ? "ok" : "bad"}`;
    if (info.ok) {
      statusText.textContent = info.collectionExists
        ? "Milvus 已连接 · 知识库就绪"
        : "Milvus 已连接 · 暂无资料";
    } else {
      statusText.textContent = "Milvus 未连接";
    }
    const llm = info.llmConfigured ? info.llmModel : "未配置大模型";
    modelLine.textContent = `${llm} · ${info.embeddingModel || "本地向量模型"}`;
    modelLine.title = `${info.milvusUri || ""} / ${info.collection || ""}`;
  } catch (err) {
    statusDot.className = "status-dot bad";
    statusText.textContent = "无法连接后端服务";
    modelLine.textContent = "";
  }
}

async function loadMe() {
  const data = await api("/api/me");
  state.user = data.user;
  renderUser();
}

function renderUser() {
  const user = state.user;
  if (!user) return;
  userAvatar.textContent = (user.username || "?").slice(0, 1).toUpperCase();
  userName.textContent = user.username;
  userDept.textContent = `${user.department} · ${user.role === "admin" ? "管理员" : "成员"}`;
  adminBtn.hidden = user.role !== "admin";
  scopeBadge.textContent =
    user.role === "admin" ? "全部部门" : `公共 + ${user.department}`;
  uploadDept.value = user.role === "admin" ? "公共" : user.department;
  uploadDept.readOnly = user.role !== "admin";
  uploadDept.title =
    user.role === "admin" ? "可选择或输入部门" : "普通用户固定为本部门";
}

async function loadDepartments() {
  try {
    const data = await api("/api/departments");
    const departments = data.departments || [];
    deptOptions.innerHTML = departments
      .map((department) => `<option value="${escapeAttr(department)}"></option>`)
      .join("");
    if (state.user && state.user.role === "admin" && !uploadDept.value) {
      uploadDept.value = departments.includes("公共")
        ? "公共"
        : departments[0] || "公共";
    }
  } catch (err) {
    // 部门列表加载失败不阻塞主流程
  }
}

function extLabel(ext) {
  return (ext || "?").toUpperCase().slice(0, 4);
}

function renderDocs(documents) {
  docCount.textContent = documents.length;
  if (!documents.length) {
    docList.innerHTML = '<li class="doc-empty">还没有资料，上传后会立即入库</li>';
    return;
  }
  docList.innerHTML = documents
    .map(
      (doc) => `
      <li class="doc-item">
        <div class="doc-icon">${extLabel(doc.ext)}</div>
        <div class="doc-info">
          <div class="doc-name" title="${escapeAttr(doc.source)}">${escapeHtml(doc.source)}</div>
          <div class="doc-meta">${doc.chunks} 个切片${doc.size != null ? ` · ${formatSize(doc.size)}` : ""} · <span class="doc-dept">${escapeHtml(doc.department || "公共")}</span>${doc.uploaded_by_name ? ` · ${escapeHtml(doc.uploaded_by_name)}` : ""}</div>
        </div>
        ${
          doc.canDelete
            ? `<button class="doc-delete" type="button" data-id="${escapeAttr(doc.id)}" data-source="${escapeAttr(doc.source)}" title="删除">✕</button>`
            : ""
        }
      </li>`
    )
    .join("");
}

async function loadDocs() {
  try {
    const data = await api("/api/documents");
    renderDocs(data.documents || []);
  } catch (err) {
    toast(`读取资料列表失败：${err.message}`, "error");
  }
}

/* ---------- 历史会话 ---------- */
function formatTime(value) {
  if (!value) return "";
  return value.slice(5, 16);
}

function renderSessions(sessions) {
  if (!sessions.length) {
    sessionList.innerHTML =
      '<li class="doc-empty">还没有历史会话<br>发送第一条问题后自动保存</li>';
    return;
  }
  sessionList.innerHTML = sessions
    .map(
      (session) => `
      <li class="session-item ${session.id === state.sessionId ? "active" : ""}" data-id="${escapeAttr(session.id)}">
        <div class="session-main">
          <div class="session-title" title="${escapeAttr(session.title)}">${escapeHtml(session.title)}</div>
          <div class="session-meta">${formatTime(session.updated_at)} · ${session.message_count} 条消息</div>
        </div>
        <button class="session-delete" type="button" data-id="${escapeAttr(session.id)}" title="删除会话">✕</button>
      </li>`
    )
    .join("");
}

async function loadSessions() {
  try {
    const data = await api("/api/sessions");
    state.sessions = data.sessions || [];
    renderSessions(state.sessions);
  } catch (err) {
    toast(`读取历史会话失败：${err.message}`, "error");
  }
}

function switchTab(name) {
  tabButtons.forEach((button) => {
    button.classList.toggle("active", button.dataset.tab === name);
  });
  panelDocs.hidden = name !== "docs";
  panelHistory.hidden = name !== "history";
}

function startNewChat() {
  if (state.busy) return;
  state.sessionId = null;
  messages.classList.remove("no-animate");
  messages.innerHTML = welcomeHTML;
  renderSessions(state.sessions);
  question.value = "";
  autoGrow();
  question.focus();
  scrollBottom();
}

async function openSession(sessionId) {
  if (state.busy) return;
  try {
    const data = await api(`/api/sessions/${encodeURIComponent(sessionId)}`);
    state.sessionId = sessionId;
    messages.classList.add("no-animate");
    messages.innerHTML = "";
    for (const message of data.messages || []) {
      if (message.role === "user") {
        addMessage("user", message.content || "");
      } else {
        addAnswer({
          answer: message.content || "",
          sources: message.sources || [],
          prompt: message.prompt || "",
          mode: message.mode,
          notice: message.notice,
        });
      }
    }
    if (!data.messages || !data.messages.length) messages.innerHTML = welcomeHTML;
    renderSessions(state.sessions);
    scrollBottom();
  } catch (err) {
    toast(`加载会话失败：${err.message}`, "error");
  }
}

/* ---------- 上传（异步任务 + 进度） ---------- */
function createUploadJobRow(filename) {
  const element = document.createElement("div");
  element.className = "upload-job";
  element.innerHTML = `
    <div class="upload-job-head">
      <span class="upload-job-name" title="${escapeAttr(filename)}">${escapeHtml(filename)}</span>
      <span class="upload-job-pct">0%</span>
    </div>
    <div class="upload-bar"><div class="upload-bar-fill"></div></div>
    <div class="upload-job-stage"><span class="stage">排队中</span><span class="status">等待</span></div>`;

  const pct = element.querySelector(".upload-job-pct");
  const bar = element.querySelector(".upload-bar-fill");
  const stage = element.querySelector(".stage");
  const status = element.querySelector(".status");

  return {
    element,
    update(job) {
      const value = Math.max(0, Math.min(100, Number(job.progress) || 0));
      pct.textContent = `${value}%`;
      bar.style.width = `${value}%`;
      stage.textContent = job.stage || "";
      status.textContent =
        job.status === "done"
          ? "完成"
          : job.status === "error"
            ? "失败"
            : job.status === "processing"
              ? "处理中"
              : "等待";
      element.classList.toggle("done", job.status === "done");
      element.classList.toggle("error", job.status === "error");
    },
    fail(message) {
      this.update({ progress: 100, stage: message, status: "error" });
    },
  };
}

async function pollUploadJob(jobId, row) {
  const deadline = Date.now() + 15 * 60 * 1000;
  while (Date.now() < deadline) {
    const data = await api(`/api/upload-jobs/${encodeURIComponent(jobId)}`);
    const job = data.job;
    row.update(job);
    if (job.status === "done") return job;
    if (job.status === "error") throw new Error(job.error || "处理失败");
    await new Promise((resolve) => setTimeout(resolve, 600));
  }
  throw new Error("处理超时");
}

async function uploadFiles(files) {
  if (!files || !files.length) return;
  const department = (uploadDept.value || "公共").trim();

  for (const file of files) {
    if (file.size > MAX_FILE_SIZE) {
      toast(`「${file.name}」超过 20MB 限制`, "error");
      continue;
    }

    const row = createUploadJobRow(file.name);
    uploadJobs.prepend(row.element);

    const form = new FormData();
    form.append("files", file);
    form.append("department", department);
    uploadState.textContent = `已提交任务：${file.name}`;
    dropzone.classList.add("dragover");

    try {
      const data = await api("/api/upload-jobs", { method: "POST", body: form });
      const job = (data.jobs || [])[0];
      if (!job || job.status === "error") {
        throw new Error((job && job.error) || "任务创建失败");
      }
      row.update(job);
      const finished = await pollUploadJob(job.id, row);
      toast(
        `已入库「${finished.filename}」，共 ${finished.chunks} 个切片`,
        "ok"
      );
      setTimeout(() => row.element.remove(), 15000);
    } catch (err) {
      row.fail(err.message);
      toast(`「${file.name}」上传失败：${err.message}`, "error");
    } finally {
      dropzone.classList.remove("dragover");
    }
  }

  uploadState.textContent = "";
  await loadDocs();
  await loadDepartments();
  await loadHealth();
}

/* ---------- 消息渲染 ---------- */
function inlineFormat(text) {
  return text
    .replace(/\*\*(.+?)\*\*/g, "<strong>$1</strong>")
    .replace(/`([^`]+)`/g, "<code>$1</code>");
}

function formatAnswer(text) {
  const lines = escapeHtml(text).split(/\r?\n/);
  const html = [];
  let inList = false;
  for (const raw of lines) {
    const line = raw.trim();
    if (/^([-*]|\d+\.)\s+/.test(line)) {
      if (!inList) {
        html.push("<ul>");
        inList = true;
      }
      html.push(`<li>${inlineFormat(line.replace(/^([-*]|\d+\.)\s+/, ""))}</li>`);
      continue;
    }
    if (inList) {
      html.push("</ul>");
      inList = false;
    }
    if (line) html.push(`<p>${inlineFormat(line)}</p>`);
  }
  if (inList) html.push("</ul>");
  return html.join("");
}

function addMessage(role, text) {
  const row = document.createElement("div");
  row.className = `message-row ${role}`;

  const avatar = document.createElement("div");
  avatar.className = `avatar ${role === "user" ? "me" : "ai"}`;
  avatar.textContent = role === "user" ? "我" : "AI";

  const bubble = document.createElement("div");
  bubble.className = "bubble";
  if (text) bubble.textContent = text;

  row.append(avatar, bubble);
  messages.appendChild(row);
  scrollBottom();
  return row;
}

function addTyping() {
  const row = document.createElement("div");
  row.className = "message-row assistant";
  row.innerHTML = `
    <div class="avatar ai">AI</div>
    <div class="bubble">
      <div class="typing"><span></span><span></span><span></span></div>
    </div>`;
  messages.appendChild(row);
  scrollBottom();
  return row;
}

function renderSources(sources, retrievalMode) {
  const modeLabel =
    retrievalMode === "hybrid+rerank"
      ? " · 混合检索 + 重排"
      : retrievalMode === "hybrid"
        ? " · 混合检索"
        : "";
  const details = document.createElement("details");
  details.className = "sources";
  details.innerHTML =
    `<summary>引用资料 ${sources.length} 条${modeLabel}</summary>` +
    sources
      .map((item, index) => {
        const preview = (item.text || "").replace(/\s+/g, " ").slice(0, 220);
        const matchLabel =
          item.matchedBy === "both"
            ? "混合召回"
            : item.matchedBy === "bm25"
              ? "关键词"
              : "向量";
        const scoreParts = [];
        if (item.cosine != null) scoreParts.push(`向量相似度 ${item.cosine}`);
        if (item.rerankScore != null) scoreParts.push(`重排分 ${item.rerankScore}`);
        if (item.rrfScore != null) scoreParts.push(`RRF ${item.rrfScore}`);
        const scoreTitle = scoreParts.join(" / ");
        const displayScore = item.cosine != null ? item.cosine : item.score;
        return `
        <div class="source-item">
          <div class="source-head">
            <span class="source-index">${index + 1}</span>
            <span class="source-name" title="${escapeAttr(item.source)}">${escapeHtml(item.source)}</span>
            ${item.department ? `<span class="source-dept">${escapeHtml(item.department)}</span>` : ""}
            <span class="source-match">${matchLabel}</span>
            <span class="score" title="${escapeAttr(scoreTitle)}">${Number(displayScore || 0).toFixed(4)}</span>
          </div>
          <p class="source-text">${escapeHtml(preview)}${preview.length >= 220 ? "…" : ""}</p>
        </div>`;
      })
      .join("");
  return details;
}

function addAnswer(data) {
  const row = addMessage("assistant", "");
  const bubble = row.querySelector(".bubble");

  if (data.notice) {
    const cls = data.mode === "general" ? "note info" : "note";
    bubble.innerHTML = `<div class="${cls}">${escapeHtml(data.notice)}</div>`;
  }

  if (data.answer) {
    bubble.insertAdjacentHTML("beforeend", formatAnswer(data.answer));
  } else if (!data.notice) {
    bubble.innerHTML =
      '<div class="note">未配置大模型凭证，以下是将要发送给模型的内容：</div>' +
      `<pre class="prompt">${escapeHtml(data.prompt || "")}</pre>`;
  }

  if (data.sources && data.sources.length) {
    bubble.appendChild(renderSources(data.sources, data.retrievalMode));
  }

  if (data.prompt && (data.answer || data.notice)) {
    const details = document.createElement("details");
    details.className = "sources";
    details.innerHTML = `<summary>查看本次 Prompt</summary><pre class="prompt">${escapeHtml(data.prompt)}</pre>`;
    bubble.appendChild(details);
  }

  scrollBottom();
}

/* ---------- 提问 ---------- */
async function sendQuestion(text) {
  if (state.busy) return;
  const value = (text ?? question.value).trim();
  if (!value) return;

  state.busy = true;
  sendBtn.disabled = true;

  const welcome = messages.querySelector(".welcome");
  if (welcome) welcome.remove();
  messages.classList.remove("no-animate");

  addMessage("user", value);
  question.value = "";
  autoGrow();
  scrollBottom();

  const typing = addTyping();
  try {
    const data = await api("/api/chat", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ question: value, session_id: state.sessionId }),
    });
    typing.remove();
    addAnswer(data);
    if (data.session_id) {
      state.sessionId = data.session_id;
      loadSessions();
    }
  } catch (err) {
    typing.remove();
    if (err.data && err.data.session_id) {
      state.sessionId = err.data.session_id;
      loadSessions();
    }
    addMessage("assistant", `抱歉，出错了：${err.message}`);
  } finally {
    state.busy = false;
    sendBtn.disabled = false;
    question.focus();
  }
}

/* ---------- 用户管理（管理员） ---------- */
function openUserModal() {
  userModal.hidden = false;
  loadUsers();
  loadAudit();
}

function closeUserModalDialog() {
  userModal.hidden = true;
}

async function loadUsers() {
  try {
    const data = await api("/api/users");
    renderUsers(data.users || []);
  } catch (err) {
    toast(`读取用户失败：${err.message}`, "error");
  }
}

async function loadAudit() {
  try {
    const data = await api("/api/audit?limit=50");
    renderAudit(data.logs || []);
  } catch (err) {
    toast(`读取审计日志失败：${err.message}`, "error");
  }
}

const AUDIT_LABELS = {
  login: "登录",
  login_failed: "登录失败",
  login_locked: "登录锁定",
  logout: "退出",
  upload: "上传",
  upload_done: "入库完成",
  upload_failed: "上传失败",
  delete_document: "删除资料",
  create_user: "创建用户",
  delete_user: "删除用户",
  reset_password: "重置密码",
  change_password: "修改密码",
  chat: "问答",
};

function renderAudit(logs) {
  if (!logs.length) {
    auditList.innerHTML = '<div class="doc-empty">暂无审计记录</div>';
    return;
  }
  auditList.innerHTML = logs
    .map(
      (log) => `
      <div class="audit-row">
        <div class="audit-main">
          <span class="audit-time">${escapeHtml(log.created_at || "")}</span>
          <span class="audit-user">${escapeHtml(log.username || "-")}</span>
          <span class="audit-action">${escapeHtml(AUDIT_LABELS[log.action] || log.action || "")}</span>
        </div>
        <div class="audit-detail">${escapeHtml(log.detail || "")}${log.ip ? ` · ${escapeHtml(log.ip)}` : ""}</div>
      </div>`
    )
    .join("");
}

function renderUsers(users) {
  userList.innerHTML = users
    .map(
      (user) => `
      <div class="user-row">
        <div class="user-row-info">
          <div class="user-row-name">${escapeHtml(user.username)}</div>
          <div class="user-row-meta">${escapeHtml(user.department)}${user.created_at ? ` · ${escapeHtml(user.created_at)}` : ""}</div>
        </div>
        <span class="user-row-role ${user.role === "admin" ? "admin" : ""}">${user.role === "admin" ? "管理员" : "普通用户"}</span>
        ${
          state.user && user.id === state.user.id
            ? ""
            : `<button class="user-row-reset" type="button" data-id="${user.id}" data-name="${escapeAttr(user.username)}">重置密码</button>
               <button class="user-row-delete" type="button" data-id="${user.id}" data-name="${escapeAttr(user.username)}">删除</button>`
        }
      </div>`
    )
    .join("");
}

/* ---------- 事件绑定 ---------- */
dropzone.addEventListener("click", (event) => {
  if (event.target === fileInput) return; // 防止 input.click() 冒泡后重复触发
  fileInput.click();
});
fileInput.addEventListener("click", (event) => event.stopPropagation());
dropzone.addEventListener("keydown", (event) => {
  if (event.key === "Enter" || event.key === " ") {
    event.preventDefault();
    fileInput.click();
  }
});
fileInput.addEventListener("change", () => {
  uploadFiles([...fileInput.files]);
  fileInput.value = "";
});

["dragenter", "dragover"].forEach((type) =>
  dropzone.addEventListener(type, (event) => {
    event.preventDefault();
    event.stopPropagation();
    dropzone.classList.add("dragover");
  })
);
["dragleave", "drop"].forEach((type) =>
  dropzone.addEventListener(type, (event) => {
    event.preventDefault();
    event.stopPropagation();
    dropzone.classList.remove("dragover");
    if (type === "drop") uploadFiles([...event.dataTransfer.files]);
  })
);
["dragover", "drop"].forEach((type) =>
  document.addEventListener(type, (event) => event.preventDefault())
);

docList.addEventListener("click", async (event) => {
  const button = event.target.closest(".doc-delete");
  if (!button) return;
  const docId = button.dataset.id;
  const name = button.dataset.source;
  if (!window.confirm(`确定从知识库删除「${name}」吗？\n对应的切片会一并删除。`)) return;
  button.disabled = true;
  try {
    await api(`/api/documents/${encodeURIComponent(docId)}`, { method: "DELETE" });
    toast(`已删除「${name}」`, "ok");
    await loadDocs();
    await loadHealth();
    await loadDepartments();
  } catch (err) {
    toast(`删除失败：${err.message}`, "error");
    button.disabled = false;
  }
});

messages.addEventListener("click", (event) => {
  const chip = event.target.closest(".chip");
  if (chip) sendQuestion(chip.dataset.q);
});

composer.addEventListener("submit", (event) => {
  event.preventDefault();
  sendQuestion();
});

question.addEventListener("keydown", (event) => {
  if (event.key === "Enter" && !event.shiftKey) {
    event.preventDefault();
    sendQuestion();
  }
});
question.addEventListener("input", autoGrow);

clearBtn.addEventListener("click", startNewChat);
newChatSideBtn.addEventListener("click", startNewChat);

adminBtn.addEventListener("click", openUserModal);
closeUserModal.addEventListener("click", closeUserModalDialog);
loadAuditBtn.addEventListener("click", loadAudit);
userModal.querySelector("[data-close-modal]").addEventListener("click", closeUserModalDialog);
document.addEventListener("keydown", (event) => {
  if (event.key !== "Escape") return;
  if (!userModal.hidden) closeUserModalDialog();
  if (!passwordModal.hidden) passwordModal.hidden = true;
});

passwordBtn.addEventListener("click", () => {
  passwordModal.hidden = false;
  oldPassword.focus();
});
closePasswordModal.addEventListener("click", () => {
  passwordModal.hidden = true;
});
passwordModal
  .querySelector("[data-close-password]")
  .addEventListener("click", () => {
    passwordModal.hidden = true;
  });

passwordForm.addEventListener("submit", async (event) => {
  event.preventDefault();
  try {
    await api("/api/me/password", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        old_password: oldPassword.value,
        new_password: newPasswordInput.value,
      }),
    });
    passwordForm.reset();
    passwordModal.hidden = true;
    toast("密码已修改，下次登录请使用新密码", "ok");
  } catch (err) {
    toast(`修改失败：${err.message}`, "error");
  }
});

createUserForm.addEventListener("submit", async (event) => {
  event.preventDefault();
  try {
    const data = await api("/api/users", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        username: newUsername.value.trim(),
        password: newPassword.value,
        department: newDepartment.value.trim(),
        role: newRole.value,
      }),
    });
    toast(`已创建用户 ${data.user.username}`, "ok");
    createUserForm.reset();
    await loadUsers();
    await loadDepartments();
  } catch (err) {
    toast(`创建失败：${err.message}`, "error");
  }
});

userList.addEventListener("click", async (event) => {
  const resetButton = event.target.closest(".user-row-reset");
  if (resetButton) {
    const password = window.prompt(
      `为「${resetButton.dataset.name}」设置新密码（至少 6 位）：`
    );
    if (!password) return;
    if (password.length < 6) {
      toast("密码至少 6 位", "error");
      return;
    }
    try {
      await api(`/api/users/${resetButton.dataset.id}/password`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ new_password: password }),
      });
      toast("密码已重置", "ok");
    } catch (err) {
      toast(`重置失败：${err.message}`, "error");
    }
    return;
  }

  const button = event.target.closest(".user-row-delete");
  if (!button) return;
  if (!window.confirm(`确定删除用户「${button.dataset.name}」吗？`)) return;
  try {
    await api(`/api/users/${button.dataset.id}`, { method: "DELETE" });
    toast("用户已删除", "ok");
    await loadUsers();
    await loadDepartments();
  } catch (err) {
    toast(`删除失败：${err.message}`, "error");
  }
});

logoutBtn.addEventListener("click", async () => {
  try {
    await api("/api/logout", { method: "POST" });
  } catch (err) {
    // 忽略退出接口错误，直接回登录页
  }
  window.location.href = "/login";
});

tabButtons.forEach((button) => {
  button.addEventListener("click", () => switchTab(button.dataset.tab));
});

sessionList.addEventListener("click", async (event) => {
  const deleteBtn = event.target.closest(".session-delete");
  if (deleteBtn) {
    event.stopPropagation();
    const sessionId = deleteBtn.dataset.id;
    if (!window.confirm("确定删除这个历史会话吗？")) return;
    try {
      await api(`/api/sessions/${encodeURIComponent(sessionId)}`, { method: "DELETE" });
      if (state.sessionId === sessionId) startNewChat();
      await loadSessions();
      toast("会话已删除", "ok");
    } catch (err) {
      toast(`删除失败：${err.message}`, "error");
    }
    return;
  }
  const item = event.target.closest(".session-item");
  if (item) openSession(item.dataset.id);
});

/* ---------- 初始化 ---------- */
async function init() {
  autoGrow();
  try {
    await loadMe();
  } catch (err) {
    toast(`加载用户信息失败：${err.message}`, "error");
    return;
  }
  await loadHealth();
  await Promise.all([loadDocs(), loadDepartments(), loadSessions()]);

  // 支持通过 ?q=问题 直接提问，便于分享与测试
  const initialQuestion = new URLSearchParams(window.location.search).get("q");
  if (initialQuestion) {
    sendQuestion(initialQuestion);
    return;
  }

  // 默认打开最近一次会话，并切到历史会话页签，体现历史持久化
  if (state.sessions.length) {
    await openSession(state.sessions[0].id);
    switchTab("history");
  } else {
    question.focus();
  }
}

init();
setInterval(() => {
  if (!document.hidden) loadHealth();
}, 30000);
