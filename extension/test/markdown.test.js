/**
 * Tests for the webview markdown renderer.
 *
 * The escaping here is the security boundary: agent output is untrusted (it can
 * echo anything the model read off disk) and we assign it with innerHTML. If
 * escaping regresses, the chat panel becomes an XSS vector inside the editor.
 *
 * markdown.js touches no DOM, so it loads straight into Node.
 *
 *   node --test extension/test/
 */

const assert = require("node:assert");
const test = require("node:test");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");

const source = fs.readFileSync(path.join(__dirname, "..", "out", "webview", "markdown.js"), "utf8");
const sandbox = {};
vm.createContext(sandbox);
vm.runInContext(source, sandbox);
const { renderMarkdown, escapeHtml } = sandbox;

// --- the security boundary --------------------------------------------------

test("script tags in agent output are inert", () => {
  const html = renderMarkdown('<script>alert("xss")</script>');
  assert.ok(!html.includes("<script>"), "raw <script> survived");
  assert.ok(html.includes("&lt;script&gt;"));
});

test("img onerror payloads are inert", () => {
  const html = renderMarkdown('<img src=x onerror="alert(1)">');
  assert.ok(!html.includes("<img"), "raw <img> survived");
  assert.ok(html.includes("&lt;img"));
});

test("javascript: links are not rendered as anchors", () => {
  const html = renderMarkdown("[click me](javascript:alert(1))");
  assert.ok(!html.includes("<a "), "javascript: became a live link");
  assert.ok(html.includes("click me"));
});

test("data: links are not rendered as anchors", () => {
  const html = renderMarkdown("[x](data:text/html,<script>alert(1)</script>)");
  assert.ok(!html.includes("<a "));
});

test("http and https links are rendered", () => {
  assert.ok(renderMarkdown("[docs](https://example.com/a)").includes('<a href="https://example.com/a">docs</a>'));
});

test("html inside fenced code is escaped, not executed", () => {
  const html = renderMarkdown("```html\n<script>alert(1)</script>\n```");
  assert.ok(!html.includes("<script>alert"));
  assert.ok(html.includes("&lt;script&gt;"));
});

test("quotes and ampersands cannot break out of attributes", () => {
  assert.strictEqual(escapeHtml(`" & ' < >`), "&quot; &amp; &#39; &lt; &gt;");
});

// --- what agents actually emit ---------------------------------------------

test("fenced code becomes a figure with its language and a copy button", () => {
  const html = renderMarkdown("```ts\nconst x = 1;\n```");
  assert.ok(html.includes('<figure class="code">'));
  assert.ok(html.includes("<span>ts</span>"));
  assert.ok(html.includes('<button class="copy"'));
  assert.ok(html.includes("const x = 1;"));
});

test("code fence with no language still renders", () => {
  assert.ok(renderMarkdown("```\nplain\n```").includes("<span>code</span>"));
});

test("prose around a code block survives", () => {
  const html = renderMarkdown("Run this:\n\n```sh\nnpm test\n```\n\nThen check.");
  assert.ok(html.includes("<p>Run this:</p>"));
  assert.ok(html.includes("npm test"));
  assert.ok(html.includes("<p>Then check.</p>"));
});

test("inline code, bold and italic", () => {
  assert.ok(renderMarkdown("use `npm run build`").includes("<code>npm run build</code>"));
  assert.ok(renderMarkdown("**important**").includes("<strong>important</strong>"));
  assert.ok(renderMarkdown("*emphasis*").includes("<em>emphasis</em>"));
});

test("bold is not mangled into italic", () => {
  const html = renderMarkdown("**bold**");
  assert.ok(!html.includes("<em>"), "** was eaten by the italic rule");
});

test("bullet lists group into one ul", () => {
  const html = renderMarkdown("- one\n- two\n- three");
  assert.strictEqual((html.match(/<ul>/g) || []).length, 1);
  assert.strictEqual((html.match(/<li>/g) || []).length, 3);
});

test("numbered lists become ol", () => {
  const html = renderMarkdown("1. first\n2. second");
  assert.ok(html.includes("<ol>"));
  assert.strictEqual((html.match(/<li>/g) || []).length, 2);
});

test("a list ends when prose resumes", () => {
  const html = renderMarkdown("- one\n\nAfter the list.");
  assert.ok(html.indexOf("</ul>") < html.indexOf("After the list."));
});

test("headings render", () => {
  assert.ok(renderMarkdown("## Findings").includes("<h4>Findings</h4>"));
});

test("empty input renders nothing", () => {
  assert.strictEqual(renderMarkdown(""), "");
});

test("plain text with no markdown is a paragraph", () => {
  assert.strictEqual(renderMarkdown("just words"), "<p>just words</p>");
});

test("a code-block placeholder in user text cannot forge a block", () => {
  // The placeholder syntax must not be reachable from the input itself.
  const html = renderMarkdown("CODE0");
  assert.ok(!html.includes("<figure"), "text forged a code block");
});
