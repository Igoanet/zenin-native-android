package com.zenin.app.viewmodel

import androidx.lifecycle.ViewModel
import androidx.lifecycle.viewModelScope
import com.zenin.app.ZeninApp
import com.zenin.app.data.DEFAULT_API_URL
import kotlinx.coroutines.flow.*
import kotlinx.coroutines.launch

class SettingsViewModel : ViewModel() {

    private val app get() = ZeninApp.instance
    private val prefsRepo get() = app.preferencesRepository

    val apiUrl: StateFlow<String> = prefsRepo.apiBaseUrl
        .stateIn(viewModelScope, SharingStarted.WhileSubscribed(5000), DEFAULT_API_URL)

    private val _saved = MutableStateFlow(false)
    val saved: StateFlow<Boolean> = _saved.asStateFlow()

    fun saveUrl(url: String) {
        viewModelScope.launch {
            prefsRepo.setApiBaseUrl(url)
            app.updateApiUrl(url)
            _saved.value = true
        }
    }

    fun resetSaved() { _saved.value = false }
}
