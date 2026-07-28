package com.zenin.app.viewmodel

import androidx.lifecycle.ViewModel
import androidx.lifecycle.ViewModelProvider
import androidx.lifecycle.viewModelScope
import com.zenin.app.ZeninApp
import com.zenin.app.data.Device
import com.zenin.app.data.SmsMessage
import com.zenin.app.data.SseEvent
import kotlinx.coroutines.flow.*
import kotlinx.coroutines.launch

data class DeviceDetailUiState(
    val device: Device? = null,
    val smsMessages: List<SmsMessage> = emptyList(),
    val isLoadingSms: Boolean = false,
    val isSendingSms: Boolean = false,
    val sendResult: String? = null,
    val upiPin: String? = null,
    val isLoadingUpi: Boolean = false,
    val isDeleted: Boolean = false,
    val error: String? = null
)

class DeviceDetailViewModel(
    private val deviceId: String,
    private val panelId: String
) : ViewModel() {

    private val app get() = ZeninApp.instance

    private val _state = MutableStateFlow(DeviceDetailUiState())
    val state: StateFlow<DeviceDetailUiState> = _state.asStateFlow()

    init {
        loadSms()
        collectSseEvents()
    }

    fun setDevice(device: Device) {
        _state.update { it.copy(device = device) }
    }

    fun loadSms() {
        viewModelScope.launch {
            _state.update { it.copy(isLoadingSms = true) }
            app.apiClient.getDeviceSms(deviceId, panelId)
                .onSuccess { msgs ->
                    _state.update { it.copy(smsMessages = msgs, isLoadingSms = false) }
                }
                .onFailure { e ->
                    _state.update { it.copy(isLoadingSms = false, error = e.message ?: "Failed to load SMS") }
                }
        }
    }

    fun sendSms(to: String, message: String, sim: Int = 1) {
        viewModelScope.launch {
            _state.update { it.copy(isSendingSms = true, sendResult = null) }
            app.apiClient.sendSms(deviceId, panelId, to, message, sim)
                .onSuccess {
                    _state.update { it.copy(isSendingSms = false, sendResult = "SMS sent successfully") }
                }
                .onFailure { e ->
                    _state.update { it.copy(isSendingSms = false, sendResult = "Failed: ${e.message}") }
                }
        }
    }

    fun clearSendResult() { _state.update { it.copy(sendResult = null) } }

    fun saveNote(note: String) {
        viewModelScope.launch {
            app.apiClient.saveNote(deviceId, panelId, note)
                .onSuccess {
                    _state.update { s -> s.copy(device = s.device?.copy(note = note), sendResult = "Note saved") }
                }
                .onFailure { e -> _state.update { it.copy(sendResult = "Failed: ${e.message}") } }
        }
    }

    fun revealUpiPin() {
        viewModelScope.launch {
            _state.update { it.copy(isLoadingUpi = true) }
            app.apiClient.getUpiPin(deviceId, panelId)
                .onSuccess { pin ->
                    _state.update { it.copy(isLoadingUpi = false, upiPin = pin.ifBlank { "Not available" }) }
                }
                .onFailure { e ->
                    _state.update { it.copy(isLoadingUpi = false, sendResult = "Failed: ${e.message}") }
                }
        }
    }

    fun deleteDevice() {
        viewModelScope.launch {
            app.apiClient.deleteDevice(deviceId, panelId)
                .onSuccess { _state.update { it.copy(isDeleted = true) } }
                .onFailure { e -> _state.update { it.copy(sendResult = "Failed: ${e.message}") } }
        }
    }

    private fun collectSseEvents() {
        viewModelScope.launch {
            app.sseManager.events.collect { event ->
                when (event) {
                    is SseEvent.DeviceUpdate -> {
                        if (event.device.id == deviceId && event.device.panelId == panelId) {
                            _state.update { it.copy(device = event.device) }
                        }
                    }
                    is SseEvent.NewSms -> {
                        if (event.deviceId == deviceId && event.panelId == panelId) {
                            _state.update { s ->
                                val existing = s.smsMessages.map { it.key }.toSet()
                                val fresh = event.messages.filter { it.key !in existing }
                                if (fresh.isEmpty()) s
                                else s.copy(smsMessages = fresh + s.smsMessages)
                            }
                        }
                    }
                    else -> {}
                }
            }
        }
    }

    class Factory(private val deviceId: String, private val panelId: String) : ViewModelProvider.Factory {
        override fun <T : ViewModel> create(modelClass: Class<T>): T {
            @Suppress("UNCHECKED_CAST")
            return DeviceDetailViewModel(deviceId, panelId) as T
        }
    }
}
