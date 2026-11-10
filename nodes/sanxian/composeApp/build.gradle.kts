import org.jetbrains.kotlin.gradle.dsl.JvmTarget

plugins {
    alias(libs.plugins.kotlinMultiplatform)
    alias(libs.plugins.androidApplication)
    alias(libs.plugins.composeMultiplatform)
    alias(libs.plugins.composeCompiler)
    alias(libs.plugins.kotlinSerialization)
}

kotlin {
    androidTarget {
        compilerOptions {
            jvmTarget.set(JvmTarget.JVM_17)
        }
    }


    sourceSets {
        androidMain.dependencies {
            implementation(compose.preview)
            implementation(libs.androidx.core.ktx)
            implementation(libs.androidx.activity.compose)
            implementation(libs.androidx.lifecycle.runtime.ktx)
            implementation(libs.androidx.lifecycle.viewmodel.compose)
            implementation(libs.androidx.lifecycle.runtime.compose)
            implementation(project(":shared"))
            implementation(project(":nativelib"))
            implementation("io.ktor:ktor-server-cio:2.3.12")
            // 节点看护：WorkManager 周期探活（系统下限 15 分钟），服务被系统停掉时发提醒
            implementation(libs.androidx.work.runtime.ktx)
            // 设备端配置（DataStore Preferences）：运行时可改端口/token/并发/模型，重启服务生效
            implementation(libs.androidx.datastore.preferences)
            // 首页访问地址二维码（纯 Java 编码库，无网络/无新权限）
            implementation(libs.zxing.core)
        }
        commonMain.dependencies {
            implementation(compose.runtime)
            implementation(compose.foundation)
            implementation(compose.material3)
            implementation(compose.ui)
            implementation(compose.components.resources)
            implementation(compose.components.uiToolingPreview)
            // material-icons-extended 必须用 CMP 访问器（多平台坐标），硬编码 Jetpack 版
            // 无 Kotlin/Native 变体，会导致 iOS target 解析失败。
            implementation(compose.materialIconsExtended)
            implementation(libs.androidx.lifecycle.viewmodel.compose)
            implementation(libs.androidx.lifecycle.runtime.compose)
            implementation(libs.kotlinx.coroutines.core)
            // shared 是跨平台模块，放到 commonMain 使其对所有 target（含 iOS）可见；
            // nativelib 仅 Android，保留在 androidMain。
            implementation(project(":shared"))
        }
    }
}

android {
    namespace = "com.biyou.sanxian"
    compileSdk = 35

    defaultConfig {
        applicationId = "com.biyou.sanxian"
        minSdk = 24
        targetSdk = 35
        versionCode = 1
        versionName = "1.0"
    }

    buildTypes {
        getByName("debug") {
            isMinifyEnabled = false
            isDebuggable = true
        }
        getByName("release") {
            isMinifyEnabled = false
            proguardFiles(
                getDefaultProguardFile("proguard-android-optimize.txt"),
                "proguard-rules.pro"
            )
        }
    }

    packaging {
        resources {
            excludes += "/META-INF/{AL2.0,LGPL2.1}"
        }
    }

    compileOptions {
        sourceCompatibility = JavaVersion.VERSION_17
        targetCompatibility = JavaVersion.VERSION_17
    }

    buildFeatures {
        compose = true
    }
}
