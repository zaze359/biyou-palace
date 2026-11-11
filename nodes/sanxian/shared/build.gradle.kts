import org.jetbrains.kotlin.gradle.dsl.JvmTarget

plugins {
    alias(libs.plugins.kotlinMultiplatform)
    alias(libs.plugins.androidLibrary)
    alias(libs.plugins.kotlinSerialization)
}


kotlin {
    androidTarget {
        compilerOptions {
            jvmTarget.set(JvmTarget.JVM_17)
        }
    }


    sourceSets {
        commonMain.dependencies {
            implementation(libs.kotlinx.coroutines.core)
            implementation("io.ktor:ktor-server-core:2.3.12")
            implementation("io.ktor:ktor-server-cio:2.3.12")
            implementation("io.ktor:ktor-server-status-pages:2.3.12")
            implementation("org.jetbrains.kotlinx:kotlinx-serialization-json:1.7.3")
        }
        commonTest.dependencies {
            // KMP 通用断言；androidTarget 的单元测试在 JVM 上执行（:shared:testDebugUnitTest）
            implementation(kotlin("test"))
        }
        androidMain.dependencies {
            implementation(libs.kotlinx.coroutines.android)
            implementation("io.ktor:ktor-server-cio:2.3.12")
        }
        androidUnitTest.dependencies {
            // 端点级集成测试：Ktor testApplication 在 JVM 内跑完整路由，不需要真机/真实模型
            implementation("io.ktor:ktor-server-test-host:2.3.12")
        }
    }
}

android {
    namespace = "com.biyou.sanxian.shared"
    compileSdk = 35

    defaultConfig {
        minSdk = 24
    }

    compileOptions {
        sourceCompatibility = JavaVersion.VERSION_17
        targetCompatibility = JavaVersion.VERSION_17
    }
}
