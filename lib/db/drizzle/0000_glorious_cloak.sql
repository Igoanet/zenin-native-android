CREATE TABLE "access_keys" (
	"id" text PRIMARY KEY NOT NULL,
	"key" text NOT NULL,
	"role" text DEFAULT 'user' NOT NULL,
	"label" text DEFAULT '' NOT NULL,
	"duration_seconds" integer,
	"expires_at" timestamp,
	"revoked" boolean DEFAULT false NOT NULL,
	"redeemed_by_tg_uid" bigint,
	"created_by_tg_uid" bigint,
	"created_by_role" text,
	"created_at" timestamp DEFAULT now() NOT NULL,
	CONSTRAINT "access_keys_key_unique" UNIQUE("key")
);
--> statement-breakpoint
CREATE TABLE "app_settings" (
	"key" text PRIMARY KEY NOT NULL,
	"value" text,
	"updated_at" timestamp DEFAULT now() NOT NULL
);
--> statement-breakpoint
CREATE TABLE "bot_store" (
	"key" text PRIMARY KEY NOT NULL,
	"data" jsonb NOT NULL,
	"updated_at" timestamp DEFAULT now() NOT NULL
);
--> statement-breakpoint
CREATE TABLE "login_events" (
	"id" text PRIMARY KEY NOT NULL,
	"user_id" text NOT NULL,
	"ip_address" text,
	"user_agent" text,
	"city" text,
	"region" text,
	"country" text,
	"created_at" timestamp DEFAULT now() NOT NULL,
	"last_active_at" timestamp DEFAULT now() NOT NULL,
	"terminated_at" timestamp
);
--> statement-breakpoint
CREATE TABLE "notify_settings" (
	"tg_uid" bigint NOT NULL,
	"device_id" text NOT NULL,
	"transaction" boolean DEFAULT true NOT NULL,
	"login" boolean DEFAULT true NOT NULL,
	"online_offline" boolean DEFAULT true NOT NULL,
	"enabled_at" bigint,
	"updated_at" timestamp DEFAULT now() NOT NULL,
	CONSTRAINT "notify_settings_tg_uid_device_id_pk" PRIMARY KEY("tg_uid","device_id")
);
--> statement-breakpoint
CREATE TABLE "otp_sessions" (
	"id" text PRIMARY KEY NOT NULL,
	"user_id" text NOT NULL,
	"code" text NOT NULL,
	"expires_at" timestamp NOT NULL,
	"used" boolean DEFAULT false NOT NULL,
	"attempts" integer DEFAULT 0 NOT NULL,
	"created_at" timestamp DEFAULT now() NOT NULL
);
--> statement-breakpoint
CREATE TABLE "panel_configs" (
	"id" text PRIMARY KEY NOT NULL,
	"owner_id" text NOT NULL,
	"name" text NOT NULL,
	"firebase_url" text NOT NULL,
	"firebase_secret" text NOT NULL,
	"is_active" boolean DEFAULT true NOT NULL,
	"created_at" timestamp DEFAULT now() NOT NULL
);
--> statement-breakpoint
CREATE TABLE "panel_sections" (
	"tg_uid" bigint PRIMARY KEY NOT NULL,
	"chat_id" bigint NOT NULL,
	"title" text NOT NULL,
	"role" text NOT NULL,
	"created_at" timestamp DEFAULT now() NOT NULL,
	"updated_at" timestamp DEFAULT now() NOT NULL
);
--> statement-breakpoint
CREATE TABLE "required_channels" (
	"chat_id" bigint PRIMARY KEY NOT NULL,
	"title" text NOT NULL,
	"invite_link" text,
	"added_by_tg_uid" bigint NOT NULL,
	"created_at" timestamp DEFAULT now() NOT NULL
);
--> statement-breakpoint
CREATE TABLE "role_events" (
	"id" serial PRIMARY KEY NOT NULL,
	"target_tg_uid" bigint NOT NULL,
	"actor_tg_uid" bigint,
	"old_role" text,
	"new_role" text NOT NULL,
	"reason" text,
	"ts" timestamp DEFAULT now() NOT NULL
);
--> statement-breakpoint
CREATE TABLE "user_panel_data" (
	"user_id" text PRIMARY KEY NOT NULL,
	"data" json DEFAULT '{}'::json NOT NULL,
	"updated_at" timestamp DEFAULT now() NOT NULL
);
--> statement-breakpoint
CREATE TABLE "users" (
	"id" text PRIMARY KEY NOT NULL,
	"name" text NOT NULL,
	"username" text NOT NULL,
	"user_id" text NOT NULL,
	"role" text DEFAULT 'user' NOT NULL,
	"password_hash" text NOT NULL,
	"password_salt" text NOT NULL,
	"tg_username" text,
	"tg_chat_id" text,
	"tg_uid" bigint,
	"panel_password" text,
	"password_backfill_notified" boolean DEFAULT false NOT NULL,
	"access_granted" boolean DEFAULT false NOT NULL,
	"access_expires_at" timestamp,
	"token_version" integer DEFAULT 0 NOT NULL,
	"created_at" timestamp DEFAULT now() NOT NULL,
	"updated_at" timestamp DEFAULT now() NOT NULL,
	CONSTRAINT "users_username_unique" UNIQUE("username"),
	CONSTRAINT "users_user_id_unique" UNIQUE("user_id"),
	CONSTRAINT "users_tg_uid_unique" UNIQUE("tg_uid")
);
--> statement-breakpoint
ALTER TABLE "login_events" ADD CONSTRAINT "login_events_user_id_users_id_fk" FOREIGN KEY ("user_id") REFERENCES "public"."users"("id") ON DELETE cascade ON UPDATE no action;--> statement-breakpoint
ALTER TABLE "panel_configs" ADD CONSTRAINT "panel_configs_owner_id_users_id_fk" FOREIGN KEY ("owner_id") REFERENCES "public"."users"("id") ON DELETE cascade ON UPDATE no action;--> statement-breakpoint
ALTER TABLE "user_panel_data" ADD CONSTRAINT "user_panel_data_user_id_users_id_fk" FOREIGN KEY ("user_id") REFERENCES "public"."users"("id") ON DELETE cascade ON UPDATE no action;