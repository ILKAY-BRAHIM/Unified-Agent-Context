/** Webview chat panel: the UI you type into (D8). */

import { execFile } from "child_process";
import * as vscode from "vscode";
import {
  Agent,
  AgentEvent,
  Capabilities,
  ChatSession,
  EMPTY_CAPS,
  TurnOptions,
  discoverCapabilities,
  makeDriver,
  readCodexModels,
} from "../driver";

function nonce(): string {
  const chars = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789";
  return Array.from({ length: 32 }, () => chars.charAt(Math.floor(Math.random() * 62))).join("");
}

interface ProjectSkill {
  name: string;
  description: string;
}

/**
 * Project skills from `.agents/skills/` — the shared layer both agents use.
 *
 * Read through the `uac` CLI rather than the filesystem so skills.py stays the
 * only parser; a second implementation here would drift.
 */
function loadProjectSkills(cwd: string): Promise<ProjectSkill[]> {
  const cli = vscode.workspace.getConfiguration("uac").get("cliPath", "uac");
  return new Promise((resolve) => {
    execFile(cli, ["skills", "list", "--json"], { cwd, timeout: 10_000 }, (err, stdout) => {
      if (err) {
        resolve([]);
        return;
      }
      try {
        resolve(JSON.parse(stdout));
      } catch {
        resolve([]);
      }
    });
  });
}

export class ChatPanel {
  public static current: ChatPanel | undefined;

  private sessions = new Map<Agent, ChatSession>();
  private agent: Agent;
  private projectSkills: ProjectSkill[] = [];
  private disposables: vscode.Disposable[] = [];

  private constructor(
    private readonly panel: vscode.WebviewPanel,
    private readonly cwd: string,
    private readonly extensionUri: vscode.Uri
  ) {
    this.agent = vscode.workspace.getConfiguration("uac").get<Agent>("defaultAgent", "claude-code");
    this.panel.webview.html = this.html();
    this.panel.webview.postMessage({ type: "init", agent: this.agent });
    // Render controls straight away, then fill in what only the CLI can tell us.
    this.sendControls("claude-code");
    this.sendControls("codex");
    void this.discover();

    this.panel.webview.onDidReceiveMessage((m) => this.onMessage(m), null, this.disposables);
    this.panel.onDidDispose(() => this.dispose(), null, this.disposables);
  }

  /**
   * Attach files from outside the webview (the Explorer's right-click menu).
   *
   * Dragging from the Explorer into a webview is swallowed by VS Code itself
   * (microsoft/vscode#182449), so this is the route that always works.
   */
  static attachFiles(uris: vscode.Uri[], cwd: string, extensionUri: vscode.Uri): void {
    ChatPanel.show(cwd, extensionUri);
    ChatPanel.current?.send_attachments(uris);
    ChatPanel.current?.panel.reveal(undefined, true);
  }

  static show(cwd: string, extensionUri: vscode.Uri): void {
    if (ChatPanel.current) {
      ChatPanel.current.panel.reveal();
      return;
    }
    const panel = vscode.window.createWebviewPanel(
      "uacChat",
      "Agent Chat",
      vscode.ViewColumn.Beside,
      {
        enableScripts: true,
        retainContextWhenHidden: true,
        localResourceRoots: [vscode.Uri.joinPath(extensionUri, "media"), vscode.Uri.joinPath(extensionUri, "out")],
      }
    );
    ChatPanel.current = new ChatPanel(panel, cwd, extensionUri);
  }

  private session(agent: Agent): ChatSession {
    let session = this.sessions.get(agent);
    if (!session) {
      const driver = makeDriver(agent, this.paths());
      // One session per agent, so switching mid-conversation doesn't lose
      // either side's thread.
      session = new ChatSession(driver, this.cwd);
      // The CLI reports its own commands/skills/subagents at session start;
      // forward them so the UI offers what actually exists.
      session.onCapabilities = (caps) =>
        this.panel.webview.postMessage({
          type: "capabilities",
          agent,
          caps,
          controls: driver.controls(caps),
        });
      this.sessions.set(agent, session);
    }
    return session;
  }

  private paths(): { claude: string; codex: string } {
    const cfg = vscode.workspace.getConfiguration("uac");
    return { claude: cfg.get("claudePath", "claude"), codex: cfg.get("codexPath", "codex") };
  }

  private post(agent: Agent, caps: Capabilities): void {
    this.panel.webview.postMessage({
      type: "capabilities",
      agent,
      caps,
      controls: makeDriver(agent, this.paths()).controls(caps),
    });
  }

  private sendControls(agent: Agent): void {
    this.post(agent, EMPTY_CAPS);
  }

  /**
   * Populate skills, commands, subagents and models before the first message.
   *
   * Without this, typing "/" on a fresh panel lists nothing: capabilities
   * otherwise only arrive once a turn starts. The probe is a free local slash
   * command (num_turns 0, $0).
   */
  private async discover(): Promise<void> {
    // Project skills reach BOTH agents, so they're sent independently of
    // whatever either CLI reports about itself.
    this.projectSkills = await loadProjectSkills(this.cwd);
    if (this.projectSkills.length) {
      this.panel.webview.postMessage({ type: "projectSkills", skills: this.projectSkills });
    }

    // Codex tells us nothing at runtime, but it caches its account's model list
    // on disk — including each model's own effort scale.
    const codex = readCodexModels();
    if (codex.models.length) {
      this.post("codex", codex);
    }

    const driver = makeDriver("claude-code", this.paths());
    const caps = await discoverCapabilities(this.paths().claude, this.cwd, driver);
    if (caps.skills.length || caps.models.length) {
      this.post("claude-code", caps);
    }
  }

  /**
   * Attach files by path.
   *
   * Both CLIs read files by path, so a path is all either agent needs — which
   * works identically for Claude and Codex, unlike --file / -i which differ per
   * agent and only cover some file types.
   */
  private send_attachments(uris: vscode.Uri[]): void {
    const files = uris.map((uri) => ({
      path: vscode.workspace.asRelativePath(uri, false),
      name: uri.path.split("/").pop() ?? "",
    }));
    this.panel.webview.postMessage({ type: "attachments", files });
  }

  /**
   * Files matching what you've typed after "@".
   *
   * Searched on demand rather than shipping a file list to the webview: a real
   * repo has thousands, and VS Code's own search already respects .gitignore and
   * the user's exclude settings.
   */
  private async findFiles(query: string): Promise<void> {
    const safe = query.replace(/[*?{}[\]]/g, "");
    const found = await vscode.workspace.findFiles(
      `**/*${safe}*`,
      "**/{node_modules,.git,.next,dist,out,__pycache__,.venv}/**",
      20
    );
    const files = found.map((uri) => ({
      path: vscode.workspace.asRelativePath(uri, false),
      name: uri.path.split("/").pop() ?? "",
    }));
    // Shallow paths first — the file you mean is rarely six levels down.
    files.sort((a, b) => a.path.split("/").length - b.path.split("/").length || a.path.localeCompare(b.path));
    this.panel.webview.postMessage({ type: "fileResults", files });
  }

  private async attach(): Promise<void> {
    const picked = await vscode.window.showOpenDialog({
      canSelectMany: true,
      openLabel: "Attach",
      defaultUri: vscode.Uri.file(this.cwd),
    });
    if (picked?.length) {
      this.send_attachments(picked);
    }
  }

  /**
   * Files dropped onto the composer.
   *
   * A webview can't see an OS file's path (browsers don't expose it), but a drag
   * from VS Code's own Explorer carries `text/uri-list` with real file:// URIs —
   * which is the case worth supporting. Directories are dropped too, and a path
   * is just as useful to the agent.
   */
  private dropped(uris: string[]): void {
    const parsed = uris
      .map((u) => u.trim())
      .filter((u) => u.startsWith("file://"))
      .map((u) => vscode.Uri.parse(u));
    if (parsed.length) {
      this.send_attachments(parsed);
    }
  }

  private async onMessage(msg: {
    type: string;
    text?: string;
    agent?: Agent;
    opts?: TurnOptions;
    uris?: string[];
    query?: string;
  }): Promise<void> {
    if (msg.type === "switchAgent" && msg.agent) {
      this.agent = msg.agent;
      return;
    }
    if (msg.type === "attach") {
      await this.attach();
      return;
    }
    if (msg.type === "dropped" && msg.uris) {
      this.dropped(msg.uris);
      return;
    }
    if (msg.type === "warn" && msg.text) {
      vscode.window.showWarningMessage(msg.text);
      return;
    }
    if (msg.type === "findFiles") {
      await this.findFiles(msg.query ?? "");
      return;
    }
    if (msg.type === "stop") {
      // The turn's own await resolves on kill and posts turnEnd, so there's
      // nothing to clean up here.
      this.sessions.get(this.agent)?.abort();
      return;
    }
    if (msg.type !== "send" || !msg.text) {
      return;
    }

    const post = (event: AgentEvent) => this.panel.webview.postMessage({ type: "event", event });
    this.panel.webview.postMessage({ type: "turnStart", agent: this.agent });
    try {
      const session = this.session(this.agent);
      // One skill file, two delivery routes: Claude already has it as a native
      // slash command, Codex gets told to fetch it over MCP.
      const prompt = makeDriver(this.agent, this.paths()).expandPrompt(
        msg.text,
        this.projectSkills.map((s) => s.name)
      );
      await session.send(prompt, post, msg.opts ?? {});
    } catch (err) {
      post({ kind: "error", text: String(err) });
    }
    this.panel.webview.postMessage({ type: "turnEnd" });
  }

  private dispose(): void {
    ChatPanel.current = undefined;
    this.disposables.forEach((d) => d.dispose());
    this.panel.dispose();
  }

  private html(): string {
    const n = nonce();
    const web = this.panel.webview;
    const css = web.asWebviewUri(vscode.Uri.joinPath(this.extensionUri, "media", "chat.css"));
    const md = web.asWebviewUri(vscode.Uri.joinPath(this.extensionUri, "out", "webview", "markdown.js"));
    const js = web.asWebviewUri(vscode.Uri.joinPath(this.extensionUri, "out", "webview", "main.js"));
    const csp = [
      "default-src 'none'",
      `style-src ${web.cspSource}`,
      `script-src 'nonce-${n}'`,
    ].join("; ");

    return /* html */ `<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta http-equiv="Content-Security-Policy" content="${csp}">
<link rel="stylesheet" href="${css}">
<title>Agent Chat</title>
</head>
<body>
  <main id="log">
    <div class="empty">
      <svg class="empty__logo" width="52" height="52" viewBox="0 0 64 64" fill="none" aria-hidden="true">
        <g stroke-linecap="round" stroke-width="3.2">
          <path d="M27 22 L22 9" stroke="var(--vscode-descriptionForeground)"/><path d="M37 22 L42 9" stroke="var(--vscode-descriptionForeground)"/>
          <path d="M20 27 L7 22" stroke="var(--vscode-descriptionForeground)"/><path d="M20 37 L8 44" stroke="var(--vscode-descriptionForeground)"/>
          <path d="M44 27 L57 22" stroke="var(--vscode-descriptionForeground)"/><path d="M44 37 L56 44" stroke="var(--vscode-descriptionForeground)"/>
          <path d="M27 44 L20 58" stroke="var(--claude)"/><path d="M37 44 L44 58" stroke="var(--codex)"/>
        </g>
        <rect x="19" y="19" width="26" height="26" rx="6" fill="var(--claude)" stroke="var(--vscode-foreground)" stroke-width="2.4"/>
        <g stroke="var(--vscode-editor-background)" stroke-width="1.9" stroke-linecap="round" opacity="0.5">
          <path d="M27.7 23 L27.7 41"/><path d="M36.3 23 L36.3 41"/><path d="M23 27.7 L41 27.7"/><path d="M23 36.3 L41 36.3"/>
        </g>
      </svg>
      <b>Ask Claude or Codex</b>
      Both read the same project memory, so whatever one learns, the other can use.
      <span class="hint">Type <kbd>/</kbd> for skills and commands.</span>
    </div>
  </main>

  <div id="composer" data-agent="claude-code">
    <div id="palette" hidden role="listbox" aria-label="Skills and commands"></div>
    <div id="menu" hidden role="menu"></div>
    <div class="box">
      <div id="files" aria-label="Attached files"></div>
      <textarea id="input" rows="1" placeholder="Ask about this project…  (drop files here)" aria-label="Message"></textarea>
      <div class="footer">
        <button class="icon" id="attach" type="button" title="Add context" aria-label="Add context">+</button>
        <button class="icon" id="slash" type="button" title="Skills and commands" aria-label="Skills and commands">/</button>
        <!-- Which agent is the most important choice on this panel, so it sits
             with the rest of the turn's settings, not in a bar far away. -->
        <select id="agent" aria-label="Which agent to talk to">
          <option value="claude-code">Claude Code</option>
          <option value="codex">Codex</option>
        </select>
        <div id="controls" aria-label="Agent settings"></div>
        <button id="send" class="send" type="button" title="Send (Enter)" aria-label="Send">↑</button>
      </div>
    </div>
  </div>

  <!-- markdown.js first: it defines the globals main.js uses. -->
  <script nonce="${n}" src="${md}"></script>
  <script nonce="${n}" src="${js}"></script>
</body>
</html>`;
  }
}
