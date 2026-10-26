import { resolve } from "node:path";

/** Resolve the release root from Next's generated standalone server cwd. */
export function releaseMetadataPathFromStandaloneCwd(cwd) {
  return resolve(cwd, "../../../..", "RELEASE.json");
}
