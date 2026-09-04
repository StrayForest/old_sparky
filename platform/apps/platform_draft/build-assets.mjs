import { copyFile, mkdir, rm } from "node:fs/promises";

const sourceRoot = new URL("./public/", import.meta.url);
const distRoot = new URL("./dist/", import.meta.url);
const draftRoot = new URL("./dist/draft/", import.meta.url);
const files = ["index.html", "styles.css", "app.js", "draft-core.js", "heroes.js"];

await rm(distRoot, { recursive: true, force: true });
await mkdir(draftRoot, { recursive: true });

for (const file of files) {
  await copyFile(new URL(file, sourceRoot), new URL(file, draftRoot));
}

console.log(`Prepared ${files.length} Draft assets under dist/draft/`);
