/* Logical Branch Trace — client UI (vanilla JS, no build, no AI) */

const COLORS = [
  "#5b9fd4", "#6bbf8a", "#d4a15b", "#c27bd4", "#d46b7a",
  "#5bc4c0", "#8aa0d4", "#bfa06b", "#7dcea0", "#d48a5b",
];

const state = {
  report: null,
  inputPath: null,
  folder: ".",
  pathId: null,
  focusFn: null,
  view: "path",
  selectedId: null,
  fileFilter: "",
  labelWidth: "compact", // compact | wide | full
};

const $ = (id) => document.getElementById(id);

function familyColor(familyId) {
  const families = state.report?.families || [];
  const idx = Math.max(0, families.findIndex((f) => f.id === familyId));
  return COLORS[idx % COLORS.length];
}

function tracesByFn() {
  const map = {};
  for (const t of state.report?.traces || []) map[t.function] = t;
  return map;
}

function currentPath() {
  const paths = state.report?.flowPaths || [];
  return paths.find((p) => p.id === state.pathId) || paths[0] || null;
}

function setStatus(msg, kind = "") {
  const el = $("status");
  el.textContent = msg;
  el.className = "status" + (kind ? ` ${kind}` : "");
}

/** Parent folder of a relative file path. Root files → "." */
function folderOf(relPath) {
  const i = relPath.lastIndexOf("/");
  return i === -1 ? "." : relPath.slice(0, i);
}

function fileNameOf(relPath) {
  const i = relPath.lastIndexOf("/");
  return i === -1 ? relPath : relPath.slice(i + 1);
}

/** Unique folders from file index, with span totals, sorted. */
function folderIndex(files) {
  const map = new Map();
  for (const f of files || []) {
    const folder = folderOf(f.relPath);
    const cur = map.get(folder) || { folder, fileCount: 0, totalSpans: 0, errors: 0 };
    cur.fileCount += 1;
    cur.totalSpans += f.totalSpans || 0;
    if (f.error) cur.errors += 1;
    map.set(folder, cur);
  }
  return [...map.values()].sort((a, b) => {
    if (a.folder === ".") return -1;
    if (b.folder === ".") return 1;
    return a.folder.localeCompare(b.folder);
  });
}

function filesInFolder(files, folder) {
  return (files || []).filter((f) => folderOf(f.relPath) === folder);
}

function folderLabel(entry) {
  const name = entry.folder === "." ? "(root)" : entry.folder;
  return `${name}  ·  ${entry.fileCount} file${entry.fileCount === 1 ? "" : "s"}  ·  ${entry.totalSpans} spans`;
}

async function analyze(opts = {}) {
  const path = (opts.path ?? $("pathInput").value).trim();
  const file = opts.file ?? null;
  if (!path) {
    setStatus("Enter a directory or .py file path", "error");
    return;
  }
  $("analyzeBtn").disabled = true;
  setStatus(file ? `Loading ${file}…` : "Scanning / analyzing…");
  try {
    const body = { path };
    if (file) body.file = file;
    const res = await fetch("/api/analyze", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    const data = await res.json();
    if (!res.ok) {
      setStatus(data.error + (data.detail ? ` — ${data.detail}` : ""), "error");
      return;
    }

    // When switching file via absolute path (single-file response), keep directory index
    if (
      opts.preserveFiles &&
      state.report?.files?.length &&
      data.kind === "file"
    ) {
      data.files = state.report.files;
      data.kind = "directory";
      data.root = state.report.root || state.inputPath;
      data.activeFile = opts.activeFile || data.fileName;
      data.fileCount = state.report.fileCount || data.files.length;
      data.workspaceSpans = state.report.workspaceSpans;
    }

    state.inputPath = opts.preserveFiles ? state.inputPath || path : path;
    state.report = data;
    state.folder = data.activeFile ? folderOf(data.activeFile) : ".";
    state.pathId = data.flowPaths[0]?.id || null;
    state.focusFn =
      data.flowPaths[0]?.steps?.[0] || data.traces[0]?.function || null;
    state.selectedId = null;

    const fileNote =
      data.kind === "directory"
        ? ` · ${data.fileCount} files · active ${data.activeFile}`
        : "";
    const spanNote =
      data.workspaceSpans != null
        ? `${data.workspaceSpans} workspace spans / ${data.totalSpans} in active file`
        : `${data.totalSpans} spans`;
    setStatus(
      `OK — ${spanNote} · ${data.families.length} families${fileNote}`,
      "ok",
    );
    renderAll();
  } catch (e) {
    setStatus(String(e), "error");
  } finally {
    $("analyzeBtn").disabled = false;
  }
}

async function switchFile(relPath) {
  const meta = (state.report?.files || []).find((f) => f.relPath === relPath);
  if (!meta) return;
  if (meta.error) {
    setStatus(`Cannot open ${relPath}: ${meta.error}`, "error");
    return;
  }
  state.folder = folderOf(relPath);
  await analyze({
    path: meta.path,
    preserveFiles: true,
    activeFile: relPath,
  });
}

function onFolderChange(folder) {
  state.folder = folder;
  fillFileSelect();
  updateBrowseChrome();

  const files = filesInFolder(state.report?.files, folder).filter((f) => !f.error);
  if (!files.length) {
    $("browseHint").textContent = "No analyzable .py files in this folder.";
    return;
  }
  // Prefer keeping current file if still in folder; else densest file
  const active = state.report?.activeFile;
  if (active && folderOf(active) === folder) {
    $("fileSelect").value = active;
    return;
  }
  files.sort((a, b) => b.totalSpans - a.totalSpans || a.relPath.localeCompare(b.relPath));
  switchFile(files[0].relPath);
}

function fillFolderSelect() {
  const r = state.report;
  const folders = folderIndex(r.files);
  const sel = $("folderSelect");
  sel.innerHTML = folders
    .map((e) => {
      const selAttr = e.folder === state.folder ? "selected" : "";
      return `<option value="${escapeAttr(e.folder)}" ${selAttr}>${escapeHtml(folderLabel(e))}</option>`;
    })
    .join("");
  if (!folders.some((e) => e.folder === state.folder) && folders.length) {
    state.folder = folders[0].folder;
    sel.value = state.folder;
  }
}

function fillFileSelect() {
  const r = state.report;
  const q = (state.fileFilter || "").trim().toLowerCase();
  let files = filesInFolder(r.files, state.folder);
  if (q) {
    files = files.filter(
      (f) =>
        fileNameOf(f.relPath).toLowerCase().includes(q) ||
        f.relPath.toLowerCase().includes(q),
    );
  }
  files = [...files].sort(
    (a, b) => b.totalSpans - a.totalSpans || a.name.localeCompare(b.name),
  );

  const sel = $("fileSelect");
  if (!files.length) {
    sel.innerHTML = `<option value="">(no matching files)</option>`;
    sel.disabled = true;
    return;
  }
  sel.disabled = false;
  sel.innerHTML = files
    .map((f) => {
      const name = fileNameOf(f.relPath);
      const label = f.error
        ? `${name}  ·  error`
        : `${name}  ·  ${f.totalSpans} spans`;
      const selected = f.relPath === r.activeFile ? "selected" : "";
      const dis = f.error ? "disabled" : "";
      return `<option value="${escapeAttr(f.relPath)}" ${selected} ${dis} title="${escapeAttr(f.relPath)}">${escapeHtml(label)}</option>`;
    })
    .join("");

  // If active file not in filtered list, don't force-switch; just show first
  if (r.activeFile && folderOf(r.activeFile) === state.folder && !q) {
    sel.value = r.activeFile;
  }
}

function updateBrowseChrome() {
  const r = state.report;
  if (!r) return;
  const folder = state.folder === "." ? "(root)" : state.folder;
  const file = r.activeFile ? fileNameOf(r.activeFile) : "—";
  $("crumb").textContent = `${folder}  /  ${file}`;
  const inFolder = filesInFolder(r.files, state.folder).length;
  $("browseHint").textContent =
    `Showing ${inFolder} file${inFolder === 1 ? "" : "s"} in ${folder}` +
    (r.activeFile ? ` · open ${r.activeFile}` : "");
}

function renderAll() {
  const r = state.report;
  if (!r) return;
  $("controls").hidden = false;
  $("main").hidden = false;
  $("stats").hidden = false;

  const fileCount = r.fileCount || r.files?.length || 1;
  $("stats").innerHTML = `
    <div class="stat"><b>${r.workspaceSpans ?? r.totalSpans}</b><span>workspace spans</span></div>
    <div class="stat"><b>${r.totalSpans}</b><span>active file spans</span></div>
    <div class="stat"><b>${fileCount}</b><span>python files</span></div>
    <div class="stat"><b>${r.families.length}</b><span>families</span></div>
  `;

  const browse = $("browseBlock");
  if (r.kind === "directory" && (r.files || []).length > 1) {
    browse.hidden = false;
    if (r.activeFile) state.folder = folderOf(r.activeFile);
    fillFolderSelect();
    fillFileSelect();
    updateBrowseChrome();
  } else if (r.kind === "directory" && (r.files || []).length === 1) {
    browse.hidden = false;
    state.folder = folderOf(r.files[0].relPath);
    fillFolderSelect();
    fillFileSelect();
    updateBrowseChrome();
  } else {
    browse.hidden = true;
  }

  const pathSel = $("pathSelect");
  pathSel.innerHTML = (r.flowPaths || [])
    .map(
      (p) =>
        `<option value="${escapeAttr(p.id)}" ${p.id === state.pathId ? "selected" : ""}>${escapeHtml(p.label)}</option>`,
    )
    .join("");

  fillFnSelect();
  const active = r.activeFile || r.fileName || "";
  $("pathLine").textContent =
    (active ? `Active: ${active} · ` : "") +
    "Call path: " +
    (currentPath()?.steps.join(" → ") || "—");
  renderCallGraph();
  renderWaterfall();
  renderLegend();
  renderSequence();
  renderDetail();
  $("reportPanel").hidden = false;
  refreshMdPreview();
}

function fillFnSelect() {
  const byFn = tracesByFn();
  const path = currentPath();
  const steps = (path?.steps || []).filter((s) => byFn[s]);
  const fnSel = $("fnSelect");
  fnSel.innerHTML = steps
    .map(
      (s) =>
        `<option value="${escapeAttr(s)}" ${s === state.focusFn ? "selected" : ""}>${escapeHtml(s)} (${byFn[s].spanCount})</option>`,
    )
    .join("");
  if (steps.length && !steps.includes(state.focusFn)) {
    state.focusFn = steps[0];
    fnSel.value = state.focusFn;
  }
}

function renderCallGraph() {
  const r = state.report;
  const path = currentPath();
  const highlight = new Set(path?.steps || []);
  const pathEdges = new Set();
  const steps = path?.steps || [];
  for (let i = 0; i < steps.length - 1; i++) {
    pathEdges.add(`${steps[i]}→${steps[i + 1]}`);
  }

  const nodes = (r.callGraph.nodes || []).filter(
    (n) =>
      highlight.has(n.id) ||
      r.callGraph.edges.some(
        (e) =>
          (e.from === n.id || e.to === n.id) &&
          (highlight.has(e.from) || highlight.has(e.to)),
      ),
  );
  const ids = new Set(nodes.map((n) => n.id));
  const edges = (r.callGraph.edges || []).filter(
    (e) => ids.has(e.from) && ids.has(e.to),
  );

  // simple layered layout by path order, others to the side
  const rank = {};
  steps.forEach((s, i) => {
    if (ids.has(s)) rank[s] = i;
  });
  let side = 0;
  for (const n of nodes) {
    if (rank[n.id] == null) {
      rank[n.id] = steps.length + Math.floor(side / 2);
      side++;
    }
  }
  const byRank = {};
  for (const n of nodes) {
    const rk = rank[n.id];
    (byRank[rk] ||= []).push(n);
  }

  const nodeW = 150;
  const nodeH = 44;
  const rankGap = 70;
  const nodeGap = 24;
  const pad = 16;
  let maxY = 0;
  const pos = {};
  const ranks = Object.keys(byRank)
    .map(Number)
    .sort((a, b) => a - b);
  ranks.forEach((rk, ri) => {
    const col = byRank[rk];
    col.forEach((n, oi) => {
      const x = pad + ri * (nodeW + rankGap);
      const y = pad + oi * (nodeH + nodeGap);
      pos[n.id] = { x, y };
      maxY = Math.max(maxY, y + nodeH);
    });
  });
  const width = pad * 2 + Math.max(1, ranks.length) * (nodeW + rankGap) - rankGap;
  const height = maxY + pad;

  const lines = edges
    .map((e) => {
      const a = pos[e.from];
      const b = pos[e.to];
      if (!a || !b) return "";
      const on = pathEdges.has(`${e.from}→${e.to}`);
      return `<line x1="${a.x + nodeW}" y1="${a.y + nodeH / 2}" x2="${b.x}" y2="${b.y + nodeH / 2}"
        stroke="${on ? "#5b9fd4" : "#3a4254"}" stroke-width="${on ? 2.5 : 1}" opacity="${on ? 1 : 0.55}" />`;
    })
    .join("");

  const rects = nodes
    .map((n) => {
      const p = pos[n.id];
      const active = highlight.has(n.id);
      const label = n.id.length > 20 ? n.id.slice(0, 18) + "…" : n.id;
      return `<g transform="translate(${p.x},${p.y})" class="span-hit" data-fn="${escapeAttr(n.id)}">
        <rect width="${nodeW}" height="${nodeH}" rx="4"
          fill="${active ? "#2a4a66" : "#1a1d26"}"
          stroke="${active ? "#5b9fd4" : "#3a4254"}" stroke-width="${active ? 2 : 1}" />
        <text x="8" y="18" fill="#e8eaef" font-size="11" font-family="ui-monospace,monospace">${escapeHtml(label)}</text>
        <text x="8" y="34" fill="#9aa3b5" font-size="10" font-family="ui-monospace,monospace">${n.spanCount} branches · L${n.line}</text>
      </g>`;
    })
    .join("");

  $("callGraph").innerHTML = `<svg width="100%" viewBox="0 0 ${width} ${height}">${lines}${rects}</svg>`;
  $("callGraph").querySelectorAll("[data-fn]").forEach((el) => {
    el.addEventListener("click", () => {
      const fn = el.getAttribute("data-fn");
      if (!tracesByFn()[fn]) return;
      state.focusFn = fn;
      state.view = "function";
      syncViewSeg();
      $("fnSelect").value = fn;
      renderWaterfall();
      renderSequence();
    });
  });
}

function renderWaterfall() {
  $("waterfallTitle").textContent =
    state.view === "path"
      ? "Distributed logic waterfall"
      : `Waterfall · ${state.focusFn}`;

  const wrap = $("waterfallWrap");
  const condPanel = $("condPanel");
  const showConds = state.labelWidth === "full";
  wrap.classList.toggle("with-conds", showConds);
  condPanel.hidden = !showConds;

  if (state.view === "path") {
    $("waterfall").innerHTML = pathWaterfallSvg();
  } else {
    const t = tracesByFn()[state.focusFn];
    $("waterfall").innerHTML = t
      ? functionWaterfallSvg(t)
      : `<p class="hint" style="padding:12px">No spans for this function.</p>`;
  }
  $("waterfall").querySelectorAll("[data-span]").forEach((el) => {
    el.addEventListener("click", () => {
      state.selectedId = el.getAttribute("data-span");
      renderDetail();
      renderSequence();
      renderWaterfall();
    });
  });
  if (showConds) renderCondPanel();
}

function spanText(s) {
  return (s.condition || s.label || "").trim();
}

function truncateLabel(text, max) {
  if (state.labelWidth === "full") return text;
  if (state.labelWidth === "wide") {
    return text.length > 72 ? text.slice(0, 70) + "…" : text;
  }
  return text.length > max ? text.slice(0, max - 2) + "…" : text;
}

function labelWidths() {
  if (state.labelWidth === "full") return { labelW: 420, chartW: 980, maxChars: 999 };
  if (state.labelWidth === "wide") return { labelW: 360, chartW: 920, maxChars: 72 };
  return { labelW: 230, chartW: 760, maxChars: 28 };
}

function functionWaterfallSvg(trace) {
  const { labelW, chartW, maxChars } = labelWidths();
  const rowH = 22;
  const padTop = 28;
  const padRight = 12;
  const minLine = trace.line;
  const maxLine = Math.max(trace.endLine, ...trace.spans.map((s) => s.endLine));
  const spanLines = Math.max(1, maxLine - minLine);
  const xScale = (line) =>
    labelW + ((line - minLine) / spanLines) * (chartW - labelW - padRight);
  const height = padTop + trace.spans.length * rowH + 12;

  const step = Math.max(5, Math.round(spanLines / 6 / 5) * 5);
  const ticks = [];
  for (let l = minLine; l <= maxLine; l += step) ticks.push(l);
  if (ticks[ticks.length - 1] !== maxLine) ticks.push(maxLine);

  const axis = ticks
    .map(
      (t) => `<g>
      <line x1="${xScale(t)}" y1="${padTop - 12}" x2="${xScale(t)}" y2="${height - 4}" stroke="#2c3342" stroke-dasharray="2 3"/>
      <text x="${xScale(t)}" y="14" text-anchor="middle" fill="#6b7385" font-size="9" font-family="ui-monospace,monospace">L${t}</text>
    </g>`,
    )
    .join("");

  const rows = trace.spans
    .map((s) => {
      const y = padTop + s.row * rowH;
      const x1 = xScale(s.line);
      const x2 = Math.max(xScale(s.endLine), x1 + 6);
      const selected = state.selectedId === s.id;
      const indent = 8 + s.depth * 10;
      const raw = (s.earlyExit ? "↩ " : "") + spanText(s);
      const label = truncateLabel(raw, maxChars);
      return `<g class="span-hit" data-span="${escapeAttr(s.id)}">
        <title>${escapeHtml(spanText(s))}</title>
        <rect x="0" y="${y}" width="${chartW}" height="${rowH}" fill="${selected ? "rgba(91,159,212,0.12)" : "transparent"}"/>
        <text x="${indent}" y="${y + 15}" fill="${s.earlyExit ? "#e8eaef" : "#9aa3b5"}" font-size="10" font-family="ui-monospace,monospace">${escapeHtml(label)}</text>
        <rect x="${x1}" y="${y + 4}" width="${x2 - x1}" height="${rowH - 8}" rx="3"
          fill="${familyColor(s.familyId)}" opacity="${selected ? 1 : 0.85}"
          stroke="${selected ? "#e8eaef" : "transparent"}" stroke-width="1.5"/>
      </g>`;
    })
    .join("");

  return `<svg width="100%" viewBox="0 0 ${chartW} ${height}">
    <line x1="${labelW}" y1="${padTop - 8}" x2="${chartW - padRight}" y2="${padTop - 8}" stroke="#3a4254"/>
    ${axis}${rows}
  </svg>`;
}

function pathWaterfallSvg() {
  const byFn = tracesByFn();
  const segments = (currentPath()?.steps || [])
    .map((fn) => byFn[fn])
    .filter(Boolean);
  if (!segments.length) {
    return `<p class="hint" style="padding:12px">No spans on this path.</p>`;
  }

  const { labelW, chartW, maxChars } = labelWidths();
  const rowH = 20;
  const serviceH = 26;
  const padRight = 12;

  const flats = [];
  const bands = [];
  let cursor = 0;
  let globalRow = 0;

  for (const seg of segments) {
    const weight = Math.max(seg.spanCount, 1);
    const t0 = cursor;
    const t1 = cursor + weight;
    const row0 = globalRow;
    globalRow += 1;
    const denom = Math.max(1, seg.endLine - seg.line);
    for (const s of seg.spans) {
      const localStart = (s.line - seg.line) / denom;
      const localEnd = (s.endLine - seg.line) / denom;
      flats.push({
        span: s,
        fn: seg.function,
        globalRow,
        t0: t0 + localStart * weight,
        t1: t0 + Math.max(localEnd, localStart + 0.02) * weight,
      });
      globalRow += 1;
    }
    bands.push({ name: seg.function, row0, rows: 1 + seg.spans.length, t0, t1 });
    cursor = t1 + 0.4;
  }

  const maxT = Math.max(cursor, 1);
  const height = 24 + globalRow * rowH + 8;
  const xScale = (t) => labelW + (t / maxT) * (chartW - labelW - padRight);

  const bandSvg = bands
    .map((b) => {
      const y = 24 + b.row0 * rowH;
      const h = b.rows * rowH;
      return `<g>
        <rect x="0" y="${y}" width="${chartW}" height="${h}" fill="#1a1d26" opacity="0.55"/>
        <rect x="${xScale(b.t0)}" y="${y + 3}" width="${Math.max(xScale(b.t1) - xScale(b.t0), 8)}"
          height="${serviceH - 6}" rx="3" fill="#5b9fd4" opacity="0.22"/>
        <text x="8" y="${y + 16}" fill="#e8eaef" font-size="11" font-weight="600" font-family="ui-monospace,monospace">${escapeHtml(b.name)}</text>
      </g>`;
    })
    .join("");

  const flatSvg = flats
    .map((f) => {
      const y = 24 + f.globalRow * rowH;
      const x1 = xScale(f.t0);
      const x2 = Math.max(xScale(f.t1), x1 + 5);
      const selected = state.selectedId === f.span.id;
      const raw = (f.span.earlyExit ? "↩ " : "") + spanText(f.span);
      const label = truncateLabel(raw, maxChars);
      return `<g class="span-hit" data-span="${escapeAttr(f.span.id)}">
        <title>${escapeHtml(spanText(f.span))}</title>
        <text x="${12 + f.span.depth * 8}" y="${y + 14}" fill="#9aa3b5" font-size="9" font-family="ui-monospace,monospace">${escapeHtml(label)}</text>
        <rect x="${x1}" y="${y + 3}" width="${x2 - x1}" height="${rowH - 6}" rx="2"
          fill="${familyColor(f.span.familyId)}" opacity="${selected ? 1 : 0.88}"
          stroke="${selected ? "#e8eaef" : "transparent"}" stroke-width="1.5"/>
      </g>`;
    })
    .join("");

  return `<svg width="100%" viewBox="0 0 ${chartW} ${height}">${bandSvg}${flatSvg}</svg>`;
}

function visibleSpansForCondPanel() {
  if (state.view === "function") {
    const t = tracesByFn()[state.focusFn];
    return (t?.spans || []).map((s) => ({ ...s, function: state.focusFn }));
  }
  return allPathSpans();
}

function renderCondPanel() {
  const spans = visibleSpansForCondPanel();
  const ol = $("condList");
  ol.innerHTML = spans
    .map((s, i) => {
      const active = s.id === state.selectedId ? "active" : "";
      return `<li class="${active}" data-span="${escapeAttr(s.id)}">
        <span class="meta">#${i + 1} · ${escapeHtml(s.function)} · L${s.line} · ${escapeHtml(s.kind)}${s.earlyExit ? " · ↩" : ""} · ${escapeHtml(s.family)}</span>
        <code>${escapeHtml(spanText(s))}</code>
      </li>`;
    })
    .join("");
  ol.querySelectorAll("[data-span]").forEach((el) => {
    el.addEventListener("click", () => {
      state.selectedId = el.getAttribute("data-span");
      renderDetail();
      renderSequence();
      renderWaterfall();
    });
  });
}

function buildMarkdownReport(report) {
  const lines = [];
  const src = report.activeFile || report.fileName || report.source;
  lines.push(`# Logical Branch Report`);
  lines.push("");
  lines.push("## Meta");
  lines.push("");
  lines.push(`- **source**: \`${report.source}\``);
  if (report.root) lines.push(`- **workspace_root**: \`${report.root}\``);
  if (report.activeFile) lines.push(`- **active_file**: \`${report.activeFile}\``);
  lines.push(`- **total_spans**: ${report.totalSpans || 0}`);
  lines.push(`- **families**: ${(report.families || []).length}`);
  lines.push(`- **entrypoints**: ${(report.entrypoints || []).length}`);
  const kinds = Object.entries(report.byKind || {})
    .sort((a, b) => a[0].localeCompare(b[0]))
    .map(([k, v]) => `${k}=${v}`)
    .join(", ");
  if (kinds) lines.push(`- **by_kind**: ${kinds}`);
  lines.push("");
  lines.push(
    "> Use family groups to cluster similar business decisions; use per-function sequences for evaluation order.",
  );
  lines.push("");

  if ((report.entrypoints || []).length) {
    lines.push("## Entrypoints");
    lines.push("");
    for (const ep of report.entrypoints) {
      lines.push(`- \`${ep.name}\` — ${ep.reason || ""} (L${ep.line ?? "?"})`);
    }
    lines.push("");
  }

  if ((report.flowPaths || []).length) {
    lines.push("## Orchestration paths");
    lines.push("");
    for (const p of report.flowPaths) {
      lines.push(
        `- **${p.label || p.id}**: ${(p.steps || []).map((s) => `\`${s}\``).join(" → ")}`,
      );
    }
    lines.push("");
  }

  lines.push("## Logical families (similar branches grouped)");
  lines.push("");
  const byFamily = {};
  for (const t of report.traces || []) {
    for (const s of t.spans || []) {
      const fid = s.familyId || "other";
      (byFamily[fid] ||= []).push({ ...s, function: t.function });
    }
  }
  const famMeta = Object.fromEntries(
    (report.families || []).map((f) => [f.id, f]),
  );
  const famIds = Object.keys(byFamily).sort(
    (a, b) => byFamily[b].length - byFamily[a].length,
  );
  for (const fid of famIds) {
    const items = byFamily[fid];
    const label = famMeta[fid]?.label || items[0]?.family || fid;
    lines.push(`### ${label} (${items.length})`);
    lines.push("");
    lines.push(`- **family_id**: \`${fid}\``);
    const funcs = [...new Set(items.map((i) => i.function))].sort();
    lines.push(`- **functions**: ${funcs.map((f) => `\`${f}\``).join(", ")}`);
    lines.push("- **sites**:");
    items
      .sort((a, b) => a.function.localeCompare(b.function) || a.line - b.line)
      .forEach((i) => {
        const exit = i.earlyExit ? " ↩" : "";
        const end =
          i.endLine && i.endLine !== i.line ? `–${i.endLine}` : "";
        lines.push(
          `  - \`${i.function}\` L${i.line}${end} · \`${i.kind}\`${exit}: \`${spanText(i)}\``,
        );
      });
    lines.push("");
  }

  lines.push("## Per-function evaluation order");
  lines.push("");
  for (const t of report.traces || []) {
    lines.push(
      `### \`${t.function}\` (L${t.line}–${t.endLine}, ${t.spanCount} spans, depth≤${t.maxDepth})`,
    );
    lines.push("");
    (t.spans || []).forEach((s, idx) => {
      const pad = "  ".repeat(s.depth || 0);
      const exit = s.earlyExit ? " ↩" : "";
      lines.push(
        `${idx + 1}. ${pad}\`${s.kind}\` L${s.line} [${s.familyId}]${exit}: \`${spanText(s)}\``,
      );
    });
    lines.push("");
  }

  lines.push("## Prompt hint for LLM grouping");
  lines.push("");
  lines.push(
    "Cluster the **family** sections into higher-level business decisions " +
      "(e.g. reprocess windowing, empty-data guards, sensor filters, feature flags). " +
      "Prefer merging families that share the same business intent even if predicates differ slightly. " +
      "Preserve evaluation order from the per-function sections when describing control flow.",
  );
  lines.push("");
  return lines.join("\n");
}

function refreshMdPreview() {
  if (!state.report) return;
  $("mdPreview").value = buildMarkdownReport(state.report);
  $("mdStatus").textContent = `Active file report · ${state.report.totalSpans || 0} spans · ${(state.report.families || []).length} families`;
}

function downloadText(filename, text) {
  const blob = new Blob([text], { type: "text/markdown;charset=utf-8" });
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = filename;
  a.click();
  URL.revokeObjectURL(url);
}

async function copyActiveMarkdown() {
  const md = buildMarkdownReport(state.report);
  try {
    await navigator.clipboard.writeText(md);
    $("mdStatus").textContent = "Copied active-file markdown to clipboard.";
  } catch {
    $("mdPreview").select();
    $("mdStatus").textContent = "Clipboard blocked — select & copy from the preview.";
  }
}

async function downloadWorkspaceMarkdown() {
  const root = state.report?.root || state.inputPath || $("pathInput").value.trim();
  if (!root) {
    $("mdStatus").textContent = "No workspace root — analyze a directory first.";
    return;
  }
  $("mdStatus").textContent = "Generating workspace markdown (can take a while)…";
  $("btnWorkspaceMd").disabled = true;
  try {
    const res = await fetch("/api/report", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ path: root, scope: "workspace" }),
    });
    const data = await res.json();
    if (!res.ok) {
      $("mdStatus").textContent = data.error || "Workspace report failed";
      return;
    }
    downloadText("logical-branches-workspace.md", data.markdown);
    $("mdStatus").textContent = "Downloaded workspace.md";
  } catch (e) {
    $("mdStatus").textContent = String(e);
  } finally {
    $("btnWorkspaceMd").disabled = false;
  }
}

function renderLegend() {
  const fams = (state.report?.families || []).slice(0, 12);
  $("legend").innerHTML = fams
    .map(
      (f) =>
        `<div class="legend-item"><span class="swatch" style="background:${familyColor(f.id)}"></span>${escapeHtml(f.label)} (${f.count})</div>`,
    )
    .join("");
}

function allPathSpans() {
  const byFn = tracesByFn();
  const out = [];
  for (const fn of currentPath()?.steps || []) {
    const t = byFn[fn];
    if (!t) continue;
    for (const s of t.spans) out.push({ ...s, function: fn });
  }
  return out;
}

function renderSequence() {
  const rows =
    state.view === "function"
      ? (tracesByFn()[state.focusFn]?.spans || []).map((s) => ({
          ...s,
          function: state.focusFn,
        }))
      : allPathSpans();
  const tbody = $("seqTable").querySelector("tbody");
  tbody.innerHTML = rows
    .slice(0, 200)
    .map((s, i) => {
      const sel = s.id === state.selectedId ? "selected" : "";
      return `<tr class="${sel} span-hit" data-span="${escapeAttr(s.id)}">
        <td class="num">${i + 1}</td>
        <td class="num">${s.line}</td>
        <td class="num">${s.depth}</td>
        <td>${escapeHtml(s.kind)}</td>
        <td>${escapeHtml(s.label.slice(0, 90))}</td>
        <td>${escapeHtml(s.family)}</td>
        <td>${s.earlyExit ? "yes" : "—"}</td>
      </tr>`;
    })
    .join("");
  tbody.querySelectorAll("[data-span]").forEach((el) => {
    el.addEventListener("click", () => {
      state.selectedId = el.getAttribute("data-span");
      renderDetail();
      renderSequence();
      renderWaterfall();
    });
  });
}

function findSpan(id) {
  if (!id || !state.report) return null;
  for (const t of state.report.traces) {
    const s = t.spans.find((x) => x.id === id);
    if (s) return { ...s, function: t.function };
  }
  return null;
}

function renderDetail() {
  const s = findSpan(state.selectedId);
  const panel = $("detail");
  if (!s) {
    panel.hidden = true;
    return;
  }
  panel.hidden = false;
  $("detailBody").innerHTML = `
    <div style="margin-bottom:8px">
      <strong>${escapeHtml(s.label)}</strong>
      ${s.earlyExit ? ' <span class="pill">early return</span>' : ""}
    </div>
    <div class="detail-grid">
      <div><div class="muted">Function</div><code>${escapeHtml(s.function)}</code></div>
      <div><div class="muted">Lines</div><code>L${s.line}${s.endLine !== s.line ? "–" + s.endLine : ""}</code></div>
      <div><div class="muted">Depth · kind</div>${s.depth} · ${escapeHtml(s.kind)}</div>
      <div><div class="muted">Family</div>${escapeHtml(s.family)}</div>
      <div style="grid-column: span 2"><div class="muted">Condition</div><code>${escapeHtml(s.condition || s.label)}</code></div>
    </div>
    ${s.code ? `<p style="margin:10px 0 0;font-size:12px;color:var(--muted)"><code>${escapeHtml(s.code)}</code></p>` : ""}
  `;
}

function syncViewSeg() {
  $("viewSeg").querySelectorAll("button").forEach((b) => {
    b.classList.toggle("active", b.dataset.view === state.view);
  });
}

function escapeHtml(s) {
  return String(s)
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;");
}
function escapeAttr(s) {
  return escapeHtml(s).replace(/'/g, "&#39;");
}

function wire() {
  $("analyzeBtn").addEventListener("click", () => analyze());
  $("pathInput").addEventListener("keydown", (e) => {
    if (e.key === "Enter") analyze();
  });
  $("folderSelect").addEventListener("change", (e) => {
    onFolderChange(e.target.value);
  });
  $("fileSelect").addEventListener("change", (e) => {
    if (e.target.value) switchFile(e.target.value);
  });
  $("fileFilter").addEventListener("input", (e) => {
    state.fileFilter = e.target.value;
    fillFileSelect();
  });
  $("pathSelect").addEventListener("change", (e) => {
    state.pathId = e.target.value;
    const p = currentPath();
    if (p?.steps?.[0]) state.focusFn = p.steps[0];
    state.selectedId = null;
    fillFnSelect();
    const active = state.report?.activeFile || state.report?.fileName || "";
    $("pathLine").textContent =
      (active ? `Active: ${active} · ` : "") +
      "Call path: " +
      (p?.steps.join(" → ") || "—");
    renderCallGraph();
    renderWaterfall();
    renderSequence();
    renderDetail();
  });
  $("fnSelect").addEventListener("change", (e) => {
    state.focusFn = e.target.value;
    state.view = "function";
    syncViewSeg();
    renderWaterfall();
    renderSequence();
  });
  $("viewSeg").addEventListener("click", (e) => {
    const btn = e.target.closest("button[data-view]");
    if (!btn) return;
    state.view = btn.dataset.view;
    syncViewSeg();
    renderWaterfall();
    renderSequence();
  });
  $("labelWidthSeg").addEventListener("click", (e) => {
    const btn = e.target.closest("button[data-width]");
    if (!btn) return;
    state.labelWidth = btn.dataset.width;
    $("labelWidthSeg").querySelectorAll("button").forEach((b) => {
      b.classList.toggle("active", b.dataset.width === state.labelWidth);
    });
    renderWaterfall();
  });
  $("btnCopyMd").addEventListener("click", () => copyActiveMarkdown());
  $("btnDlMd").addEventListener("click", () => {
    const name = (state.report?.activeFile || state.report?.fileName || "active")
      .replace(/\//g, "__")
      .replace(/\.py$/, "");
    downloadText(`logical-branches-${name}.md`, buildMarkdownReport(state.report));
    $("mdStatus").textContent = "Downloaded active-file markdown.";
  });
  $("btnWorkspaceMd").addEventListener("click", () => downloadWorkspaceMarkdown());

  const defaultPath = new URLSearchParams(location.search).get("path") || "";
  if (defaultPath) {
    $("pathInput").value = defaultPath;
    analyze();
  }
}

wire();
