package com.zenin.app.data

import android.util.Log
import com.google.gson.Gson
import com.google.gson.reflect.TypeToken
import okhttp3.MediaType.Companion.toMediaType
import okhttp3.OkHttpClient
import okhttp3.Request
import okhttp3.RequestBody.Companion.toRequestBody
import java.util.concurrent.TimeUnit

private const val TAG = "ZeninApi"
private val JSON = "application/json".toMediaType()
private val gson = Gson()

class ApiClient(private val baseUrl: String, private val tokenProvider: () -> String?) {

    private val client = OkHttpClient.Builder()
        .connectTimeout(15, TimeUnit.SECONDS)
        .readTimeout(30, TimeUnit.SECONDS)
        .writeTimeout(15, TimeUnit.SECONDS)
        .build()

    // Long-lived client for SSE (no read timeout)
    val sseClient: OkHttpClient = OkHttpClient.Builder()
        .connectTimeout(15, TimeUnit.SECONDS)
        .readTimeout(0, TimeUnit.MILLISECONDS)
        .build()

    private fun request(method: String, path: String, body: Any? = null, token: String? = null): Request {
        val effectiveToken = token ?: tokenProvider()
        val rb = Request.Builder()
            .url("${baseUrl.trimEnd('/')}$path")
            .apply {
                if (effectiveToken != null) header("Authorization", "Bearer $effectiveToken")
                header("Content-Type", "application/json")
                header("Accept", "application/json")
            }
        val reqBody = body?.let { gson.toJson(it).toRequestBody(JSON) }
        return when (method.uppercase()) {
            "POST" -> rb.post(reqBody ?: "{}".toRequestBody(JSON)).build()
            "PUT" -> rb.put(reqBody ?: "{}".toRequestBody(JSON)).build()
            "DELETE" -> rb.delete(reqBody).build()
            else -> rb.get().build()
        }
    }

    private inline fun <reified T> execute(req: Request): Result<T> {
        return try {
            val resp = client.newCall(req).execute()
            val body = resp.body?.string() ?: ""
            if (resp.isSuccessful) {
                Result.success(gson.fromJson(body, object : TypeToken<T>() {}.type))
            } else {
                val raw = runCatching { gson.fromJson(body, RawLoginResponse::class.java) }.getOrNull()
                Result.failure(ApiException(resp.code, raw?.error ?: "HTTP ${resp.code}"))
            }
        } catch (e: Exception) {
            Log.e(TAG, "Request failed: ${req.url}", e)
            Result.failure(e)
        }
    }

    // ─── Auth ────────────────────────────────────────────────────────────────

    suspend fun login(username: String, password: String): LoginResult {
        val req = request("POST", "/auth/login", mapOf("username" to username, "password" to password))
        return try {
            val resp = client.newCall(req).execute()
            val body = resp.body?.string() ?: ""
            val raw = gson.fromJson(body, RawLoginResponse::class.java)
            when {
                resp.code == 409 -> LoginResult.CapacityFull(
                    raw.activeSessions ?: emptyList(),
                    raw.preAuthId ?: ""
                )
                !resp.isSuccessful -> LoginResult.Error(friendlyError(raw.error ?: "Login failed"))
                raw.otpPending == true -> LoginResult.OtpPending(raw.otpId ?: "")
                raw.token != null && raw.user != null -> LoginResult.Success(raw.token, raw.user, raw.loginEventId ?: 0L)
                else -> LoginResult.Error("Unexpected response")
            }
        } catch (e: Exception) {
            LoginResult.Error("Connection failed: ${e.message}")
        }
    }

    suspend fun verifyOtp(otpId: String, otp: String): OtpResult {
        val req = request("POST", "/auth/otp/verify", mapOf("otpId" to otpId, "otp" to otp))
        return try {
            val resp = client.newCall(req).execute()
            val body = resp.body?.string() ?: ""
            val raw = gson.fromJson(body, RawLoginResponse::class.java)
            when {
                resp.code == 409 -> OtpResult.CapacityFull(
                    raw.activeSessions ?: emptyList(),
                    raw.preAuthId ?: ""
                )
                !resp.isSuccessful -> OtpResult.Error(
                    friendlyError(raw.error ?: "Verification failed"),
                    raw.attemptsLeft ?: 0
                )
                raw.token != null && raw.user != null -> OtpResult.Success(raw.token, raw.user, raw.loginEventId ?: 0L)
                else -> OtpResult.Error("Unexpected response")
            }
        } catch (e: Exception) {
            OtpResult.Error("Connection failed: ${e.message}")
        }
    }

    suspend fun evictAndLogin(preAuthId: String, evictEventId: Int): EvictResult {
        val req = request("POST", "/auth/login/evict-and-login",
            mapOf("preAuthId" to preAuthId, "evictEventId" to evictEventId))
        return try {
            val resp = client.newCall(req).execute()
            val body = resp.body?.string() ?: ""
            val raw = gson.fromJson(body, RawLoginResponse::class.java)
            when {
                !resp.isSuccessful -> EvictResult.Error(raw.error ?: "Evict failed")
                raw.otpPending == true -> EvictResult.OtpPending(raw.otpId ?: "")
                raw.token != null && raw.user != null -> EvictResult.Success(raw.token, raw.user)
                else -> EvictResult.Error("Unexpected response")
            }
        } catch (e: Exception) {
            EvictResult.Error("Connection failed: ${e.message}")
        }
    }

    suspend fun logout() {
        try {
            client.newCall(request("POST", "/auth/logout")).execute().close()
        } catch (_: Exception) {}
    }

    suspend fun getSessions(): Result<List<SessionInfo>> {
        val req = request("GET", "/auth/sessions")
        val result = execute<RawSessionsResponse>(req)
        return result.map { it.sessions }
    }

    suspend fun revokeSession(id: Int): Result<Unit> {
        val req = request("DELETE", "/auth/sessions/$id")
        return execute<Any>(req).map {}
    }

    // ─── Devices ─────────────────────────────────────────────────────────────

    suspend fun getDevices(): Result<Pair<List<Device>, MoneyPoolSummary>> {
        val req = request("GET", "/devices")
        val result = execute<RawDevicesResponse>(req)
        return result.map { raw ->
            Pair(raw.devices, raw.summary ?: MoneyPoolSummary())
        }
    }

    // ─── SMS ─────────────────────────────────────────────────────────────────

    suspend fun getDeviceSms(deviceId: String, panelId: String, limit: Int = 100): Result<List<SmsMessage>> {
        val path = "/sms/${encode(deviceId)}?panelId=${encode(panelId)}&limit=$limit"
        val req = request("GET", path)
        val result = execute<RawSmsResponse>(req)
        return result.map { it.messages }
    }

    suspend fun sendSms(deviceId: String, panelId: String, to: String, message: String, sim: Int = 1): Result<Unit> {
        val req = request("POST", "/sms/send/${encode(deviceId)}",
            mapOf("to" to to, "message" to message, "sim" to sim, "panelId" to panelId))
        return execute<Any>(req).map {}
    }

    // ─── SSE request builder (used by SseManager) ────────────────────────────

    fun buildSseRequest(token: String): Request {
        val url = "${baseUrl.trimEnd('/')}/events?token=${encode(token)}"
        return Request.Builder()
            .url(url)
            .header("Accept", "text/event-stream")
            .header("Cache-Control", "no-cache")
            .build()
    }

    companion object {
        private fun encode(s: String) = java.net.URLEncoder.encode(s, "UTF-8")

        private fun friendlyError(raw: String): String = when (raw) {
            "otp_bot_not_started" -> "Open Telegram and send /start to @ZeninPortalBot, then sign in again."
            "otp_send_failed" -> "Could not send OTP to your Telegram. Please try again."
            "otp_no_telegram" -> "Your account has no Telegram linked. Contact your administrator."
            "otp_invalid" -> "Incorrect OTP code."
            "otp_expired" -> "Code expired. Please sign in again."
            "otp_too_many_attempts" -> "Too many incorrect attempts. Please sign in again."
            else -> raw
        }
    }
}

class ApiException(val code: Int, override val message: String) : Exception(message)
