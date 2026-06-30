package com.nayakacode.nova

import android.accessibilityservice.AccessibilityService
import android.view.accessibility.AccessibilityEvent
import com.nayakacode.nova.core.CoreClient

class NovaAccessibilityService : AccessibilityService() {
    private lateinit var client: CoreClient

    override fun onCreate() {
        super.onCreate()
        client = CoreClient("android_client")
    }

    override fun onAccessibilityEvent(event: AccessibilityEvent?) {
        if (event == null) return

        if (event.eventType == AccessibilityEvent.TYPE_NOTIFICATION_STATE_CHANGED) {
            val packageName = event.packageName?.toString() ?: "unknown"
            
            val textBuilder = StringBuilder()
            event.text?.forEach { charSeq ->
                textBuilder.append(charSeq).append(" ")
            }
            val content = textBuilder.toString().trim()

            if (content.isNotEmpty()) {
                // Post event back to FastAPI Core
                client.postNotificationEvent(packageName, content)
            }
        }
    }

    override fun onInterrupt() {
        // Interrupted
    }
}
