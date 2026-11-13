import { copyFileSync, mkdirSync } from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

const webRoot = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const source = path.resolve(webRoot, "../../python_packages/platform_domain/appearance_palette.json");
const generatedDirectory = path.join(webRoot, "node_modules/.cache/platform");
const generated = path.join(generatedDirectory, "appearance_palette.json");

mkdirSync(generatedDirectory, { recursive: true });
copyFileSync(source, generated);
