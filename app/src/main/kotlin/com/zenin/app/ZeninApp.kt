package com.zenin.app

import android.app.Application
import com.zenin.app.data.ApiClient
import com.zenin.app.data.AuthRepository
import com.zenin.app.data.PreferencesRepository
import com.zenin.app.service.SseManager
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.SupervisorJob

class ZeninApp : Application() {

    val appScope = CoroutineScope(SupervisorJob() + Dispatchers.Main)

    lateinit var authRepository: AuthRepository
        private set

    lateinit var preferencesRepository: PreferencesRepository
        private set

    lateinit var apiClient: ApiClient
        private set

    lateinit var sseManager: SseManager
        private set

    override fun onCreate() {
        super.onCreate()
        instance = this
        authRepository = AuthRepository(this)
        preferencesRepository = PreferencesRepository(this)
        apiClient = ApiClient(
            baseUrl = com.zenin.app.data.DEFAULT_API_URL,
            tokenProvider = { authRepository.currentToken() }
        )
        sseManager = SseManager(apiClient)
    }

    fun updateApiUrl(url: String) {
        apiClient = ApiClient(
            baseUrl = url,
            tokenProvider = { authRepository.currentToken() }
        )
        // Restart SSE with new client if token is active
        val token = authRepository.currentToken()
        if (token != null) {
            val newSseManager = SseManager(apiClient)
            sseManager.stop()
            sseManager = newSseManager
            sseManager.start(token, appScope)
        }
    }

    companion object {
        lateinit var instance: ZeninApp
            private set
    }
}
