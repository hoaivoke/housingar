plugins {
    id("com.android.application")
    id("org.jetbrains.kotlin.android")
}

android {
    namespace = "com.housingar.inspect"
    compileSdk = 36

    defaultConfig {
        applicationId = "com.housingar.inspect"
        // ARCore "required" apps need minSdk >= 24. The Pixel 9a ships with Android 15.
        minSdk = 24
        targetSdk = 36
        versionCode = 1
        versionName = "0.1.0-m1"
    }

    buildTypes {
        release {
            isMinifyEnabled = false
            proguardFiles(getDefaultProguardFile("proguard-android-optimize.txt"), "proguard-rules.pro")
        }
    }
    compileOptions {
        sourceCompatibility = JavaVersion.VERSION_17
        targetCompatibility = JavaVersion.VERSION_17
    }
    kotlinOptions {
        jvmTarget = "17"
    }
    buildFeatures {
        buildConfig = true
    }
    testOptions {
        // android.util.Log etc. return defaults in JVM unit tests.
        unitTests.isReturnDefaultValues = true
    }
}

// Contract test against a running segmentation service:
//   ./gradlew testDebugUnitTest -PsegmentationUrl=http://127.0.0.1:8000
tasks.withType<Test>().configureEach {
    systemProperty("segmentation.url", (project.findProperty("segmentationUrl") as String?) ?: "")
    systemProperty("capture.out", (project.findProperty("captureOut") as String?) ?: "")
    systemProperty("glb.in", (project.findProperty("glbIn") as String?) ?: "")
    systemProperty("e2e.dir", (project.findProperty("e2eDir") as String?) ?: "")
    systemProperty("scan.e2e.dir", (project.findProperty("scanE2eDir") as String?) ?: "")
    systemProperty("glb.dir", (project.findProperty("glbDir") as String?) ?: "")
    // MacServiceClient sets PATCH through reflection on the desktop JVM (Android allows it directly).
    jvmArgs("--add-opens", "java.base/java.net=ALL-UNNAMED")
}

dependencies {
    // ARCore (Google Play Services for AR).
    implementation("com.google.ar:core:1.57.0")

    // Wavefront OBJ loader used by the sample renderer's Mesh.createFromAsset.
    implementation("de.javagl:obj:0.4.0")

    implementation("androidx.appcompat:appcompat:1.7.0")
    implementation("androidx.lifecycle:lifecycle-common-java8:2.8.7")
    implementation("com.google.android.material:material:1.12.0")

    testImplementation("junit:junit:4.13.2")
    // Real org.json for JVM tests (the Android stub throws).
    testImplementation("org.json:json:20240303")
}
