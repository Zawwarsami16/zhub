plugins {
    kotlin("jvm") version "2.0.0"
    kotlin("plugin.serialization") version "2.0.0"
    `java-library`
    `maven-publish`
}

group = "com.zawwar"
version = "0.1.0"

java {
    sourceCompatibility = JavaVersion.VERSION_17
    targetCompatibility = JavaVersion.VERSION_17
}

kotlin {
    jvmToolchain(17)
}

repositories {
    mavenCentral()
}

dependencies {
    // Coroutines for async handlers + WS dispatch
    implementation("org.jetbrains.kotlinx:kotlinx-coroutines-core:1.8.1")
    // JSON wire format
    implementation("org.jetbrains.kotlinx:kotlinx-serialization-json:1.7.3")
    // OkHttp for WebSocket transport
    implementation("com.squareup.okhttp3:okhttp:4.12.0")

    testImplementation(kotlin("test"))
    testImplementation("org.jetbrains.kotlinx:kotlinx-coroutines-test:1.8.1")
}

tasks.test {
    useJUnitPlatform()
}

publishing {
    publications {
        create<MavenPublication>("maven") {
            from(components["java"])
            artifactId = "zhub"
        }
    }
}
