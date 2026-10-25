package com.housingar.inspect.capture

import kotlin.math.PI
import kotlin.math.acos
import kotlin.math.atan2
import kotlin.math.sqrt

/**
 * Walk-around coverage and keyframe rules for "Create mesh". Pure Kotlin (no Android/ARCore) so
 * the rules can be unit tested. World space is ARCore's: +Y up (gravity), metres.
 */
class OrbitCoverage(
  /** Object centre in world space. */
  val center: FloatArray,
  val sectors: Int = 24,
) {
  private val covered = BooleanArray(sectors)

  val coveredCount get() = covered.count { it }
  val fraction get() = coveredCount.toFloat() / sectors
  fun isCovered(i: Int) = covered[i]

  /** Azimuth of the camera around the object, degrees in [0, 360), measured in the horizontal plane. */
  fun azimuthDeg(cameraPos: FloatArray): Float {
    val dx = cameraPos[0] - center[0]
    val dz = cameraPos[2] - center[2]
    var a = Math.toDegrees(atan2(dx.toDouble(), dz.toDouble())).toFloat()
    if (a < 0f) a += 360f
    return a
  }

  fun sectorOf(cameraPos: FloatArray): Int = (azimuthDeg(cameraPos) / (360f / sectors)).toInt().coerceIn(0, sectors - 1)

  fun markCovered(cameraPos: FloatArray) {
    covered[sectorOf(cameraPos)] = true
  }
}

/** What the keyframe rules need to know about the current frame. */
class FrameCandidate(
  val timestampNs: Long,
  val tracking: Boolean,
  val cameraPos: FloatArray,
  /** Object centre projected to normalised device coordinates (x, y in [-1, 1]), or null if behind the camera. */
  val objectNdc: FloatArray?,
  /** Camera rotation speed, degrees per second, from consecutive frame poses. */
  val angularSpeedDegPerS: Float,
)

class KeyframeDecision(val capture: Boolean, val guidance: String?)

/**
 * Decides when to keep a keyframe: the object must be in view, the camera reasonably still, and the
 * viewpoint meaningfully different from the last keyframe (angle around the object or translation).
 */
class KeyframePolicy(
  private val coverage: OrbitCoverage,
  val minAngleDeg: Float = 7f,
  val minTranslationM: Float = 0.12f,
  val minIntervalNs: Long = 350_000_000L,
  val maxAngularSpeedDegPerS: Float = 35f,
  val minDistanceM: Float = 0.25f,
  val maxDistanceM: Float = 3.0f,
  val maxFrames: Int = 90,
  /** Object centre must project inside this NDC box (keeps it away from the image border). */
  val viewMargin: Float = 0.7f,
) {
  private var lastPos: FloatArray? = null
  private var lastTimestampNs = 0L
  private val sharpness = ArrayList<Float>()
  var frameCount = 0
    private set

  fun evaluate(c: FrameCandidate): KeyframeDecision {
    if (frameCount >= maxFrames) return KeyframeDecision(false, "Enough frames captured. Press Finish.")
    if (!c.tracking) return KeyframeDecision(false, "Tracking lost: move slowly and point at textured surfaces.")
    val ndc = c.objectNdc
    if (ndc == null || ndc[0] < -viewMargin || ndc[0] > viewMargin || ndc[1] < -viewMargin || ndc[1] > viewMargin) {
      return KeyframeDecision(false, "Keep the object in the middle of the screen.")
    }
    val dist = distance(c.cameraPos, coverage.center)
    if (dist < minDistanceM) return KeyframeDecision(false, "Too close: step back a little.")
    if (dist > maxDistanceM) return KeyframeDecision(false, "Too far: move closer to the object.")
    if (c.angularSpeedDegPerS > maxAngularSpeedDegPerS) return KeyframeDecision(false, "Slow down: turn the phone more slowly.")
    val progress = "Walk slowly around the object: ${coverage.coveredCount}/${coverage.sectors} angles, $frameCount frames."
    val prev = lastPos
    if (prev != null) {
      if (c.timestampNs - lastTimestampNs < minIntervalNs) return KeyframeDecision(false, progress)
      val angle = angleAroundCenter(prev, c.cameraPos)
      val moved = distance(prev, c.cameraPos)
      if (angle < minAngleDeg && moved < minTranslationM) return KeyframeDecision(false, progress)
    }
    return KeyframeDecision(true, progress)
  }

  /**
   * Second stage, after the image has been read: reject blurry frames relative to what this capture
   * has achieved so far. Returns true if the frame should be kept.
   */
  fun acceptSharpness(value: Float): Boolean {
    if (sharpness.size >= 3) {
      val sorted = sharpness.sorted()
      val median = sorted[sorted.size / 2]
      if (value < 0.55f * median) return false
    }
    return true
  }

  fun onCaptured(c: FrameCandidate, sharpnessValue: Float) {
    lastPos = c.cameraPos.copyOf()
    lastTimestampNs = c.timestampNs
    sharpness.add(sharpnessValue)
    frameCount++
    coverage.markCovered(c.cameraPos)
  }

  /** Registers a frame captured outside the rules (the seed frame). */
  fun onSeed(cameraPos: FloatArray, timestampNs: Long, sharpnessValue: Float) =
    onCaptured(FrameCandidate(timestampNs, true, cameraPos, floatArrayOf(0f, 0f), 0f), sharpnessValue)

  fun angleAroundCenter(a: FloatArray, b: FloatArray): Float {
    val va = floatArrayOf(a[0] - coverage.center[0], a[1] - coverage.center[1], a[2] - coverage.center[2])
    val vb = floatArrayOf(b[0] - coverage.center[0], b[1] - coverage.center[1], b[2] - coverage.center[2])
    val la = len(va)
    val lb = len(vb)
    if (la < 1e-6f || lb < 1e-6f) return 0f
    val cos = ((va[0] * vb[0] + va[1] * vb[1] + va[2] * vb[2]) / (la * lb)).coerceIn(-1f, 1f)
    return (acos(cos.toDouble()) * 180.0 / PI).toFloat()
  }

  private fun len(v: FloatArray) = sqrt(v[0] * v[0] + v[1] * v[1] + v[2] * v[2])
  private fun distance(a: FloatArray, b: FloatArray) = len(floatArrayOf(a[0] - b[0], a[1] - b[1], a[2] - b[2]))

  companion object {
    /**
     * Sharpness proxy: variance of a 4-neighbour Laplacian over the luma plane, sampled on a grid
     * in the central half of the image. Higher is sharper; only compared within one capture.
     */
    fun laplacianVariance(luma: ByteArray, width: Int, height: Int, step: Int = 4): Float {
      var sum = 0.0
      var sumSq = 0.0
      var n = 0
      val x0 = maxOf(1, width / 4)
      val x1 = minOf(width - 2, width * 3 / 4)
      val y0 = maxOf(1, height / 4)
      val y1 = minOf(height - 2, height * 3 / 4)
      var y = y0
      while (y <= y1) {
        var x = x0
        val row = y * width
        while (x <= x1) {
          val c = luma[row + x].toInt() and 0xFF
          val l = (luma[row + x - 1].toInt() and 0xFF) + (luma[row + x + 1].toInt() and 0xFF) +
            (luma[row - width + x].toInt() and 0xFF) + (luma[row + width + x].toInt() and 0xFF) - 4 * c
          sum += l
          sumSq += l.toDouble() * l
          n++
          x += step
        }
        y += step
      }
      if (n == 0) return 0f
      val mean = sum / n
      return (sumSq / n - mean * mean).toFloat()
    }
  }
}
