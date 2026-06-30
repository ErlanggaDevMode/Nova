package com.nayakacode.nova.core

import okhttp3.Call
import okhttp3.Callback
import okhttp3.MediaType.Companion.toMediaType
import okhttp3.OkHttpClient
import okhttp3.Request
import okhttp3.RequestBody.Companion.toRequestBody
import okhttp3.Response
import okhttp3.WebSocket
import okhttp3.WebSocketListener
import org.json.JSONObject
import java.io.IOException

class CoreClient(
    private val deviceId: String,
    private val serverUrl: String = "http://10.0.2.2:8000"
) {
    private val client = OkHttpClient()
    private val JSON_MEDIA_TYPE = "application/json; charset=utf-8".toMediaType()
    private var webSocket: WebSocket? = null

    fun registerCapabilities(name: String, capabilities: Map<String, Any>, onResult: (Boolean) -> Unit) {
        val url = "$serverUrl/capabilities/$deviceId"
        val json = JSONObject().apply {
            put("name", name)
            put("platform", "android")
            put("capabilities", JSONObject(capabilities))
        }

        val request = Request.Builder()
            .url(url)
            .post(json.toString().toRequestBody(JSON_MEDIA_TYPE))
            .build()

        client.newCall(request).enqueue(object : Callback {
            override fun onFailure(call: Call, e: IOException) {
                onResult(false)
            }

            override fun onResponse(call: Call, response: Response) {
                onResult(response.isSuccessful)
            }
        })
    }

    fun sendCommand(rawText: String, onResult: (String) -> Unit) {
        val url = "$serverUrl/command"
        val json = JSONObject().apply {
            put("raw_text", rawText)
            put("source_device_id", deviceId)
        }

        val request = Request.Builder()
            .url(url)
            .post(json.toString().toRequestBody(JSON_MEDIA_TYPE))
            .build()

        client.newCall(request).enqueue(object : Callback {
            override fun onFailure(call: Call, e: IOException) {
                onResult("Error: ${e.message}")
            }

            override fun onResponse(call: Call, response: Response) {
                if (response.isSuccessful) {
                    onResult(response.body?.string() ?: "")
                } else {
                    onResult("Error status: ${response.code}")
                }
            }
        })
    }

    fun postNotificationEvent(source: String, text: String) {
        val url = "$serverUrl/event"
        val json = JSONObject().apply {
            put("type", "notification")
            put("source", source)
            put("text", text)
        }

        val request = Request.Builder()
            .url(url)
            .post(json.toString().toRequestBody(JSON_MEDIA_TYPE))
            .build()

        client.newCall(request).enqueue(object : Callback {
            override fun onFailure(call: Call, e: IOException) {}
            override fun onResponse(call: Call, response: Response) {}
        })
    }

    fun connectWebSocket(onActionRequest: (JSONObject) -> JSONObject) {
        val wsScheme = if (serverUrl.startsWith("http://")) "ws" else "wss"
        val hostPort = serverUrl.split("://")[1]
        val wsUrl = "$wsScheme://$hostPort/ws/$deviceId"

        val request = Request.Builder().url(wsUrl).build()
        webSocket = client.newWebSocket(request, object : WebSocketListener() {
            override fun onMessage(webSocket: WebSocket, text: String) {
                try {
                    val data = JSONObject(text)
                    val event = data.optString("event")
                    if (event == "action.execute") {
                        val actionId = data.optString("action_id")
                        val result = onActionRequest(data)
                        
                        val response = JSONObject().apply {
                            put("event", "action.result")
                            put("action_id", actionId)
                            put("result", result)
                        }
                        webSocket.send(response.toString())
                    }
                } catch (e: Exception) {
                    e.printStackTrace()
                }
            }
        })
    }
}
