package com.housingar.inspect

import com.google.ar.core.CameraConfig
import com.google.ar.core.Config
import com.google.ar.core.Session
import com.google.ar.core.TrackingFailureReason
import com.google.ar.core.TrackingState
import org.json.JSONObject
import java.util.Locale

/**
 * Milestone 0 evidence collector: tracking, depth, render rate, camera configuration and the
 * phone-to-Mac connection. Frame-related fields are written on the GL thread; [health] and
 * [lastRoundTrip] are written from the network thread.
 */
class Diagnostics {
  val sessionId: String = java.util.UUID.randomUUID().toString().substring(0, 8)

  // Session / camera configuration
  var depthAutomaticSupported = false
  var rawDepthSupported = false
  var depthEnabled = false
  var cameraImageSize = ""
  var cameraTextureSize = ""
  var cameraFpsRange = ""
  var depthSensorUsage = ""
  var cameraId = ""
  private var sessionResumeNs = 0L

  // Tracking
  var trackingState: TrackingState = TrackingState.STOPPED
    private set
  var failureReason: TrackingFailureReason = TrackingFailureReason.NONE
    private set
  var firstTrackingMs: Long? = null
    private set
  var trackingResets = 0
    private set
  private var wasTracking = false

  // Render rate
  private val frameTimes = LongArray(120)
  private var frameCount = 0L
  var cameraFramesSeen = 0L
    private set
  private var lastCameraTimestamp = 0L

  // Depth
  var depthFrames = 0L
    private set
  var firstDepthMs: Long? = null
    private set
  var depthSize = ""
    private set
  var depthAgeMs = -1L
    private set
  var lastDepthTimestampNs: Long? = null
    private set
  var rawDepthFrames = 0L
    private set
  var rawDepthSize = ""
    private set

  // Scene
  var planesHorizontal = 0
  var planesVertical = 0
  var featurePoints = 0
  var markerCount = 0
  var surfaceCount = 0
  var mode = "place"

  // Mac service
  @Volatile var serviceUrl = ""
  @Volatile var health: MacServiceClient.Health? = null
  @Volatile var lastRoundTrip: JSONObject? = null

  fun onSessionConfigured(session: Session, config: Config) {
    depthAutomaticSupported = session.isDepthModeSupported(Config.DepthMode.AUTOMATIC)
    rawDepthSupported = session.isDepthModeSupported(Config.DepthMode.RAW_DEPTH_ONLY)
    depthEnabled = config.depthMode != Config.DepthMode.DISABLED
    val cc: CameraConfig = session.cameraConfig
    cameraImageSize = "${cc.imageSize.width}x${cc.imageSize.height}"
    cameraTextureSize = "${cc.textureSize.width}x${cc.textureSize.height}"
    cameraFpsRange = "${cc.fpsRange.lower}-${cc.fpsRange.upper}"
    depthSensorUsage = cc.depthSensorUsage.name
    cameraId = cc.cameraId
    sessionResumeNs = System.nanoTime()
  }

  fun onFrame(cameraTimestampNs: Long, state: TrackingState, reason: TrackingFailureReason) {
    val now = System.nanoTime()
    frameTimes[(frameCount % frameTimes.size).toInt()] = now
    frameCount++
    if (cameraTimestampNs != 0L && cameraTimestampNs != lastCameraTimestamp) {
      cameraFramesSeen++
      lastCameraTimestamp = cameraTimestampNs
    }
    trackingState = state
    failureReason = reason
    val tracking = state == TrackingState.TRACKING
    if (tracking && firstTrackingMs == null && sessionResumeNs != 0L) {
      firstTrackingMs = (now - sessionResumeNs) / 1_000_000
    }
    if (wasTracking && !tracking) trackingResets++
    wasTracking = tracking
  }

  fun onDepth(width: Int, height: Int, depthTimestampNs: Long, frameTimestampNs: Long) {
    depthFrames++
    if (firstDepthMs == null && sessionResumeNs != 0L) firstDepthMs = (System.nanoTime() - sessionResumeNs) / 1_000_000
    depthSize = "${width}x$height"
    depthAgeMs = (frameTimestampNs - depthTimestampNs) / 1_000_000
    lastDepthTimestampNs = depthTimestampNs
  }

  fun onRawDepth(width: Int, height: Int) {
    rawDepthFrames++
    rawDepthSize = "${width}x$height"
  }

  /** Render rate over the last ~120 frames. */
  fun fps(): Float {
    val n = minOf(frameCount, frameTimes.size.toLong()).toInt()
    if (n < 2) return 0f
    val newest = frameTimes[((frameCount - 1) % frameTimes.size).toInt()]
    val oldest = frameTimes[((frameCount - n) % frameTimes.size).toInt()]
    val dt = (newest - oldest) / 1e9f
    return if (dt > 0f) (n - 1) / dt else 0f
  }

  fun hudText(): String {
    val h = health
    val service = when {
      h == null -> "checking…"
      h.ok -> "OK ${h.backend}/${h.model} on ${h.device} (${h.latencyMs} ms)"
      else -> "unreachable (${h.error?.take(40)})"
    }
    val track = if (trackingState == TrackingState.TRACKING) "TRACKING" else "$trackingState/${failureReason.name}"
    val depth = when {
      !depthAutomaticSupported -> "NOT SUPPORTED"
      depthFrames == 0L -> "supported, waiting for first depth image"
      else -> "$depthSize, age ${depthAgeMs} ms, n=$depthFrames"
    }
    val rt = lastRoundTrip
    val rtLine = if (rt == null) "—" else if (rt.optBoolean("ok")) {
      String.format(Locale.US, "%d ms total, score %.2f, %s", rt.optLong("round_trip_ms"), rt.optDouble("score"), rt.optString("backend"))
    } else "failed: ${rt.optString("error").take(40)}"
    return buildString {
      appendLine("Track  $track  resets=$trackingResets")
      appendLine("Depth  $depth")
      appendLine("Raw    ${if (rawDepthSupported) (if (rawDepthFrames > 0) rawDepthSize else "supported") else "no"}")
      appendLine(String.format(Locale.US, "Render %.1f fps  cam %s", fps(), cameraImageSize))
      appendLine("Planes ${planesHorizontal}H ${planesVertical}V  points $featurePoints")
      appendLine("Mark   $markerCount  surfaces $surfaceCount  mode=$mode")
      appendLine("Mac    $service")
      append("Frame  $rtLine")
    }
  }

  /** Milestone 0 readiness report, saved on the phone and posted to the Mac. */
  fun report(device: JSONObject): JSONObject {
    val h = health
    val rt = lastRoundTrip
    val checks = JSONObject()
      .put("camera_frames", cameraFramesSeen > 0)
      .put("tracking", firstTrackingMs != null)
      .put("depth_supported", depthAutomaticSupported)
      .put("depth_images_received", depthFrames > 0)
      .put("mac_service_reachable", h?.ok == true)
      .put("frame_round_trip", rt?.optBoolean("ok") == true)
    return JSONObject()
      .put("kind", "m0_readiness")
      .put("session_id", sessionId)
      .put("created_ms", System.currentTimeMillis())
      .put("device", device)
      .put("m0_checks", checks)
      .put("camera", JSONObject()
        .put("id", cameraId)
        .put("image_size", cameraImageSize)
        .put("texture_size", cameraTextureSize)
        .put("fps_range", cameraFpsRange)
        .put("depth_sensor_usage", depthSensorUsage)
        .put("frames_seen", cameraFramesSeen))
      .put("tracking", JSONObject()
        .put("state", trackingState.name)
        .put("failure_reason", failureReason.name)
        .put("first_tracking_ms", firstTrackingMs ?: JSONObject.NULL)
        .put("resets", trackingResets))
      .put("depth", JSONObject()
        .put("automatic_supported", depthAutomaticSupported)
        .put("raw_supported", rawDepthSupported)
        .put("enabled", depthEnabled)
        .put("frames", depthFrames)
        .put("first_depth_ms", firstDepthMs ?: JSONObject.NULL)
        .put("size", depthSize)
        .put("last_age_ms", depthAgeMs)
        .put("raw_frames", rawDepthFrames)
        .put("raw_size", rawDepthSize))
      .put("render", JSONObject().put("fps_recent", fps().toDouble()))
      .put("scene", JSONObject()
        .put("planes_horizontal", planesHorizontal)
        .put("planes_vertical", planesVertical)
        .put("feature_points", featurePoints))
      .put("service", JSONObject()
        .put("url", serviceUrl)
        .put("ok", h?.ok ?: false)
        .put("backend", h?.backend ?: "")
        .put("model", h?.model ?: "")
        .put("device", h?.device ?: "")
        .put("ping_ms", h?.latencyMs ?: -1)
        .put("error", h?.error ?: JSONObject.NULL))
      .put("last_frame_round_trip", rt ?: JSONObject.NULL)
  }
}
