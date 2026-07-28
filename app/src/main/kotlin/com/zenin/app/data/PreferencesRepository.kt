package com.zenin.app.data

import android.content.Context
import androidx.datastore.core.DataStore
import androidx.datastore.preferences.core.Preferences
import androidx.datastore.preferences.core.edit
import androidx.datastore.preferences.core.stringPreferencesKey
import androidx.datastore.preferences.preferencesDataStore
import kotlinx.coroutines.flow.Flow
import kotlinx.coroutines.flow.map

private val Context.dataStore: DataStore<Preferences> by preferencesDataStore(name = "zenin_settings")

private val KEY_API_URL = stringPreferencesKey("api_base_url")
const val DEFAULT_API_URL = "https://api-server-production-9692.up.railway.app/api"

class PreferencesRepository(private val context: Context) {

    val apiBaseUrl: Flow<String> = context.dataStore.data.map { prefs ->
        prefs[KEY_API_URL] ?: DEFAULT_API_URL
    }

    suspend fun setApiBaseUrl(url: String) {
        context.dataStore.edit { prefs ->
            prefs[KEY_API_URL] = url.trimEnd('/')
        }
    }
}
