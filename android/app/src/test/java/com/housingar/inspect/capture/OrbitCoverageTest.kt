package com.housingar.inspect.capture

import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test
import kotlin.math.cos
import kotlin.math.sin

class OrbitCoverageTest {
  private val center = floatArrayOf(0f, 0.8f, -1f)

  private fun posAt(deg: Float, r: Float = 1.2f) = floatArrayOf(
    center[0] + r * sin(Math.toRadians(deg.toDouble())).toFloat(), 1.4f, center[2] + r * cos(Math.toRadians(deg.toDouble())).toFloat())

  private fun cand(t: Long, pos: FloatArray, ndc: FloatArray? = floatArrayOf(0f, 0f), speed: Float = 5f, tracking: Boolean = true) =
    FrameCandidate(t, tracking, pos, ndc, speed)

  @Test
  fun azimuthAndSectorsCoverTheCircle() {
    val c = OrbitCoverage(center)
    assertEquals(0f, c.azimuthDeg(posAt(0f)), 0.01f)
    assertEquals(90f, c.azimuthDeg(posAt(90f)), 0.01f)
    assertEquals(270f, c.azimuthDeg(posAt(270f)), 0.01f)
    for (d in 0 until 360 step 15) c.markCovered(posAt(d + 7.5f))
    assertEquals(24, c.coveredCount)
    assertEquals(1f, c.fraction, 1e-6f)
  }

  @Test
  fun walkingAroundProducesKeyframesEveryFewDegrees() {
    val coverage = OrbitCoverage(center)
    val policy = KeyframePolicy(coverage)
    var t = 0L
    var kept = 0
    // 360° in 1° steps at 30 fps-ish spacing (≈ 0.1 s per degree).
    for (d in 0 until 360) {
      t += 100_000_000L
      val c = cand(t, posAt(d.toFloat()))
      val decision = policy.evaluate(c)
      if (decision.capture && policy.acceptSharpness(100f)) {
        policy.onCaptured(c, 100f)
        kept++
      }
    }
    // Every ~6°: at 1.2 m radius the 12 cm translation rule triggers just before the 7° angle rule.
    assertTrue("kept $kept", kept in 45..65)
    assertEquals(24, coverage.coveredCount)
  }

  @Test
  fun rulesRejectBadFramesWithGuidance() {
    val policy = KeyframePolicy(OrbitCoverage(center))
    assertFalse(policy.evaluate(cand(1, posAt(0f), tracking = false)).capture)
    val outOfView = policy.evaluate(cand(1, posAt(0f), ndc = floatArrayOf(0.9f, 0f)))
    assertFalse(outOfView.capture)
    assertTrue(outOfView.guidance!!.contains("middle"))
    val tooClose = floatArrayOf(center[0] + 0.1f, center[1] + 0.05f, center[2])
    assertTrue(policy.evaluate(cand(1, tooClose)).guidance!!.contains("close"))
    assertTrue(policy.evaluate(cand(1, posAt(0f), speed = 90f)).guidance!!.contains("Slow"))
    assertTrue(policy.evaluate(cand(1, posAt(0f))).capture)
  }

  @Test
  fun standingStillDoesNotAddFramesButSidewaysMotionDoes() {
    val policy = KeyframePolicy(OrbitCoverage(center))
    val p = posAt(0f)
    policy.onCaptured(cand(0, p), 100f)
    assertFalse(policy.evaluate(cand(1_000_000_000L, p)).capture)
    val stepped = floatArrayOf(p[0], p[1] + 0.2f, p[2]) // moved up 20 cm, same azimuth
    assertTrue(policy.evaluate(cand(1_000_000_000L, stepped)).capture)
  }

  @Test
  fun blurryFramesAreRejectedRelativeToTheCapture() {
    val policy = KeyframePolicy(OrbitCoverage(center))
    for (i in 0 until 3) policy.onCaptured(cand(i * 1_000_000_000L, posAt(i * 20f)), 200f)
    assertFalse(policy.acceptSharpness(80f))
    assertTrue(policy.acceptSharpness(150f))
  }

  @Test
  fun laplacianVarianceIsHigherForSharpImages() {
    val w = 64
    val h = 64
    val sharp = ByteArray(w * h) { i -> if (((i % w) / 4 + (i / w) / 4) % 2 == 0) 0 else 255.toByte() }
    val flat = ByteArray(w * h) { 128.toByte() }
    assertTrue(KeyframePolicy.laplacianVariance(sharp, w, h, step = 1) > 1000f)
    assertEquals(0f, KeyframePolicy.laplacianVariance(flat, w, h, step = 1), 1e-3f)
  }
}
