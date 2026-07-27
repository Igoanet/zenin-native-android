package com.zenin.app.data

import android.content.Context
import android.content.SharedPreferences
import androidx.security.crypto.EncryptedSharedPreferences
import androidx.security.crypto.MasterKey
import com.google.gson.Gson
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.asStateFlow

private const val FILE = "zenin_secure"
private const val KEY_TOKEN = "token"
private const val KEY_USER = "user"

class AuthRepository(private val context: Context) {

    private val prefs: SharedPreferences by lazy {
        try {
            val masterKey = MasterKey.Builder(context)
                .setKeyScheme(MasterKey.KeyScheme.AES256_GCM)
                .build()
            EncryptedSharedPreferences.create(
                context, FILE, masterKey,
                EncryptedSharedPreferences.PrefKeyEncryptionScheme.AES256_SIV,
                EncryptedSharedPreferences.PrefValueEncryptionScheme.AES256_GCM
            )
        } catch (e: Exception) {
            context.getSharedPreferences(FILE, Context.MODE_PRIVATE)
        }
    }

    private val gson = Gson()
    private val _token = MutableStateFlow<String?>(null)
    val token: StateFlow<String?> = _token.asStateFlow()
    private val _user = MutableStateFlow<UserProfile?>(null)
    val user: StateFlow<UserProfile?> = _user.asStateFlow()

    init { loadFromPrefs() }

    private fun loadFromPrefs() {
        val t = prefs.getString(KEY_TOKEN, null)
        val u = prefs.getString(KEY_USER, null)
            ?.let { runCatching { gson.fromJson(it, UserProfile::class.java) }.getOrNull() }
        _token.value = t
        _user.value = u
    }

    fun saveSession(token: String, user: UserProfile) {
        prefs.edit().putString(KEY_TOKEN, token).putString(KEY_USER, gson.toJson(user)).apply()
        _token.value = token
        _user.value = user
    }

    fun clearSession() {
        prefs.edit().remove(KEY_TOKEN).remove(KEY_USER).apply()
        _token.value = null
        _user.value = null
    }

    fun currentToken(): String? = _token.value
}
