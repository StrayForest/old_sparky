import assert from "node:assert/strict";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { Linter } from "eslint";
import nextPlugin from "@next/eslint-plugin-next";

const fixtureRoot = fs.mkdtempSync(path.join(os.tmpdir(), "next-rootdir-glob-"));

try {
  const appRoot = path.join(fixtureRoot, "app-one");
  const secondAppRoot = path.join(fixtureRoot, "packages", "shop-two");
  const pagesRoot = path.join(appRoot, "pages");
  const secondPagesRoot = path.join(secondAppRoot, "pages");
  fs.mkdirSync(path.join(pagesRoot, "nested"), { recursive: true });
  fs.mkdirSync(path.join(secondPagesRoot, "remote"), { recursive: true });
  fs.writeFileSync(
    path.join(pagesRoot, "index.jsx"),
    'export default function Home() { return <a href="/nested">Nested</a>; }',
  );
  fs.writeFileSync(
    path.join(pagesRoot, "nested", "index.jsx"),
    "export default function Nested() { return <div>Nested</div>; }",
  );
  fs.writeFileSync(
    path.join(secondPagesRoot, "remote", "index.jsx"),
    "export default function Remote() { return <div>Remote</div>; }",
  );
  fs.writeFileSync(
    path.join(secondPagesRoot, "index.jsx"),
    'export default function Shop() { return <a href="/nested">Nested</a>; }',
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
      settings: {
        next: {
          rootDir: [
            path.join(fixtureRoot, "app-*"),
            path.join(fixtureRoot, "packages", "shop-*"),
          ],
        },
      },
      rules: { "@next/next/no-html-link-for-pages": "error" },
    },
  ];
  const firstRootMessages = linter.verify(
    'export default function Home() { return <a href="/remote">Remote</a>; }',
    config,
    { filename: path.join(appRoot, "pages", "index.jsx") },
  );
  const secondRootMessages = linter.verify(
    'export default function Shop() { return <a href="/nested">Nested</a>; }',
    config,
    { filename: path.join(secondPagesRoot, "index.jsx") },
  );
  const internalLinkMessages = [...firstRootMessages, ...secondRootMessages].filter(
    (message) => message.ruleId === "@next/next/no-html-link-for-pages",
  );
  assert.equal(
    internalLinkMessages.length,
    2,
    `expected Next.js to flag routes crossing both patterned rootDir entries; got ${JSON.stringify([...firstRootMessages, ...secondRootMessages])}`,
  );

  const allowedLinkMessages = linter.verify(
    'export default function Home() { return <><a href="https://example.com/nested">External</a><a href="#section">Same page</a></>; }',
    config,
    { filename: path.join(appRoot, "pages", "index.jsx") },
  );
  assert.equal(
    allowedLinkMessages.some(
      (message) => message.ruleId === "@next/next/no-html-link-for-pages",
    ),
    false,
    `expected Next.js to allow external and same-page links; got ${JSON.stringify(allowedLinkMessages)}`,
  );
} finally {
  fs.rmSync(fixtureRoot, { recursive: true, force: true });
}

console.log("Next rootDir glob compatibility contract passed.");
