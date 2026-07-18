/**
 * Chat webview script. Runs in the webview, not the extension host.
 *
 * `renderMarkdown` / `escapeHtml` come from markdown.ts, loaded as a separate
 * script tag before this one (module "none" makes them globals).
 *
 * Self-contained on purpose: no imports/exports, so tsc emits a plain browser
 * script rather than CommonJS `require` calls that would throw here.
 *
 * Design: a turn is the unit, not a message. Everything one agent does in a turn
 * hangs off a single coloured rail, so a handoff between agents is visible as a
 * shape. Prose gets the UI font; machine events (commands, file writes,
 * reasoning) get the editor's mono — these agents are CLIs underneath, and that
 * distinction is the honest one.
 */

declare function acquireVsCodeApi(): { postMessage(msg: unknown): void };

type EventKind =
  | "session"
  | "text"
  | "reasoning"
  | "shell"
  | "tool"
  | "file_change"
  | "result"
  | "error"
  | "other";

interface AgentEvent {
  kind: EventKind;
  text: string;
}

// --- view -------------------------------------------------------------------

interface ModelInfo {
  label: string;
  description?: string;
  efforts?: string[];
  defaultEffort?: string;
}

interface Capabilities {
  model?: string;
  models: string[];
  modelInfo?: Record<string, ModelInfo>;
  commands: string[];
  skills: string[];
  subagents: string[];
}

interface Control {
  id: string;
  label: string;
  values: string[];
  blank: string;
  editable?: boolean;
  widget?: "menu" | "dots";
  descriptions?: Record<string, string>;
  labels?: Record<string, string>;
  icon?: string;
  defaultValue?: string;
}

/** Show the human name; keep the flag value for the command line. */
function nameOf(c: Control, value: string): string {
  return c.labels?.[value] ?? value;
}

interface Attachment {
  path: string;
  name: string;
}

/**
 * Brand colours by file type.
 *
 * Not the real logos: the CSP blocks external images, there's no asset pipeline
 * here, and VS Code doesn't expose its file-icon theme to webviews — so real
 * marks would mean vendoring trademarked SVGs. A brand-coloured extension badge
 * is recognisable at a glance and costs nothing, which is what most file trees
 * settle on too.
 */
const FILE_COLORS: Record<string, string> = {
  py: "#3776ab",
  ipynb: "#f37626",
  ts: "#3178c6",
  tsx: "#3178c6",
  js: "#f7df1e",
  jsx: "#f7df1e",
  mjs: "#f7df1e",
  json: "#8a8a8a",
  md: "#519aba",
  css: "#563d7c",
  scss: "#c6538c",
  html: "#e34c26",
  yml: "#cb171e",
  yaml: "#cb171e",
  toml: "#9c4221",
  sh: "#4eaa25",
  bash: "#4eaa25",
  sql: "#336791",
  rs: "#dea584",
  go: "#00add8",
  prisma: "#5a67d8",
  lock: "#8a8a8a",
  env: "#edd612",
  svg: "#ffb13b",
  png: "#a074c4",
  jpg: "#a074c4",
};

/** The full extension — truncating here would miss `prisma` in the colour map. */
function extOf(name: string): string {
  const dot = name.lastIndexOf(".");
  // Dotfiles (.env, .gitignore) have no extension — the name IS the type.
  const ext = dot > 0 ? name.slice(dot + 1) : name.replace(/^\./, "");
  return ext.toLowerCase();
}

/** Badges are ~4 characters wide; the lookup uses the full extension. */
function badgeText(ext: string): string {
  return ext.slice(0, 4);
}

const vscode = acquireVsCodeApi();
const log = document.getElementById("log") as HTMLElement;
const input = document.getElementById("input") as HTMLTextAreaElement;
const sendBtn = document.getElementById("send") as HTMLButtonElement;
const agentSel = document.getElementById("agent") as HTMLSelectElement;
const composer = document.getElementById("composer") as HTMLElement;
const palette = document.getElementById("palette") as HTMLElement;
const controlsBar = document.getElementById("controls") as HTMLElement;
const menu = document.getElementById("menu") as HTMLElement;
const attachBtn = document.getElementById("attach") as HTMLButtonElement;
const slashBtn = document.getElementById("slash") as HTMLButtonElement;
const filesBar = document.getElementById("files") as HTMLElement;

/** Files attached to the next message, by path (paths are what agents read). */
let attached: Attachment[] = [];

const LABELS: Record<string, string> = { "claude-code": "Claude Code", codex: "Codex" };

let turn: HTMLElement | null = null;
let streaming = false;

/** Populated from each CLI's own session-init event — never hardcoded. */
const caps: Record<string, Capabilities> = {};
/** From .agents/skills/ — the shared layer, offered for BOTH agents. */
let projectSkills: { name: string; description: string }[] = [];
const controls: Record<string, Control[]> = {};
/** Chosen values, per agent, so switching agents doesn't lose your settings. */
const chosen: Record<string, Record<string, string>> = {};

function currentCaps(): Capabilities {
  return caps[agentSel.value] ?? { models: [], commands: [], skills: [], subagents: [] };
}

function scroll(): void {
  log.scrollTop = log.scrollHeight;
}

function setSendMode(running: boolean): void {
  sendBtn.disabled = false;
  sendBtn.classList.toggle("stop", running);
  sendBtn.textContent = running ? "■" : "↑";
  sendBtn.title = running ? "Stop this turn" : "Send (Enter)";
  sendBtn.setAttribute("aria-label", running ? "Stop" : "Send");
}

function clearEmptyState(): void {
  log.querySelector(".empty")?.remove();
}

function startTurn(agent: string): HTMLElement {
  clearEmptyState();
  const el = document.createElement("section");
  el.className = "turn streaming";
  el.dataset.agent = agent;

  const head = document.createElement("header");
  head.innerHTML = `<span class="who">${escapeHtml(LABELS[agent] ?? agent)}</span>
    <span class="status">working…</span>`;
  el.appendChild(head);

  log.appendChild(el);
  scroll();
  return el;
}

/** Machine events read as terminal lines; prose reads as prose. */
const MARKERS: Partial<Record<EventKind, string>> = {
  shell: "$",
  tool: "▸",
  file_change: "±",
  reasoning: "…",
};

/**
 * Consecutive activity collects into one collapsible group.
 *
 * A turn can be forty `Read`/`grep` lines around two paragraphs of answer. Left
 * flat they read as one wall and the answer is lost in its own footnotes. The
 * group stays open while the agent works — watching it is the point — and folds
 * itself away the moment prose arrives, so the answer stands alone.
 */
function activityGroup(): HTMLDetailsElement {
  const last = turn!.lastElementChild;
  if (last instanceof HTMLDetailsElement && last.classList.contains("activity")) {
    return last;
  }
  const group = document.createElement("details");
  group.className = "activity";
  group.open = true;
  const summary = document.createElement("summary");
  summary.innerHTML = `<span class="steps"></span>`;
  group.appendChild(summary);
  turn!.appendChild(group);
  return group;
}

function countSteps(group: HTMLDetailsElement): void {
  const n = group.querySelectorAll(".event").length;
  const last = group.querySelector(".event:last-of-type .body")?.textContent ?? "";
  const el = group.querySelector(".steps") as HTMLElement;
  el.textContent = `${n} ${n === 1 ? "step" : "steps"}`;
  // The last action doubles as live progress while it's still working.
  el.title = last;
}

function addProse(html: string): void {
  // The answer arrived: fold the working away behind it.
  const last = turn!.lastElementChild;
  if (last instanceof HTMLDetailsElement && last.classList.contains("activity")) {
    last.open = false;
  }
  const el = document.createElement("div");
  el.className = "prose";
  el.innerHTML = html;
  turn!.appendChild(el);
}

function addEvent(event: AgentEvent): void {
  if (!turn || !event.text) {
    return;
  }
  // The session id is plumbing — we keep it to resume the conversation, it was
  // never something the agent said.
  if (event.kind === "session") {
    return;
  }
  // Claude sends its answer as `assistant` and then repeats it verbatim in
  // `result`, so rendering both duplicates the reply. But a local slash command
  // (/context, /model) answers ONLY in `result` — so show it just when the turn
  // produced no prose of its own.
  if (event.kind === "result") {
    if (turn.querySelector(".prose")) {
      return;
    }
    addProse(renderMarkdown(event.text));
    scroll();
    return;
  }
  if (event.kind === "text") {
    addProse(renderMarkdown(event.text));
    scroll();
    return;
  }

  // Errors are not working — they're the outcome. They never hide in a group.
  if (event.kind === "error") {
    const el = document.createElement("div");
    el.className = "event error";
    el.innerHTML = `<span class="marker">!</span><span class="body">${escapeHtml(event.text)}</span>`;
    turn.appendChild(el);
    scroll();
    return;
  }

  // One line each; click to see the whole thing.
  const el = document.createElement("div");
  el.className = `event ${event.kind}`;
  const marker = MARKERS[event.kind];
  el.innerHTML =
    (marker ? `<span class="marker">${marker}</span>` : "") +
    `<span class="body">${escapeHtml(event.text)}</span>`;
  el.title = "Click to expand";
  el.addEventListener("click", () => {
    const open = el.classList.toggle("open");
    el.title = open ? "Click to collapse" : "Click to expand";
  });

  const group = activityGroup();
  group.appendChild(el);
  countSteps(group);
  scroll();
}

function addUserMessage(text: string, files: Attachment[] = []): void {
  clearEmptyState();
  const el = document.createElement("div");
  el.className = "you";

  const body = document.createElement("span");
  body.className = "you-text";
  body.textContent = text;
  if (files.length) {
    // Show what you attached as the same cards you saw in the box.
    const strip = document.createElement("span");
    strip.className = "you-files";
    for (const f of files) {
      const chip = document.createElement("span");
      chip.className = "file mini";
      chip.title = f.path;
      const badge = document.createElement("span");
      badge.className = "ext";
      badge.textContent = badgeText(extOf(f.name));
      const color = FILE_COLORS[extOf(f.name)];
      if (color) {
        badge.style.setProperty("--c", color);
      }
      chip.appendChild(badge);
      const n = document.createElement("span");
      n.className = "fname";
      n.textContent = f.name;
      chip.appendChild(n);
      strip.appendChild(chip);
    }
    body.appendChild(strip);
  }
  el.appendChild(body);

  const edit = document.createElement("button");
  edit.type = "button";
  edit.className = "edit";
  edit.textContent = "Edit";
  // Honest wording: the agent's session already contains the original, so this
  // reopens the prompt to send again — it can't unsay what was said.
  edit.title = "Put this back in the box to change and send again";
  edit.setAttribute("aria-label", `Edit message: ${text.slice(0, 40)}`);
  edit.addEventListener("click", () => {
    input.value = text;
    input.focus();
    input.setSelectionRange(text.length, text.length);
    // Let the auto-grow re-measure for the restored text.
    input.dispatchEvent(new Event("input"));
  });
  el.appendChild(edit);

  log.appendChild(el);
  scroll();
}

// --- controls, rendered from what the driver declares ------------------------
// Each agent's driver says what it supports, so Codex never shows a subagent
// picker and Claude never shows a sandbox one. A control with no values and no
// free text is dropped entirely rather than rendered dead.

function pick(agent: string, id: string): string | undefined {
  return chosen[agent]?.[id] || undefined;
}

// --- attachments ------------------------------------------------------------

function renderFiles(): void {
  filesBar.textContent = "";
  for (const f of attached) {
    const ext = extOf(f.name);
    const card = document.createElement("div");
    card.className = "file";
    card.title = f.path;

    const badge = document.createElement("span");
    badge.className = "ext";
    badge.textContent = badgeText(ext);
    const color = FILE_COLORS[ext];
    if (color) {
      badge.style.setProperty("--c", color);
    }
    card.appendChild(badge);

    const name = document.createElement("span");
    name.className = "fname";
    name.textContent = f.name;
    card.appendChild(name);

    const x = document.createElement("button");
    x.type = "button";
    x.className = "x";
    x.textContent = "×";
    x.title = `Remove ${f.name}`;
    x.setAttribute("aria-label", `Remove ${f.name}`);
    x.addEventListener("click", () => {
      attached = attached.filter((a) => a.path !== f.path);
      renderFiles();
    });
    card.appendChild(x);

    filesBar.appendChild(card);
  }
}

function addFiles(files: Attachment[]): void {
  for (const f of files) {
    if (!attached.some((a) => a.path === f.path)) {
      attached.push(f);
    }
  }
  renderFiles();
  input.focus();
}

function set(agent: string, id: string, value: string | undefined): void {
  chosen[agent] = chosen[agent] ?? {};
  if (value) {
    chosen[agent][id] = value;
  } else {
    delete chosen[agent][id];
  }
  renderControls();
}

// --- popup menu -------------------------------------------------------------

function closeMenu(): void {
  menu.hidden = true;
  menu.textContent = "";
}

/** A chip's menu: the options, each with what it actually does. */
function openMenu(anchor: HTMLElement, c: Control, agent: string): void {
  closeMenu();
  const current = pick(agent, c.id);

  const fallback = c.defaultValue ? nameOf(c, c.defaultValue) : undefined;
  const rows: { value: string; label: string; desc?: string }[] = [
    { value: "", label: fallback ? `Default (${fallback})` : "Default" },
    ...c.values.map((v) => ({ value: v, label: nameOf(c, v), desc: c.descriptions?.[v] })),
  ];
  if (c.editable) {
    rows.push({ value: "__custom__", label: "Custom…", desc: "Type a name this CLI accepts" });
  }

  for (const r of rows) {
    const row = document.createElement("button");
    row.type = "button";
    row.className = "row" + (r.value === (current ?? "") ? " on" : "");
    row.setAttribute("role", "menuitem");
    row.innerHTML =
      `<span class="label">${escapeHtml(r.label)}</span>` +
      (r.desc ? `<span class="desc">${escapeHtml(r.desc)}</span>` : "");
    row.addEventListener("click", () => {
      closeMenu();
      if (r.value === "__custom__") {
        const typed = window.prompt(`${c.label} for ${LABELS[agent] ?? agent}`)?.trim();
        set(agent, c.id, typed || undefined);
        return;
      }
      set(agent, c.id, r.value || undefined);
    });
    menu.appendChild(row);
  }

  menu.hidden = false;
  // Sit above the chip that opened it, clamped inside the panel.
  const box = anchor.getBoundingClientRect();
  const width = menu.offsetWidth;
  const left = Math.max(8, Math.min(box.left, window.innerWidth - width - 8));
  menu.style.left = `${left}px`;
  menu.style.bottom = `${window.innerHeight - box.top + 6}px`;
}

// --- controls ---------------------------------------------------------------

function chipFor(c: Control, agent: string): HTMLElement {
  const current = pick(agent, c.id);
  const chip = document.createElement("button");
  chip.type = "button";
  chip.className = "chip" + (current ? " set" : "");
  chip.title = c.label;
  const shown = current ?? c.defaultValue;
  chip.innerHTML =
    (c.icon ? `<span class="glyph">${c.icon}</span>` : "") +
    `<span>${escapeHtml(shown ? nameOf(c, shown) : c.blank)}</span>`;
  chip.addEventListener("click", (e) => {
    e.stopPropagation();
    openMenu(chip, c, agent);
  });
  return chip;
}

/**
 * Effort: one button that steps up the scale, wrapping at the top.
 *
 * The values are an ordered scale, so the dots show where you sit on it — but
 * they're a readout, not five targets. Raising effort is one repeated click in
 * the same place, which is faster than aiming at a specific dot, and the wrap
 * means you can always get back down without a second control.
 */
function dotsFor(c: Control, agent: string): HTMLElement {
  const current = pick(agent, c.id);
  const index = current ? c.values.indexOf(current) : -1;
  const next = c.values[(index + 1) % c.values.length];

  const btn = document.createElement("button");
  btn.type = "button";
  btn.className = "dots" + (current ? " set" : "");
  btn.setAttribute("aria-label", `${c.label}: ${current ?? "default"}. Click for ${next}.`);
  btn.title = current
    ? `${c.label}: ${nameOf(c, current)} — click for ${nameOf(c, next)}`
    : `${c.label}: the CLI's default — click for ${nameOf(c, next)}`;

  // Name it: bare dots don't say what they control.
  const label = document.createElement("span");
  label.className = "dots-label";
  label.textContent = current ? `${c.label} (${nameOf(c, current)})` : c.label;
  btn.appendChild(label);

  for (let i = 0; i < c.values.length; i++) {
    const dot = document.createElement("span");
    dot.className = "dot" + (index >= 0 && i <= index ? " on" : "");
    dot.setAttribute("aria-hidden", "true");
    btn.appendChild(dot);
  }

  btn.addEventListener("click", () => set(agent, c.id, next));
  return btn;
}

/**
 * Effort belongs to the model, not the agent.
 *
 * Codex's scales differ per model — `ultra` exists on Terra and nowhere else —
 * so offering one fixed list would let you pick a level the chosen model would
 * reject. Narrow it to whatever the selected model actually supports.
 */
function scopeToModel(c: Control, agent: string): Control {
  if (c.id !== "effort") {
    return c;
  }
  const model = pick(agent, "model") ?? caps[agent]?.model;
  const efforts = model ? caps[agent]?.modelInfo?.[model]?.efforts : undefined;
  return efforts?.length ? { ...c, values: efforts } : c;
}

function renderControls(): void {
  const agent = agentSel.value;
  controlsBar.textContent = "";

  for (const raw of controls[agent] ?? []) {
    const c = scopeToModel(raw, agent);
    if (!c.values.length && !c.editable) {
      continue;
    }
    // A level the new model doesn't have would be sent and rejected.
    const current = pick(agent, c.id);
    if (current && c.values.length && !c.values.includes(current) && !c.editable) {
      delete chosen[agent]?.[c.id];
    }
    controlsBar.appendChild(c.widget === "dots" ? dotsFor(c, agent) : chipFor(c, agent));
  }
}

function refreshControls(): void {
  renderControls();
}

// --- the palette ------------------------------------------------------------
// Two triggers, one menu:
//   "/" at the start  -> skills and slash commands (both run by being sent as
//                        the prompt, so one list covers both)
//   "@" anywhere      -> a file, so you can point at one mid-sentence
//
// "/" only counts at position 0: a slash command IS the prompt, so "fix /foo"
// isn't one and shouldn't offer to become one.

let paletteIndex = 0;
/** Filled by the extension as you type after "@"; searching is its job. */
let fileHits: Attachment[] = [];

interface Token {
  trigger: "/" | "@";
  term: string;
  /** Index of the trigger character in the input. */
  at: number;
}

function activeToken(): Token | null {
  const upto = input.value.slice(0, input.selectionStart ?? input.value.length);
  if (/^\/[^\s]*$/.test(upto)) {
    return { trigger: "/", term: upto.slice(1), at: 0 };
  }
  const m = upto.match(/(?:^|\s)@([^\s]*)$/);
  if (m) {
    return { trigger: "@", term: m[1], at: upto.length - m[1].length - 1 };
  }
  return null;
}

function paletteMatches(): { name: string; kind: string; hint?: string; insert: string }[] {
  const token = activeToken();
  if (!token) {
    return [];
  }
  const term = token.term.toLowerCase();

  if (token.trigger === "@") {
    return fileHits
      .slice(0, 8)
      .map((f) => ({ name: f.name, kind: extOf(f.name) || "file", hint: f.path, insert: `@${f.path} ` }));
  }

  const c = currentCaps();
  // Project skills first: they're yours, they're the shared layer, and they work
  // on BOTH agents. Everything below is whatever that one CLI happens to ship.
  const all = [
    ...projectSkills.map((s) => ({ name: s.name, kind: "project", hint: s.description })),
    ...c.skills
      .filter((name) => !projectSkills.some((s) => s.name === name))
      .map((name) => ({ name, kind: "skill", hint: undefined as string | undefined })),
    ...c.commands.map((name) => ({ name, kind: "command", hint: undefined as string | undefined })),
  ];
  return all
    .filter((e) => e.name.toLowerCase().includes(term))
    .slice(0, 8)
    .map((e) => ({ ...e, insert: `/${e.name} ` }));
}

function renderPalette(): void {
  const matches = paletteMatches();
  if (!matches.length) {
    palette.hidden = true;
    return;
  }
  paletteIndex = Math.min(paletteIndex, matches.length - 1);
  palette.textContent = "";
  matches.forEach((m, i) => {
    const row = document.createElement("div");
    row.className = "row" + (i === paletteIndex ? " on" : "");
    row.setAttribute("role", "option");
    row.innerHTML =
      `<span class="name">/${escapeHtml(m.name)}</span>` +
      (m.hint ? `<span class="hint">${escapeHtml(m.hint)}</span>` : "") +
      `<span class="kind">${m.kind}</span>`;
    row.addEventListener("mousedown", (e) => {
      e.preventDefault();
      choosePalette(m.insert);
    });
    palette.appendChild(row);
  });
  palette.hidden = false;
}

/** Replace just the token being typed, so the rest of the sentence survives. */
function choosePalette(insert: string): void {
  const token = activeToken();
  if (!token) {
    return;
  }
  const caret = input.selectionStart ?? input.value.length;
  const before = input.value.slice(0, token.at);
  const after = input.value.slice(caret);

  // The insert carries a trailing space so you can keep typing — but not when
  // the rest of the sentence already begins with one.
  const text = /^\s/.test(after) ? insert.trimEnd() : insert;
  input.value = before + text + after;

  const pos = before.length + text.length;
  palette.hidden = true;
  input.focus();
  input.setSelectionRange(pos, pos);
  // Re-measure: a long path can push the box to a second line.
  input.dispatchEvent(new Event("input"));
}

function submit(): void {
  const text = input.value.trim();
  if ((!text && !attached.length) || streaming) {
    return;
  }
  palette.hidden = true;

  // Paths, not contents: both CLIs read files themselves, and a path costs a
  // handful of tokens where an inlined file costs thousands.
  const paths = attached.map((f) => f.path);
  const prompt = paths.length ? `Files: ${paths.join(", ")}\n\n${text}` : text;

  addUserMessage(text, attached);
  vscode.postMessage({ type: "send", text: prompt, opts: chosen[agentSel.value] ?? {} });

  input.value = "";
  input.style.height = "auto";
  attached = [];
  renderFiles();
}

/** While a turn runs, the same button stops it — a long turn must be escapable
 *  without closing the panel and orphaning the process. */
sendBtn.addEventListener("click", () => {
  if (streaming) {
    vscode.postMessage({ type: "stop" });
    sendBtn.disabled = true;
    return;
  }
  submit();
});

// `/` opens the same palette typing "/" does, for people who reach for the mouse.
slashBtn.addEventListener("click", () => {
  if (!input.value.startsWith("/")) {
    input.value = `/${input.value}`;
  }
  input.focus();
  paletteIndex = 0;
  renderPalette();
});

attachBtn.addEventListener("click", () => vscode.postMessage({ type: "attach" }));

// --- drag and drop ----------------------------------------------------------
//
// Two things matter here, and both are counter-intuitive:
//
// 1. preventDefault() on dragover must claim the WHOLE document. Bound to the
//    composer alone, VS Code's editor drop overlay wins first and opens the file
//    in a split editor — the webview never sees the drop at all.
//
// 2. Dropping from VS Code's own Explorer does NOT reach a webview: VS Code
//    handles that drag itself (microsoft/vscode#182449). Drops from the OS file
//    manager DO arrive, carrying `text/uri-list` with real file:// paths. So the
//    case that works is the opposite of the one you'd assume.

function dropUris(dt: DataTransfer | null): string[] {
  const list = dt?.getData("text/uri-list") ?? "";
  return list.split(/\r?\n/).filter((u) => u.trim() && !u.startsWith("#"));
}

document.addEventListener("dragover", (e) => {
  // Claims the drop for the webview and stops the split-editor takeover.
  e.preventDefault();
  if (e.dataTransfer) {
    e.dataTransfer.dropEffect = "copy";
  }
  composer.classList.add("dropping");
});

document.addEventListener("dragleave", (e) => {
  // relatedTarget is null when the pointer actually leaves the webview.
  if (!e.relatedTarget) {
    composer.classList.remove("dropping");
  }
});

document.addEventListener("drop", (e) => {
  e.preventDefault();
  composer.classList.remove("dropping");

  const uris = dropUris(e.dataTransfer);
  if (uris.length) {
    vscode.postMessage({ type: "dropped", uris });
    return;
  }
  // A drop with files but no URIs means the source gave us names without paths,
  // and a path is the only thing the agent can act on. Say so rather than drop
  // it on the floor.
  if (e.dataTransfer?.files.length) {
    vscode.postMessage({
      type: "warn",
      text: "Those files didn't come with a path, so the agent can't open them. Use + to pick them instead.",
    });
  }
});

// A menu should close when you look away from it.
document.addEventListener("click", (e) => {
  if (!menu.hidden && !menu.contains(e.target as Node)) {
    closeMenu();
  }
});

document.addEventListener("keydown", (e) => {
  if (e.key === "Escape" && !menu.hidden) {
    closeMenu();
  }
});

input.addEventListener("keydown", (e) => {
  if (!palette.hidden) {
    const matches = paletteMatches();
    if (e.key === "ArrowDown" || e.key === "ArrowUp") {
      e.preventDefault();
      paletteIndex =
        (paletteIndex + (e.key === "ArrowDown" ? 1 : -1) + matches.length) % matches.length;
      renderPalette();
      return;
    }
    if (e.key === "Tab" || (e.key === "Enter" && matches[paletteIndex])) {
      e.preventDefault();
      choosePalette(matches[paletteIndex].insert);
      return;
    }
    if (e.key === "Escape") {
      palette.hidden = true;
      return;
    }
  }
  if (e.key === "Enter" && !e.shiftKey) {
    e.preventDefault();
    submit();
  }
});

// Grow with the prompt instead of trapping long text in two lines.
input.addEventListener("input", () => {
  input.style.height = "auto";
  input.style.height = `${Math.min(input.scrollHeight, 220)}px`;
  paletteIndex = 0;

  // Only the extension can see the workspace, so "@" hands the search over and
  // the results come back asynchronously.
  const token = activeToken();
  if (token?.trigger === "@") {
    vscode.postMessage({ type: "findFiles", query: token.term });
  } else {
    fileHits = [];
  }
  renderPalette();
});

input.addEventListener("blur", () => {
  palette.hidden = true;
});

agentSel.addEventListener("change", () => {
  composer.dataset.agent = agentSel.value;
  refreshControls();
  vscode.postMessage({ type: "switchAgent", agent: agentSel.value });
});

log.addEventListener("click", (e) => {
  const btn = (e.target as HTMLElement).closest(".copy") as HTMLButtonElement | null;
  if (!btn) {
    return;
  }
  const code = btn.closest("figure")?.querySelector("code")?.textContent ?? "";
  navigator.clipboard.writeText(code).then(() => {
    btn.textContent = "Copied";
    setTimeout(() => (btn.textContent = "Copy"), 1200);
  });
});

window.addEventListener("message", (e: MessageEvent) => {
  const msg = e.data;
  if (msg.type === "turnStart") {
    streaming = true;
    setSendMode(true);
    turn = startTurn(msg.agent);
  } else if (msg.type === "turnEnd") {
    streaming = false;
    setSendMode(false);
    turn?.classList.remove("streaming");
    turn?.querySelector(".status")?.remove();
    turn = null;
  } else if (msg.type === "event") {
    addEvent(msg.event as AgentEvent);
  } else if (msg.type === "init") {
    agentSel.value = msg.agent;
    composer.dataset.agent = msg.agent;
  } else if (msg.type === "capabilities") {
    const existing = caps[msg.agent];
    // The model list arrives separately (a free `/model` probe); don't let the
    // init event's empty list clobber it.
    const models = msg.caps.models.length ? msg.caps.models : existing?.models ?? [];
    caps[msg.agent] = { ...msg.caps, models };
    controls[msg.agent] = (msg.controls as Control[]).map((c) =>
      c.id === "model" && models.length ? { ...c, values: models } : c
    );
    refreshControls();
  } else if (msg.type === "projectSkills") {
    projectSkills = msg.skills;
  } else if (msg.type === "attachments") {
    addFiles(msg.files as Attachment[]);
  } else if (msg.type === "fileResults") {
    // Results are async; ignore any that arrive after you stopped typing "@".
    if (activeToken()?.trigger === "@") {
      fileHits = msg.files as Attachment[];
      paletteIndex = 0;
      renderPalette();
    }
  } else if (msg.type === "models") {
    const existing = caps[msg.agent] ?? { commands: [], skills: [], subagents: [], models: [] };
    caps[msg.agent] = { ...existing, models: msg.models };
    controls[msg.agent] = (controls[msg.agent] ?? []).map((c) =>
      c.id === "model" ? { ...c, values: msg.models } : c
    );
    refreshControls();
  }
});
