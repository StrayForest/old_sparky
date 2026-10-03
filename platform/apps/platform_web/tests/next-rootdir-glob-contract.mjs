import assert from "node:assert/strict";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { createRequire } from "node:module";
import { Linter } from "eslint";
import nextPlugin from "@next/eslint-plugin-next";

const resolveFromHere = createRequire(import.meta.url);
const pluginEntry = resolveFromHere.resolve("@next/eslint-plugin-next");
const globEntry = resolveFromHere.resolve("fast-glob", { paths: [pluginEntry] });
const glob = await import(globEntry);
const globPackagePath = path.join(path.dirname(path.dirname(globEntry)), "package.json");
const globPackage = JSON.parse(fs.readFileSync(globPackagePath, "utf8"));

assert.equal(globPackage.name, "tinyglobby");
assert.equal(globPackage.version, "0.2.17");
assert.equal(typeof glob.globSync, "function");

const fixtureRoot = fs.mkdtempSync(path.join(os.tmpdir(), "next-rootdir-glob-"));

try {
  const appRoot = path.join(fixtureRoot, "app-one");
  const pagesRoot = path.join(appRoot, "pages");
  fs.mkdirSync(path.join(pagesRoot, "nested"), { recursive: true });
  fs.writeFileSync(
    path.join(pagesRoot, "index.jsx"),
    'export default function Home() { return <a href="/nested">Nested</a>; }',
  );
  fs.writeFileSync(
    path.join(pagesRoot, "nested", "index.jsx"),
    "export default function Nested() { return <div>Nested</div>; }",
  );

  const linter = new Linter({ cwd: fixtureRoot });
  const config = [
    {
      files: ["**/*.jsx"],
      languageOptions: {
        ecmaVersion: "latest",
        sourceType: "module",
        parserOptions: { ecmaFeatures: { jsx: true } },
      },
      plugins: { "@next/next": nextPlugin },
      settings: { next: { rootDir: path.join(fixtureRoot, "app-*") } },
      rules: { "@next/next/no-html-link-for-pages": "error" },
    },
  ];
  const filename = path.join(appRoot, "pages", "index.jsx");
  const messages = linter.verify(
    'export default function Home() { return <a href="/nested">Nested</a>; }',
    config,
    { filename },
  );

  assert.ok(
    messages.some(
      (message) =>
        message.ruleId === "@next/next/no-html-link-for-pages" &&
        message.message.includes("Use `<Link />` from `next/link` instead"),
    ),
    `expected Next.js to flag internal navigation through patterned rootDir; got ${JSON.stringify(messages)}`,
  );

  const externalMessages = linter.verify(
    'export default function Home() { return <a href="https://example.com/nested">Nested</a>; }',
    config,
    { filename },
  );
  assert.equal(
    externalMessages.some((message) => message.ruleId === "@next/next/no-html-link-for-pages"),
    false,
    `expected Next.js to allow external navigation links; got ${JSON.stringify(externalMessages)}`,
  );
} finally {
  fs.rmSync(fixtureRoot, { recursive: true, force: true });
}

console.log("Next rootDir glob compatibility contract passed.");
