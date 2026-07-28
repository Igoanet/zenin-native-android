package com.zenin.app.viewmodel

import androidx.lifecycle.ViewModel
import androidx.lifecycle.viewModelScope
import com.zenin.app.ZeninApp
import com.zenin.app.data.*
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.asStateFlow
import kotlinx.coroutines.launch

sealed interface AuthUiState {
    object Idle : AuthUiState
    object Loading : AuthUiState
    data class OtpPending(val otpId: String) : AuthUiState
    data class CapacityFull(val sessions: List<SessionInfo>, val preAuthId: String) : AuthUiState
    data class Error(val message: String) : AuthUiState
    object Success : AuthUiState
}

class AuthViewModel : ViewModel() {

    private val app get() = ZeninApp.instance
    private val authRepo get() = app.authRepository
    val api get() = app.apiClient

    private val _uiState = MutableStateFlow<AuthUiState>(AuthUiState.Idle)
    val uiState: StateFlow<AuthUiState> = _uiState.asStateFlow()

    val tokenFlow get() = authRepo.token
    val userFlow get() = authRepo.user

    fun login(username: String, password: String) {
        if (username.isBlank() || password.isBlank()) {
            _uiState.value = AuthUiState.Error("Username and password are required")
            return
        }
        viewModelScope.launch {
            _uiState.value = AuthUiState.Loading
            _uiState.value = when (val result = api.login(username.trim(), password.trim())) {
                is LoginResult.Success -> {
                    authRepo.saveSession(result.token, result.user)
                    app.sseManager.start(result.token, app.appScope)
                    AuthUiState.Success
                }
                is LoginResult.OtpPending -> AuthUiState.OtpPending(result.otpId)
                is LoginResult.CapacityFull -> AuthUiState.CapacityFull(result.sessions, result.preAuthId)
                is LoginResult.Error -> AuthUiState.Error(result.message)
            }
        }
    }

    fun verifyOtp(otpId: String, otp: String) {
        viewModelScope.launch {
            _uiState.value = AuthUiState.Loading
            _uiState.value = when (val result = api.verifyOtp(otpId, otp)) {
                is OtpResult.Success -> {
                    authRepo.saveSession(result.token, result.user)
                    app.sseManager.start(result.token, app.appScope)
                    AuthUiState.Success
                }
                is OtpResult.CapacityFull -> AuthUiState.CapacityFull(result.sessions, result.preAuthId)
                is OtpResult.Error -> AuthUiState.Error(result.message)
            }
        }
    }

    fun evictAndLogin(preAuthId: String, evictEventId: Int) {
        viewModelScope.launch {
            _uiState.value = AuthUiState.Loading
            _uiState.value = when (val result = api.evictAndLogin(preAuthId, evictEventId)) {
                is EvictResult.Success -> {
                    authRepo.saveSession(result.token, result.user)
                    app.sseManager.start(result.token, app.appScope)
                    AuthUiState.Success
                }
                is EvictResult.OtpPending -> AuthUiState.OtpPending(result.otpId)
                is EvictResult.Error -> AuthUiState.Error(result.message)
            }
        }
    }

    fun logout() {
        viewModelScope.launch {
            api.logout()
            app.sseManager.stop()
            authRepo.clearSession()
        }
    }

    fun resetState() { _uiState.value = AuthUiState.Idle }
}
