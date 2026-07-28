/**
 * Database migration script — run via `node ./dist/migrate.mjs` as a
 * Railway pre-deploy command (or standalone: `pnpm migrate`).
 *
 * Uses drizzle-orm's migrate() helper which applies any pending SQL
 * migration files from ./drizzle.  Falls back silently when no migrations
 * folder exists (schema-push workflow).
 *
 * All errors are non-fatal: a DB connectivity issue at deploy time must not
 * block the rollout — the server starts with its existing schema.
 */

async function main() {
  if (!process.env.DATABASE_URL) {
    console.log("[migrate] DATABASE_URL not set — skipping migrations.");
    return;
  }

  let pool: { end(): Promise<void> } | undefined;

  try {
    // Dynamic import keeps the top-level throw from lib/db inside this try block.
    const dbModule = await import("@workspace/db");
    pool = dbModule.pool;
    const db = dbModule.db;

    const { migrate } = await import("drizzle-orm/node-postgres/migrator");
    await migrate(db, { migrationsFolder: "./drizzle" });
    console.log("[migrate] Migrations applied successfully.");
  } catch (err: unknown) {
    const msg = err instanceof Error ? err.message : String(err);
    if (msg.includes("ENOENT") || msg.includes("no such file")) {
      console.log("[migrate] No migrations folder found — skipping.");
    } else {
      // Non-fatal: log the warning but let the deployment proceed.
      console.warn("[migrate] Migration warning (non-fatal):", msg);
    }
  } finally {
    try {
      await pool?.end();
    } catch {
      // pool.end() errors are also non-fatal.
    }
  }
}

main().catch((err) => {
  console.warn("[migrate] Unexpected error (non-fatal):", err);
  // Do NOT process.exit(1) — preDeployCommand failure blocks the deployment.
});
