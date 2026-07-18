/**
 * Sidebar showing the shared memory both agents read from.
 *
 * This is the part of the extension that doesn't already exist elsewhere: Claude
 * Code and Codex each ship their own chat, but neither can show you what the
 * *other* one has learned. Seeing the store — and being able to delete a wrong
 * memory before it misleads the other agent — is the point.
 *
 * Reads through the `uac` CLI rather than opening memory.db directly, so the
 * schema stays owned by one place (src/uac/memory.py).
 */

import { execFile } from "child_process";
import * as vscode from "vscode";

export interface Memory {
  id: string;
  content: string;
  kind: string;
  source: string;
  tags: string[];
  created_at: string;
  origin_project: string;
}

function uacPath(): string {
  return vscode.workspace.getConfiguration("uac").get("cliPath", "uac");
}

function run(args: string[], cwd: string): Promise<string> {
  return new Promise((resolve, reject) => {
    execFile(uacPath(), args, { cwd }, (err, stdout, stderr) => {
      if (err) {
        reject(new Error(stderr?.trim() || err.message));
        return;
      }
      resolve(stdout);
    });
  });
}

/** `kind` -> a codicon that reads at a glance in the tree. */
const ICONS: Record<string, string> = {
  decision: "law",
  fact: "info",
  gotcha: "warning",
  preference: "person",
};

export class MemoryItem extends vscode.TreeItem {
  constructor(public readonly memory: Memory) {
    super(memory.content.split("\n")[0], vscode.TreeItemCollapsibleState.None);

    const foreign =
      memory.origin_project && memory.origin_project !== "current"
        ? ` · from ${memory.origin_project}`
        : "";
    this.description = `${memory.kind} · ${memory.source}${foreign}`;
    this.iconPath = new vscode.ThemeIcon(ICONS[memory.kind] ?? "circle-filled");
    this.contextValue = "uacMemory";
    this.tooltip = new vscode.MarkdownString(
      `${memory.content}\n\n---\n\n` +
        `**kind** ${memory.kind}  \n` +
        `**saved by** ${memory.source}  \n` +
        (memory.tags.length ? `**tags** ${memory.tags.join(", ")}  \n` : "") +
        `**when** ${memory.created_at}`
    );
  }
}

export class MemoryProvider implements vscode.TreeDataProvider<MemoryItem> {
  private changed = new vscode.EventEmitter<void>();
  readonly onDidChangeTreeData = this.changed.event;

  constructor(private readonly cwd: string) {}

  refresh(): void {
    this.changed.fire();
  }

  getTreeItem(item: MemoryItem): vscode.TreeItem {
    return item;
  }

  async getChildren(): Promise<MemoryItem[]> {
    try {
      const raw = await run(["memory", "list", "--json", "--limit", "100"], this.cwd);
      const memories: Memory[] = JSON.parse(raw);
      return memories.map((m) => new MemoryItem(m));
    } catch (err) {
      // An empty store is normal, not an error worth a modal.
      vscode.window.showWarningMessage(`uac: could not read memories — ${err}`);
      return [];
    }
  }

  async forget(item: MemoryItem): Promise<void> {
    const answer = await vscode.window.showWarningMessage(
      `Delete this memory? Both agents will stop seeing it.\n\n"${item.memory.content.slice(0, 160)}"`,
      { modal: true },
      "Delete"
    );
    if (answer !== "Delete") {
      return;
    }
    await run(["memory", "forget", item.memory.id], this.cwd);
    this.refresh();
  }

  async add(): Promise<void> {
    const content = await vscode.window.showInputBox({
      title: "Save a shared memory",
      prompt: "One durable, non-obvious fact. Both agents will see it.",
      placeHolder: "e.g. Staging deploys need VPN — the runner has no direct DB access",
      validateInput: (v) => (v.trim().length < 8 ? "Too short to be useful" : undefined),
    });
    if (!content) {
      return;
    }
    const kind = await vscode.window.showQuickPick(
      [
        { label: "gotcha", detail: "A trap that will bite someone again" },
        { label: "decision", detail: "A choice made, and why" },
        { label: "fact", detail: "Something true and non-obvious" },
        { label: "preference", detail: "How you like things done" },
      ],
      { title: "What kind of memory?" }
    );
    if (!kind) {
      return;
    }
    await run(["memory", "add", content, "--kind", kind.label], this.cwd);
    this.refresh();
  }
}
