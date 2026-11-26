// GlobaleSIM Mobile — the phone-plan app: calls, texts, data left and top-ups.
// Screens come from globalesim.tel/app.php?mobile; calls run on the Linphone engine (same as the main app).
plugins {
    id("com.android.application")
    id("org.jetbrains.kotlin.android")
}

val linphoneVer: String = (project.findProperty("linphoneVer") as String?) ?: "5.4.+"

android {
    namespace = "tel.globalesim.app"
    compileSdk = 36

    defaultConfig {
        applicationId = "tel.globalesim.mobile"
        minSdk = 28
        targetSdk = 36
        versionCode = (System.getenv("GS_VERSION_CODE") ?: "1").toInt()
        versionName = System.getenv("GS_VERSION_NAME") ?: "0.1"
    }

    signingConfigs {
        create("release") {
            val f = System.getenv("GS_KEYSTORE")
            if (f != null) {
                storeFile = file(f)
                storePassword = System.getenv("GS_KEYSTORE_PASSWORD")
                keyAlias = System.getenv("GS_KEY_ALIAS") ?: "globalesim"
                keyPassword = System.getenv("GS_KEY_PASSWORD") ?: System.getenv("GS_KEYSTORE_PASSWORD")
            }
        }
    }

    buildTypes {
        release {
            isMinifyEnabled = true
            isShrinkResources = true
            proguardFiles(getDefaultProguardFile("proguard-android-optimize.txt"), "proguard-rules.pro")
            if (System.getenv("GS_KEYSTORE") != null) signingConfig = signingConfigs.getByName("release")
        }
    }

    compileOptions {
        sourceCompatibility = JavaVersion.VERSION_17
        targetCompatibility = JavaVersion.VERSION_17
    }
    kotlinOptions { jvmTarget = "17" }
    packaging { jniLibs { useLegacyPackaging = false } }
    // test APKs per phone type (smaller downloads); Google Play gets the .aab and splits it itself
    splits {
        abi {
            isEnable = true
            reset()
            include("arm64-v8a", "armeabi-v7a")
            isUniversalApk = false
        }
    }
}

dependencies {
    implementation("org.linphone.no-video:linphone-sdk-android:$linphoneVer")
}
