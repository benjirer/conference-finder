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
  anchor: null,           // {lat, lng} when user has picked a point on the world map
  compareIds: new Set(),  // conference row IDs ticked for PC comparison
  compareMode: false,     // when true, the checkbox column is visible
};

function haversineKm(a, b) {
  const R = 6371;
  const toR = (d) => (d * Math.PI) / 180;
  const dLat = toR(b.lat - a.lat);
  const dLng = toR(b.lng - a.lng);
  const s = Math.sin(dLat / 2) ** 2
          + Math.cos(toR(a.lat)) * Math.cos(toR(b.lat)) * Math.sin(dLng / 2) ** 2;
  return 2 * R * Math.asin(Math.sqrt(s));
}

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
  location: (c) => (c.location || "").toLowerCase(),
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

function distanceFor(c) {
  if (!state.anchor || c.latitude == null || c.longitude == null) return null;
  return haversineKm(state.anchor, { lat: c.latitude, lng: c.longitude });
}

function sortRows(rows) {
  // When the world-map anchor is set, distance sort overrides the column sort.
  if (state.anchor) {
    return rows.slice().sort((a, b) => {
      const da = distanceFor(a), db_ = distanceFor(b);
      if (da == null && db_ == null) return 0;
      if (da == null) return 1;
      if (db_ == null) return -1;
      return da - db_;
    });
  }
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

// ───────────────────────────── PC compare selection ─────────────────────────────
function updateCompareUI() {
  document.body.classList.toggle("compare-mode", state.compareMode);
  const btn = $("#compare-btn");
  const label = $("#compare-btn-label");
  const exit = $("#compare-exit");
  const n = state.compareIds.size;

  if (!state.compareMode) {
    label.textContent = "Compare PCs";
    btn.disabled = false;
    btn.classList.remove("compare-active");
    btn.title = "Enter PC-comparison mode";
    exit.style.display = "none";
  } else {
    btn.classList.add("compare-active");
    exit.style.display = "";
    if (n < 2) {
      label.textContent = n === 0 ? "Pick venues to compare" : "Pick one more venue";
      btn.disabled = true;
      btn.title = "Tick at least 2 venues with PC data";
    } else {
      label.textContent = `Compare (${n}) →`;
      btn.disabled = false;
      btn.title = "Open comparison";
    }
  }

  document.querySelectorAll("tbody tr[data-id]").forEach((tr) => {
    const id = Number(tr.dataset.id);
    tr.classList.toggle("compare-selected", state.compareIds.has(id));
    const cb = tr.querySelector("input.compare-checkbox");
    if (cb) cb.checked = state.compareIds.has(id);
  });
}

// ───────────────────────────── rendering ─────────────────────────────
function renderRow(c) {
  const tr = document.createElement("tr");
  tr.dataset.id = c.id;
  if (state.compareIds.has(c.id)) tr.classList.add("compare-selected");

  const hasPC = (c.pc_members_count || 0) > 0;
  const compareCell = `<td class="compare-cell compare-col">${
    hasPC
      ? `<input type="checkbox" class="compare-checkbox" data-id="${c.id}"
              ${state.compareIds.has(c.id) ? "checked" : ""}
              title="${c.pc_members_count} PC members — tick to compare"
              aria-label="Add ${escapeHtml(c.acronym)} ${c.year} to PC comparison" />`
      : `<span class="no-pc" title="No PC data extracted yet for this venue. Run python -m app.enrich_pc --acronym ${escapeHtml(c.acronym)} to populate.">—</span>`
  }</td>`;

  const roundLabel = (c.round && c.round > 1) || (c.rounds_total && c.rounds_total > 1)
    ? `<span class="round-flag" title="${c.rounds_total ? `Round ${c.round} of ${c.rounds_total}` : `Round ${c.round}`}">R${c.round}${c.rounds_total ? `/${c.rounds_total}` : ""}</span>`
    : "";
  const distKm = distanceFor(c);
  const distTag = distKm != null
    ? `<span class="distance-tag">${distKm < 100 ? "<100" : Math.round(distKm).toLocaleString()} km</span>`
    : "";
  const venueCell = `
    <td class="venue-cell">
      <div>
        <strong>${escapeHtml(c.acronym)}</strong>
        <span class="muted">${escapeHtml(c.year)}</span>
        ${roundLabel}
        ${c.is_workshop ? `<span class="workshop-tag">(workshop${c.parent_venue ? " @ " + escapeHtml(c.parent_venue) : ""})</span>` : ""}
        ${c.predicted ? `<span class="predicted-flag">predicted</span>` : ""}
        ${c.diverged ? `<button type="button" class="diverged-flag diverged-btn" data-id="${c.id}" title="Sources disagree on this venue's dates. Click for breakdown.">verify</button>` : ""}
        ${distTag}
      </div>
      <div class="muted" style="font-size:12px;">
        ${c.cfp_url
          ? `<a href="${escapeHtml(c.cfp_url)}" target="_blank" rel="noreferrer">${escapeHtml(c.name)}</a>`
          : escapeHtml(c.name)}
      </div>
    </td>`;

  const locationCell = `<td class="muted location-cell" style="font-size:12px;" title="${c.location ? escapeHtml(c.location) : ""}">${c.location ? escapeHtml(c.location) : "—"}</td>`;

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
    ${compareCell}
    ${venueCell}
    ${locationCell}
    ${areasCell}
    ${tierCell}
    <td class="deadline ${deadlineClass(c.abstract_deadline, c.predicted)}">${fmt(c.abstract_deadline)}</td>
    <td class="deadline ${deadlineClass(c.submission_deadline, c.predicted)}">${fmt(c.submission_deadline)}</td>
    <td class="deadline ${deadlineClass(c.notification_date, c.predicted)}">${fmt(c.notification_date)}</td>
    <td class="deadline${c.predicted ? " predicted" : ""}">${conf}</td>
    <td>${c.page_limit ?? "—"}</td>
    <td>${accept}</td>
    <td class="muted source-cell" style="font-size:12px;" title="${escapeHtml(c.source)}${c.last_verified ? " — verified " + fmt(c.last_verified) : ""}">
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
  updateCompareUI();
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

function setupCompareModal() {
  const modal = $("#pc-modal");
  $("#pc-close").onclick = () => (modal.style.display = "none");
  modal.onclick = (e) => { if (e.target === modal) modal.style.display = "none"; };
  $("#pc-clear").onclick = () => {
    state.compareIds.clear();
    updateCompareUI();
    modal.style.display = "none";
  };

  // Delegate checkbox toggles from any rendered row.
  document.addEventListener("change", (e) => {
    const cb = e.target.closest && e.target.closest("input.compare-checkbox");
    if (!cb) return;
    const id = Number(cb.dataset.id);
    if (cb.checked) state.compareIds.add(id);
    else state.compareIds.delete(id);
    updateCompareUI();
  });

  // Header button: 1st click enters compare mode; subsequent click with ≥2
  // selected opens the comparison modal. The X button exits & clears.
  $("#compare-btn").onclick = async () => {
    if (!state.compareMode) {
      state.compareMode = true;
      updateCompareUI();
      return;
    }
    if (state.compareIds.size < 2) return;
    $("#pc-title").textContent = "PC comparison";
    $("#pc-summary").textContent = "Loading…";
    $("#pc-pairwise").innerHTML = "";
    $("#pc-table-wrap").innerHTML = "";
    modal.style.display = "";
    try {
      const r = await fetch("/api/pc/compare", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ conference_ids: [...state.compareIds] }),
      });
      const body = await r.json();
      if (!r.ok) {
        $("#pc-summary").textContent = "Error: " + (body.detail || r.statusText);
        return;
      }
      renderCompareResult(body);
    } catch (err) {
      $("#pc-summary").textContent = "Error: " + err.message;
    }
  };

  $("#compare-exit").onclick = () => {
    state.compareMode = false;
    state.compareIds.clear();
    updateCompareUI();
  };
}

function renderCompareResult(body) {
  const venues = body.venues || [];
  const labels = venues.map((v) =>
    `${v.acronym} ${v.year}${v.round && v.round > 1 ? " R" + v.round : ""}`
  );
  $("#pc-title").textContent = `PC comparison — ${labels.join(" ∩ ")}`;

  const sizes = venues.map((v) => `${v.acronym}: ${v.pc_size}`).join(" · ");
  const missing = venues.filter((v) => v.pc_size === 0);
  let summary = `${sizes}. Intersection size: ${body.intersection_size}.`;
  if (missing.length) {
    summary += `  ⚠ No PC data for: ${missing.map(v=>v.acronym).join(", ")}. Run python -m app.enrich_pc --acronym <X> locally to extract.`;
  }
  $("#pc-summary").textContent = summary;

  // Pairwise overlap cards (useful when N > 2).
  if (venues.length > 2 && body.pairwise) {
    const idToLabel = Object.fromEntries(venues.map((v, i) => [v.id, labels[i]]));
    const cards = body.pairwise.map((p) =>
      `<div class="pc-pair">${escapeHtml(idToLabel[p.a])} ∩ ${escapeHtml(idToLabel[p.b])}: <span class="count">${p.count}</span></div>`
    ).join("");
    $("#pc-pairwise").innerHTML = `<div class="pc-pair-grid">${cards}</div>`;
  }

  if (!body.intersection.length) {
    $("#pc-table-wrap").innerHTML = '<p class="muted">No people appear on all selected PCs.</p>';
    return;
  }

  const idToLabel = Object.fromEntries(venues.map((v, i) => [String(v.id), labels[i]]));
  const venueHeaders = venues.map((v, i) => `<th>${escapeHtml(labels[i])}</th>`).join("");
  const rows = body.intersection.map((m) => {
    const cells = venues.map((v) => {
      const pv = m.per_venue[String(v.id)];
      if (!pv) return "<td>—</td>";
      const role = pv.role && pv.role !== "member" ? ` <span class="muted" style="font-size:11px;">(${escapeHtml(pv.role)})</span>` : "";
      const aff = pv.affiliation ? `<div class="muted" style="font-size:11px;">${escapeHtml(pv.affiliation)}</div>` : "";
      return `<td>${escapeHtml(pv.name)}${role}${aff}</td>`;
    }).join("");
    return `<tr><td><strong>${escapeHtml(m.name)}</strong></td>${cells}</tr>`;
  }).join("");
  $("#pc-table-wrap").innerHTML = `
    <table>
      <thead><tr><th>Person</th>${venueHeaders}</tr></thead>
      <tbody>${rows}</tbody>
    </table>`;
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

function setupMapModal() {
  const modal = $("#map-modal");
  const status = $("#map-status");

  // CartoDB tile layers — Positron for light theme, Dark Matter for dark.
  // Both are free, no API key, fair-use friendly.
  const TILES_LIGHT = "https://{s}.basemaps.cartocdn.com/light_all/{z}/{x}/{y}{r}.png";
  const TILES_DARK  = "https://{s}.basemaps.cartocdn.com/dark_all/{z}/{x}/{y}{r}.png";
  const ATTRIBUTION =
    '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> ' +
    '&copy; <a href="https://carto.com/attributions">CARTO</a>';

  let map = null;
  let tileLayer = null;
  const venueMarkers = L.layerGroup();
  let anchorMarker = null;

  const venuePinIcon = L.divIcon({
    className: "",
    html: '<div class="venue-pin-marker"></div>',
    iconSize: [10, 10],
    iconAnchor: [5, 5],
  });
  const anchorIcon = L.divIcon({
    className: "",
    html: '<div class="anchor-marker"></div>',
    iconSize: [14, 14],
    iconAnchor: [7, 7],
  });

  function currentTileUrl() {
    return document.documentElement.getAttribute("data-theme") === "light"
      ? TILES_LIGHT : TILES_DARK;
  }

  function setTiles() {
    if (!map) return;
    if (tileLayer) map.removeLayer(tileLayer);
    tileLayer = L.tileLayer(currentTileUrl(), {
      attribution: ATTRIBUTION,
      maxZoom: 10, minZoom: 2,
      noWrap: false,
    }).addTo(map);
  }

  function renderVenuePins() {
    venueMarkers.clearLayers();
    const seen = new Set();
    for (const c of lastData) {
      if (c.latitude == null || c.longitude == null) continue;
      // De-duplicate by rounded coordinate so dozens of conferences in the same
      // city don't stack into an unreadable blob.
      const key = `${c.latitude.toFixed(2)},${c.longitude.toFixed(2)}`;
      if (seen.has(key)) continue;
      seen.add(key);
      L.marker([c.latitude, c.longitude], { icon: venuePinIcon })
        .bindTooltip(`${c.acronym} ${c.year} — ${c.location || ""}`, { direction: "top" })
        .addTo(venueMarkers);
    }
  }

  function renderAnchor() {
    if (anchorMarker) { map.removeLayer(anchorMarker); anchorMarker = null; }
    if (!state.anchor) return;
    anchorMarker = L.marker([state.anchor.lat, state.anchor.lng], { icon: anchorIcon })
      .addTo(map);
  }

  function updateStatus() {
    if (state.anchor) {
      const { lat, lng } = state.anchor;
      const ns = lat >= 0 ? "N" : "S";
      const ew = lng >= 0 ? "E" : "W";
      status.textContent = `Sorting by distance from ${Math.abs(lat).toFixed(1)}°${ns}, ${Math.abs(lng).toFixed(1)}°${ew}`;
    } else {
      status.textContent = "No anchor set — table uses normal sort.";
    }
  }

  function ensureMap() {
    if (map) return;
    map = L.map("world-map", {
      center: [25, 10], zoom: 2,
      worldCopyJump: true,
      zoomControl: true,
    });
    setTiles();
    venueMarkers.addTo(map);
    map.on("click", (e) => {
      state.anchor = { lat: e.latlng.lat, lng: e.latlng.lng };
      renderAnchor();
      updateStatus();
      renderRows(lastData);
    });
    // Keep tiles in sync with theme toggles.
    new MutationObserver(setTiles).observe(document.documentElement, {
      attributes: true, attributeFilter: ["data-theme"],
    });
  }

  $("#map-btn").onclick = () => {
    modal.style.display = "";
    // Leaflet needs the container to have real dimensions when init runs, so
    // we init/invalidate AFTER the modal becomes visible.
    requestAnimationFrame(() => {
      ensureMap();
      map.invalidateSize();
      renderVenuePins();
      renderAnchor();
      updateStatus();
    });
  };
  $("#map-close").onclick = () => (modal.style.display = "none");
  modal.onclick = (e) => { if (e.target === modal) modal.style.display = "none"; };

  $("#map-clear").onclick = () => {
    state.anchor = null;
    renderAnchor();
    updateStatus();
    renderRows(lastData);
  };
}

const ICON_SUN = `
  <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"
       stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">
    <circle cx="12" cy="12" r="4"/>
    <path d="M12 2v2M12 20v2M4.93 4.93l1.41 1.41M17.66 17.66l1.41 1.41
             M2 12h2M20 12h2M4.93 19.07l1.41-1.41M17.66 6.34l1.41-1.41"/>
  </svg>`;
const ICON_MOON = `
  <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"
       stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">
    <path d="M21 12.79A9 9 0 1 1 11.21 3 7 7 0 0 0 21 12.79z"/>
  </svg>`;

function setupThemeToggle() {
  const btn = $("#theme-btn");
  const sync = () => {
    const isLight = document.documentElement.getAttribute("data-theme") === "light";
    // In light mode show a moon (click to go dark); in dark mode show a sun (click to go light).
    btn.innerHTML = isLight ? ICON_MOON : ICON_SUN;
    btn.title = `Switch to ${isLight ? "dark" : "light"} mode`;
  };
  btn.addEventListener("click", () => {
    const next = document.documentElement.getAttribute("data-theme") === "light" ? "dark" : "light";
    document.documentElement.setAttribute("data-theme", next);
    try { localStorage.setItem("theme", next); } catch (e) { /* private mode */ }
    sync();
  });
  sync();
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
setupThemeToggle();
setupMapModal();
setupCompareModal();
updateCompareUI();
load();
