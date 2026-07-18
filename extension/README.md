<h1 align="center">Unified Agent Context</h1>

<p align="center">Chat with <b>Claude Code</b> and <b>Codex</b> over one shared memory layer — right inside VS Code.</p>

---

**No API key.** The extension drives the CLIs you already have installed and pay for (`claude`, `codex`) as subprocesses. Your keys, tokens, and auth never leave those tools.

## What it does

- **One chat, two agents.** Talk to Claude Code and Codex in the same panel and give each the same shared context.
- **Shared memory layer.** Notes, decisions, and skills live in one place both agents can read — no more re-explaining your project to each tool.
- **Local-first.** Everything runs on your machine. No cloud relay, no telemetry.
- **`@` mentions & attachments.** Pull files into the conversation; drag and drop to attach.
- **Model & effort pickers.** Choose the model and reasoning effort per turn.

## Requirements

- **VS Code** 1.90 or newer.
- The agent CLIs you want to use, already installed and signed in:
  - [Claude Code](https://claude.com/claude-code) (`claude` on your `PATH`)
  - Codex (`codex` on your `PATH`)

The extension calls whichever of these it finds; you don't need both.

## Getting started

1. Install the extension.
2. Open the **Agent Context** view from the activity bar (the chip icon).
3. Ask a question — pick Claude or Codex, and go.

## Privacy & security

This extension never reads, stores, or proxies your CLI credentials. It invokes the agent CLIs as subprocesses and shows you their output. Shared memory is stored locally in your workspace.

## License

MIT
