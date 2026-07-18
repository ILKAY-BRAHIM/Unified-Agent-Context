/**
 * Subprocess drivers for Claude Code and Codex (D8).
 *
 * The extension has no model. A user turn is forwarded to the official CLI
 * spawned as a child process; the model runs there under the user's own OAuth
 * login. No API key is ever involved.
 *
 * This mirrors src/uac/agents.py — keep the two in step. Parsing is lenient on
 * purpose: both CLIs ship new event types constantly, and an unrecognised event
 * must degrade to noise rather than break the chat.
 */

import { execFile, spawn } from "child_process";
import * as fs from "fs";
import * as os from "os";
import * as path from "path";
import * as readline from "readline";

export type Agent = "claude-code" | "codex";

export type EventKind =
  | "session"
  | "text"
  | "reasoning"
  /** A shell command the agent ran. */
  | "shell"
  /** Any other tool: reading, searching, fetching, delegating. */
  | "tool"
  | "file_change"
  | "result"
  | "error"
  | "other";

export interface AgentEvent {
  kind: EventKind;
  text: string;
  raw?: unknown;
}

/**
 * One assistant message carries several content blocks at once — text next to
 * tool_use next to thinking — so a message maps to a list of events, not one.
 * Returning a single event silently dropped every tool call.
 */
function claudeBlocks(message: any): AgentEvent[] {
  const blocks = message?.content;
  if (typeof blocks === "string") {
    return blocks ? [{ kind: "text", text: blocks }] : [];
  }
  if (!Array.isArray(blocks)) {
    return [];
  }

  const events: AgentEvent[] = [];
  for (const b of blocks) {
    switch (b?.type) {
      case "text":
        if (b.text?.trim()) {
          events.push({ kind: "text", text: b.text, raw: b });
        }
        break;
      case "thinking":
        if (b.thinking?.trim()) {
          events.push({ kind: "reasoning", text: b.thinking, raw: b });
        }
        break;
      case "tool_use":
        events.push({ ...describeTool(b.name, b.input), raw: b });
        break;
      default:
        break;
    }
  }
  return events;
}

/**
 * The one argument worth showing for a tool call.
 *
 * A bare tool name is useless: two searches in a row both render as
 * "ToolSearch" and you can't tell what either looked for. The argument is the
 * information.
 */
function summarizeInput(input: any): string {
  if (!input || typeof input !== "object") {
    return "";
  }
  // Most-identifying key first; these cover both CLIs' tools and MCP servers.
  for (const key of ["query", "pattern", "command", "description", "url", "prompt", "name", "content"]) {
    const v = input[key];
    if (typeof v === "string" && v.trim()) {
      return v.trim();
    }
  }
  const first = Object.values(input).find((v) => typeof v === "string" && v.trim());
  return typeof first === "string" ? first.trim() : "";
}

/** Turn a tool call into a line a person can read at a glance. */
function describeTool(name: string, input: any): AgentEvent {
  const rel = (p: string) => String(p ?? "").split("/").slice(-2).join("/");
  switch (name) {
    case "Bash":
      return { kind: "shell", text: String(input?.command ?? "") };
    case "Read":
      return { kind: "tool", text: `Read ${rel(input?.file_path)}` };
    case "Write":
      return { kind: "file_change", text: `Write ${rel(input?.file_path)}` };
    case "Edit":
    case "NotebookEdit":
      return { kind: "file_change", text: `Edit ${rel(input?.file_path ?? input?.notebook_path)}` };
    case "Grep":
      return { kind: "tool", text: `Grep ${input?.pattern ?? ""}` };
    case "Glob":
      return { kind: "tool", text: `Glob ${input?.pattern ?? ""}` };
    case "WebSearch":
      return { kind: "tool", text: `Search ${input?.query ?? ""}` };
    case "WebFetch":
      return { kind: "tool", text: `Fetch ${input?.url ?? ""}` };
    case "Task":
      return { kind: "tool", text: `Agent ${input?.description ?? ""}` };
    case "TodoWrite":
      return { kind: "tool", text: "Updated the plan" };
    default: {
      // MCP tools arrive as mcp__server__tool — show the tool, not the plumbing.
      const mcp = name?.match(/^mcp__(\w+)__(\w+)$/);
      const label = mcp ? `${mcp[1]}: ${mcp[2]}` : String(name ?? "tool");
      const arg = summarizeInput(input);
      // Without the argument, two searches in a row are the same opaque line.
      return { kind: "tool", text: arg ? `${label} ${arg}` : label };
    }
  }
}

/** Per-turn overrides chosen in the UI, keyed by Control.id. */
export type TurnOptions = Record<string, string | undefined>;

/**
 * One control the UI should render for this agent.
 *
 * Drivers declare their own controls so the panel renders exactly what the CLI
 * supports and nothing it doesn't — Codex has no subagents, Claude has no
 * sandbox modes, and showing a dead dropdown is worse than showing none.
 * Adding a feature is one entry here plus one line in command().
 */
export interface Control {
  id: string;
  label: string;
  /** Empty when the CLI can't enumerate them; the UI then allows free text. */
  values: string[];
  /** Placeholder for "leave the CLI's own default alone". */
  blank: string;
  /** Free text allowed alongside the suggestions. */
  editable?: boolean;
  /**
   * How to draw it. "menu" is a chip that opens a list; "dots" is a discrete
   * slider — right for effort, where the values are an ordered scale and seeing
   * where you sit on it matters more than reading a word.
   */
  widget?: "menu" | "dots";
  /** Shown under each option in a menu. Explains what the choice actually does. */
  descriptions?: Record<string, string>;
  /**
   * Human names for the values. `acceptEdits` and `workspace-write` are flag
   * values, not words anyone says — the UI shows these instead and keeps the
   * raw value for the command line.
   */
  labels?: Record<string, string>;
  /** A glyph for the chip, so the mode is recognisable without reading. */
  icon?: string;
  /** Value shown when nothing is chosen — i.e. what the CLI does by default. */
  defaultValue?: string;
}

/**
 * What the CLI told us it can do, read off its session-init event rather than
 * hardcoded — the lists move every release, and the CLI is the only honest
 * source for them.
 */
export interface ModelInfo {
  label: string;
  description?: string;
  /** Effort scales differ per model — `ultra` exists only on some. */
  efforts?: string[];
  defaultEffort?: string;
}

export interface Capabilities {
  model?: string;
  models: string[];
  /** Per-model names, blurbs and effort scales, when the CLI knows them. */
  modelInfo?: Record<string, ModelInfo>;
  commands: string[];
  skills: string[];
  subagents: string[];
}

export interface Driver {
  readonly agent: Agent;
  readonly label: string;
  binary(): string;
  command(prompt: string, sessionId?: string, opts?: TurnOptions): string[];
  /** One protocol message can describe several things at once. */
  parse(obj: any): AgentEvent[];
  /** Pull capabilities out of a session-init event, if this CLI reports any. */
  capabilities(initRaw: any): Capabilities;
  /** What the UI should offer for this agent, given what it just reported. */
  controls(caps: Capabilities): Control[];
  /**
   * Turn a `/project-skill …` prompt into something this agent understands.
   *
   * Project skills live once in `.agents/skills/` but reach the two agents by
   * different routes (D6): Claude gets a native mirror in `.claude/skills/` and
   * so already knows the slash command; Codex has no skills concept at all and
   * must be told to fetch the body over MCP.
   */
  expandPrompt(text: string, projectSkills: string[]): string;
}

/** Split "/name rest of the prompt" into its parts. */
function parseSlash(text: string): { name: string; rest: string } | undefined {
  const m = text.match(/^\/([a-z0-9][a-z0-9-]*)\s*([\s\S]*)$/i);
  return m ? { name: m[1], rest: m[2].trim() } : undefined;
}

/** "claude-opus-4-8[1m]" -> "opus[1m]" — the chip has room for a name, not an id. */
function shortModel(id?: string): string | undefined {
  if (!id) {
    return undefined;
  }
  const m = id.match(/(opus|sonnet|haiku|fable)[\w.-]*(\[1m\])?/i);
  return m ? `${m[1].toLowerCase()}${m[2] ?? ""}` : id;
}

/**
 * Deliberately NOT offered as one-click controls: Claude's `bypassPermissions`
 * and Codex's `danger-full-access`. Both disable the approval/sandbox gate
 * entirely, and a dropdown makes that a mis-click. Both CLIs require an explicit
 * flag for a reason. Set them in .agents/config.toml if you really want them.
 */
const CLAUDE_PERMISSION_MODES = ["acceptEdits", "plan", "manual", "dontAsk", "auto"];
// Verified live: `claude -p --effort bogus` answers
// "Valid values: low, medium, high, xhigh, max".
const CLAUDE_EFFORT = ["low", "medium", "high", "xhigh", "max"];
// Verified live against the API, which rejected a bad value with
// "Supported values are: 'none', 'minimal', 'low', 'medium', 'high', 'xhigh'".
// Note the docs omit 'none' — this list comes from the API, not a blog post.
const CODEX_EFFORT = ["none", "minimal", "low", "medium", "high", "xhigh"];
const CODEX_SANDBOX = ["read-only", "workspace-write"];
/**
 * Codex's real models, read from its own cache.
 *
 * Codex advertises nothing about itself at runtime — `thread.started` carries a
 * thread id and nothing else, and `/model` isn't a command there (it's treated
 * as a prompt). But the CLI caches the account's model list on disk, including
 * each model's effort scale. That beats the guesses this used to hardcode: it's
 * per-account, and it stays right as OpenAI ships new models.
 *
 * `codex doctor` reports the current model too, but it's a slow subprocess for
 * one line; the cache has the same answer plus the rest.
 */
export function readCodexModels(home: string = os.homedir()): Capabilities {
  const caps: Capabilities = { ...EMPTY_CAPS };
  try {
    const raw = fs.readFileSync(path.join(home, ".codex", "models_cache.json"), "utf8");
    const parsed = JSON.parse(raw);
    const info: Record<string, ModelInfo> = {};
    for (const m of parsed?.models ?? []) {
      if (!m?.slug) {
        continue;
      }
      info[m.slug] = {
        label: m.display_name ?? m.slug,
        description: m.description,
        efforts: (m.supported_reasoning_levels ?? [])
          .map((l: any) => (typeof l === "string" ? l : l?.effort))
          .filter(Boolean),
        defaultEffort: m.default_reasoning_level,
      };
    }
    caps.models = Object.keys(info);
    caps.modelInfo = info;
  } catch {
    // No cache (fresh install, or never signed in) — the picker stays editable,
    // so a typed model name still works.
  }
  return caps;
}

export class ClaudeDriver implements Driver {
  readonly agent: Agent = "claude-code";
  readonly label = "Claude Code";

  constructor(
    private readonly path: string = "claude",
    private readonly approvals: string = "acceptEdits"
  ) {}

  binary(): string {
    return this.path;
  }

  command(prompt: string, sessionId?: string, opts?: TurnOptions): string[] {
    const args = ["-p", prompt, "--output-format", "stream-json", "--verbose"];
    // Our own MCP tools, pre-approved on the command line.
    //
    // `permissions.allow` in .claude/settings.json is IGNORED until the user
    // accepts the workspace trust dialog — so in a fresh project every
    // memory_write is silently denied and Claude quietly falls back to its own
    // private memory, which is exactly the split-brain this project exists to
    // prevent. --allowed-tools works regardless of trust.
    args.push("--allowed-tools", "mcp__uac");
    args.push("--permission-mode", opts?.permission || this.approvals);
    if (opts?.model) {
      args.push("--model", opts.model);
    }
    if (opts?.effort) {
      args.push("--effort", opts.effort);
    }
    if (opts?.subagent) {
      args.push("--agent", opts.subagent);
    }
    if (opts?.budget) {
      args.push("--max-budget-usd", opts.budget);
    }
    if (sessionId) {
      args.push("--resume", sessionId);
    }
    return args;
  }

  controls(caps: Capabilities): Control[] {
    return [
      {
        id: "permission",
        label: "Mode",
        values: CLAUDE_PERMISSION_MODES,
        // Names and wording taken from Claude Code's own mode picker, so a
        // choice means the same thing here as it does there.
        labels: {
          manual: "Manual",
          acceptEdits: "Edit automatically",
          plan: "Plan",
          auto: "Auto",
          dontAsk: "Don't ask",
        },
        descriptions: {
          manual: "Ask for approval before making each edit",
          acceptEdits: "Edit the selected text or the whole file",
          plan: "Explore the code and present a plan before editing",
          auto: "Approve actions that pass a safety check, pause for anything risky",
          dontAsk: "Don't ask again about edits already allowed",
        },
        blank: "Mode",
        defaultValue: "acceptEdits",
        icon: "⚡",
        widget: "menu",
      },
      {
        id: "model",
        label: "Model",
        values: caps.models,
        blank: "Model",
        defaultValue: shortModel(caps.model),
        widget: "menu",
      },
      { id: "effort", label: "Effort", values: CLAUDE_EFFORT, blank: "Effort", widget: "dots" },
      {
        id: "subagent",
        label: "Subagent",
        values: caps.subagents,
        blank: "Agent",
        defaultValue: "default",
        widget: "menu",
      },
    ];
  }

  /**
   * Nothing to do: `uac sync` mirrors every project skill into
   * `.claude/skills/<name>/SKILL.md`, so Claude already lists it as a real slash
   * command — verified live, a skill added to `.agents/skills/` shows up in the
   * init event's `skills` array after a sync.
   */
  expandPrompt(text: string, _projectSkills: string[]): string {
    return text;
  }

  parse(obj: any): AgentEvent[] {
    switch (obj?.type) {
      case "system":
        return obj.subtype === "init" ? [{ kind: "session", text: obj.session_id ?? "", raw: obj }] : [];
      case "assistant":
        return claudeBlocks(obj.message);
      case "user":
        // Tool results. The call itself is already shown; echoing every result
        // would bury the conversation in file contents.
        return [];
      case "result":
        return [{ kind: "result", text: obj.result ?? "", raw: obj }];
      case "error":
        return [{ kind: "error", text: obj.error ?? obj.message ?? "", raw: obj }];
      default:
        return [];
    }
  }

  /**
   * Claude's init event enumerates its own slash commands, skills and
   * subagents. Reading them beats a hardcoded list that silently rots as the
   * CLI ships new ones.
   */
  capabilities(initRaw: any): Capabilities {
    const skills: string[] = initRaw?.skills ?? [];
    const commands: string[] = initRaw?.slash_commands ?? [];
    return {
      model: initRaw?.model,
      models: [],
      // Skills surface as slash commands too; don't list them twice.
      commands: commands.filter((c) => !skills.includes(c)),
      skills,
      subagents: initRaw?.agents ?? [],
    };
  }
}

const CODEX_ITEM_KINDS: Record<string, EventKind> = {
  agent_message: "text",
  reasoning: "reasoning",
  command_execution: "shell",
  mcp_tool_call: "tool",
  web_search: "tool",
  file_change: "file_change",
};

export class CodexDriver implements Driver {
  readonly agent: Agent = "codex";
  readonly label = "Codex";

  constructor(
    private readonly path: string = "codex",
    private readonly approvals: string = "workspace-write"
  ) {}

  binary(): string {
    return this.path;
  }

  command(prompt: string, sessionId?: string, opts?: TurnOptions): string[] {
    const flags = ["--json", "--sandbox", opts?.sandbox || this.approvals];
    if (opts?.model) {
      flags.push("--model", opts.model);
    }
    if (opts?.effort) {
      // Codex has no --effort flag; reasoning effort is a config key, and -c
      // sets any of them. The value is TOML-parsed, hence the quotes.
      flags.push("-c", `model_reasoning_effort="${opts.effort}"`);
    }
    return sessionId
      ? ["exec", "resume", sessionId, ...flags, prompt]
      : ["exec", ...flags, prompt];
  }

  controls(caps: Capabilities): Control[] {
    const info = caps.modelInfo ?? {};
    const entries = Object.entries(info);
    const pick = <T>(get: (m: ModelInfo) => T | undefined) =>
      Object.fromEntries(entries.map(([slug, m]) => [slug, get(m)]).filter(([, v]) => v)) as Record<
        string,
        string
      >;

    // Effort scales differ per model (`ultra` is Terra-only), so the UI narrows
    // this to the chosen model's own scale. This is the fallback for when the
    // cache is missing: the raw API enum, which the API itself reported when it
    // rejected a bad value.
    const efforts = info[caps.model ?? ""]?.efforts ?? CODEX_EFFORT;

    return [
      {
        id: "sandbox",
        label: "Mode",
        values: CODEX_SANDBOX,
        labels: { "read-only": "Read only", "workspace-write": "Edit workspace" },
        descriptions: {
          "read-only": "Read the workspace, change nothing",
          "workspace-write": "Edit files in this workspace, but nothing outside it",
        },
        blank: "Mode",
        defaultValue: "workspace-write",
        icon: "✋",
        widget: "menu",
      },
      {
        // Stays editable: the cache is the account's list, but a model it hasn't
        // heard of yet should still be reachable by typing its name.
        id: "model",
        label: "Model",
        values: Object.keys(info),
        labels: pick((m) => m.label),
        descriptions: pick((m) => m.description),
        blank: "Model",
        defaultValue: caps.model,
        editable: true,
        widget: "menu",
      },
      { id: "effort", label: "Effort", values: efforts, blank: "Effort", widget: "dots" },
    ];
  }

  /**
   * Codex has no skills concept, so `/verify-web fix the build` means nothing to
   * it. Rewrite it into an instruction to fetch the body over MCP — which is
   * exactly the bridge this project exists to provide (D6).
   *
   * `skill_load` is annotated readOnlyHint, so it survives Codex's headless
   * approval gate (§9a). Only project skills are expanded: a bare `/something`
   * we don't own is left alone rather than mangled.
   */
  expandPrompt(text: string, projectSkills: string[]): string {
    const slash = parseSlash(text);
    if (!slash || !projectSkills.includes(slash.name)) {
      return text;
    }
    const task = slash.rest ? `\n\nThen apply it to this task: ${slash.rest}` : "";
    return (
      `Use the skill_load tool to load the project skill "${slash.name}", then follow ` +
      `its instructions exactly.${task}`
    );
  }

  parse(obj: any): AgentEvent[] {
    switch (obj?.type) {
      case "thread.started":
        return [{ kind: "session", text: obj.thread_id ?? "", raw: obj }];
      case "item.completed": {
        const item = obj.item ?? {};
        const kind = CODEX_ITEM_KINDS[item.type];
        if (!kind) {
          return [];
        }
        const text =
          item.text ?? item.message ?? item.command ?? item.path ?? item.summary ?? "";
        const label =
          kind === "tool" && item.type === "mcp_tool_call"
            ? `${item.server ?? "mcp"}: ${item.tool ?? ""}`
            : String(text);
        return label ? [{ kind, text: label, raw: obj }] : [];
      }
      case "turn.completed":
        return [{ kind: "result", text: "", raw: obj }];
      case "turn.failed":
      case "error":
        return [{ kind: "error", text: obj.error?.message ?? obj.message ?? "", raw: obj }];
      default:
        return [];
    }
  }

  /**
   * Codex's thread.started carries only a thread id — it advertises no
   * commands, skills or subagents. Returning empty is honest; the UI hides what
   * it can't offer rather than showing controls that do nothing.
   */
  capabilities(_initRaw: any): Capabilities {
    return { models: [], commands: [], skills: [], subagents: [] };
  }
}

export function makeDriver(
  agent: Agent,
  paths: { claude: string; codex: string },
  approvals?: { claude?: string; codex?: string }
): Driver {
  return agent === "codex"
    ? new CodexDriver(paths.codex, approvals?.codex)
    : new ClaudeDriver(paths.claude, approvals?.claude);
}

/**
 * One multi-turn conversation with one agent.
 *
 * The session id from the first turn is reused on every later turn, so the CLI
 * keeps its own conversation state — we never replay history ourselves.
 */
export const EMPTY_CAPS: Capabilities = {
  models: [],
  commands: [],
  skills: [],
  subagents: [],
};

/**
 * Ask Claude what it can do, before the user's first message.
 *
 * `claude -p "/model"` is a local slash command — verified free (num_turns 0,
 * $0) — and in stream-json it emits the full session-init event on the way past.
 * So one probe yields everything at once:
 *   • init event  → skills, slash_commands, agents, current model
 *   • result text → "Usage: /model <name>. Available: sonnet, opus, haiku, …"
 *
 * This has to happen at panel open. Capabilities otherwise only arrive once a
 * turn starts, so typing "/" before sending anything would show an empty
 * palette — the CLI knows its skills, we just hadn't asked yet.
 */
export function discoverCapabilities(
  binary: string,
  cwd: string,
  driver: Driver
): Promise<Capabilities> {
  return new Promise((resolve) => {
    execFile(
      binary,
      ["-p", "/model", "--output-format", "stream-json", "--verbose"],
      { cwd, timeout: 25_000, maxBuffer: 8 * 1024 * 1024 },
      (err, stdout) => {
        if (err && !stdout) {
          resolve(EMPTY_CAPS);
          return;
        }
        let caps: Capabilities = { ...EMPTY_CAPS };
        for (const line of stdout.split("\n")) {
          if (!line.trim()) {
            continue;
          }
          let obj: any;
          try {
            obj = JSON.parse(line);
          } catch {
            continue;
          }
          if (obj.type === "system" && obj.subtype === "init") {
            caps = { ...driver.capabilities(obj), models: caps.models };
          } else if (obj.type === "result") {
            const available = String(obj.result ?? "").match(/Available:\s*([^.\n]+)/);
            if (available) {
              caps.models = available[1]
                .split(",")
                .map((m) => m.trim())
                .filter((m) => m && !m.startsWith("or "));
            }
          }
        }
        resolve(caps);
      }
    );
  });
}

export class ChatSession {
  private sessionId?: string;
  private child?: ReturnType<typeof spawn>;

  constructor(private readonly driver: Driver, private readonly cwd: string) {}

  get label(): string {
    return this.driver.label;
  }

  /**
   * Cancel the turn in flight.
   *
   * Without this a long or wedged turn can only be escaped by closing the
   * panel — and the child process would keep running, still burning the
   * subscription. The session id survives, so the next message resumes the same
   * conversation rather than starting over.
   */
  abort(): boolean {
    if (!this.child || this.child.exitCode !== null) {
      return false;
    }
    this.child.kill("SIGTERM");
    return true;
  }

  /** Capabilities are only known once a session has started and reported them. */
  onCapabilities?: (caps: Capabilities) => void;

  async send(
    prompt: string,
    onEvent: (event: AgentEvent) => void,
    opts?: TurnOptions
  ): Promise<void> {
    const args = this.driver.command(prompt, this.sessionId, opts);
    const child = spawn(this.driver.binary(), args, {
      cwd: this.cwd,
      // `codex exec` reads stdin whenever it isn't a TTY and appends it to the
      // prompt. spawn()'s default gives it an open pipe nobody writes to, which
      // hangs every turn forever. Close it: the prompt is an argument.
      stdio: ["ignore", "pipe", "pipe"],
    });

    // A failed spawn also closes with a nonzero code; report the useful error
    // once rather than following it with a meaningless exit status.
    this.child = child;
    let spawnFailed = false;
    child.on("error", (err: NodeJS.ErrnoException) => {
      spawnFailed = true;
      onEvent({
        kind: "error",
        text:
          err.code === "ENOENT"
            ? `'${this.driver.binary()}' not found. Install the ${this.driver.label} CLI and log in — this extension never calls a model API itself.`
            : String(err),
      });
    });

    const stderr: string[] = [];
    child.stderr.on("data", (chunk) => stderr.push(String(chunk)));

    const lines = readline.createInterface({ input: child.stdout });
    for await (const line of lines) {
      const trimmed = line.trim();
      if (!trimmed) {
        continue;
      }
      let obj: unknown;
      try {
        obj = JSON.parse(trimmed);
      } catch {
        // Not every line is JSON (banners, warnings). Surface, don't die.
        onEvent({ kind: "other", text: trimmed });
        continue;
      }
      for (const event of this.driver.parse(obj)) {
        if (event.kind === "session") {
          if (event.text) {
            this.sessionId = event.text;
          }
          this.onCapabilities?.(this.driver.capabilities(event.raw));
        }
        onEvent(event);
      }
    }

    const code: number = await new Promise((resolve) => child.on("close", resolve));
    const killed = child.killed;
    this.child = undefined;
    if (killed) {
      onEvent({ kind: "other", text: "Stopped." });
      return;
    }
    if (code !== 0 && !spawnFailed) {
      const detail = stderr.join("").trim();
      onEvent({ kind: "error", text: detail || `${this.driver.binary()} exited ${code}` });
    }
  }
}
