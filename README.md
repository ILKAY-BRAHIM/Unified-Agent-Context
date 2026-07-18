# unified-agent-context (`uac`)

Shared **memory** and **skills** across **Claude Code** and **Codex**, so what one agent learns
the other can use — across sessions, and across projects.

**No API keys.** Every model call happens inside the official CLIs under your own subscription
login. This tool never talks to a model API and never reads either CLI's credentials.

## Install

Install the `uac` CLI with [pipx](https://pipx.pypa.io) — isolated, and on your PATH:

```bash
pipx install unified-agent-context      # the `uac` command
pipx install uac-tui                     # optional: the `uac-chat` terminal app
```

Or the one-liner (installs `uv`/`pipx` first if you don't have it):

```bash
curl -fsSL https://uac-ai.com/install.sh | bash            # just `uac`
curl -fsSL https://uac-ai.com/install.sh | bash -s -- --chat   # + `uac-chat`
```

You also need both agent CLIs installed and logged in with your subscriptions:

```bash
npm install -g @anthropic-ai/claude-code
npm install -g @openai/codex
claude   # log in with your Claude subscription
codex    # log in with your ChatGPT subscription
```

<details>
<summary>Install from source (for development)</summary>

```bash
uv venv
uv pip install -e ".[dev]"
```
</details>

## Set up a project

```bash
cd /path/to/your-project
uac init
```

Scaffolds `AGENTS.md` + `.agents/`, creates the memory DB, generates `CLAUDE.md` and the native
Claude skill mirror, and registers the MCP server + hooks + tool permissions in **both** agents.

### ⚠️ Then do the three steps `uac init` can't do for you

Until you do these, **both agents silently ignore everything `uac init` wrote.** They are
security decisions, so `uac` won't automate them.

1. **Run `claude` here once and accept the trust prompt.** Otherwise Claude drops the
   permission rule — *"Ignoring 1 permissions.allow entry from .claude/settings.json: this
   workspace has not been trusted"* — and then **silently refuses every `memory_write`**. It
   looks like it's working. It isn't.
2. **Run `codex` here once and accept the directory trust prompt.** Otherwise: *"Not inside a
   trusted directory."*
3. **Inside codex, run `/hooks` and trust the uac hooks.** Codex ignores wrapper-installed
   hooks until you approve them there ([#21615](https://github.com/openai/codex/issues/21615)).
   **This one is not optional:** without it the session-start hook never runs, Codex never
   learns the shared memory exists, and when you ask it something it will grep your files and
   tell you it found nothing. That is the observed behaviour, not a hypothetical.

Claude's escape hatch, if you want it: `projects["/abs/path"].hasTrustDialogAccepted: true` in
`~/.claude.json`.

Then fill in `AGENTS.md` — it's the source of truth.

## How it works

```
Claude Code ─┐                        ┌─ .agents/memory.db     this project
             ├─ MCP (stdio) ─ uac ────┼─ ~/.agents/global.db   everywhere
Codex ───────┘                        ├─ linked projects       read-only
                                      ├─ .agents/skills/*.md
                                      └─ AGENTS.md
```

Six MCP tools: `memory_write`, `memory_search`, `memory_forget`, `skill_list`, `skill_load`,
`project_context`.

Two hooks do the work you'd otherwise have to remember:

- **SessionStart** injects `AGENTS.md` + the skills index + recent memories, so a session
  *starts* informed.
- **Stop** blocks the first stop of a session and asks the agent to save anything durable via
  `memory_write`. The agent's own model writes it — that's how we summarize without an API key.

**The SessionStart hook is the load-bearing piece.** Tested on a real project: without it, Codex
was asked about a fact sitting in shared memory and grepped the filesystem instead, reporting it
found nothing — even though `AGENTS.md` told it to use `memory_search`. With the hook trusted,
the same question in a fresh session got the right answer with no mention of any tool. Injecting
context beats hoping the agent decides to go looking for it.

Memories are **written deliberately**, not captured silently: a small, clean store instead of a
noisy one that burns tokens and goes stale.

## Memory

```bash
uac memory list
uac memory search "deploy"                     # this project + global + linked
uac memory add "Staging needs VPN" --kind gotcha --tags deploy,staging
uac memory add "I prefer tabs" --kind preference --scope global
uac memory forget <id>
```

`kind` is one of `decision`, `fact`, `gotcha`, `preference`. Every memory records its `source`
(`claude-code` / `codex` / `human`) so you can see which agent wrote what.

## Skills

A skill is one markdown file in `.agents/skills/`:

```markdown
---
name: deploy-runbook
description: Use when the user mentions deploying, releasing, or shipping.
---

# Deploy runbook
...
```

The `description` must say **when to use** it — that's what makes it trigger.

```bash
uac skills list
uac skills show deploy-runbook
uac sync                # regenerate CLAUDE.md + .claude/skills/
uac sync --check        # exit 1 if stale — wire this as a pre-commit hook
```

One source of truth, two adapters: Claude Code gets native skills in `.claude/skills/`; Codex
gets the same content via `skill_load`. Bodies load **on demand** — the always-in-context index
is names and descriptions only.

### Using your Claude skills in Codex

```bash
uac skills import          # copies ~/.claude/skills/* into .agents/skills/
```

Claude keeps using them natively; Codex can now load them with `skill_load`. They become the
project's copy — commit them, edit them for both agents.

**Only Claude's *user* skills can be imported** (the ones in `~/.claude/skills/`). The skills
that ship inside the `claude` binary — `deep-research`, `dataviz`, `verify`, `code-review`,
`simplify` … — aren't files, so there's nothing to copy. Several of them are about Claude Code's
own features and would mean nothing to Codex anyway.

## Cross-project memory

Reuse one project's memory in another. Links are **explicit, read-only, and one-way**:

```bash
uac link add shared-infra    # this project now READS shared-infra's memories
uac link ls
uac link rm shared-infra
```

Results from a linked project are labelled with their origin, so the agent knows a fact came
from somewhere else and can discount it. Your writes never touch the linked project.

## Chat (experimental)

```bash
uac chat --agent claude-code    # or codex
uac run codex "explain this repo"
uac review "add retry logic to the client"   # Claude writes, Codex reviews the diff
```

Your prompt is forwarded to the agent's CLI as a subprocess and its event stream is rendered
back. No API key — it drives the CLI you already pay for.

There's also a **VS Code extension** in `extension/` (chat panel + agent picker):

```bash
cd extension && npm install && npm run compile
```

Then open the folder in VS Code and press F5 → "Unified Agent Context: Open Chat".

> **Both are unverified against the real CLIs.** The flags and event shapes (`claude -p
> --output-format stream-json`, `codex exec --json`) move often. Check `--help` before trusting
> them. See "Status" below.

## What `uac init` writes

| File | Purpose |
|---|---|
| `.mcp.json` | Claude Code: MCP server, `UAC_SOURCE=claude-code` |
| `.claude/settings.json` | Claude Code: `SessionStart` + `Stop` hooks |
| `.claude/skills/` | generated native skill mirror |
| `.codex/config.toml` | Codex: MCP server, `UAC_SOURCE=codex` |
| `.codex/hooks.json` | Codex: `SessionStart` + `Stop` hooks |
| `CLAUDE.md` | generated — `@AGENTS.md` + Claude extras |
| `.agents/config.toml` | your settings |
| `~/.agents/registry.toml` | makes this project linkable by name |

Re-running is idempotent and won't touch your own hooks or servers. `uac` refuses to overwrite
a hand-written `CLAUDE.md`.

Attribution works because each agent spawns **its own** stdio server process, so `UAC_SOURCE` is
set per agent. (This breaks under a single shared `--http` server — stdio only for now.)

## Settings

`.agents/config.toml`:

```toml
[memory]
scope = "project"        # "project" | "global"
max_results = 5

[links]
read = ["shared-infra"]  # read-only, one-way

[claude]
extra = "- Use /self-review before PRs"   # appended to generated CLAUDE.md

[hooks]
inject_context_on_start = true
flush_memory_on_end     = true
claude_auto_memory      = "ours"   # "ours" | "native"
```

Claude ships its own Auto Memory. Running both can double-write. Set
`claude_auto_memory = "native"` to leave Claude's side alone (Codex is unaffected).

`UAC_HOME` overrides `~/.agents` (used by the tests).

## Status

Tested against Claude Code **2.1.210** and Codex **0.144.4**, both live on a real project.

| Phase | State |
|---|---|
| 1 — memory + hooks | built. Real Claude calls `mcp__uac__memory_write` unprompted; real Codex calls `memory_search` unprompted and gets results; the Stop hook nudge fires for real |
| 2 — skills | built, verified from both agents over real MCP |
| 3 — cross-project links | built, verified |
| 4 — chat CLI + VS Code extension | built. All flags + event shapes confirmed against the real CLIs; VS Code UI itself unexercised |
| 5 — `uac review` | built, **unverified** |

### Known limitation: headless Codex can read, not write

`codex exec` auto-cancels MCP tool calls that aren't advertised read-only — a known upstream
bug ([#16685](https://github.com/openai/codex/issues/16685),
[#24135](https://github.com/openai/codex/issues/24135)) whose only official workaround is
`--dangerously-bypass-approvals-and-sandbox`. We don't use that.

Instead our read tools are annotated `readOnlyHint: true`, which passes Codex's gate cleanly.
So:

- **`memory_search` / `skill_list` / `skill_load` / `project_context`** work in headless Codex ✅
- **`memory_write` / `memory_forget`** are cancelled in headless Codex ❌ — they work in an
  **interactive** `codex` session, where you approve them.

We annotate honestly (`memory_write` is not read-only; `memory_forget` is destructive) rather
than mislabel a write to slip past an approval gate.

**Still to prove:** the full round trip with interactive Codex.

**Phase 0 was skipped.** Nobody has yet confirmed, by living with it, that plain `AGENTS.md` +
an existing tool (agentmemory) isn't already enough. See `project.md` §8.

`project.md` §9a records everything that testing against the real CLIs changed — including two
real bugs (Codex hanging on stdin; MCP tools being permission-gated) and the trust gate.

## The test that matters

Save a memory in Claude Code, then open Codex in the same project and ask what it knows. If
that handoff doesn't work, nothing else here matters.

```bash
pytest                              # 103 tests
cd extension && npx tsc -p . --noEmit
```
