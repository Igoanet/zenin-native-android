---
name: APK release pipeline
description: The ZENIN Android APK auto-builds via GitHub Actions on every push to main; backend URL is baked in; bot download button must match the asset name
---

## Rule
`.github/workflows/build-native-apk.yml` builds a signed release APK on every push to main and uploads it to the `native-latest` GitHub release. The asset filename is `app-release.apk` — the bot's `APK_URL` in telegram-bots/config.py must point at exactly that name. The backend URL the app talks to is `DEFAULT_API_URL` in `app/src/main/kotlin/com/zenin/app/data/PreferencesRepository.kt` — changing the backend host requires editing that constant and pushing (CI rebuilds the APK).

**Why:** The APK hardcodes the Railway API domain; when the old Railway project was deleted the URL had to be repointed and a new APK cut. The workflow needs repo secrets ZENIN_KEYSTORE_BASE64 + ZENIN_KEYSTORE_PASSWORD (already configured in GitHub).

**How to apply:** If the API domain ever changes again: edit DEFAULT_API_URL → push → wait for CI → the bot's Download button serves the fresh APK automatically. Users can also override the URL in the app's Settings screen without reinstalling.
