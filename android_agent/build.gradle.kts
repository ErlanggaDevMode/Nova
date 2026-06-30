// Placeholder build.gradle.kts for android_agent [P2]
plugins {
    id("com.android.application") version "8.1.0"
    kotlin("android") version "1.8.10"
}

android {
    namespace = "com.nayakacode.nova"
    compileSdk = 33

    defaultConfig {
        applicationId = "com.nayakacode.nova"
        minSdk = 26
        targetSdk = 33
        versionCode = 1
        versionName = "1.0"
    }
}
