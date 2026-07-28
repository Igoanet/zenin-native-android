package com.zenin.app.viewmodel

import androidx.lifecycle.ViewModel
import androidx.lifecycle.viewModelScope
import com.zenin.app.ZeninApp
import com.zenin.app.data.Device
import com.zenin.app.data.MoneyPoolSummary
import com.zenin.app.data.SseEvent
import kotlinx.coroutines.flow.*
import kotlinx.coroutines.launch

data class DevicesUiState(
    val devices: List<Device> = emptyList(),
    val summary: MoneyPoolSummary = MoneyPoolSummary(),
    val isLoading: Boolean = false,
    val error: String? = null
)

class DevicesViewModel : ViewModel() {

    private val app get() = ZeninApp.instance

    private val _state = MutableStateFlow(DevicesUiState(isLoading = true))
    val state: StateFlow<DevicesUiState> = _state.asStateFlow()

    init {
        loadDevices()
        collectSseEvents()
    }

    fun loadDevices() {
        viewModelScope.launch {
            _state.update { it.copy(isLoading = true, error = null) }
            app.apiClient.getDevices()
                .onSuccess { (devices, summary) ->
                    _state.update { it.copy(devices = devices, summary = summary, isLoading = false) }
                }
                .onFailure { e ->
                    _state.update { it.copy(isLoading = false, error = e.message ?: "Failed to load devices") }
                }
        }
    }

    private fun collectSseEvents() {
        viewModelScope.launch {
            app.sseManager.events.collect { event ->
                when (event) {
                    is SseEvent.DeviceUpdate -> {
                        _state.update { state ->
                            val list = state.devices.toMutableList()
                            val idx = list.indexOfFirst { it.id == event.device.id && it.panelId == event.device.panelId }
                            if (idx >= 0) list[idx] = event.device else list.add(0, event.device)
                            state.copy(devices = list)
                        }
                    }
                    else -> {}
                }
            }
        }
    }
}
