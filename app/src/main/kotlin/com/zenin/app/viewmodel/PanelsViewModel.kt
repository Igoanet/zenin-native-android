package com.zenin.app.viewmodel

import androidx.lifecycle.ViewModel
import androidx.lifecycle.viewModelScope
import com.zenin.app.ZeninApp
import com.zenin.app.data.PanelConfigInfo
import kotlinx.coroutines.flow.*
import kotlinx.coroutines.launch

data class PanelsUiState(
    val configs: List<PanelConfigInfo> = emptyList(),
    val isLoading: Boolean = false,
    val error: String? = null,
    val actionMsg: String? = null,
    val busyId: String? = null
)

class PanelsViewModel : ViewModel() {

    private val app get() = ZeninApp.instance

    private val _state = MutableStateFlow(PanelsUiState(isLoading = true))
    val state: StateFlow<PanelsUiState> = _state.asStateFlow()

    init { load() }

    fun load() {
        viewModelScope.launch {
            _state.update { it.copy(isLoading = true, error = null) }
            app.apiClient.getPanelConfigs()
                .onSuccess { configs ->
                    _state.update { it.copy(configs = configs, isLoading = false) }
                }
                .onFailure { e ->
                    _state.update { it.copy(isLoading = false, error = e.message ?: "Failed to load panels") }
                }
        }
    }

    fun add(name: String, firebaseUrl: String, firebaseSecret: String) {
        if (name.isBlank() || firebaseUrl.isBlank() || firebaseSecret.isBlank()) {
            _state.update { it.copy(actionMsg = "All three fields are required") }
            return
        }
        viewModelScope.launch {
            app.apiClient.addPanelConfig(name.trim(), firebaseUrl.trim(), firebaseSecret.trim())
                .onSuccess {
                    _state.update { it.copy(actionMsg = "Panel added") }
                    load()
                }
                .onFailure { e -> _state.update { it.copy(actionMsg = "Failed: ${e.message}") } }
        }
    }

    fun toggleActive(config: PanelConfigInfo) {
        viewModelScope.launch {
            _state.update { it.copy(busyId = config.id) }
            app.apiClient.updatePanelConfig(config.id, null, !config.isActive, null)
                .onSuccess { load() }
                .onFailure { e -> _state.update { it.copy(actionMsg = "Failed: ${e.message}") } }
            _state.update { it.copy(busyId = null) }
        }
    }

    fun delete(id: String) {
        viewModelScope.launch {
            _state.update { it.copy(busyId = id) }
            app.apiClient.deletePanelConfig(id)
                .onSuccess { load() }
                .onFailure { e -> _state.update { it.copy(actionMsg = "Failed: ${e.message}") } }
            _state.update { it.copy(busyId = null) }
        }
    }

    fun test(id: String) {
        viewModelScope.launch {
            _state.update { it.copy(busyId = id) }
            app.apiClient.testPanelConfig(id)
                .onSuccess { count ->
                    _state.update { it.copy(actionMsg = "Connection OK — $count device${if (count == 1) "" else "s"} found") }
                }
                .onFailure { e -> _state.update { it.copy(actionMsg = "Test failed: ${e.message}") } }
            _state.update { it.copy(busyId = null) }
        }
    }

    fun clearMsg() { _state.update { it.copy(actionMsg = null) } }
}
