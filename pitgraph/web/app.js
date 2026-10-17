// PitGraph web UI: chat, the "Why?" evidence subgraph, and the Memory panel.
// All text from the server is inserted with textContent, never as HTML.

const $ = (id) => document.getElementById(id);
const USER_ID = /^[A-Za-z0-9_]{1,64}$/;
const MAX_NEIGHBOURS = 8; // per evidence node in the Why? graph, to keep it readable

let sessionId = null;
let network = null;

// ---------- small helpers ----------

function el(tag, props = {}, ...children) {
  const node = document.createElement(tag);
  for (const [key, value] of Object.entries(props)) {
    if (key === "class") node.className = value;
    else if (key.startsWith("on")) node.addEventListener(key.slice(2), value);
    else node.setAttribute(key, value);
  }
  for (const child of children.flat()) {
    if (child == null || child === false) continue;
    node.append(child instanceof Node ? child : document.createTextNode(String(child)));
  }
  return node;
}

function stored(key, value) {
  // localStorage can be unavailable (private mode, blocked storage): never let it break the page.
  try {
    if (value === undefined) return localStorage.getItem(key);
    localStorage.setItem(key, value);
  } catch { return null; }
  return value;
}

const pct = (x) => `${Math.round((x ?? 0) * 100)}%`;
const chip = (text, kind = "") => el("span", { class: `chip ${kind}` }, text);

async function api(path, options = {}) {
  const response = await fetch(path, {
    headers: { "Content-Type": "application/json" },
    ...options,
  });
  const body = await response.json().catch(() => ({}));
  if (!response.ok) {
    const detail = typeof body.detail === "string" ? body.detail : "Invalid request.";
    throw new Error(detail);
  }
  return body;
}

function currentUser() {
  const user = $("user").value.trim();
  if (!USER_ID.test(user)) throw new Error("User ID: letters, digits and _ only (max 64).");
  return user;
}

// ---------- session ----------

function setSession(id) {
  sessionId = id;
  $("session-label").textContent = id ? `Session ${id}` : "New session";
}

function newSession() {
  setSession(null);
  $("messages").replaceChildren(el("p", { class: "empty" }, "New session. Earlier answers stay in memory."));
}

// ---------- chat ----------

function addMessage(node) {
  $("messages").append(node);
  node.scrollIntoView({ behavior: "smooth", block: "end" });
  return node;
}

// The handoff chain as the agents ran it, e.g. Planner → … → Critic (revise) → Analyst → …
function chainView(reviews) {
  const steps = ["Planner", "Retriever"];
  for (const review of reviews) steps.push("Analyst", `Critic (${review.verdict})`);
  steps.push("MemoryWriter");
  return el("div", { class: "chain" }, "Handoffs: ",
    steps.flatMap((step, i) => [i ? " → " : "", el("b", {}, step)]));
}

function answerView(data) {
  const chips = el("div", { class: "chips" },
    chip(`confidence ${pct(data.confidence)}`, data.confidence >= 0.7 ? "ok" : "warn"),
    chip(data.intent),
    data.circuits.map((c) => chip(c)),
    data.recovery.used && chip("recovery path: similar circuits", "warn"),
    data.used_similar_circuits && !data.recovery.used && chip("uses similar circuits", "warn"),
    data.procedure_reused && chip("reused a proven plan", "info"),
    data.recalled_insights.length > 0 && chip(`recalled ${data.recalled_insights.length} insight(s)`, "info"),
  );

  const parts = [
    el("div", { class: "verdict" }, data.verdict),
    el("div", { class: "reasoning" }, data.reasoning),
    chips,
    chainView(data.reviews),
  ];

  if (data.recovery.used) {
    parts.push(el("div", { class: "box" }, el("h4", {}, "Why the recovery path ran"), data.recovery.reason));
  }
  if (data.recalled_insights.length) {
    parts.push(el("div", { class: "box" }, el("h4", {}, "From your earlier sessions"),
      el("ul", { class: "plain" }, data.recalled_insights.map((i) =>
        el("li", {}, `${i.statement} (confirmed ${i.times_confirmed}×)`)))));
  }
  if (data.insights_written.length) {
    parts.push(el("div", { class: "box" }, el("h4", {}, "Saved to memory"),
      el("ul", { class: "plain" }, data.insights_written.map((i) =>
        el("li", {}, `${i.relation}: ${i.statement}`)))));
  }
  if (data.brief) {
    parts.push(el("div", { class: "box" }, el("h4", {}, "Strategy brief saved"),
      el("div", {}, `Primary: ${data.brief.primary_strategy}`),
      el("div", {}, `Fallback: ${data.brief.fallback_strategy}`),
      el("div", {}, `Safety-car window: ${data.brief.sc_window}`)));
  }

  parts.push(el("details", {},
    el("summary", {}, `${data.tool_calls.length} tool calls over MCP · Critic notes`),
    el("ul", { class: "plain" }, data.tool_calls.map((c) =>
      el("li", {}, `${c.tool}(${JSON.stringify(c.params)}) → ${c.ok ? `${c.rows} rows` : `error: ${c.error}`}`))),
    el("ul", { class: "plain" }, data.reviews.map((r) => el("li", {}, `Critic ${r.verdict}: ${r.note}`))),
  ));
  parts.push(el("div", { class: "chips" },
    el("button", { type: "button", onclick: () => showWhy(data.a_id) }, "Why?"),
    chip(`answer ${data.a_id}`)));
  return el("div", { class: "msg" }, parts);
}

async function ask(event) {
  event.preventDefault();
  const question = $("question").value.trim();
  if (!question) return;
  let user;
  try { user = currentUser(); } catch (error) { alert(error.message); return; }

  $("messages").querySelector(".empty")?.remove();
  addMessage(el("div", { class: "msg user" }, question));
  const pending = addMessage(el("div", { class: "msg" },
    el("div", { class: "chain" }, "Planner → Retriever → Analyst → Critic → MemoryWriter are working… (about a minute)")));
  $("send").disabled = true;
  $("question").value = "";

  try {
    const data = await api("/ask", {
      method: "POST",
      body: JSON.stringify({ user_id: user, question, session_id: sessionId }),
    });
    setSession(data.session_id);
    pending.replaceWith(answerView(data));
    showWhy(data.a_id);
    if (!$("memory").hidden) loadMemory();
  } catch (error) {
    pending.replaceChildren(el("div", { class: "error" }, error.message));
  } finally {
    $("send").disabled = false;
  }
}

// ---------- Why? panel ----------

const ID_PROPS = ["race_id", "entry_id", "circuit_id", "driver_id", "team_id", "stint_id",
  "pit_id", "n_id", "pattern", "year", "name"];
const NEIGHBOUR_PRIORITY = ["HELD_AT", "HAD_NEUTRALISATION", "USED_STRATEGY", "IN_RACE", "DROVE",
  "FOR_TEAM", "SIMILAR_TO", "FAVOURS", "IN_SEASON"];
const COLORS = {
  Answer: "#c8102e", Question: "#6b6b70", Insight: "#1f4f8a", QueryRun: "#9a9aa2",
  Race: "#1f7a4d", Circuit: "#9a5b00", Entry: "#7a3fb0", Other: "#5aa0a8",
};

function nodeKey(node) {
  const prop = ID_PROPS.find((p) => node[p] != null);
  return `${node.label}:${prop ? node[prop] : JSON.stringify(node)}`;
}

function nodeText(node) {
  switch (node.label) {
    case "Race": return `${node.name}\n${node.race_id}`;
    case "Circuit": return node.name;
    case "Entry": return `${node.entry_id}\nP${node.finish_pos} from P${node.grid}`;
    case "Stint": return `stint ${node.stint_no}: laps ${node.lap_start}-${node.lap_end}`;
    case "Neutralisation": return `${node.type} laps ${node.lap_start}-${node.lap_end}`;
    case "Strategy": return node.pattern;
    case "Season": return String(node.year);
    default: return node.name ?? node.label;
  }
}

function buildGraph(trace) {
  const nodes = new Map();
  const edges = [];
  const add = (id, label, group, title) => {
    if (!nodes.has(id)) nodes.set(id, { id, label, title, color: COLORS[group] ?? COLORS.Other, font: { color: "#888" } });
  };
  const link = (from, to, label) => edges.push({ from, to, label, arrows: "to", font: { size: 10, color: "#888" } });

  add("answer", `Answer\n${pct(trace.analysis.confidence)}`, "Answer", trace.analysis.verdict);
  if (trace.question) { add("question", "Question", "Question", trace.question.text); link("answer", "question", "ANSWERS"); }

  for (const item of trace.evidence) {
    const key = nodeKey(item.node);
    add(key, nodeText(item.node), item.node.label, item.evidence.why);
    link("answer", key, "USED");
    // A race links to every entry in it; only the cited entries matter here.
    const neighbours = item.neighbours.filter((nb) => !(item.node.label === "Race" && nb.type === "IN_RACE"));
    neighbours.sort((a, b) =>
      (NEIGHBOUR_PRIORITY.indexOf(a.type) + 1 || 99) - (NEIGHBOUR_PRIORITY.indexOf(b.type) + 1 || 99));
    for (const nb of neighbours.slice(0, MAX_NEIGHBOURS)) {
      const nbKey = nodeKey(nb.node);
      add(nbKey, nodeText(nb.node), nb.node.label, nb.node.label);
      link(key, nbKey, nb.type);
    }
  }
  trace.queries.forEach((q, i) => {
    add(`qr${i}`, q.tool, "QueryRun", `${q.params} → ${q.ok ? `${q.row_count} rows` : q.error}`);
    link("answer", `qr${i}`, "RAN");
  });
  for (const i of trace.recalled_insights) {
    add(i.i_id, "Insight (recalled)", "Insight", i.statement);
    link("answer", i.i_id, "RECALLED");
  }
  for (const i of trace.learned_insights) {
    add(i.i_id, "Insight (learned)", "Insight", i.statement);
    link(i.i_id, "answer", "DERIVED_FROM");
  }
  return { nodes: [...nodes.values()], edges };
}

// The Planner hands off its plan as JSON; show it as a readable line.
function handoffText(summary) {
  try {
    const plan = JSON.parse(summary);
    if (plan && Array.isArray(plan.tool_sequence)) {
      return `${plan.intent} for ${plan.circuits.join(", ") || "no circuit"}; tools: ${plan.tool_sequence.join(" → ") || "none"}. ${plan.reasoning}`;
    }
  } catch { /* not JSON: a plain summary */ }
  return summary;
}

function legend() {
  return el("div", { class: "legend" }, ["Answer", "Race", "Entry", "Circuit", "Insight", "QueryRun", "Other"].map((g) =>
    el("span", {}, el("i", { style: `background:${COLORS[g]}` }), g)));
}

async function showWhy(aId) {
  selectTab("why");
  const panel = $("why");
  panel.replaceChildren(el("p", { class: "empty" }, "Loading trace…"));
  let trace;
  try {
    trace = await api(`/trace/${encodeURIComponent(aId)}?user=${encodeURIComponent(currentUser())}`);
  } catch (error) {
    panel.replaceChildren(el("p", { class: "error" }, error.message));
    return;
  }

  const graphBox = el("div", { id: "graph" });
  panel.replaceChildren(
    el("h3", {}, trace.question ? trace.question.text : "Answer"),
    el("p", {}, trace.analysis.verdict),
    graphBox,
    legend(),
    el("h3", {}, "Handoff chain (Task nodes)"),
    el("ol", { class: "plain" }, trace.handoffs.map((h) => el("li", {}, `${h.from_agent} → ${h.to_agent}: ${handoffText(h.summary)}`))),
    el("h3", {}, "Evidence"),
    trace.evidence.length
      ? el("ul", { class: "plain" }, trace.evidence.map((e) => el("li", {}, `${e.evidence.ref_label} ${e.evidence.ref_id}: ${e.evidence.why}`)))
      : el("p", { class: "empty" }, "No race nodes cited (answer came from memory)."),
    el("h3", {}, "Critic"),
    el("ul", { class: "plain" }, trace.reviews.map((r) => el("li", {}, `${r.verdict}: ${r.note}`))),
  );

  if (!window.vis) {
    graphBox.replaceChildren(el("p", { class: "empty" }, "Graph library didn't load; details below."));
    return;
  }
  const data = buildGraph(trace);
  network?.destroy();
  network = new vis.Network(graphBox, data, {
    nodes: { shape: "dot", size: 12, font: { size: 12, multi: false } },
    edges: { color: { color: "#bbb" }, smooth: { type: "continuous" } },
    physics: { stabilization: { iterations: 150 }, barnesHut: { springLength: 110 } },
    interaction: { hover: true, tooltipDelay: 100 },
  });
}

// ---------- Memory panel ----------

async function loadMemory() {
  const panel = $("memory");
  let mem;
  try {
    mem = await api(`/memory?user=${encodeURIComponent(currentUser())}`);
  } catch (error) {
    panel.replaceChildren(el("p", { class: "error" }, error.message));
    return;
  }

  const counts = Object.entries(mem.node_counts);
  panel.replaceChildren(
    el("h3", {}, `Memory graph mem_${mem.user_id}`),
    counts.length
      ? el("div", { class: "chips" }, counts.map(([label, n]) => chip(`${label} ${n}`)))
      : el("p", { class: "empty" }, "Nothing yet: this user has no memory graph."),

    el("h3", {}, "Insights (semantic memory)"),
    mem.insights.length ? el("div", {}, mem.insights.map(insightView)) : el("p", { class: "empty" }, "No insights yet."),

    el("h3", {}, "Past answers (episodic memory)"),
    mem.answers.length
      ? el("div", {}, mem.answers.map((a) => el("button", { type: "button", class: "answer-row", onclick: () => showWhy(a.a_id) },
          `${a.question} → ${a.verdict.slice(0, 90)}… (${pct(a.confidence)})`)))
      : el("p", { class: "empty" }, "No answers yet."),

    el("h3", {}, "Saved strategy briefs (actions)"),
    mem.briefs.length
      ? el("ul", { class: "plain" }, mem.briefs.map((b) =>
          el("li", {}, `${b.content.circuits.join(", ")}: ${b.content.primary_strategy} (fallback ${b.content.fallback_strategy}; SC ${b.content.sc_window})`)))
      : el("p", { class: "empty" }, "None yet."),

    el("h3", {}, "Proven plans, shared by all users (procedural memory)"),
    mem.procedures.length
      ? el("table", {},
          el("tr", {}, el("th", {}, "Intent"), el("th", {}, "Tools"), el("th", {}, "Approved")),
          mem.procedures.map((p) => el("tr", {},
            el("td", {}, p.intent),
            el("td", {}, p.tool_sequence.replaceAll(",", " → "), p.fallbacks.length ? ` (fallback: ${p.fallbacks.join("; ")})` : ""),
            el("td", {}, `${p.successes} / ${p.successes + p.failures}`))))
      : el("p", { class: "empty" }, "None yet."),
  );
}

function insightView(i) {
  const retired = i.superseded_by.length > 0;
  const meta = [
    `${i.circuits.join(", ")}`,
    `confidence ${pct(i.confidence)}`,
    `confirmed ${i.times_confirmed}×`,
    retired && `superseded by ${i.superseded_by.join(", ")}`,
    i.conflicts_with.length && `conflicts with ${i.conflicts_with.join(", ")}`,
  ].filter(Boolean).join(" · ");
  return el("div", { class: `insight${retired ? " retired" : ""}` },
    el("div", {}, i.statement), el("div", { class: "meta" }, `${i.i_id} · ${meta}`));
}

// ---------- tabs and start-up ----------

function selectTab(name) {
  for (const tab of ["why", "memory"]) {
    $(tab).hidden = tab !== name;
    $(`tab-${tab}`).setAttribute("aria-selected", String(tab === name));
  }
  if (name === "memory") loadMemory();
}

$("tab-why").addEventListener("click", () => selectTab("why"));
$("tab-memory").addEventListener("click", () => selectTab("memory"));
$("new-session").addEventListener("click", newSession);
$("ask").addEventListener("submit", ask);
$("question").addEventListener("keydown", (e) => {
  if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); $("ask").requestSubmit(); }
});
$("user").addEventListener("change", () => {
  stored("pitgraph-user", $("user").value.trim());
  newSession();
  if (!$("memory").hidden) loadMemory();
});
$("user").value = stored("pitgraph-user") || "demo";
