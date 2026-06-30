package com.nayakacode.nova

import android.accessibilityservice.AccessibilityService
import android.view.accessibility.AccessibilityEvent

class NovaAccessibilityService : AccessibilityService() {
    override fun onAccessibilityEvent(event: AccessibilityEvent?) {
        // Handle read notifications, click buttons, app launch triggers in Phase 2
    }

    override fun onInterrupt() {
        // Interrupted
    }
}
