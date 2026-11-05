import { createHash } from "node:crypto";
import { SourceMap } from "node:module";
import { constants, closeSync, fstatSync, lstatSync, openSync, realpathSync, readSync } from "node:fs";
import { isAbsolute, relative, resolve, sep } from "node:path";
import { fileURLToPath } from "node:url";

export const CPU_PROFILE_SOURCE_MAP_LIMITS = Object.freeze({
  files: 192,
  mapBytes: 2 * 1024 * 1024,
  totalBytes: 12 * 1024 * 1024,
  generatedBytes: 16 * 1024 * 1024,
});

function within(root, candidate) {
  const rel = relative(root, candidate);
  return rel !== "" && rel !== ".." && !rel.startsWith(`..${sep}`) && !isAbsolute(rel);
}

function regularNoSymlink(path, maxBytes = Number.MAX_SAFE_INTEGER) {
  try {
    const info = lstatSync(path);
    return info.isFile() && !info.isSymbolicLink() && info.size > 0 && info.size <= maxBytes;
  } catch {
    return false;
  }
}

function hashRegularFile(path, maxBytes = Number.MAX_SAFE_INTEGER) {
  let fd;
  try {
    fd = openSync(path, constants.O_RDONLY | constants.O_NOFOLLOW);
    const info = fstatSync(fd);
    if (!info.isFile() || info.size <= 0 || info.size > maxBytes) return null;
    const hash = createHash("sha256");
    const buffer = Buffer.alloc(64 * 1024);
    let offset = 0;
    while (offset < info.size) {
      const bytes = readSync(fd, buffer, 0, Math.min(buffer.length, info.size - offset), offset);
      if (bytes <= 0) return null;
      hash.update(buffer.subarray(0, bytes));
      offset += bytes;
    }
    const after = fstatSync(fd);
    if (after.dev !== info.dev || after.ino !== info.ino || after.size !== info.size) return null;
    return hash.digest("hex");
  } catch {
    return null;
  } finally {
    if (fd !== undefined) closeSync(fd);
  }
}

function readRegularFile(path, maxBytes) {
  let fd;
  try {
    fd = openSync(path, constants.O_RDONLY | constants.O_NOFOLLOW);
    const info = fstatSync(fd);
    if (!info.isFile() || info.size <= 0 || info.size > maxBytes) return null;
    const buffer = Buffer.alloc(info.size);
    const bytes = readSync(fd, buffer, 0, info.size, 0);
    const after = fstatSync(fd);
    if (bytes !== info.size || after.dev !== info.dev || after.ino !== info.ino || after.size !== info.size) return null;
    return buffer;
  } catch {
    return null;
  } finally {
    if (fd !== undefined) closeSync(fd);
  }
}

function sourcePath(url) {
  try {
    if (typeof url !== "string" || url.length === 0) return null;
    const path = url.startsWith("file:") ? fileURLToPath(url) : url;
    return isAbsolute(path) ? path : null;
  } catch {
    return null;
  }
}

/** Maps only sampled frames from verified standalone chunks; every failure keeps the input frame. */
export function mapSampledFrames(nodes, sampledNodeIds, runtimeCwd, limits = CPU_PROFILE_SOURCE_MAP_LIMITS) {
  const appRoot = resolve(runtimeCwd, "../..");
  const standaloneRoot = resolve(runtimeCwd);
  const runtimeChunks = resolve(standaloneRoot, ".next/server/chunks");
  const siblingChunks = resolve(appRoot, ".next/server/chunks");
  const selected = new Set(sampledNodeIds);
  const output = new Map(nodes);
  const byFile = new Map();
  for (const id of selected) {
    const frame = nodes.get(id);
    const path = sourcePath(frame?.url);
    if (path === null || !within(runtimeChunks, path) || !Number.isSafeInteger(frame?.lineNumber)
        || frame.lineNumber < 0 || !Number.isSafeInteger(frame?.columnNumber) || frame.columnNumber < 0) continue;
    byFile.set(path, [...(byFile.get(path) ?? []), { id, frame }]);
  }
  if (byFile.size > limits.files) return output;
  let totalBytes = 0;
  for (const [runtimePath, frames] of byFile) {
    try {
      const runtimeReal = realpathSync(runtimePath);
      if (!within(realpathSync(runtimeChunks), runtimeReal) || !regularNoSymlink(runtimePath)) continue;
      const rel = relative(runtimeChunks, runtimePath);
      const siblingPath = resolve(siblingChunks, rel);
      if (!within(siblingChunks, siblingPath) || !regularNoSymlink(siblingPath)) continue;
      const siblingReal = realpathSync(siblingPath);
      if (!within(realpathSync(siblingChunks), siblingReal)) continue;
      const mapPath = `${siblingPath}.map`;
      if (!regularNoSymlink(mapPath, limits.mapBytes)) continue;
      const mapInfo = lstatSync(mapPath);
      if (totalBytes + mapInfo.size > limits.totalBytes) continue;
      const runtimeHash = hashRegularFile(runtimePath, limits.generatedBytes);
      const siblingHash = hashRegularFile(siblingPath, limits.generatedBytes);
      if (runtimeHash === null || runtimeHash !== siblingHash) continue;
      totalBytes += mapInfo.size;
      const mapBytes = readRegularFile(mapPath, limits.mapBytes);
      if (mapBytes === null || mapBytes.length !== mapInfo.size) continue;
      const map = new SourceMap(JSON.parse(mapBytes.toString("utf8")));
      for (const { id, frame } of frames) {
        const entry = map.findEntry(frame.lineNumber, frame.columnNumber);
        if (typeof entry.originalSource !== "string" || entry.originalSource.length === 0) continue;
        output.set(id, { ...frame, url: entry.originalSource });
      }
    } catch {
      // Missing, malformed, unsupported or out-of-bounds maps retain ordinary frame classification.
    }
  }
  return output;
}
