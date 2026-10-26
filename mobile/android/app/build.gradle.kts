plugins {
    id("com.android.application")
    id("org.jetbrains.kotlin.android")
}

android {
    namespace = "com.atip.mobile"
    compileSdk = 35
    defaultConfig {
        applicationId = "com.atip.mobile"
        minSdk = 26
        targetSdk = 35
        versionCode = 2
        versionName = "2026.10.26"
        // The ATIP server the app opens first; changeable in the app (long-press the banner).
        buildConfigField("String", "DEFAULT_SERVER", "\"http://192.168.1.10:8000\"")
    }
    buildFeatures { buildConfig = true }
    compileOptions {
        sourceCompatibility = JavaVersion.VERSION_17
        targetCompatibility = JavaVersion.VERSION_17
    }
    kotlinOptions { jvmTarget = "17" }
}

dependencies {
    implementation("androidx.appcompat:appcompat:1.7.0")
    implementation("com.google.android.material:material:1.12.0")
    implementation("androidx.swiperefreshlayout:swiperefreshlayout:1.1.0")
}
