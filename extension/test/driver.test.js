/**
 * Driver tests.
 *
 * The init-event shape below is RECORDED from a real `claude -p --output-format
 * stream-json` run — the CLI enumerates its own models, skills, slash commands
 * and subagents there, and the UI is built from it. If a field is renamed
 * upstream, the pickers silently go empty with no error; these tests are what
 * would catch it.
 */

const assert = require("node:assert");
const test = require("node:test");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");
const { execSync } = require("node:child_process");
const { ChatSession, ClaudeDriver, EMPTY_CAPS, makeDriver } = require("../out/driver.js");

const PATHS = { claude: "claude", codex: "codex" };
const claude = () => makeDriver("claude-code", PATHS);
const codex = () => makeDriver("codex", PATHS);

// Recorded from a real run.
const REAL_INIT = {
  type: "system",
  subtype: "init",
  session_id: "212ba2d0-ae5a-42da-9d70-430dde2f7235",
  model: "claude-opus-4-8[1m]",
  permissionMode: "default",
  apiKeySource: "none",
  slash_commands: ["init", "mcp", "model", "review", "grill-me", "deep-research"],
  skills: ["grill-me", "deep-research"],
  agents: ["claude", "Explore", "general-purpose", "Plan"],
};

// --- capability discovery ---------------------------------------------------

test("capabilities are read off the real init event", () => {
  const caps = claude().capabilities(REAL_INIT);
  assert.strictEqual(caps.model, "claude-opus-4-8[1m]");
  assert.deepStrictEqual(caps.skills, ["grill-me", "deep-research"]);
  assert.deepStrictEqual(caps.subagents, ["claude", "Explore", "general-purpose", "Plan"]);
});

test("skills are not listed twice as commands", () => {
  // The CLI reports skills inside slash_commands as well.
  const caps = claude().capabilities(REAL_INIT);
  assert.deepStrictEqual(caps.commands, ["init", "mcp", "model", "review"]);
  for (const skill of caps.skills) {
    assert.ok(!caps.commands.includes(skill), `${skill} listed twice`);
  }
});

test("an init event with no capability fields degrades to empty", () => {
  const caps = claude().capabilities({ type: "system", subtype: "init" });
  assert.deepStrictEqual(caps, { model: undefined, models: [], commands: [], skills: [], subagents: [] });
});

test("codex advertises nothing, and says so honestly", () => {
  // thread.started carries only a thread id. Empty means the UI hides those
  // controls rather than offering ones that do nothing.
  const caps = codex().capabilities({ type: "thread.started", thread_id: "t1" });
  assert.deepStrictEqual(caps.commands, []);
  assert.deepStrictEqual(caps.skills, []);
  assert.deepStrictEqual(caps.subagents, []);
});

// --- what the log must and must not show ------------------------------------
// Recorded from a real run that rendered wrong: the session id appeared as a
// message, and the reply was printed twice.

const one = (driver, obj) => {
  const events = driver.parse(obj);
  assert.strictEqual(events.length, 1, `expected 1 event, got ${events.length}`);
  return events[0];
};

test("the session id is plumbing, not a message", () => {
  const e = one(claude(), {
    type: "system",
    subtype: "init",
    session_id: "e0835620-a7ef-4c9a-abf9-4d0e4bc774b3",
  });
  // The webview drops `session` events; the id only exists so the next turn can
  // resume. If this kind ever changes, that filter silently stops matching.
  assert.strictEqual(e.kind, "session");
  assert.strictEqual(e.text, "e0835620-a7ef-4c9a-abf9-4d0e4bc774b3");
});

test("claude repeats its reply in `result`, which is why the log must dedupe", () => {
  const answer = "Hello! What are you working on today?";
  const assistant = one(claude(), {
    type: "assistant",
    message: { content: [{ type: "text", text: answer }] },
  });
  const result = one(claude(), { type: "result", subtype: "success", result: answer });

  assert.strictEqual(assistant.kind, "text");
  assert.strictEqual(result.kind, "result");
  // Same text, two events — render both and the reply appears twice.
  assert.strictEqual(assistant.text, result.text);
});

test("a slash command answers only in `result`, so it can't be dropped outright", () => {
  // /context and /model produce no assistant event at all; blanket-skipping
  // `result` would render an empty turn.
  const e = one(claude(), { type: "result", subtype: "success", result: "## Context Usage" });
  assert.strictEqual(e.kind, "result");
  assert.ok(e.text);
});

// --- showing what the agent is doing ----------------------------------------
// Shapes recorded from a real tool-using run: an assistant message carries text
// and tool_use in ONE content array, so a message maps to several events. The
// old parser returned a single event and silently dropped every tool call.

test("one message yields text AND the tool call it made", () => {
  const events = claude().parse({
    type: "assistant",
    message: {
      content: [
        { type: "text", text: "I will read it." },
        { type: "tool_use", id: "t1", name: "Read", input: { file_path: "/a/b/sample.txt" } },
      ],
    },
  });
  assert.deepStrictEqual(
    events.map((e) => [e.kind, e.text]),
    [
      ["text", "I will read it."],
      ["tool", "Read b/sample.txt"],
    ]
  );
});

test("thinking is surfaced as reasoning", () => {
  const events = claude().parse({
    type: "assistant",
    message: { content: [{ type: "thinking", thinking: "The file is small." }] },
  });
  assert.deepStrictEqual(events.map((e) => e.kind), ["reasoning"]);
  assert.strictEqual(events[0].text, "The file is small.");
});

test("tool calls read as actions, not as JSON", () => {
  const cases = [
    [{ name: "Bash", input: { command: "wc -l x.txt" } }, "shell", "wc -l x.txt"],
    [{ name: "Edit", input: { file_path: "/w/src/app.ts" } }, "file_change", "Edit src/app.ts"],
    [{ name: "Write", input: { file_path: "/w/src/new.ts" } }, "file_change", "Write src/new.ts"],
    [{ name: "Grep", input: { pattern: "TODO" } }, "tool", "Grep TODO"],
    [{ name: "WebSearch", input: { query: "codex mcp" } }, "tool", "Search codex mcp"],
    [{ name: "mcp__uac__memory_write", input: {} }, "tool", "uac: memory_write"],
  ];
  for (const [block, kind, text] of cases) {
    const e = one(claude(), { type: "assistant", message: { content: [{ type: "tool_use", ...block }] } });
    assert.strictEqual(e.kind, kind, `${block.name} -> wrong kind`);
    assert.strictEqual(e.text, text, `${block.name} -> wrong text`);
  }
});

test("a tool call shows its argument, not just its name", () => {
  // Observed for real: two searches in a row both rendered as bare "ToolSearch"
  // and "uac: memory_search", so you couldn't tell what either looked for.
  const search = (query) =>
    one(claude(), {
      type: "assistant",
      message: { content: [{ type: "tool_use", name: "ToolSearch", input: { query, max_results: 5 } }] },
    }).text;

  assert.strictEqual(search("select:Read,Grep"), "ToolSearch select:Read,Grep");
  assert.notStrictEqual(search("select:Read,Grep"), search("notebook jupyter"));
});

test("mcp calls show the query alongside the tool", () => {
  const e = one(claude(), {
    type: "assistant",
    message: {
      content: [
        { type: "tool_use", name: "mcp__uac__memory_search", input: { query: "project status", limit: 10 } },
      ],
    },
  });
  assert.strictEqual(e.text, "uac: memory_search project status");
});

test("the most identifying argument wins, not just the first key", () => {
  const e = one(claude(), {
    type: "assistant",
    message: {
      content: [{ type: "tool_use", name: "SomeTool", input: { limit: 5, verbose: true, query: "the point" } }],
    },
  });
  assert.strictEqual(e.text, "SomeTool the point");
});

test("a tool with no string arguments still renders its name", () => {
  const e = one(claude(), {
    type: "assistant",
    message: { content: [{ type: "tool_use", name: "mcp__uac__skill_list", input: {} }] },
  });
  assert.strictEqual(e.text, "uac: skill_list");
});

test("tool results are not echoed back into the chat", () => {
  // The call is already shown; dumping every file's contents would bury the
  // conversation.
  assert.deepStrictEqual(
    claude().parse({
      type: "user",
      message: { content: [{ type: "tool_result", tool_use_id: "t1", content: "1\thello" }] },
    }),
    []
  );
});

test("empty text blocks don't become blank lines", () => {
  assert.deepStrictEqual(
    claude().parse({ type: "assistant", message: { content: [{ type: "text", text: "  " }] } }),
    []
  );
});

test("codex shell commands and mcp calls are distinguished", () => {
  const shell = one(codex(), {
    type: "item.completed",
    item: { type: "command_execution", command: "pytest -q" },
  });
  assert.strictEqual(shell.kind, "shell");

  const mcp = one(codex(), {
    type: "item.completed",
    item: { type: "mcp_tool_call", server: "uac", tool: "skill_load" },
  });
  assert.deepStrictEqual([mcp.kind, mcp.text], ["tool", "uac: skill_load"]);
});

// --- the / palette depends on these ----------------------------------------

test("skills survive capability parsing so the palette can list them", () => {
  // The palette is built from caps.skills. If this ever comes back empty,
  // typing "/" silently lists nothing — no error, just an empty menu.
  const caps = claude().capabilities(REAL_INIT);
  assert.ok(caps.skills.length > 0, "no skills parsed — the / palette would be empty");
});

test("EMPTY_CAPS is shaped like real caps, so the UI can render before discovery", () => {
  assert.deepStrictEqual(Object.keys(EMPTY_CAPS).sort(), [
    "commands",
    "models",
    "skills",
    "subagents",
  ]);
});

// --- project skills: one file, two delivery routes (D6) ---------------------

test("claude gets a project skill as-is — uac sync already mirrored it natively", () => {
  // Verified live: after `uac sync`, a skill in .agents/skills/ appears in
  // Claude's init `skills` array, so /verify-web is a real slash command.
  assert.strictEqual(
    claude().expandPrompt("/verify-web fix the build", ["verify-web"]),
    "/verify-web fix the build"
  );
});

test("codex gets a project skill rewritten into a skill_load instruction", () => {
  // Codex has no skills concept; "/verify-web" would be meaningless to it.
  const out = codex().expandPrompt("/verify-web", ["verify-web"]);
  assert.ok(out.includes("skill_load"));
  assert.ok(out.includes('"verify-web"'));
  assert.ok(!out.startsWith("/"), "codex must not receive a bare slash command");
});

test("codex keeps the task alongside the skill", () => {
  const out = codex().expandPrompt("/verify-web fix the build", ["verify-web"]);
  assert.ok(out.includes("skill_load"));
  assert.ok(out.includes("fix the build"));
});

test("a slash command we do not own is left alone", () => {
  // /context is Claude's own; rewriting it would break it.
  assert.strictEqual(codex().expandPrompt("/context", ["verify-web"]), "/context");
  assert.strictEqual(codex().expandPrompt("/unknown thing", []), "/unknown thing");
});

test("ordinary prompts are never touched", () => {
  for (const d of [claude(), codex()]) {
    assert.strictEqual(d.expandPrompt("what does this repo do?", ["verify-web"]), "what does this repo do?");
    assert.strictEqual(d.expandPrompt("use a / in prose", ["verify-web"]), "use a / in prose");
  }
});

// --- turn options -----------------------------------------------------------

test("claude passes the chosen model and subagent", () => {
  const cmd = claude().command("go", undefined, { model: "sonnet", subagent: "Explore" });
  assert.strictEqual(cmd[cmd.indexOf("--model") + 1], "sonnet");
  assert.strictEqual(cmd[cmd.indexOf("--agent") + 1], "Explore");
});

test("no model chosen means no flag — the CLI keeps its default", () => {
  const cmd = claude().command("go", undefined, {});
  assert.ok(!cmd.includes("--model"));
  assert.ok(!cmd.includes("--agent"));
});

test("codex passes the chosen model", () => {
  assert.strictEqual(
    codex().command("go", undefined, { model: "gpt-5" })[
      codex().command("go", undefined, { model: "gpt-5" }).indexOf("--model") + 1
    ],
    "gpt-5"
  );
});

test("model choice survives a resumed turn", () => {
  const cmd = claude().command("next", "sess-1", { model: "haiku" });
  assert.strictEqual(cmd[cmd.indexOf("--resume") + 1], "sess-1");
  assert.strictEqual(cmd[cmd.indexOf("--model") + 1], "haiku");
});

// --- effort, verified live against each CLI ---------------------------------

test("claude effort values match what the CLI reports as valid", () => {
  // `claude -p --effort bogus` answers: "Valid values: low, medium, high, xhigh, max".
  const effort = claude().controls({ models: [], commands: [], skills: [], subagents: [] })
    .find((c) => c.id === "effort");
  assert.deepStrictEqual(effort.values, ["low", "medium", "high", "xhigh", "max"]);
});

test("claude passes --effort", () => {
  const cmd = claude().command("go", undefined, { effort: "xhigh" });
  assert.strictEqual(cmd[cmd.indexOf("--effort") + 1], "xhigh");
});

// --- Codex's real models, from its own cache --------------------------------
// Codex advertises nothing at runtime (thread.started carries a thread id and
// nothing else, and `/model` is treated as a prompt, not a command). But it
// caches its account's model list on disk, with each model's effort scale.

const { readCodexModels } = require("../out/driver.js");

function fakeCodexHome(models) {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), "uac-codex-"));
  fs.mkdirSync(path.join(dir, ".codex"));
  fs.writeFileSync(path.join(dir, ".codex", "models_cache.json"), JSON.stringify({ models }));
  return dir;
}

test("models, names and blurbs come from the cache", () => {
  const home = fakeCodexHome([
    {
      slug: "gpt-5.6-terra",
      display_name: "GPT-5.6-Terra",
      description: "Balanced agentic coding model for everyday work.",
      default_reasoning_level: "medium",
      supported_reasoning_levels: [{ effort: "low" }, { effort: "ultra" }],
    },
  ]);
  const caps = readCodexModels(home);

  assert.deepStrictEqual(caps.models, ["gpt-5.6-terra"]);
  assert.strictEqual(caps.modelInfo["gpt-5.6-terra"].label, "GPT-5.6-Terra");
  assert.match(caps.modelInfo["gpt-5.6-terra"].description, /Balanced agentic/);
  fs.rmSync(home, { recursive: true, force: true });
});

test("effort scales are per-model, because they genuinely differ", () => {
  // `ultra` exists on Terra and nowhere else. One fixed list would offer it for
  // every model and the CLI would reject the turn.
  const home = fakeCodexHome([
    { slug: "terra", display_name: "Terra", supported_reasoning_levels: [{ effort: "low" }, { effort: "ultra" }] },
    { slug: "mini", display_name: "Mini", supported_reasoning_levels: [{ effort: "low" }, { effort: "high" }] },
  ]);
  const caps = readCodexModels(home);

  assert.deepStrictEqual(caps.modelInfo.terra.efforts, ["low", "ultra"]);
  assert.deepStrictEqual(caps.modelInfo.mini.efforts, ["low", "high"]);
  assert.ok(!caps.modelInfo.mini.efforts.includes("ultra"));
  fs.rmSync(home, { recursive: true, force: true });
});

test("no cache means no crash — the model picker stays typeable", () => {
  const home = fs.mkdtempSync(path.join(os.tmpdir(), "uac-nocodex-"));
  const caps = readCodexModels(home);
  assert.deepStrictEqual(caps.models, []);

  const model = codex().controls(caps).find((c) => c.id === "model");
  assert.ok(model.editable, "with no cache, a typed model name must still work");
  fs.rmSync(home, { recursive: true, force: true });
});

test("a corrupt cache degrades instead of breaking the panel", () => {
  const home = fs.mkdtempSync(path.join(os.tmpdir(), "uac-badcodex-"));
  fs.mkdirSync(path.join(home, ".codex"));
  fs.writeFileSync(path.join(home, ".codex", "models_cache.json"), "{not json");
  assert.deepStrictEqual(readCodexModels(home).models, []);
  fs.rmSync(home, { recursive: true, force: true });
});

test("codex effort values match what the API reports as valid", () => {
  // The API rejected a bad value with:
  // "Supported values are: 'none', 'minimal', 'low', 'medium', 'high', 'xhigh'".
  // The published docs omit 'none'; the API is the authority.
  const effort = codex().controls({ models: [], commands: [], skills: [], subagents: [] })
    .find((c) => c.id === "effort");
  assert.deepStrictEqual(effort.values, ["none", "minimal", "low", "medium", "high", "xhigh"]);
});

test("codex effort goes through -c, since it has no --effort flag", () => {
  const cmd = codex().command("go", undefined, { effort: "high" });
  const i = cmd.indexOf("-c");
  assert.ok(i >= 0, "no -c override emitted");
  // The value is TOML-parsed by the CLI, so it must stay quoted.
  assert.strictEqual(cmd[i + 1], 'model_reasoning_effort="high"');
});

test("codex model is editable free text, because codex cannot enumerate models", () => {
  const model = codex().controls({ models: [], commands: [], skills: [], subagents: [] })
    .find((c) => c.id === "model");
  assert.ok(model.editable, "codex model picker must accept a typed name");
});

// --- the dangerous modes stay out of one-click reach ------------------------

test("no one-click switch disables the approval gate", () => {
  // Both CLIs make you pass an explicit flag for these; a dropdown makes it a
  // mis-click. They remain reachable via config, just not by accident.
  const empty = { models: [], commands: [], skills: [], subagents: [] };
  const perms = claude().controls(empty).find((c) => c.id === "permission");
  assert.ok(!perms.values.includes("bypassPermissions"));
  const sandbox = codex().controls(empty).find((c) => c.id === "sandbox");
  assert.ok(!sandbox.values.includes("danger-full-access"));
});

// --- stopping a turn --------------------------------------------------------

test("abort() on an idle session is a no-op, not a crash", () => {
  assert.strictEqual(new ChatSession(claude(), process.cwd()).abort(), false);
});

test("stop actually kills the child process", async () => {
  // A stop button that leaves the CLI running would be worse than none: the
  // turn keeps burning the subscription with nothing rendering it.
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), "uac-stop-"));
  const bin = path.join(dir, "hangs");
  fs.writeFileSync(
    bin,
    `#!/usr/bin/env node
console.log(JSON.stringify({ type: "system", subtype: "init", session_id: "s-1" }));
setTimeout(() => {}, 120000);
`
  );
  fs.chmodSync(bin, 0o755);

  const alive = () =>
    Number(
      execSync(`ps -eo args= | grep -F ${bin} | grep -v 'sh -c' | grep -v grep | wc -l`)
        .toString()
        .trim()
    );

  const session = new ChatSession(new ClaudeDriver(bin, ""), dir);
  const events = [];
  const turn = session.send("hi", (e) => events.push(e));

  await new Promise((r) => setTimeout(r, 600));
  assert.strictEqual(alive(), 1, "the fake CLI never started");

  assert.strictEqual(session.abort(), true);
  await turn;
  await new Promise((r) => setTimeout(r, 300));

  assert.strictEqual(alive(), 0, "stop left an orphaned process behind");
  assert.ok(
    events.some((e) => e.text === "Stopped."),
    "a stopped turn must say so rather than look like it finished"
  );
  // A killed turn is not an error — it's what you asked for.
  assert.ok(!events.some((e) => e.kind === "error"), "stopping reported a spurious error");
  fs.rmSync(dir, { recursive: true, force: true });
});

// --- the permission fix -----------------------------------------------------

test("claude pre-approves our MCP tools on the command line", () => {
  // settings.json permissions are ignored until the workspace is trusted, so
  // without this every memory_write is denied and Claude silently falls back to
  // its own private memory — the split-brain this project exists to prevent.
  const cmd = claude().command("go");
  assert.strictEqual(cmd[cmd.indexOf("--allowed-tools") + 1], "mcp__uac");
});

test("a slash command is just a prompt", () => {
  // Slash commands and skills run headlessly by being sent as the prompt —
  // verified live: `claude -p "/context"` returns with num_turns 0.
  const cmd = claude().command("/grill-me");
  assert.ok(cmd.includes("/grill-me"));
});
