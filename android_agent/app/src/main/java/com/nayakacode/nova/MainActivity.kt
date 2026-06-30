package com.nayakacode.nova

import android.app.Activity
import android.content.Intent
import android.os.Bundle
import android.speech.RecognizerIntent
import androidx.activity.ComponentActivity
import androidx.activity.compose.setContent
import androidx.compose.runtime.*
import com.nayakacode.nova.core.CoreClient
import com.nayakacode.nova.ui.CommandScreen
import org.json.JSONObject

class MainActivity : ComponentActivity() {
    private lateinit var client: CoreClient
    private val speechRequestCode = 101

    private val conversation = mutableStateListOf<String>()
    private var isConnected by mutableStateOf(false)

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)

        client = CoreClient("android_client")

        // Register device profile and establish connections
        val capabilities = mapOf("open_app" to true, "get_battery" to true)
        client.registerCapabilities("Android Client", capabilities) { success ->
            isConnected = success
            if (success) {
                client.connectWebSocket { actionData ->
                    handleRemoteAction(actionData)
                }
            }
        }

        setContent {
            CommandScreen(
                isConnected = isConnected,
                conversation = conversation,
                onSendCommand = { cmdText ->
                    conversation.add("You: $cmdText")
                    client.sendCommand(cmdText) { response ->
                        val text = try {
                            val json = JSONObject(response)
                            json.optString("response_text", "Done")
                        } catch (e: Exception) {
                            response
                        }
                        runOnUiThread {
                            conversation.add("Nova: $text")
                        }
                    }
                },
                onStartSpeech = {
                    startSpeechRecognizer()
                }
            )
        }
    }

    private fun handleRemoteAction(data: JSONObject): JSONObject {
        val result = JSONObject()
        try {
            val actionType = data.optString("action_type")
            if (actionType == "open_app") {
                val params = data.optJSONObject("params")
                val appName = params?.optString("app_name") ?: ""
                val launchIntent = packageManager.getLaunchIntentForPackage(appName)
                if (launchIntent != null) {
                    startActivity(launchIntent)
                    result.put("success", true)
                    result.put("launched", appName)
                } else {
                    result.put("success", false)
                    result.put("error", "Package '$appName' not installed on this Android device.")
                }
            } else {
                result.put("success", false)
                result.put("error", "Action type '$actionType' not implemented on Android client.")
            }
        } catch (e: Exception) {
            result.put("success", false)
            result.put("error", e.message)
        }
        return result
    }

    private fun startSpeechRecognizer() {
        val intent = Intent(RecognizerIntent.ACTION_RECOGNIZE_SPEECH).apply {
            putExtra(RecognizerIntent.EXTRA_LANGUAGE_MODEL, RecognizerIntent.LANGUAGE_MODEL_FREE_FORM)
        }
        try {
            startActivityForResult(intent, speechRequestCode)
        } catch (e: Exception) {
            conversation.add("Error: Speech recognition not supported on this device.")
        }
    }

    override fun onActivityResult(requestCode: Int, resultCode: Int, data: Intent?) {
        super.onActivityResult(requestCode, resultCode, data)
        if (requestCode == speechRequestCode && resultCode == Activity.RESULT_OK) {
            val results = data?.getStringArrayListExtra(RecognizerIntent.EXTRA_RESULTS)
            val spokenText = results?.get(0)
            if (spokenText != null) {
                conversation.add("You (Spoken): $spokenText")
                client.sendCommand(spokenText) { response ->
                    val text = try {
                        val json = JSONObject(response)
                        json.optString("response_text", "Done")
                    } catch (e: Exception) {
                        response
                    }
                    runOnUiThread {
                        conversation.add("Nova: $text")
                    }
                }
            }
        }
    }
}
