import { Router, type IRouter } from "express";
import healthRouter from "./health.js";
import stubsRouter from "./stubs.js";
import authRouter from "./auth.js";
import setupRouter from "./setup.js";
import smsRouter from "./sms.js";
import devicesRouter from "./devices.js";
import panelRouter from "./panel.js";
import eventsRouter from "./events.js";
import shareRouter from "./share.js";
import notifyChannelsRouter from "./notify-channels.js";
import notifySettingsRouter from "./notify-settings.js";
import pushTokenRouter from "./push-token.js";

const router: IRouter = Router();

// ── Health ────────────────────────────────────────────────────────────────
router.use(healthRouter);

// ── First-run setup (unauthenticated, only works when no users exist) ─────
router.use("/setup", setupRouter);

// ── Auth ──────────────────────────────────────────────────────────────────
router.use("/auth", authRouter);

// ── Real-Time SSE — /api/events ───────────────────────────────────────────
// Must be registered BEFORE stubsRouter so the events path isn't shadowed.
router.use(eventsRouter);

// ── Notification channels (reads telegram-bots store.json) ────────────────
// Registered BEFORE stubs so the real implementation wins.
router.use(notifyChannelsRouter);

// ── Per-device notification preferences (persisted to DB) ─────────────────
// Registered BEFORE stubs so the real implementation wins.
router.use(notifySettingsRouter);

// ── Stubs for secondary frontend features ─────────────────────────────────
router.use(stubsRouter);

// ── SMS (REST + SSE) ──────────────────────────────────────────────────────
router.use("/sms", smsRouter);

// ── Devices (REST snapshot + CRUD) ────────────────────────────────────────
router.use("/devices", devicesRouter);

// ── Panel Config + User Management ────────────────────────────────────────
router.use("/panel", panelRouter);

// ── Share link generation (used by mobile) ────────────────────────────────
router.use(shareRouter);

// ── Expo push token registration ──────────────────────────────────────────
router.use(pushTokenRouter);

export default router;
