package com.zenin.app.viewmodel

import androidx.lifecycle.ViewModel
import androidx.lifecycle.viewModelScope
import com.zenin.app.ZeninApp
import com.zenin.app.data.DEFAULT_API_URL
import com.zenin.app.data.UserProfile
import kotlinx.coroutines.flow.*
import kotlinx.coroutines.launch

class SettingsViewModel : ViewModel() {

    private val app get() = ZeninApp.instance
    private val prefsRepo get() = app.preferencesRepository

    val apiUrl: StateFlow<String> = prefsRepo.apiBaseUrl
        .stateIn(viewModelScope, SharingStarted.WhileSubscribed(5000), DEFAULT_API_URL)

    private val _saved = MutableStateFlow(false)
    val saved: StateFlow<Boolean> = _saved.asStateFlow()

    private val _profile = MutableStateFlow<UserProfile?>(null)
    val profile: StateFlow<UserProfile?> = _profile.asStateFlow()

    private val _profileMsg = MutableStateFlow<String?>(null)
    val profileMsg: StateFlow<String?> = _profileMsg.asStateFlow()

    private val _pwMsg = MutableStateFlow<String?>(null)
    val pwMsg: StateFlow<String?> = _pwMsg.asStateFlow()

    private val _pwBusy = MutableStateFlow(false)
    val pwBusy: StateFlow<Boolean> = _pwBusy.asStateFlow()

    init { loadProfile() }

    fun saveUrl(url: String) {
        viewModelScope.launch {
            prefsRepo.setApiBaseUrl(url)
            app.updateApiUrl(url)
            _saved.value = true
        }
    }

    fun resetSaved() { _saved.value = false }

    fun loadProfile() {
        viewModelScope.launch {
            app.apiClient.getMe().onSuccess { _profile.value = it }
        }
    }

    fun saveName(name: String) {
        val trimmed = name.trim()
        if (trimmed.isEmpty()) {
            _profileMsg.value = "Name cannot be empty"
            return
        }
        viewModelScope.launch {
            app.apiClient.updateMe(trimmed)
                .onSuccess {
                    _profile.value = it
                    _profileMsg.value = "Name saved"
                }
                .onFailure { _profileMsg.value = "Failed: ${it.message}" }
        }
    }

    fun changePassword(current: String, new: String) {
        when {
            current.isBlank() -> { _pwMsg.value = "Enter your current password"; return }
            new.length < 8 -> { _pwMsg.value = "New password must be at least 8 characters"; return }
            new.length > 64 -> { _pwMsg.value = "New password must be 64 characters or less"; return }
            new.contains(" ") -> { _pwMsg.value = "Password cannot contain spaces"; return }
        }
        viewModelScope.launch {
            _pwBusy.value = true
            app.apiClient.changePassword(current, new)
                .onSuccess { _pwMsg.value = "Password changed — use it on your next sign-in" }
                .onFailure { _pwMsg.value = "Failed: ${it.message}" }
            _pwBusy.value = false
        }
    }

    fun clearMessages() {
        _profileMsg.value = null
        _pwMsg.value = null
    }
}
