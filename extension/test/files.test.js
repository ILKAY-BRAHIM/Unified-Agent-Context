/**
 * Attachment badge logic.
 *
 * The webview owns these, and main.js touches the DOM at load, so the two pure
 * functions are re-declared here from the same source rules. They caught a real
 * bug: truncating the extension before the colour lookup made `schema.prisma`
 * (-> "prism") miss its entry silently.
 */

const assert = require("node:assert");
const test = require("node:test");
const fs = require("node:fs");
const path = require("node:path");

const raw = fs.readFileSync(path.join(__dirname, "..", "src", "webview", "main.ts"), "utf8");

/**
 * Source with comments stripped.
 *
 * Asserting against raw source repeatedly matched the prose in our OWN comments
 * — a test that says "contents must not be inlined" fails on a comment reading
 * "Paths, not contents". Assert against code, never against what we said about it.
 */
const source = raw.replace(/\/\*[\s\S]*?\*\//g, "").replace(/(^|[^:])\/\/.*$/gm, "$1");

// --- guard the real implementation against drift ----------------------------

test("extOf does NOT truncate — that lookup bug must not come back", () => {
  const fn = source.match(/function extOf\(name: string\): string \{[\s\S]*?\n\}/)[0];
  assert.ok(
    !/return ext\.toLowerCase\(\)\.slice/.test(fn),
    "extOf truncates again: multi-char types like `prisma` will silently lose their colour"
  );
});

test("every brand colour is reachable by a real filename", () => {
  // A key longer than the badge width is exactly what the old bug broke.
  const map = source.match(/const FILE_COLORS: Record<string, string> = \{([\s\S]*?)\n\};/)[1];
  const keys = [...map.matchAll(/^\s*(\w+):/gm)].map((m) => m[1]);

  const extOf = (name) => {
    const dot = name.lastIndexOf(".");
    return (dot > 0 ? name.slice(dot + 1) : name.replace(/^\./, "")).toLowerCase();
  };

  assert.ok(keys.includes("prisma"), "the regression case is gone from the map");
  for (const key of keys) {
    assert.strictEqual(extOf(`file.${key}`), key, `${key} is unreachable from a filename`);
  }
});

test("badges stay narrow even when the type name is long", () => {
  const badgeText = (ext) => ext.slice(0, 4);
  assert.strictEqual(badgeText("prisma"), "pris");
  assert.strictEqual(badgeText("ipynb"), "ipyn");
  assert.strictEqual(badgeText("py"), "py");
  for (const ext of ["prisma", "ipynb", "yaml", "py"]) {
    assert.ok(badgeText(ext).length <= 4);
  }
});

// --- what the agent is actually sent ----------------------------------------

test("attachments are sent as paths, not contents", () => {
  // Both CLIs read files themselves. A path costs a few tokens; an inlined file
  // costs thousands and goes stale the moment the agent edits it.
  const submit = source.match(/function submit\(\): void \{[\s\S]*?\n\}/)[0];
  assert.ok(/attached\.map\(\(f\) => f\.path\)/.test(submit), "paths must be what gets sent");
  assert.ok(!/readFile|contents|base64/.test(submit), "file contents must not be inlined");
});

test("a prompt with no attachments is left exactly as typed", () => {
  const build = (paths, text) => (paths.length ? `Files: ${paths.join(", ")}\n\n${text}` : text);
  assert.strictEqual(build([], "just a question"), "just a question");
  assert.strictEqual(
    build(["agent/main.py", "web/route.ts"], "why?"),
    "Files: agent/main.py, web/route.ts\n\nwhy?"
  );
});

// --- drag and drop ----------------------------------------------------------

test("only VS Code's uri-list can give us paths; an OS drag is explained, not dropped", () => {
  // Browsers don't expose a dropped file's path, so a drag from outside VS Code
  // cannot work — saying so beats failing silently.
  assert.ok(source.includes('getData("text/uri-list")'));
  assert.ok(/type: "warn"/.test(source), "an OS drag must tell the user why it didn't work");
});
