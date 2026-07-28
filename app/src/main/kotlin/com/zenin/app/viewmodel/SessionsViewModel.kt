package com.zenin.app.viewmodel

import androidx.lifecycle.ViewModel
import androidx.lifecycle.viewModelScope
import com.zenin.app.ZeninApp
import com.zenin.app.data.SessionInfo
import kotlinx.coroutines.flow.*
import kotlinx.coroutines.launch

data class SessionsUiState(
    val sessions: List<SessionInfo> = emptyList(),
    val isLoading: Boolean = false,
    val error: String? = null,
    val revokingId: Int? = null
)

class SessionsViewModel : ViewModel() {

    private val app get() = ZeninApp.instance

    private val _state = MutableStateFlow(SessionsUiState(isLoading = true))
    val state: StateFlow<SessionsUiState> = _state.asStateFlow()

    init { loadSessions() }

    fun loadSessions() {
        viewModelScope.launch {
            _state.update { it.copy(isLoading = true, error = null) }
            app.apiClient.getSessions()
                .onSuccess { sessions ->
                    _state.update { it.copy(sessions = sessions, isLoading = false, revokingId = null) }
                }
                .onFailure { e ->
                    _state.update { it.copy(isLoading = false, revokingId = null, error = e.message ?: "Failed to load sessions") }
                }
        }
    }

    fun revokeSession(id: Int) {
        viewModelScope.launch {
            _state.update { it.copy(revokingId = id) }
            app.apiClient.revokeSession(id)
                .onSuccess { loadSessions() }
                .onFailure { e ->
                    _state.update { it.copy(revokingId = null, error = "Failed to revoke: ${e.message}") }
                }
        }
    }

    fun terminateOthers() {
        // Server requires an explicit ids array and has no "current session"
        // marker; the list is ordered newest-first, so keep the most recent
        // session (almost always this app) and terminate the rest.
        val others = _state.value.sessions.drop(1).map { it.id }
        if (others.isEmpty()) return
        viewModelScope.launch {
            _state.update { it.copy(revokingId = -1) }
            app.apiClient.terminateSessions(others)
                .onSuccess { loadSessions() }
                .onFailure { e ->
                    _state.update { it.copy(revokingId = null, error = "Failed: ${e.message}") }
                }
        }
    }
}
