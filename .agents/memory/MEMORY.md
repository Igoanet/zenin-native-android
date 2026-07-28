# Zenin memory index

- [Railway project setup via API](railway-project-setup.md) — full zenin-portal-bot project was rebuilt from scratch via Railway GraphQL; key mutations + gotchas (preDeployCommand rejected, postgres image needs POSTGRES_* env)
- [Shared bot/API schema](shared-db-schema.md) — one Postgres serves both the Python bot (raw SQL) and Node API (drizzle); schemas conflict on access_keys/role_events, resolved with union schema + sync trigger
- [APK release pipeline](apk-release-pipeline.md) — APK CI builds on every push to main; asset is named app-release.apk, backend URL is baked into PreferencesRepository DEFAULT_API_URL
