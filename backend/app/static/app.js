const ALL_AREAS = ["control", "networking", "ml", "systems", "multimedia", "robotics"];

const state = {
  areas: new Set(),
  years: new Set(),
  workshops: "all",
  deadline: "upcoming",
  predicted: "all",
  diverged: "all",
  q: "",
  sortKey: "submission_deadline",
  sortDir: "asc",
};

const addState = { areas: new Set() };
let lastData = [];

const $ = (sel) => document.querySelector(sel);

function fmt(d) {
  if (!d) return "—";
  const dt = new Date(d);
  if (isNaN(dt.getTime())) return "—";
  return dt.toLocaleDateString(undefined, { year: "numeric", month: "short", day: "numeric" });
}

function daysUntil(d) {
  if (!d) return null;
  const dt = new Date(d);
  if (isNaN(dt.getTime())) return null;
  return Math.round((dt.getTime() - Date.now()) / 86400000);
}

function deadlineClass(d, predicted) {
  const base = predicted ? " predicted" : "";
  const n = daysUntil(d);
  if (n === null) return base;
  if (n < 0) return "past" + base;
  if (n <= 14) return "urgent" + base;
  if (n <= 45) return "soon" + base;
  return base;
}

function tierClass(t) {
  if (!t) return "";
  return t === "A*" ? "tier-Astar" : `tier-${t}`;
}

function escapeHtml(s) {
  return String(s ?? "").replace(/[&<>"']/g, (c) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
  })[c]);
}

function buildParams() {
  const p = new URLSearchParams();
  [...state.areas].forEach((a) => p.append("area", a));
  [...state.years].forEach((y) => p.append("year", y));
  p.set("workshops", state.workshops);
  p.set("deadline", state.deadline);
  p.set("predicted", state.predicted);
  p.set("diverged", state.diverged);
  if (state.q) p.set("q", state.q);
  return p;
}

function makeAreaButton(area, isActive, onToggle) {
  const b = document.createElement("button");
  b.type = "button";
  b.className = "area-btn" + (isActive ? " active" : "");
  b.textContent = area;
  b.addEventListener("click", (ev) => {
    ev.preventDefault();
    ev.stopPropagation();
    onToggle(area);
  });
  return b;
}

function renderAreaChips() {
  const host = $("#area-chips");
  host.innerHTML = "";
  for (const a of ALL_AREAS) {
    host.appendChild(makeAreaButton(a, state.areas.has(a), (area) => {
      state.areas.has(area) ? state.areas.delete(area) : state.areas.add(area);
      renderAreaChips();
      load();
    }));
  }
}

function renderAddAreaChips() {
  const host = $("#add-area-chips");
  host.innerHTML = "";
  for (const a of ALL_AREAS) {
    host.appendChild(makeAreaButton(a, addState.areas.has(a), (area) => {
      addState.areas.has(area) ? addState.areas.delete(area) : addState.areas.add(area);
      renderAddAreaChips();
    }));
  }
}

async function loadYearChips() {
  const host = $("#year-chips");
  host.innerHTML = "";
  let years = [];
  try {
    years = await (await fetch("/api/years")).json();
  } catch { /* leave empty */ }
  for (const y of years) {
    host.appendChild(makeAreaButton(String(y), state.years.has(y), (label) => {
      const yr = Number(label);
      state.years.has(yr) ? state.years.delete(yr) : state.years.add(yr);
      loadYearChips();
      load();
    }));
  }
}

// ───────────────────────────── sorting ─────────────────────────────
// Each table column maps to a sort key. Special-case dates and rank ordering.
const SORT_KEYS = {
  venue: (c) => `${c.acronym} ${c.year}`,
  areas: (c) => (c.areas || []).join(","),
  tier: (c) => {
    const order = { "A*": 0, "A": 1, "B": 2, "C": 3 };
    return order[c.tier] ?? 99;
  },
  abstract_deadline: (c) => c.abstract_deadline ? new Date(c.abstract_deadline).getTime() : Infinity,
  submission_deadline: (c) => c.submission_deadline ? new Date(c.submission_deadline).getTime() : Infinity,
  notification_date: (c) => c.notification_date ? new Date(c.notification_date).getTime() : Infinity,
  conference_start: (c) => c.conference_start ? new Date(c.conference_start).getTime() : Infinity,
  page_limit: (c) => c.page_limit ?? -Infinity,
  acceptance_rate: (c) => c.acceptance_rate ?? Infinity,
  source: (c) => c.source || "",
};

function sortRows(rows) {
  const keyFn = SORT_KEYS[state.sortKey] || SORT_KEYS.submission_deadline;
  const dir = state.sortDir === "asc" ? 1 : -1;
  return rows.slice().sort((a, b) => {
    const va = keyFn(a), vb = keyFn(b);
    if (va < vb) return -1 * dir;
    if (va > vb) return  1 * dir;
    return 0;
  });
}

function renderHeaderSort() {
  document.querySelectorAll("th[data-sort]").forEach((th) => {
    const key = th.dataset.sort;
    th.classList.toggle("sort-active", key === state.sortKey);
    let indicator = "";
    if (key === state.sortKey) indicator = state.sortDir === "asc" ? " ▲" : " ▼";
    th.dataset.indicator = indicator;
  });
}

function setupSortableHeaders() {
  document.querySelectorAll("th[data-sort]").forEach((th) => {
    th.addEventListener("click", () => {
      const key = th.dataset.sort;
      if (state.sortKey === key) {
        state.sortDir = state.sortDir === "asc" ? "desc" : "asc";
      } else {
        state.sortKey = key;
        state.sortDir = "asc";
      }
      renderHeaderSort();
      renderRows(lastData);
    });
  });
}

// ───────────────────────────── rendering ─────────────────────────────
function renderRow(c) {
  const tr = document.createElement("tr");

  const venueCell = `
    <td>
      <div>
        <strong>${escapeHtml(c.acronym)}</strong>
        <span class="muted">${escapeHtml(c.year)}</span>
        ${c.is_workshop ? `<span class="workshop-tag">(workshop${c.parent_venue ? " @ " + escapeHtml(c.parent_venue) : ""})</span>` : ""}
        ${c.predicted ? `<span class="predicted-flag">predicted</span>` : ""}
        ${c.diverged ? `<button type="button" class="diverged-flag diverged-btn" data-id="${c.id}" title="Sources disagree on this venue's dates. Click for breakdown.">verify</button>` : ""}
      </div>
      <div class="muted" style="font-size:12px;">
        ${c.cfp_url
          ? `<a href="${escapeHtml(c.cfp_url)}" target="_blank" rel="noreferrer">${escapeHtml(c.name)}</a>`
          : escapeHtml(c.name)}
        ${c.location ? " · " + escapeHtml(c.location) : ""}
      </div>
    </td>`;

  const areasCell = `<td>${(c.areas || []).map((a) =>
    `<span class="chip area-${escapeHtml(a)}">${escapeHtml(a)}</span>`).join("")}</td>`;

  const tierCell = c.tier
    ? `<td><span class="tier ${tierClass(c.tier)}${c.tier_predicted ? " tier-pred" : ""}" title="${c.tier_predicted ? "Predicted from h5-index / acceptance rate" : "Known CORE / community rank"}">${c.tier_predicted ? "~" : ""}${escapeHtml(c.tier)}</span></td>`
    : `<td>—</td>`;

  const conf = c.conference_start
    ? fmt(c.conference_start) + (c.conference_end && c.conference_end !== c.conference_start ? ` – ${fmt(c.conference_end)}` : "")
    : "—";

  const accept = c.acceptance_rate != null ? `${Math.round(c.acceptance_rate * 100)}%` : "—";

  tr.innerHTML = `
    ${venueCell}
    ${areasCell}
    ${tierCell}
    <td class="deadline ${deadlineClass(c.abstract_deadline, c.predicted)}">${fmt(c.abstract_deadline)}</td>
    <td class="deadline ${deadlineClass(c.submission_deadline, c.predicted)}">${fmt(c.submission_deadline)}</td>
    <td class="deadline ${deadlineClass(c.notification_date, c.predicted)}">${fmt(c.notification_date)}</td>
    <td class="deadline${c.predicted ? " predicted" : ""}">${conf}</td>
    <td>${c.page_limit ?? "—"}</td>
    <td>${accept}</td>
    <td class="muted" style="font-size:12px;">
      ${escapeHtml(c.source)}
      ${c.last_verified ? `<div style="font-size:11px;">${fmt(c.last_verified)}</div>` : ""}
    </td>`;
  return tr;
}

function renderRows(data) {
  const tbody = $("#conf-tbody");
  tbody.innerHTML = "";
  const sorted = sortRows(data);
  for (const c of sorted) tbody.appendChild(renderRow(c));
}

async function load() {
  const status = $("#status");
  const table = $("#conf-table");
  status.textContent = "Loading…";
  status.style.display = "";
  table.style.display = "none";
  try {
    const r = await fetch(`/api/conferences?${buildParams()}`);
    const data = await r.json();
    lastData = data;
    $("#subtitle").textContent = `Control · Networking · ML · Systems — ${data.length} venue${data.length === 1 ? "" : "s"}`;
    if (data.length === 0) {
      status.textContent = "No conferences match. Try clearing filters or run the refresh script.";
      return;
    }
    renderRows(data);
    status.style.display = "none";
    table.style.display = "";
  } catch (e) {
    status.textContent = `Error: ${e.message}`;
  }
}

// ───────────────────────────── modals ─────────────────────────────
function setupSubscribeModal() {
  const modal = $("#modal");
  const url = () => `${location.origin}/calendar.ics?${buildParams()}`;
  $("#subscribe-btn").onclick = () => {
    $("#ics-url").textContent = url();
    $("#download-btn").href = url();
    modal.style.display = "";
  };
  $("#close-btn").onclick = () => (modal.style.display = "none");
  modal.onclick = (e) => { if (e.target === modal) modal.style.display = "none"; };
  $("#copy-btn").onclick = () => navigator.clipboard.writeText(url());
}

function setupAddModal() {
  const modal = $("#add-modal");
  const status = $("#add-status");
  $("#add-btn").onclick = () => {
    $("#add-url").value = "";
    addState.areas.clear();
    renderAddAreaChips();
    status.textContent = "";
    modal.style.display = "";
  };
  $("#add-close").onclick = () => (modal.style.display = "none");
  modal.onclick = (e) => { if (e.target === modal) modal.style.display = "none"; };
  $("#add-submit").onclick = async () => {
    const url = $("#add-url").value.trim();
    if (!url) { status.textContent = "Please paste a URL."; return; }
    status.textContent = "Fetching page and extracting (two-pass)…";
    try {
      const r = await fetch("/api/venues", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ url, area_hints: [...addState.areas] }),
      });
      const body = await r.json();
      if (!r.ok) {
        status.textContent = "Error: " + (body.detail || r.statusText);
        return;
      }
      status.textContent = `Added: ${body.acronym} ${body.year} (source: ${body.source})`;
      await load();
      setTimeout(() => { modal.style.display = "none"; }, 1500);
    } catch (e) {
      status.textContent = "Error: " + e.message;
    }
  };
}

function setupSourcesModal() {
  const modal = $("#sources-modal");
  $("#sources-close").onclick = () => (modal.style.display = "none");
  modal.onclick = (e) => { if (e.target === modal) modal.style.display = "none"; };
  document.addEventListener("click", async (e) => {
    const btn = e.target.closest && e.target.closest(".diverged-btn");
    if (!btn) return;
    e.preventDefault();
    const id = btn.dataset.id;
    $("#sources-title").textContent = "Per-source dates";
    $("#sources-summary").textContent = "Loading…";
    $("#sources-table-wrap").innerHTML = "";
    modal.style.display = "";
    try {
      const r = await fetch(`/api/conferences/${id}/sources`);
      const body = await r.json();
      $("#sources-title").textContent = `${body.acronym} ${body.year} — per-source dates`;
      $("#sources-summary").textContent =
        `${body.sources.length} sources recorded. Canonical row was written by "${body.canonical_source}". ` +
        `Aggregators disagree on at least one date — review and reconcile manually if needed.`;
      const rows = body.sources.map((s) => `
        <tr>
          <td><strong>${escapeHtml(s.source)}</strong></td>
          <td class="deadline">${fmt(s.abstract_deadline)}</td>
          <td class="deadline">${fmt(s.submission_deadline)}</td>
          <td class="deadline">${fmt(s.notification_date)}</td>
          <td class="deadline">${s.conference_start ? fmt(s.conference_start) : "—"}</td>
          <td>${s.link ? `<a href="${escapeHtml(s.link)}" target="_blank" rel="noreferrer">link</a>` : "—"}</td>
        </tr>`).join("");
      $("#sources-table-wrap").innerHTML = `
        <table>
          <thead><tr>
            <th>Source</th><th>Abstract</th><th>Submission</th>
            <th>Notification</th><th>Conference start</th><th>Link</th>
          </tr></thead>
          <tbody>${rows}</tbody>
        </table>`;
    } catch (err) {
      $("#sources-summary").textContent = "Error: " + err.message;
    }
  });
}

// ───────────────────────────── filters ─────────────────────────────
function setupControls() {
  $("#q").addEventListener("input", (e) => { state.q = e.target.value; load(); });
  $("#workshops").addEventListener("change", (e) => { state.workshops = e.target.value; load(); });
  $("#deadline").addEventListener("change", (e) => { state.deadline = e.target.value; load(); });
  $("#predicted").addEventListener("change", (e) => { state.predicted = e.target.value; load(); });
  $("#diverged").addEventListener("change", (e) => { state.diverged = e.target.value; load(); });
}

renderAreaChips();
renderAddAreaChips();
loadYearChips();
setupControls();
setupSubscribeModal();
setupAddModal();
setupSourcesModal();
setupSortableHeaders();
renderHeaderSort();
load();
