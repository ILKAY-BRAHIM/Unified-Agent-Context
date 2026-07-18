import * as vscode from "vscode";
import { ChatPanel } from "./chat/panel";
import { MemoryItem, MemoryProvider } from "./memory/view";

export function activate(context: vscode.ExtensionContext): void {
  const folder = vscode.workspace.workspaceFolders?.[0];

  const requireFolder = (): string | undefined => {
    if (!folder) {
      vscode.window.showErrorMessage(
        "Open a folder first — the agents run in your project directory."
      );
      return undefined;
    }
    return folder.uri.fsPath;
  };

  const memories = new MemoryProvider(folder?.uri.fsPath ?? process.cwd());
  context.subscriptions.push(
    vscode.window.registerTreeDataProvider("uacMemories", memories),
    vscode.commands.registerCommand("uac.openChat", () => {
      const cwd = requireFolder();
      if (cwd) {
        ChatPanel.show(cwd, context.extensionUri);
      }
    }),
    // Explorer right-click. VS Code passes the clicked uri plus the whole
    // selection, so multi-select attaches in one go.
    vscode.commands.registerCommand("uac.attachFile", (uri?: vscode.Uri, selected?: vscode.Uri[]) => {
      const cwd = requireFolder();
      if (!cwd) {
        return;
      }
      const uris = selected?.length ? selected : uri ? [uri] : [];
      if (uris.length) {
        ChatPanel.attachFiles(uris, cwd, context.extensionUri);
      }
    }),
    vscode.commands.registerCommand("uac.refreshMemories", () => memories.refresh()),
    vscode.commands.registerCommand("uac.addMemory", () => memories.add()),
    vscode.commands.registerCommand("uac.forgetMemory", (item: MemoryItem) =>
      memories.forget(item)
    )
  );

  // A memory the agents wrote during a session won't show up on its own —
  // refresh when the window regains focus, which is when you'd look.
  context.subscriptions.push(
    vscode.window.onDidChangeWindowState((s) => {
      if (s.focused) {
        memories.refresh();
      }
    })
  );
}

export function deactivate(): void {
  // Child processes are per-turn and exit on their own.
}
