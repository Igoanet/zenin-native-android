package com.zenin.app.service

import android.util.Log
import com.google.gson.Gson
import com.zenin.app.data.ApiClient
import com.zenin.app.data.Device
import com.zenin.app.data.RawSsePayload
import com.zenin.app.data.SmsMessage
import com.zenin.app.data.SseEvent
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.Job
import kotlinx.coroutines.delay
import kotlinx.coroutines.flow.MutableSharedFlow
import kotlinx.coroutines.flow.SharedFlow
import kotlinx.coroutines.flow.asSharedFlow
import kotlinx.coroutines.isActive
import kotlinx.coroutines.launch
import okhttp3.Call
import okhttp3.Response
import java.io.IOException

private const val TAG = "SseManager"

class SseManager(private val apiClient: ApiClient) {

    private val _events = MutableSharedFlow<SseEvent>(extraBufferCapacity = 64)
    val events: SharedFlow<SseEvent> = _events.asSharedFlow()

    private val gson = Gson()
    private var connectJob: Job? = null
    private var currentCall: Call? = null

    fun start(token: String, scope: CoroutineScope) {
        stop()
        connectJob = scope.launch(Dispatchers.IO) {
            var backoff = 2_000L
            while (isActive) {
                try {
                    val req = apiClient.buildSseRequest(token)
                    val call = apiClient.sseClient.newCall(req)
                    currentCall = call

                    val resp: Response = call.execute()
                    if (!resp.isSuccessful) {
                        Log.w(TAG, "SSE response ${resp.code}")
                        resp.close()
                        delay(backoff)
                        backoff = minOf(backoff * 2, 30_000L)
                        continue
                    }

                    backoff = 2_000L
                    Log.i(TAG, "SSE connected")

                    val body = resp.body
                    if (body == null) {
                        resp.close()
                        delay(2_000)
                        backoff = minOf(backoff * 2, 30_000L)
                        continue
                    }
                    val source = body.source()

                    var dataStr = ""
                    try {
                        while (isActive && !source.exhausted()) {
                            val line = source.readUtf8Line() ?: break
                            when {
                                line.startsWith("data: ") -> dataStr = line.removePrefix("data: ")
                                line.isEmpty() && dataStr.isNotEmpty() -> {
                                    parseEvent(dataStr)
                                    dataStr = ""
                                }
                                line.startsWith(":") -> dataStr = "" // heartbeat
                            }
                        }
                    } catch (e: IOException) {
                        if (isActive) Log.w(TAG, "SSE stream closed: ${e.message}")
                    } finally {
                        resp.close()
                    }
                } catch (e: Exception) {
                    if (isActive) {
                        Log.w(TAG, "SSE error: ${e.message}")
                        delay(backoff)
                        backoff = minOf(backoff * 2, 30_000L)
                    }
                }
            }
        }
    }

    fun stop() {
        connectJob?.cancel()
        connectJob = null
        currentCall?.cancel()
        currentCall = null
    }

    private fun parseEvent(data: String) {
        try {
            val payload = gson.fromJson(data, RawSsePayload::class.java)
            val event: SseEvent = when (payload.type) {
                "device_update" -> {
                    val device = payload.device ?: return
                    SseEvent.DeviceUpdate(payload.panelId, device)
                }
                "new_sms" -> {
                    val msgs = payload.messages ?: return
                    SseEvent.NewSms(payload.panelId, payload.deviceId, msgs)
                }
                else -> SseEvent.Unknown(payload.type)
            }
            _events.tryEmit(event)
        } catch (e: Exception) {
            Log.d(TAG, "Failed to parse SSE event: $data")
        }
    }
}
