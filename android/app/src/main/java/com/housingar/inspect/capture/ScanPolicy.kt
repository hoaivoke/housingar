package com.housingar.inspect.capture

import kotlin.math.PI
import kotlin.math.acos
import kotlin.math.sqrt

/** What the scan keyframe rules need to know about the current frame (world space, metres). */
class ScanCandidate(
  val timestampNs: Long,
  val tracking: Boolean,
  val cameraPos: FloatArray,
  /** Unit viewing direction of the camera (its −Z axis) in world space. */
  val forward: FloatArray,
  val angularSpeedDegPerS: Float,
)

/**
 * Keyframe rules for "Scan stuff": a free walk-through of an area, not an orbit around one object.
 * A frame is kept when the camera has moved or turned enough since the last keyframe, is not
 * turning fast, and is not blurry relative to the scan so far. Pure Kotlin for unit tests.
 */
class ScanPolicy(
  val minTranslationM: Float = 0.10f,
  val minRotationDeg: Float = 10f,
  val minIntervalNs: Long = 250_000_000L,
  val maxAngularSpeedDegPerS: Float = 45f,
  val maxFrames: Int = 400,
) {
  private var lastPos: FloatArray? = null
  private var lastForward: FloatArray? = null
  private var lastTimestampNs = 0L
  private var prevPos: FloatArray? = null
  private val sharpness = ArrayList<Float>()

  var frameCount = 0
    private set
  /** Path length walked while scanning (all frames, not just keyframes). */
  var distanceWalkedM = 0f
    private set

  fun evaluate(c: ScanCandidate): KeyframeDecision {
    prevPos?.let { if (c.tracking) distanceWalkedM += distance(it, c.cameraPos) }
    prevPos = c.cameraPos.copyOf()
    if (frameCount >= maxFrames) return KeyframeDecision(false, "Scan is full ($maxFrames frames). Press Finish.")
    if (!c.tracking) return KeyframeDecision(false, "Tracking lost: move slowly and point at textured surfaces.")
    if (c.angularSpeedDegPerS > maxAngularSpeedDegPerS) return KeyframeDecision(false, "Slow down: turn the phone more slowly.")
    // No advice while things are fine: the scan's guidance then follows the tapped objects.
    val progress: String? = null
    val prev = lastPos
    val prevF = lastForward
    if (prev != null && prevF != null) {
      if (c.timestampNs - lastTimestampNs < minIntervalNs) return KeyframeDecision(false, progress)
      val moved = distance(prev, c.cameraPos)
      val turned = angleDeg(prevF, c.forward)
      if (moved < minTranslationM && turned < minRotationDeg) return KeyframeDecision(false, progress)
    }
    return KeyframeDecision(true, progress)
  }

  /** Rejects frames much blurrier than the scan's median so far. */
  fun acceptSharpness(value: Float): Boolean {
    if (sharpness.size >= 5) {
      val sorted = sharpness.sorted()
      if (value < 0.5f * sorted[sorted.size / 2]) return false
    }
    return true
  }

  fun onCaptured(c: ScanCandidate, sharpnessValue: Float) {
    lastPos = c.cameraPos.copyOf()
    lastForward = c.forward.copyOf()
    lastTimestampNs = c.timestampNs
    sharpness.add(sharpnessValue)
    frameCount++
  }

  private fun distance(a: FloatArray, b: FloatArray): Float {
    val dx = a[0] - b[0]
    val dy = a[1] - b[1]
    val dz = a[2] - b[2]
    return sqrt(dx * dx + dy * dy + dz * dz)
  }

  private fun angleDeg(a: FloatArray, b: FloatArray): Float {
    val la = sqrt(a[0] * a[0] + a[1] * a[1] + a[2] * a[2])
    val lb = sqrt(b[0] * b[0] + b[1] * b[1] + b[2] * b[2])
    if (la < 1e-6f || lb < 1e-6f) return 0f
    val c = ((a[0] * b[0] + a[1] * b[1] + a[2] * b[2]) / (la * lb)).coerceIn(-1f, 1f)
    return (acos(c.toDouble()) * 180.0 / PI).toFloat()
  }
}
