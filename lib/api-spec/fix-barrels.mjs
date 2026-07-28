/**
 * Post-codegen barrel fix.
 *
 * Orval appends to existing barrel files instead of replacing them, which
 * causes duplicate export errors on every codegen run after the first.
 * This script runs after orval and rewrites both barrels to their correct
 * final state.
 *
 * api-zod: Only re-exports the Zod schemas. The generated/types folder has
 *   conflicting names (Orval uses <OperationId>Params for BOTH path-param Zod
 *   schemas AND query-param TypeScript interfaces), so we skip it entirely —
 *   the inferred types from the Zod consts are sufficient for backend usage.
 *
 * api-client-react: Re-exports generated hooks + schemas, plus the custom
 *   fetch helpers (setBaseUrl, setAuthTokenGetter) from custom-fetch.ts.
 */

import { writeFileSync } from "node:fs";
import { resolve, dirname } from "node:path";
import { fileURLToPath } from "node:url";

const root = resolve(dirname(fileURLToPath(import.meta.url)), "../..");

writeFileSync(
  resolve(root, "lib/api-zod/src/index.ts"),
  `// Only re-export the Zod schema consts.
// generated/types is intentionally excluded: Orval uses <OperationId>Params
// for both path-param Zod consts and query-param TS interfaces, which causes
// TS2308 ambiguity errors when both are barrel-re-exported. The Zod-inferred
// types are sufficient for backend validation.
export * from "./generated/api";
`,
);

writeFileSync(
  resolve(root, "lib/api-client-react/src/index.ts"),
  `export * from "./generated/api";
export * from "./generated/api.schemas";
export { setBaseUrl, setAuthTokenGetter } from "./custom-fetch";
export type { AuthTokenGetter } from "./custom-fetch";
`,
);

console.log("✓ Barrels fixed");
