# uac-chat — terminal chat for Claude Code and Codex

A full-screen terminal front end for talking to **both** agents over your shared
`uac` memory and skills — switch between Claude and Codex without leaving the
window.

This is a **separate app** from the VS Code extension, on purpose: pick whichever
front end you like. The two share only the `uac` core (memory, skills, hooks) —
never each other's code, and never a second copy of your memory store.

No API key. Every model call happens inside the official CLI under your own
subscription login.

## Install

```bash
uv tool install --editable ./cli --with-editable .
```

(The `--with-editable .` makes the local `uac` core resolvable, since it isn't on
PyPI.) Then, in a project you've run `uac init` in:

```bash
uac-chat                 # start with Claude Code
uac-chat --agent codex   # start with Codex
```

## Keys

| Key | |
|---|---|
| `Enter` | send |
| `Ctrl+T` | switch agent (Claude ⇄ Codex) |
| `Ctrl+G` | model |
| `Ctrl+E` | effort |
| `Ctrl+P` | mode (permissions / sandbox) |
| `Ctrl+B` | toggle the shared-memory panel |
| `Ctrl+C` | stop the running turn — or quit when idle |
| `/` | skills and slash commands (your project skills work on both agents) |
| `@` | mention a file by path |

The controls bar shows the current agent, model, effort and mode. A setting you
change lights up; the effort dots follow the model you pick (Codex's scales
differ per model — `ultra` is Terra-only).

## How it works

Each turn is forwarded to `claude -p …` / `codex exec …` as a subprocess; the
streamed events are rendered as they arrive. The agent's *working* (reads,
searches, commands, edits) collects into a collapsible group that folds away once
the answer lands, so the reply stands on its own.

Project skills reach both agents: Claude has them natively; for Codex, `/skill`
is rewritten into a `skill_load` call over MCP.

## Develop

```bash
cd cli
pytest            # driver + headless app tests (no real agent, no quota)
```

The driver is this app's own copy — a deliberate sibling of the extension's
`driver.ts`. A change to how an agent is driven or parsed belongs in both.
