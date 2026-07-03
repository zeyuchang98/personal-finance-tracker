let pieChart = null;
let incomePie = null;
let trendChart = null;
let trendData = null;
let categories = [];
let gran = "monthly";   // master granularity: monthly | quarterly | yearly
let period = "";        // selected period key (e.g. 2026-06 / 2026-Q2 / 2026)

const $ = (s) => document.querySelector(s);
const fmt = (n) => "$" + Number(n).toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 });

// Escape text for safe insertion into HTML / attributes.
const esc = (s) => String(s == null ? "" : s)
  .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
  .replace(/"/g, "&quot;");
// Merchant cell: fixed-width, truncated with an ellipsis; full name on hover
// (long-press on mobile) via the title tooltip. Keeps tables from side-scrolling.
const mcell = (name) => `<span class="mtext" title="${esc(name)}">${esc(name)}</span>`;

async function api(url, opts) {
  const r = await fetch(url, opts);
  return r.json();
}

function setStatus(msg) { $("#status").textContent = msg; }

// Match Chart.js text/grid colors to the OS light/dark theme.
if (window.Chart && matchMedia("(prefers-color-scheme: dark)").matches) {
  Chart.defaults.color = "#98a1b1";
  Chart.defaults.borderColor = "rgba(255,255,255,0.08)";
}

// ---------- Plaid Link (initial auth on computer) ----------
$("#connect-btn").onclick = async () => {
  setStatus("Getting Link token...");
  const { link_token } = await api("/api/create_link_token", { method: "POST" });
  const handler = Plaid.create({
    token: link_token,
    onSuccess: async (public_token, metadata) => {
      setStatus("Exchanging token...");
      await api("/api/exchange_public_token", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          public_token,
          institution: metadata.institution ? metadata.institution.name : null,
        }),
      });
      setStatus("Connected. Click Sync.");
    },
    onExit: () => setStatus(""),
  });
  handler.open();
};

// ---------- Sandbox quick connect (no Link popup) ----------
$("#sandbox-btn").onclick = async () => {
  setStatus("Creating sandbox item...");
  const r = await api("/api/sandbox_connect", { method: "POST" });
  if (r.ok) {
    setStatus("Sandbox bank connected. Click Sync.");
  } else {
    setStatus("Error: " + (r.error || "failed"));
  }
};

// ---------- Re-apply de-noise (after logic changes; no re-sync) ----------
$("#reannotate-btn").onclick = async () => {
  setStatus("Re-applying de-noise...");
  const r = await api("/api/reannotate", { method: "POST" });
  setStatus(`De-noise re-applied: ${r.changed} of ${r.rows} rows changed`);
  await refresh();
};

// ---------- Re-classify with Claude (after classifier/guide changes) ----------
// months = null -> all history; a number -> only rows within the last N months
// (faster; pairing/finalize still re-run across everything).
async function runReclassify(months, label) {
  const scope = months ? `the last ${label}` : "all auto-classified merchants";
  if (!confirm(`Re-run Claude classification for ${scope}? (Calls the Claude API; manual corrections are kept.)`)) return;
  setStatus(`Re-classifying ${months ? label : "all history"} with Claude... this can take a minute`);
  const r = await api("/api/reclassify", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(months ? { months } : {}),
  });
  setStatus(`Re-classified: ${r.updated} transactions updated`);
  await refresh();
}
$("#reset-learning-btn").onclick = async () => {
  if (!confirm("Delete all learned rules and trusted-merchant records?\n\nYour guide, categories, transactions, and manual edits are KEPT. Afterwards run Re-classify to re-walk from the guide.")) return;
  setStatus("Clearing learned rules & trust...");
  const r = await api("/api/reset_learning", { method: "POST" });
  setStatus(`Cleared ${r.rules} rules and ${r.trust} trusted merchants — now run Re-classify.`);
  await refresh();
};
// Full re-walk: clear rules + trust + manual pins (except Ignore), then reclassify all.
$("#rewalk-btn").onclick = async () => {
  if (!confirm("Full re-walk from the current guide?\n\nThis clears learned rules + trusted merchants AND un-pins your manual edits (rows you set to Ignore stay hidden), then re-classifies EVERYTHING with Claude. Your guide and categories are kept.\n\nCalls the Claude API; can take a minute.")) return;
  setStatus("Clearing rules, trust & manual pins...");
  const r = await api("/api/reset_learning", {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ manual: true }),
  });
  setStatus(`Cleared ${r.rules} rules, ${r.trust} trusted, un-pinned ${r.manual} manual edits — re-classifying...`);
  const rc = await api("/api/reclassify", {
    method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({}),
  });
  setStatus(`Re-walk complete: ${rc.updated} transactions re-classified from the guide.`);
  await refresh();
};
$("#reclassify-3m-btn").onclick = () => runReclassify(3, "3 months");
$("#reclassify-24m-btn").onclick = () => runReclassify(24, "24 months");
$("#reclassify-btn").onclick = () => runReclassify(null);

// ---------- ⚙ Tools menu (show/hide advanced + dev actions) ----------
(() => {
  const toggle = $("#tools-toggle");
  const menu = $("#tools-menu");
  toggle.onclick = (e) => { e.stopPropagation(); menu.hidden = !menu.hidden; };
  // Close after picking an action, or when clicking anywhere outside.
  menu.querySelectorAll("button").forEach((b) =>
    b.addEventListener("click", () => { menu.hidden = true; }));
  document.addEventListener("click", (e) => {
    if (!menu.hidden && !menu.contains(e.target) && e.target !== toggle) menu.hidden = true;
  });
})();

// ---------- Sandbox de-noise seed (custom user, no Link popup) ----------
$("#seed-btn").onclick = async () => {
  setStatus("Seeding de-noise test data...");
  const r = await api("/api/sandbox_seed", { method: "POST" });
  if (r.ok) {
    setStatus("De-noise test bank connected. Click Sync.");
  } else {
    setStatus("Error: " + (r.error || "failed"));
  }
};

// ---------- Sync ----------
// One action that pulls new transactions, refreshes every view, and jumps to
// the latest month so freshly synced rows are immediately visible. Runs on the
// Sync button and once automatically when the app opens.
async function doSync() {
  setStatus("Syncing…");
  try {
    const r = await api("/api/sync", { method: "POST" });
    const extra = [r.removed ? `${r.removed} removed` : "", r.updated ? `${r.updated} updated` : ""].filter(Boolean).join(" / ");
    setStatus(`Sync done: ${r.inserted} new (rule ${r.by_rule} / Claude ${r.by_claude} / excluded ${r.excluded ?? 0})${extra ? " · " + extra : ""}`);
  } catch (e) {
    setStatus("Sync failed — check your connection / Plaid keys");
    return;
  }
  await loadCategories();
  await loadAccounts();
  period = "";  // jump to the latest period after syncing
  await refresh();
}
$("#sync-btn").onclick = doSync;

// Full re-sync: resets Plaid cursors and replays everything, cleaning up old
// pending/posted duplicate rows. Heavier than a normal sync.
$("#resync-btn").onclick = async () => {
  if (!confirm("Re-sync all transactions from scratch? This cleans up duplicate pending/posted rows and may take a minute. Your manual corrections are kept.")) return;
  setStatus("Re-syncing from scratch…");
  try {
    const r = await api("/api/resync_full", { method: "POST" });
    setStatus(`Re-sync done: ${r.inserted} new, ${r.removed} duplicates removed`);
  } catch (e) {
    setStatus("Re-sync failed");
    return;
  }
  await loadCategories();
  await loadAccounts();
  period = "";
  await refresh();
};

// ---------- Accounts ----------
let accounts = [];

function accountLabel(a) {
  let label = a.name || a.account_type || "account";
  if (a.mask) label += " ••" + a.mask;
  if (a.institution) label += ` (${a.institution})`;
  return label;
}

async function loadAccounts() {
  accounts = await api("/api/accounts");
  const filter = $("#account-filter");
  const cur = filter.value;
  filter.innerHTML = '<option value="">All</option>' +
    accounts.map((a) => `<option value="${a.account_id}">${accountLabel(a)}</option>`).join("");
  filter.value = cur;
}

// ---------- Categories ----------
async function loadCategories() {
  categories = (await api("/api/categories")).sort((a, b) => a.localeCompare(b));
  const filter = $("#category-filter");
  const cur = filter.value;
  filter.innerHTML = '<option value="">All</option>' +
    categories.map((c) => `<option value="${c}">${c}</option>`).join("");
  filter.value = cur;
}

$("#add-cat-btn").onclick = async () => {
  const name = $("#new-cat").value.trim();
  if (!name) return;
  const choice = await describeCategory(name);
  if (!choice) return;  // cancelled
  await api("/api/categories", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ name, description: choice.description }),
  });
  $("#new-cat").value = "";
  setStatus(`Added category "${name}"`);
  await loadCategories();
};

// ---------- Manage existing categories (rename / delete-merge) ----------
$("#manage-cat-btn").onclick = openCategoryManager;
function openCategoryManager() {
  const overlay = document.createElement("div");
  overlay.className = "mode-overlay";
  overlay.innerHTML =
    `<div class="mode-modal cat-mgr">
      <p><b>Manage categories</b> <span class="muted">— rename, delete/merge, or edit the guide. System labels are locked.</span></p>
      <div class="cat-list"></div>
      <div class="mode-actions"><button class="mode-cancel" type="button">Close</button></div>
    </div>`;
  document.body.appendChild(overlay);
  const listEl = overlay.querySelector(".cat-list");
  const close = async () => { overlay.remove(); await loadCategories(); refresh(); };
  overlay.querySelector(".mode-cancel").onclick = close;
  // only close on a backdrop click that also STARTED on the backdrop, so a
  // text-selection drag that ends on the backdrop doesn't dismiss the dialog
  overlay.onmousedown = (e) => { overlay._downOnBackdrop = e.target === overlay; };
  overlay.onclick = (e) => { if (e.target === overlay && overlay._downOnBackdrop) close(); };

  let cats = [];
  async function render() {
    cats = await api("/api/categories_detail");
    listEl.innerHTML = cats.map((c) => {
      const meta = `<span class="cat-mgr-meta">${c.count}×${c.locked ? " · system" : ""}</span>`;
      const editActions = c.locked ? ""
        : `<button class="cat-rename" data-name="${c.name}">Rename</button>` +
          `<button class="cat-delete" data-name="${c.name}">Delete</button>`;
      return `<div class="cat-mgr-row">
        <span class="cat-mgr-name">${c.name}</span> ${meta}
        <span class="cat-mgr-actions">${editActions}<button class="cat-meaning" data-name="${c.name}">Guide</button></span>
      </div>`;
    }).join("");
    listEl.querySelectorAll(".cat-rename").forEach((b) =>
      b.onclick = () => startRename(b.closest(".cat-mgr-row"), b.dataset.name));
    listEl.querySelectorAll(".cat-delete").forEach((b) =>
      b.onclick = () => startDelete(b.closest(".cat-mgr-row"), b.dataset.name));
    listEl.querySelectorAll(".cat-meaning").forEach((b) =>
      b.onclick = () => startMeaning(b.closest(".cat-mgr-row"), b.dataset.name));
  }

  function startMeaning(row, name) {
    const cur = (cats.find((c) => c.name === name) || {}).meaning || "";
    row.innerHTML =
      `<div class="cat-meaning-edit">
        <div class="muted">Guide meaning for <b>${name}</b> — what Claude uses to pick it:</div>
        <textarea class="cat-mean-ta" rows="2" placeholder="e.g. vet, pet food, grooming">${cur}</textarea>
        <span class="cat-mgr-actions"><button class="cat-mean-save">Save</button><button class="cat-cancel">Cancel</button></span>
      </div>`;
    const ta = row.querySelector(".cat-mean-ta"); ta.focus();
    row.querySelector(".cat-cancel").onclick = render;
    row.querySelector(".cat-mean-save").onclick = async () => {
      const r = await api("/api/categories/meaning", { method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ name, meaning: ta.value.trim() }) });
      if (!r.ok) { alert(r.error || "Save failed"); return; }
      await render();
    };
  }

  function startRename(row, name) {
    row.innerHTML =
      `<input class="cat-edit" value="${name}"> ` +
      `<span class="cat-mgr-actions"><button class="cat-save">Save</button><button class="cat-cancel">Cancel</button></span>`;
    const inp = row.querySelector(".cat-edit"); inp.focus(); inp.select();
    row.querySelector(".cat-cancel").onclick = render;
    const save = async () => {
      const nn = inp.value.trim();
      if (!nn || nn === name) return render();
      const r = await api("/api/categories/rename", { method: "POST",
        headers: { "Content-Type": "application/json" }, body: JSON.stringify({ old: name, new: nn }) });
      if (!r.ok) { alert(r.error || "Rename failed"); return; }
      await render();
    };
    row.querySelector(".cat-save").onclick = save;
    inp.onkeydown = (e) => { if (e.key === "Enter") save(); if (e.key === "Escape") render(); };
  }

  function startDelete(row, name) {
    const opts = cats.filter((c) => c.name !== name)
      .map((c) => `<option ${c.name === "Other" ? "selected" : ""}>${c.name}</option>`).join("");
    row.innerHTML =
      `<span>Delete <b>${name}</b>, move its transactions to </span>` +
      `<select class="cat-move">${opts}</select> ` +
      `<span class="cat-mgr-actions"><button class="cat-do-del">Delete</button><button class="cat-cancel">Cancel</button></span>`;
    row.querySelector(".cat-cancel").onclick = render;
    row.querySelector(".cat-do-del").onclick = async () => {
      const to = row.querySelector(".cat-move").value;
      const r = await api("/api/categories/delete", { method: "POST",
        headers: { "Content-Type": "application/json" }, body: JSON.stringify({ name, reassign_to: to }) });
      if (!r.ok) { alert(r.error || "Delete failed"); return; }
      await render();
    };
  }

  render();
}

// ---------- Manage learned data: rules / trusted merchants / ignored rows ----------
$("#manage-data-btn").onclick = openDataManager;
function openDataManager() {
  const overlay = document.createElement("div");
  overlay.className = "mode-overlay";
  overlay.innerHTML =
    `<div class="mode-modal cat-mgr">
      <p><b>Manage rules, trusted merchants &amp; ignored</b></p>
      <div class="data-tabs">
        <button data-tab="rules" class="on">Rules</button>
        <button data-tab="trust">Trusted</button>
        <button data-tab="ignored">Ignored</button>
      </div>
      <div class="cat-list data-list"></div>
      <div class="mode-actions"><button class="mode-cancel" type="button">Close</button></div>
    </div>`;
  document.body.appendChild(overlay);
  const listEl = overlay.querySelector(".data-list");
  const close = async () => { overlay.remove(); refresh(); };
  overlay.querySelector(".mode-cancel").onclick = close;
  // only close on a backdrop click that also STARTED on the backdrop, so a
  // text-selection drag that ends on the backdrop doesn't dismiss the dialog
  overlay.onmousedown = (e) => { overlay._downOnBackdrop = e.target === overlay; };
  overlay.onclick = (e) => { if (e.target === overlay && overlay._downOnBackdrop) close(); };

  let tab = "rules";
  overlay.querySelectorAll(".data-tabs button").forEach((b) => {
    b.onclick = () => {
      tab = b.dataset.tab;
      overlay.querySelectorAll(".data-tabs button").forEach((x) => x.classList.toggle("on", x === b));
      render();
    };
  });

  async function render() {
    listEl.innerHTML = `<p class="muted">Loading…</p>`;
    if (tab === "rules") {
      const rules = await api("/api/rules");
      listEl.innerHTML = rules.length ? rules.map((r) =>
        `<div class="cat-mgr-row"><span class="cat-mgr-name">${esc(r.pattern)}</span>
          <span class="cat-mgr-meta">→ ${esc(r.category)}</span>
          <span class="cat-mgr-actions"><button class="d-del" data-p="${esc(r.pattern)}">Delete</button></span></div>`).join("")
        : `<p class="muted">No learned rules.</p>`;
      listEl.querySelectorAll(".d-del").forEach((b) => b.onclick = async () => {
        await api("/api/rules/delete", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ pattern: b.dataset.p }) });
        render();
      });
    } else if (tab === "trust") {
      const trust = await api("/api/trust");
      listEl.innerHTML = trust.length ? trust.map((t) =>
        `<div class="cat-mgr-row"><span class="cat-mgr-name">${esc(t.merchant)}</span>
          <span class="cat-mgr-meta">${esc(t.category)}</span>
          <span class="cat-mgr-actions"><button class="d-del" data-m="${esc(t.merchant)}" data-c="${esc(t.category)}">Remove</button></span></div>`).join("")
        : `<p class="muted">No trusted merchants yet.</p>`;
      listEl.querySelectorAll(".d-del").forEach((b) => b.onclick = async () => {
        await api("/api/trust/delete", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ merchant: b.dataset.m, category: b.dataset.c }) });
        render();
      });
    } else {
      const ign = await api("/api/ignored");
      listEl.innerHTML = ign.length ? ign.map((t) =>
        `<div class="cat-mgr-row"><span class="cat-mgr-name">${esc(t.merchant_raw || t.merchant_normalized || "—")}</span>
          <span class="cat-mgr-meta">${t.date} · ${fmt(t.amount)}</span>
          <span class="cat-mgr-actions"><button class="d-unign" data-id="${t.id}">Un-ignore</button></span></div>`).join("")
        : `<p class="muted">Nothing is ignored.</p>`;
      listEl.querySelectorAll(".d-unign").forEach((b) => b.onclick = async () => {
        b.disabled = true; b.textContent = "…";
        await api(`/api/transactions/${b.dataset.id}/unignore`, { method: "POST" });
        render();
      });
    }
  }
  render();
}

// Ask what a new category means; prefill an editable Claude draft so the guide
// can teach Claude when to pick it.
function describeCategory(name) {
  return new Promise((resolve) => {
    const overlay = document.createElement("div");
    overlay.className = "mode-overlay";
    overlay.innerHTML =
      `<div class="mode-modal">
        <p>What does <b>${name}</b> mean? Claude uses this to decide when to pick it.</p>
        <textarea rows="3" placeholder="Drafting with Claude…" disabled></textarea>
        <div class="mode-actions">
          <button class="mode-cancel" type="button">Cancel</button>
          <button class="mode-apply" type="button">Add category</button>
        </div>
      </div>`;
    document.body.appendChild(overlay);
    const ta = overlay.querySelector("textarea");
    const close = (val) => { overlay.remove(); resolve(val); };
    overlay.querySelector(".mode-cancel").onclick = () => close(null);
    overlay.querySelector(".mode-apply").onclick = () => close({ description: ta.value.trim() });
    overlay.onmousedown = (e) => { overlay._downOnBackdrop = e.target === overlay; };
    overlay.onclick = (e) => { if (e.target === overlay && overlay._downOnBackdrop) close(null); };
    // fetch Claude's draft and prefill it (fully editable)
    api("/api/category_draft?name=" + encodeURIComponent(name)).then((r) => {
      ta.disabled = false;
      ta.placeholder = "Describe what belongs in this category…";
      if (r.draft && !ta.value) ta.value = r.draft;
      ta.focus();
    });
  });
}

// Shared category color palette — used for both the doughnut and the ranked
// breakdown list so a category's dot matches its slice.
const CAT_COLORS = [
  "#3b82f6", "#ef4444", "#f59e0b", "#10b981", "#8b5cf6", "#ec4899",
  "#14b8a6", "#f97316", "#6366f1", "#84cc16", "#06b6d4", "#a855f7",
  "#eab308", "#64748b", "#0ea5e9", "#d946ef",
];

// Stable color per category (same color everywhere it appears).
function catColor(name) {
  let i = categories.indexOf(name);
  if (i < 0) { i = 0; for (const ch of name) i = (i * 31 + ch.charCodeAt(0)) >>> 0; }
  return CAT_COLORS[i % CAT_COLORS.length];
}

const PERIOD_WORD = { monthly: "month", quarterly: "quarter", yearly: "year" };
const MONTH_NAMES = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];

// Pretty-print a period key: 2026-06 -> "Jun 2026", 2026-Q2 -> "Q2 2026", 2026 -> "2026".
function fmtPeriod(p) {
  if (!p) return "";
  if (/^\d{4}$/.test(p)) return p;
  if (p.includes("-Q")) { const [y, q] = p.split("-Q"); return `Q${q} ${y}`; }
  const [y, m] = p.split("-");
  return `${MONTH_NAMES[+m - 1]} ${y}`;
}

// ---------- Refresh charts + table ----------
async function refresh() {
  const summary = await api(`/api/summary?gran=${gran}` + (period ? `&period=${period}` : ""));
  period = summary.period;

  // ranked categories (desc) drive both the doughnut and the breakdown list
  const cats = [...summary.by_category].sort((a, b) => b.total - a.total);
  const total = cats.reduce((a, d) => a + d.total, 0);
  const colors = cats.map((d) => catColor(d.category));

  // headline figures (period is chosen by clicking a bar in the trend chart)
  $("#spend-total").textContent = fmt(total);
  $("#spend-income").textContent = fmt(summary.income || 0);
  $("#spend-label-period").textContent = `Spent · ${fmtPeriod(period)}`;

  // doughnut (legend off — the breakdown list below is the legend)
  const ctx = $("#pie");
  if (pieChart) pieChart.destroy();
  pieChart = new Chart(ctx, {
    type: "doughnut",
    data: { labels: cats.map((d) => d.category),
            datasets: [{ data: cats.map((d) => d.total), backgroundColor: colors, borderWidth: 0 }] },
    options: { cutout: "64%", maintainAspectRatio: false, plugins: { legend: { display: false } } },
  });

  // ranked breakdown list — click a category to filter the table
  const cb = $("#cat-breakdown");
  cb.innerHTML = cats.map((d, i) => {
    const pct = total > 0 ? Math.round((d.total / total) * 100) : 0;
    const w = Math.max(0, pct);
    return `<button class="cat-row" data-cat="${d.category}">
      <span class="cat-name"><span class="dot" style="background:${colors[i]}"></span>${d.category}</span>
      <span class="cat-amt">${fmt(d.total)}<span class="cat-pct">${pct}%</span></span>
      <span class="cat-bar"><span style="width:${w}%;background:${colors[i]}"></span></span>
    </button>`;
  }).join("");
  const activeCat = $("#category-filter").value;
  cb.querySelectorAll(".cat-row").forEach((b) => {
    b.classList.toggle("active", b.dataset.cat === activeCat);
    b.onclick = () => {
      const cf = $("#category-filter");
      // click the active category again to clear the filter (back to All)
      const next = cf.value === b.dataset.cat ? "" : b.dataset.cat;
      cf.value = next;
      cb.querySelectorAll(".cat-row").forEach((x) => x.classList.toggle("active", x.dataset.cat === next));
      loadTable();
    };
  });
  breakdownShown = BREAKDOWN_TOP_N;
  applyBreakdown();

  // ----- Income doughnut + breakdown (mirrors the spending widget) -----
  const incCats = [...(summary.income_by_category || [])].sort((a, b) => b.total - a.total);
  const incTotal = incCats.reduce((a, d) => a + d.total, 0);
  const incColors = incCats.map((d) => catColor(d.category));
  $("#income-label-period").textContent = `Income · ${fmtPeriod(period)}`;
  const ictx = $("#income-pie");
  if (incomePie) incomePie.destroy();
  incomePie = new Chart(ictx, {
    type: "doughnut",
    data: { labels: incCats.map((d) => d.category),
            datasets: [{ data: incCats.map((d) => d.total), backgroundColor: incColors, borderWidth: 0 }] },
    options: { cutout: "64%", maintainAspectRatio: false, plugins: { legend: { display: false } } },
  });
  const ib = $("#income-breakdown");
  ib.innerHTML = incCats.map((d, i) => {
    const pct = incTotal > 0 ? Math.round((d.total / incTotal) * 100) : 0;
    return `<button class="cat-row" data-cat="${d.category}">
      <span class="cat-name"><span class="dot" style="background:${incColors[i]}"></span>${d.category}</span>
      <span class="cat-amt">${fmt(d.total)}<span class="cat-pct">${pct}%</span></span>
      <span class="cat-bar"><span style="width:${Math.max(0, pct)}%;background:${incColors[i]}"></span></span>
    </button>`;
  }).join("") || `<p class="muted" style="padding:8px 4px">No income this period.</p>`;
  const incActive = $("#category-filter").value;
  ib.querySelectorAll(".cat-row").forEach((b) => {
    b.classList.toggle("active", b.dataset.cat === incActive);
    b.onclick = () => {
      const cf = $("#category-filter");
      const next = cf.value === b.dataset.cat ? "" : b.dataset.cat;
      cf.value = next;
      // income rows are excluded from the default "expenses" view -> show them
      if (next) $("#expenses-only").checked = false;  // show income (excluded) rows
      ib.querySelectorAll(".cat-row").forEach((x) => x.classList.toggle("active", x.dataset.cat === next));
      loadTable();
    };
  });

  await loadTable();
  await loadReview();
  await loadTopMerchants();
  await loadIncome();
  await loadRecurring();
  await loadTrend();  // sets trendData

  // headline delta vs the previous period, read from the trend series
  let deltaHtml = "";
  if (trendData) {
    const i = trendData.findIndex((p) => p.label === period);
    if (i > 0 && trendData[i - 1].spending > 0) {
      const prev = trendData[i - 1].spending;
      const pct = Math.round(((total - prev) / prev) * 100);
      deltaHtml = `<span class="${pct > 0 ? "up" : "down"}">` +
        `${pct > 0 ? "▲" : "▼"} ${Math.abs(pct)}% vs last ${PERIOD_WORD[gran]}</span>`;
    }
  }
  $("#spend-delta").innerHTML = deltaHtml;
}

// ---------- Spending vs income trend (follows the master granularity) ----------
async function loadTrend() {
  const r = await api("/api/trend?period=" + gran);
  trendData = r.points;
  const labels = r.points.map((p) => (gran === "monthly" ? p.label.slice(2) : p.label));
  const sel = r.points.findIndex((p) => p.label === period);
  // highlight the currently-selected period; dim the rest
  const spendColors = r.points.map((_, i) => (i === sel ? "#2563eb" : "#bfdbfe"));
  const incomeColors = r.points.map((_, i) => (i === sel ? "#059669" : "#a7f3d0"));
  // widen the inner chart area so all periods fit, making it horizontally
  // scrollable; the user can scroll left to reach the earliest period.
  const viewport = document.querySelector(".chart-trend");
  const inner = document.querySelector(".chart-trend-inner");
  const perPeriod = gran === "yearly" ? 90 : 70;
  inner.style.width = Math.max(viewport.clientWidth, r.points.length * perPeriod) + "px";
  if (trendChart) trendChart.destroy();
  trendChart = new Chart($("#trend-bar"), {
    type: "bar",
    data: {
      labels,
      datasets: [
        { label: "Spending", data: r.points.map((p) => p.spending),
          backgroundColor: spendColors, borderRadius: 4, maxBarThickness: 30 },
        { label: "Income", data: r.points.map((p) => p.income),
          backgroundColor: incomeColors, borderRadius: 4, maxBarThickness: 30 },
      ],
    },
    options: {
      maintainAspectRatio: false,
      onClick: (e, els) => {
        if (els.length) { period = r.points[els[0].index].label; refresh(); }
      },
      plugins: {
        legend: { position: "top", align: "end",
                  labels: { boxWidth: 8, boxHeight: 8, usePointStyle: true, padding: 14 } },
        tooltip: { callbacks: { label: (c) => `${c.dataset.label}: ${fmt(c.parsed.y)}` } },
      },
      scales: {
        x: { grid: { display: false } },
        y: { border: { display: false },
             ticks: { callback: (v) => "$" + (v >= 1000 ? (v / 1000) + "k" : v) } },
      },
    },
  });
  // start scrolled to the most recent period (right edge)
  viewport.scrollLeft = viewport.scrollWidth;
}

// master granularity toggle — drives the whole view
document.querySelectorAll("#gran-toggle button").forEach((b) => {
  b.onclick = () => {
    gran = b.dataset.p;
    period = "";  // reset to the latest period of the new granularity
    document.querySelectorAll("#gran-toggle button").forEach((x) => x.classList.toggle("on", x === b));
    refresh();
  };
});

// ---------- Generic "Show N more / Show all / Show less" control ----------
const SHOW_STEP = 10;
const LIST_LIMIT = 8;  // collapsed row count for income / recurring / merchants
function _mkShowBtn(label, fn) {
  const b = document.createElement("button");
  b.className = "show-all"; b.textContent = label; b.onclick = fn;
  return b;
}
// container: selector; shown: current count; total: full count; base: collapsed
// count; setShown(n): updates the widget's shown count and re-renders.
function renderShowMore(sel, shown, total, base, setShown) {
  const el = document.querySelector(sel);
  el.innerHTML = "";
  if (total <= base) return;
  if (shown < total) {  // more to reveal
    const more = Math.min(SHOW_STEP, total - shown);
    el.append(_mkShowBtn(`Show ${more} more`, () => setShown(Math.min(shown + SHOW_STEP, total))));
    el.append(_mkShowBtn(`Show all (${total})`, () => setShown(total)));
  }
  if (shown > base) {  // expanded beyond the base -> always allow collapsing
    el.append(_mkShowBtn("Show less", () => setShown(base)));
  }
}

// ---------- Generic click-to-sort helpers (for income / recurring tables) ----------
function applySort(rows, state, valFn) {
  if (!state.key) return rows;  // no sort chosen -> keep server order
  return [...rows].sort((a, b) => {
    const va = valFn(a, state.key), vb = valFn(b, state.key);
    return va < vb ? -state.dir : va > vb ? state.dir : 0;
  });
}
function wireSortHeaders(tableSel, state, descFirst, rerender) {
  document.querySelectorAll(`${tableSel} th[data-sort]`).forEach((th) => {
    th.onclick = () => {
      const k = th.dataset.sort;
      if (state.key === k) state.dir = -state.dir;
      else { state.key = k; state.dir = descFirst.has(k) ? -1 : 1; }
      rerender();
    };
  });
}
function markSortHeaders(tableSel, state) {
  document.querySelectorAll(`${tableSel} th[data-sort]`).forEach((th) => {
    const on = th.dataset.sort === state.key;
    th.classList.toggle("sorted", on);
    th.setAttribute("data-dir", on ? (state.dir < 0 ? "desc" : "asc") : "");
  });
}

// ---------- Top merchants (follows the master period) ----------
let merchantRows = [], merchantShown = LIST_LIMIT;
const merchantSort = { key: "count", dir: -1 };  // default: most frequent first
const merchantVal = (m, k) =>
  (k === "count" || k === "total") ? +m[k] : (m[k] ?? "").toString().toLowerCase();
async function loadTopMerchants() {
  const params = new URLSearchParams();
  if (period) { params.set("gran", gran); params.set("period", period); }
  merchantRows = await api("/api/top_merchants?" + params.toString());
  $("#merchant-empty").hidden = merchantRows.length > 0;
  merchantShown = LIST_LIMIT;
  renderMerchants();
}
function renderMerchants() {
  const tbody = $("#merchant-table tbody");
  tbody.innerHTML = "";
  const sorted = applySort(merchantRows, merchantSort, merchantVal);
  for (const m of sorted.slice(0, merchantShown)) {
    const tr = document.createElement("tr");
    tr.innerHTML = `<td>${mcell(m.merchant)}</td><td>${m.count}×</td><td>${fmt(m.total)}</td>`;
    tbody.appendChild(tr);
  }
  markSortHeaders("#merchant-table", merchantSort);
  renderShowMore("#merchant-show-more", merchantShown, sorted.length, LIST_LIMIT,
    (n) => { merchantShown = n; renderMerchants(); });
}
wireSortHeaders("#merchant-table", merchantSort, new Set(["count", "total"]), renderMerchants);

// ---------- Income detail (follows the master period) ----------
let incomeRows = [], incomeShown = LIST_LIMIT;
const incomeSort = { key: null, dir: -1 };
const incomeVal = (it, k) =>
  k === "amount" ? -it.amount
  : k === "account_name" ? (it.account_name || it.account_type || "").toLowerCase()
  : (it[k] ?? "").toString().toLowerCase();
async function loadIncome() {
  const params = new URLSearchParams();
  if (period) { params.set("gran", gran); params.set("period", period); }
  const r = await api("/api/income?" + params.toString());
  incomeRows = r.items;
  $("#income-total").textContent = r.items.length ? `· ${fmt(r.total)}` : "";
  $("#income-empty").hidden = r.items.length > 0;
  incomeShown = LIST_LIMIT;
  renderIncome();
}
function renderIncome() {
  const tbody = $("#income-table tbody");
  tbody.innerHTML = "";
  const sorted = applySort(incomeRows, incomeSort, incomeVal);
  for (const it of sorted.slice(0, incomeShown)) {
    const acct = it.account_name
      ? `${it.account_name}${it.account_mask ? " ••" + it.account_mask : ""}`
      : (it.account_type || "");
    const tr = document.createElement("tr");
    tr.innerHTML =
      `<td>${it.date}</td><td>${mcell(it.merchant_raw)}</td>` +
      `<td>${acct}</td><td><span class="offset">${fmt(-it.amount)}</span></td>`;
    tbody.appendChild(tr);
  }
  markSortHeaders("#income-table", incomeSort);
  renderShowMore("#income-show-more", incomeShown, incomeRows.length, LIST_LIMIT,
    (n) => { incomeShown = n; renderIncome(); });
}
wireSortHeaders("#income-table", incomeSort, new Set(["date", "amount"]), renderIncome);

// ---------- Recurring (subscriptions + periodic bills) ----------
let recurringRows = [], recurringShown = LIST_LIMIT;
const recurringSort = { key: null, dir: -1 };
const _RECUR_NUM = new Set(["typical_amount", "occurrences", "monthly_cost", "last_date", "next_expected"]);
const recurringVal = (it, k) =>
  ["typical_amount", "occurrences", "monthly_cost"].includes(k) ? +it[k]
  : k === "next_expected" ? (it.active ? it.next_expected : "~")  // ended sort last
  : (it[k] ?? "").toString().toLowerCase();
async function loadRecurring() {
  const r = await api("/api/recurring");
  recurringRows = r.items;
  $("#recurring-total").textContent =
    r.items.length ? `· est. ${fmt(r.monthly_total)}/month` : "";
  $("#recurring-empty").hidden = r.items.length > 0;
  recurringShown = LIST_LIMIT;
  renderRecurring();
}
function renderRecurring() {
  const tbody = $("#recurring-table tbody");
  tbody.innerHTML = "";
  const sorted = applySort(recurringRows, recurringSort, recurringVal);
  for (const it of sorted.slice(0, recurringShown)) {
    const tr = document.createElement("tr");
    if (!it.active) tr.className = "inactive-row";
    const nextCell = it.active
      ? it.next_expected
      : '<span class="badge ended">ended</span>';
    tr.innerHTML =
      `<td>${mcell(it.merchant)}</td>` +
      `<td><span class="badge ${it.kind}">${it.kind}</span></td>` +
      `<td>${it.cadence}</td>` +
      `<td>${fmt(it.typical_amount)}</td>` +
      `<td>${it.category || ""}</td>` +
      `<td>${it.occurrences}×</td>` +
      `<td>${it.last_date}</td>` +
      `<td>${nextCell}</td>` +
      `<td>${fmt(it.monthly_cost)}</td>`;
    tbody.appendChild(tr);
  }
  markSortHeaders("#recurring-table", recurringSort);
  renderShowMore("#recurring-show-more", recurringShown, recurringRows.length, LIST_LIMIT,
    (n) => { recurringShown = n; renderRecurring(); });
}
wireSortHeaders("#recurring-table", recurringSort, _RECUR_NUM, renderRecurring);

let txnRows = [];
let sortKey = "date", sortDir = -1;  // default: newest first
const TXN_LIMIT = 10;
let txnShown = TXN_LIMIT;

async function loadTable() {
  const params = new URLSearchParams();
  if (period) { params.set("gran", gran); params.set("period", period); }
  const category = $("#category-filter").value;
  const account = $("#account-filter").value;
  if (category) params.set("category", category);
  if (account) params.set("account", account);
  if ($("#review-only").checked) params.set("review", "1");
  txnRows = await api("/api/transactions?" + params.toString());

  // review count badge (global, unfiltered)
  const rc = await api("/api/review_count");
  const badge = $("#review-count");
  badge.hidden = rc.count === 0;
  badge.textContent = rc.count;

  // keep the current expansion (don't collapse after a category change / approve);
  // never show fewer than the default though
  txnShown = Math.max(TXN_LIMIT, Math.min(txnShown, txnRows.length));
  renderTxns();
}

function txnCmp(a, b) {
  let va = a[sortKey], vb = b[sortKey];
  if (sortKey === "amount") { va = +va; vb = +vb; }
  else { va = (va == null ? "" : ("" + va)).toLowerCase(); vb = (vb == null ? "" : ("" + vb)).toLowerCase(); }
  return va < vb ? -sortDir : va > vb ? sortDir : 0;
}

// ----- shared transaction-row rendering (main table + Needs-review list) -----
function buildTxnRow(t) {
  const tr = document.createElement("tr");
  // Reset option: if we kept Claude's original pick, offer to restore it; else
  // (e.g. a manually-ignored row that never went through Claude) offer to run
  // Claude on it fresh so nothing gets stuck as Ignore/manual.
  const resetOpt = t.claude_category
    ? `<option value="__claude__">↺ Reset to Claude (${t.claude_category})</option>`
    : (t.classified_by === "manual"
        ? `<option value="__reclassify__">↺ Re-classify with Claude</option>` : "");
  const placeholder = !t.category
    ? '<option value="" selected disabled hidden>—</option>' : "";
  const catCell = `<select data-id="${t.id}" data-claude="${t.claude_category || ""}" ` +
    `data-merchant="${t.merchant_normalized || ""}" data-spending="${t.is_spending}">` +
    placeholder +
    categories.map((c) => `<option ${c === t.category ? "selected" : ""}>${c}</option>`).join("") +
    resetOpt + `</select>`;
  let badge;
  if (t.is_spending === 0) {
    // Excluded noise row: greyed out, exclusion reason shown as the badge.
    tr.className = "excluded-row";
    badge = `<span class="badge excluded">${t.exclude_reason || "excluded"}</span>`;
  } else {
    badge = `<span class="badge ${t.classified_by}">${t.classified_by}</span>`;
  }
  const review = t.needs_review
    ? ` <span class="review" title="Needs review">⚠</span>` +
      `<button class="review-done" data-id="${t.id}" title="Mark as reviewed">✓</button>` : "";
  const isOffset = t.is_spending === 1 && t.signed_amount < 0;
  const amtCell = `<span class="${isOffset ? "offset" : ""}">${fmt(t.amount)}</span>`;
  const acctName = t.account_name
    ? `${t.account_name}${t.account_mask ? " ••" + t.account_mask : ""}` : t.account_type;
  const acctCell = `<span title="${t.account_type}">${acctName}</span>`;
  tr.innerHTML =
    `<td>${t.date}</td><td>${mcell(t.merchant_raw)}${review}</td>` +
    `<td>${amtCell}</td><td>${acctCell}</td>` +
    `<td>${catCell}</td><td>${badge}</td>`;
  return tr;
}

// Wire category dropdowns + review-done buttons inside a tbody. `reload` runs
// after any change so both the main table and the review list stay in sync.
function wireTxnBody(tbody, reload) {
  tbody.querySelectorAll(".review-done").forEach((b) => {
    b.onclick = async () => {
      const r = await api(`/api/transactions/${b.dataset.id}/review_done`, { method: "POST" });
      setStatus(r && r.cleared > 1
        ? `Trusted this merchant — cleared ${r.cleared} rows from review`
        : "Marked as reviewed");
      await reload();
    };
  });
  tbody.querySelectorAll("select").forEach((s) => {
    s.onchange = async () => {
      // Re-classify this one row with Claude (used to un-stick a manually
      // ignored/edited row that has no stored Claude pick to reset to).
      if (s.value === "__reclassify__") {
        setStatus("Re-classifying with Claude…");
        await api(`/api/transactions/${s.dataset.id}/reclassify_one`, { method: "POST" });
        setStatus("Re-classified with Claude");
        await refresh();
        return;
      }
      const value = s.value === "__claude__" ? s.dataset.claude : s.value;
      // Only "reset to Claude" skips the chooser; every other relabel goes through
      // the rule/guide chooser so it's learned, and the backend re-derives spending.
      const special = s.value === "__claude__";
      let body = { category: value };
      if (!special) {
        const choice = await chooseCorrectionMode(s.dataset.merchant || value, value);
        if (!choice) { await reload(); return; }  // cancelled -> revert dropdown
        body = { category: value, mode: choice.mode, note: choice.note, pattern: choice.pattern };
      }
      const r = await api(`/api/transactions/${s.dataset.id}/category`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
      });
      if (r.mode === "guide") {
        setStatus(r.matched
          ? `Added to guide — Claude now classifies "${s.dataset.merchant}" as ${r.applied} ✓`
          : `Added to guide, but Claude still says ${r.applied}. Refine the note and try again.`);
      } else if (r.mode === "once") {
        setStatus("Changed just this transaction");
      } else {
        setStatus(r.reverted ? "Reset to Claude; rule removed" : "Saved as a rule");
      }
      await refresh();  // a relabel can change totals -> full refresh (incl. review list)
    };
  });
}

async function reloadTables() { await loadTable(); await loadReview(); }

function renderTxns() {
  const reviewOnly = $("#review-only").checked;
  const expensesOnly = $("#expenses-only").checked;  // checked = expenses only, unchecked = all
  const q = ($("#txn-search").value || "").trim().toLowerCase();
  const tbody = $("#txn-table tbody");
  tbody.innerHTML = "";
  // filter (merchant search applies first, then the review/view filters), then sort
  const visible = [...txnRows].sort(txnCmp).filter((t) => {
    if (q && !(`${t.merchant_raw || ""} ${t.merchant_normalized || ""}`.toLowerCase().includes(q))) return false;
    if (reviewOnly) return true;
    if (!expensesOnly) return true;  // unchecked = show everything (incl. excluded/offsets)
    // "Expenses only" = money actually paid out; hide excluded rows and offsets
    // (refunds / reimbursements / transfers / income / ignored).
    return t.is_spending === 1 && t.signed_amount > 0;
  });
  for (const t of visible.slice(0, txnShown)) tbody.appendChild(buildTxnRow(t));
  wireTxnBody(tbody, reloadTables);
  // reflect the active sort on the column headers
  document.querySelectorAll("#txn-table th[data-sort]").forEach((th) => {
    const on = th.dataset.sort === sortKey;
    th.classList.toggle("sorted", on);
    th.setAttribute("data-dir", on ? (sortDir < 0 ? "desc" : "asc") : "");
  });
  // show only the top rows until "Show all"
  renderShowMore("#txn-show-more", txnShown, visible.length, TXN_LIMIT,
    (n) => { txnShown = n; renderTxns(); });
}

// ---------- Needs-review section (its own list below Transactions) ----------
// Global (all periods) so nothing hides behind the master period toggle.
let reviewRows = [];
const REVIEW_LIMIT = 10;
let reviewShown = REVIEW_LIMIT;
const reviewSort = { key: null, dir: -1 };  // null = keep API order (newest first)
const reviewVal = (t, k) =>
  k === "amount" ? +t.amount : (t[k] ?? "").toString().toLowerCase();

async function loadReview() {
  reviewRows = await api("/api/transactions?review=1");  // newest-first from the API
  // keep the current expansion so approving a row doesn't collapse the list
  reviewShown = Math.max(REVIEW_LIMIT, Math.min(reviewShown, reviewRows.length));
  renderReview();
}

function renderReview() {
  const tbody = $("#review-table tbody");
  if (!tbody) return;
  tbody.innerHTML = "";
  $("#review-empty").hidden = reviewRows.length > 0;
  const head = $("#review-heading");
  if (head) head.textContent = reviewRows.length ? `Needs review (${reviewRows.length})` : "Needs review";
  const sorted = applySort(reviewRows, reviewSort, reviewVal);
  for (const t of sorted.slice(0, reviewShown)) tbody.appendChild(buildTxnRow(t));
  wireTxnBody(tbody, reloadTables);
  markSortHeaders("#review-table", reviewSort);
  renderShowMore("#review-show-more", reviewShown, sorted.length, REVIEW_LIMIT,
    (n) => { reviewShown = n; renderReview(); });
}
wireSortHeaders("#review-table", reviewSort, new Set(["date", "amount"]), renderReview);

// ---------- Correction mode chooser (rule vs. teach-Claude-a-pattern) ----------
function chooseCorrectionMode(merchant, category) {
  return new Promise((resolve) => {
    const overlay = document.createElement("div");
    overlay.className = "mode-overlay";
    overlay.innerHTML =
      `<div class="mode-modal">
        <p>Apply <b>${category}</b> to "<span class="muted">${merchant}</span>" as:</p>
        <div class="mode-btns">
          <button data-mode="once">Just this once<br><small>change only this transaction</small></button>
          <button data-mode="rule">This merchant<br><small>fixed category, ignores amount</small></button>
          <button data-mode="guide">Teach Claude a pattern<br><small>plain-English rule, amount-aware</small></button>
        </div>
        <div class="mode-rule" hidden>
          <label class="rule-label">Match merchants containing:</label>
          <input type="text" class="rule-pattern" placeholder="e.g. MT LAW">
          <p class="muted rule-hint">Any transaction whose name contains this text becomes <b>${category}</b>. Shorten it to generalize.</p>
          <div class="mode-actions">
            <button class="mode-cancel" type="button">Cancel</button>
            <button class="rule-apply" type="button">Save rule</button>
          </div>
        </div>
        <div class="mode-note" hidden>
          <textarea rows="3"></textarea>
          <div class="mode-actions">
            <button class="mode-cancel" type="button">Cancel</button>
            <button class="mode-apply" type="button">Add to guide &amp; re-run</button>
          </div>
        </div>
      </div>`;
    document.body.appendChild(overlay);
    const ta = overlay.querySelector("textarea");
    const patInp = overlay.querySelector(".rule-pattern");
    const close = (val) => { overlay.remove(); resolve(val); };
    overlay.querySelector('[data-mode="once"]').onclick = () => close({ mode: "once" });
    overlay.querySelector('[data-mode="rule"]').onclick = () => {
      overlay.querySelector(".mode-btns").hidden = true;
      overlay.querySelector(".mode-rule").hidden = false;
      patInp.value = merchant;  // prefill full merchant; user can shorten to a keyword
      patInp.focus();
      patInp.select();
    };
    overlay.querySelector(".rule-apply").onclick = () => {
      const pattern = patInp.value.trim();
      if (!pattern) { patInp.focus(); return; }
      close({ mode: "rule", pattern });
    };
    patInp.onkeydown = (e) => { if (e.key === "Enter") overlay.querySelector(".rule-apply").click(); };
    overlay.querySelector('[data-mode="guide"]').onclick = () => {
      overlay.querySelector(".mode-btns").hidden = true;
      overlay.querySelector(".mode-note").hidden = false;
      ta.value = `Charges from "${merchant}" should be ${category} — explain why so Claude can generalize.`;
      ta.focus();
      ta.select();
    };
    overlay.querySelectorAll(".mode-cancel").forEach((b) => b.onclick = () => close(null));
    overlay.querySelector(".mode-apply").onclick = () => {
      const note = ta.value.trim();
      if (!note) { ta.focus(); return; }
      close({ mode: "guide", note });
    };
    overlay.onmousedown = (e) => { overlay._downOnBackdrop = e.target === overlay; };
    overlay.onclick = (e) => { if (e.target === overlay && overlay._downOnBackdrop) close(null); };
  });
}

// category breakdown: show the top N, expand to show all
const BREAKDOWN_TOP_N = 5;
let breakdownShown = BREAKDOWN_TOP_N;
function applyBreakdown() {
  const rows = $("#cat-breakdown").querySelectorAll(".cat-row");
  rows.forEach((r, i) => { r.style.display = i < breakdownShown ? "" : "none"; });
  renderShowMore("#breakdown-show-more", breakdownShown, rows.length, BREAKDOWN_TOP_N,
    (n) => { breakdownShown = n; applyBreakdown(); });
}

$("#category-filter").onchange = loadTable;
$("#account-filter").onchange = loadTable;
$("#expenses-only").onchange = renderTxns;
$("#review-only").onchange = loadTable;
// merchant search filters the loaded rows client-side (current period)
$("#txn-search").oninput = () => { txnShown = TXN_LIMIT; renderTxns(); };

// click a column header to sort the transactions
document.querySelectorAll("#txn-table th[data-sort]").forEach((th) => {
  th.onclick = () => {
    const k = th.dataset.sort;
    if (sortKey === k) sortDir = -sortDir;
    else { sortKey = k; sortDir = (k === "amount" || k === "date") ? -1 : 1; }
    renderTxns();
  };
});

// ---------- First-run setup check ----------
async function checkSetup() {
  let s;
  try {
    s = await api("/api/setup_status");
  } catch (e) {
    return true;  // endpoint unavailable (old backend) -- don't block the app
  }
  const missing = [];
  if (!s.plaid) {
    missing.push(
      '<li><b>Plaid</b> — <code>PLAID_CLIENT_ID</code> + <code>PLAID_SECRET</code>: ' +
      'sign up free at <a href="https://dashboard.plaid.com" target="_blank" rel="noopener">dashboard.plaid.com</a>, ' +
      'then copy both from <i>Developers → Keys</i>. New accounts include a free ' +
      'Trial plan (up to 10 real banks, no review needed).</li>');
  }
  if (!s.anthropic) {
    missing.push(
      '<li><b>Anthropic</b> — <code>ANTHROPIC_API_KEY</code>: create one at ' +
      '<a href="https://console.anthropic.com" target="_blank" rel="noopener">console.anthropic.com</a> ' +
      '(<i>API keys</i>). Used to auto-categorize new merchants; a full first ' +
      'sync costs a few cents.</li>');
  }
  if (missing.length) {
    $("#setup-missing").innerHTML = missing.join("");
    $("#setup-modal").hidden = false;
  }
  return missing.length === 0;
}

$("#setup-recheck").onclick = async () => {
  const ok = await checkSetup();
  if (ok) {
    $("#setup-modal").hidden = true;
    setStatus("Keys look good — connect a bank to get started.");
  } else {
    setStatus("Still missing keys — see the dialog.");
  }
};
$("#setup-dismiss").onclick = () => { $("#setup-modal").hidden = true; };

// init: render the saved data immediately (fast), then auto-sync once in the
// background so opening the app shows fresh data without a manual click.
// If API keys aren't configured yet, show the setup dialog and skip the sync.
(async () => {
  const configured = await checkSetup();
  await loadCategories();
  await loadAccounts();
  await refresh();
  if (configured) {
    doSync();  // not awaited: UI is usable right away, updates when sync returns
  }
})();
