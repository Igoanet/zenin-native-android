import {
  pgTable,
  text,
  boolean,
  integer,
  bigint,
  serial,
  jsonb,
  timestamp,
  json,
  primaryKey,
  uniqueIndex,
} from "drizzle-orm/pg-core";

// Role constants used by both Node API and Python bot service
export const ROLE_VALUES = [
  "owner",
  "dev_admin",
  "base_admin",
  "user",
  "management",
] as const;
export type RoleValue = (typeof ROLE_VALUES)[number];

// ─── Users ────────────────────────────────────────────────────────────────────
export const users = pgTable("users", {
  /** Primary key — Telegram UID or generated UUID when Telegram is not used */
  id: text("id").primaryKey(),
  /** Display / human-readable name */
  name: text("name").notNull(),
  /** Public username (unique, shown in the UI) */
  username: text("username").notNull().unique(),
  /** Internal operator ID (unique, used for login alongside username) */
  userId: text("user_id").notNull().unique(),
  /** owner | dev_admin | base_admin | user */
  role: text("role").notNull().default("user"),
  passwordHash: text("password_hash").notNull(),
  passwordSalt: text("password_salt").notNull(),
  /** Telegram username for display */
  tgUsername: text("tg_username"),
  /** Telegram chat ID for sending OTPs (legacy text column) */
  tgChatId: text("tg_chat_id"),
  /**
   * Telegram UID as bigint — the primary reference used by the Python bot
   * service. Populated when a user is created via the Pyrogram bot. Legacy
   * accounts created through the Node API may have this null until they
   * complete Telegram OTP verification.
   */
  tgUid: bigint("tg_uid", { mode: "number" }).unique(),
  /** Panel password set by bot3 for first-time credential issuance */
  panelPassword: text("panel_password"),
  /** Whether the user has been notified to set a password (bot3 backfill) */
  passwordBackfillNotified: boolean("password_backfill_notified")
    .notNull()
    .default(false),
  /** Whether this account is currently active/allowed */
  accessGranted: boolean("access_granted").notNull().default(false),
  /** When the account expires (null = never) */
  accessExpiresAt: timestamp("access_expires_at"),
  /** Incremented on password change / forced logout to invalidate all tokens */
  tokenVersion: integer("token_version").notNull().default(0),
  createdAt: timestamp("created_at").notNull().defaultNow(),
  updatedAt: timestamp("updated_at").notNull().defaultNow(),
});

export type User = typeof users.$inferSelect;
export type InsertUser = typeof users.$inferInsert;

// ─── Login Events (Sessions) ───────────────────────────────────────────────
export const loginEvents = pgTable("login_events", {
  id: text("id").primaryKey(),
  userId: text("user_id")
    .notNull()
    .references(() => users.id, { onDelete: "cascade" }),
  ipAddress: text("ip_address"),
  userAgent: text("user_agent"),
  city: text("city"),
  region: text("region"),
  country: text("country"),
  createdAt: timestamp("created_at").notNull().defaultNow(),
  lastActiveAt: timestamp("last_active_at").notNull().defaultNow(),
  terminatedAt: timestamp("terminated_at"),
});

export type LoginEvent = typeof loginEvents.$inferSelect;

// ─── OTP Sessions ──────────────────────────────────────────────────────────
export const otpSessions = pgTable("otp_sessions", {
  id: text("id").primaryKey(),
  /** The userId attempting login */
  userId: text("user_id").notNull(),
  /** 6-digit code */
  code: text("code").notNull(),
  expiresAt: timestamp("expires_at").notNull(),
  used: boolean("used").notNull().default(false),
  attempts: integer("attempts").notNull().default(0),
  createdAt: timestamp("created_at").notNull().defaultNow(),
});

export type OtpSession = typeof otpSessions.$inferSelect;

// ─── Panel Configs (Firebase Projects) ────────────────────────────────────
export const panelConfigs = pgTable("panel_configs", {
  id: text("id").primaryKey(),
  /** Owner user ID */
  ownerId: text("owner_id")
    .notNull()
    .references(() => users.id, { onDelete: "cascade" }),
  /** Friendly display name */
  name: text("name").notNull(),
  /** Full Firebase RTDB URL, e.g. https://abc-default-rtdb.firebaseio.com */
  firebaseUrl: text("firebase_url").notNull(),
  /**
   * Encrypted Firebase Database Secret (or signed JWT).
   * Encrypted with AES-256-GCM using SESSION_SECRET.
   */
  firebaseSecret: text("firebase_secret").notNull(),
  isActive: boolean("is_active").notNull().default(true),
  createdAt: timestamp("created_at").notNull().defaultNow(),
});

export type PanelConfig = typeof panelConfigs.$inferSelect;
export type InsertPanelConfig = typeof panelConfigs.$inferInsert;

// ─── User Panel Data (localStorage sync) ──────────────────────────────────
export const userPanelData = pgTable("user_panel_data", {
  userId: text("user_id")
    .primaryKey()
    .references(() => users.id, { onDelete: "cascade" }),
  /** JSON blob containing synced panel settings */
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  data: json("data").$type<Record<string, any>>().notNull().default({}),
  updatedAt: timestamp("updated_at").notNull().defaultNow(),
});

export type UserPanelData = typeof userPanelData.$inferSelect;

// ─── Access Keys ───────────────────────────────────────────────────────────
export const accessKeys = pgTable("access_keys", {
  id: text("id").primaryKey(),
  /** The human-readable invite code (stored in DB column "key") */
  code: text("key").notNull().unique(),
  /** Role that will be assigned on redemption */
  role: text("role").notNull().default("user"),
  /** Optional human label set by the creator */
  label: text("label").notNull().default(""),
  /** How many seconds of access the key grants (null = unlimited) */
  durationSeconds: integer("duration_seconds"),
  /** Absolute expiry time once the key has been redeemed (null = no expiry) */
  expiresAt: timestamp("expires_at"),
  /** Whether the key has been revoked by an admin */
  revoked: boolean("revoked").notNull().default(false),
  /** Telegram UID of the user who redeemed the key */
  redeemedByTgUid: bigint("redeemed_by_tg_uid", { mode: "number" }),
  /** Telegram UID of the admin who created the key */
  createdByTgUid: bigint("created_by_tg_uid", { mode: "number" }),
  /** Role of the creator at the time of key creation */
  createdByRole: text("created_by_role"),
  createdAt: timestamp("created_at").notNull().defaultNow(),
});

export type AccessKey = typeof accessKeys.$inferSelect;

// ─── Required Channels (bot3 membership gate) ──────────────────────────────
export const requiredChannels = pgTable("required_channels", {
  chatId: bigint("chat_id", { mode: "number" }).primaryKey(),
  title: text("title").notNull(),
  inviteLink: text("invite_link"),
  addedByTgUid: bigint("added_by_tg_uid", { mode: "number" }).notNull(),
  createdAt: timestamp("created_at").notNull().defaultNow(),
});

export type RequiredChannel = typeof requiredChannels.$inferSelect;

// ─── Role Events (audit log for role changes) ──────────────────────────────
export const roleEvents = pgTable("role_events", {
  id: serial("id").primaryKey(),
  targetTgUid: bigint("target_tg_uid", { mode: "number" }).notNull(),
  actorTgUid: bigint("actor_tg_uid", { mode: "number" }),
  oldRole: text("old_role"),
  newRole: text("new_role").notNull(),
  reason: text("reason"),
  ts: timestamp("ts").notNull().defaultNow(),
});

export type RoleEvent = typeof roleEvents.$inferSelect;

// ─── Bot Store (persistent KV store for bot1/bot2 state) ───────────────────
export const botStore = pgTable("bot_store", {
  key: text("key").primaryKey(),
  data: jsonb("data").notNull(),
  updatedAt: timestamp("updated_at").notNull().defaultNow(),
});

// ─── App Settings (global key/value config) ────────────────────────────────
export const appSettings = pgTable("app_settings", {
  key: text("key").primaryKey(),
  value: text("value"),
  updatedAt: timestamp("updated_at").notNull().defaultNow(),
});

// ─── Notify Settings (per-user per-device notification prefs) ──────────────
export const notifySettings = pgTable(
  "notify_settings",
  {
    tgUid: bigint("tg_uid", { mode: "number" }).notNull(),
    deviceId: text("device_id").notNull(),
    transaction: boolean("transaction").notNull().default(true),
    login: boolean("login").notNull().default(true),
    onlineOffline: boolean("online_offline").notNull().default(true),
    /** Unix ms timestamp — only SMS with date >= enabledAt are notified */
    enabledAt: bigint("enabled_at", { mode: "number" }),
    updatedAt: timestamp("updated_at").notNull().defaultNow(),
  },
  (t) => [primaryKey({ columns: [t.tgUid, t.deviceId] })],
);

export type NotifySetting = typeof notifySettings.$inferSelect;

// ─── Push Tokens (Expo push notification tokens per user device) ───────────
export const pushTokens = pgTable(
  "push_tokens",
  {
    id: serial("id").primaryKey(),
    userId: text("user_id").notNull(),
    token: text("token").notNull(),
    platform: text("platform").notNull().default("android"),
    createdAt: timestamp("created_at").notNull().defaultNow(),
    updatedAt: timestamp("updated_at").notNull().defaultNow(),
  },
  (t) => [uniqueIndex("push_tokens_user_token_idx").on(t.userId, t.token)],
);

export type PushToken = typeof pushTokens.$inferSelect;

// ─── Panel Sections (bot5 forward targets) ─────────────────────────────────
export const panelSections = pgTable("panel_sections", {
  tgUid: bigint("tg_uid", { mode: "number" }).primaryKey(),
  chatId: bigint("chat_id", { mode: "number" }).notNull(),
  title: text("title").notNull(),
  role: text("role").notNull(),
  createdAt: timestamp("created_at").notNull().defaultNow(),
  updatedAt: timestamp("updated_at").notNull().defaultNow(),
});
