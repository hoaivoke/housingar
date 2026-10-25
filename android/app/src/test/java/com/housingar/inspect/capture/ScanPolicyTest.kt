package com.housingar.inspect.capture

import org.json.JSONObject
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test

class ScanPolicyTest {
  private fun cand(t: Long, x: Float, fwd: FloatArray = floatArrayOf(0f, 0f, -1f), speed: Float = 5f, tracking: Boolean = true) =
    ScanCandidate(t, tracking, floatArrayOf(x, 1.4f, 0f), fwd, speed)

  @Test
  fun walkingProducesAKeyframeEveryTenToFifteenCentimetres() {
    val p = ScanPolicy()
    var kept = 0
    var t = 0L
    // 3 m sideways walk at 0.5 m/s, 30 fps.
    for (i in 0..180) {
      t += 33_333_333L
      val c = cand(t, i * 0.5f / 30f)
      if (p.evaluate(c).capture && p.acceptSharpness(100f)) { p.onCaptured(c, 100f); kept++ }
    }
    // At 0.5 m/s the 250 ms minimum gap (≈ 13 cm) binds before the 10 cm rule: ≈ 23 keyframes.
    assertTrue("kept $kept", kept in 20..31)
    assertEquals(3f, p.distanceWalkedM, 0.05f)
  }

  @Test
  fun turningInPlaceAlsoAddsKeyframes() {
    val p = ScanPolicy()
    var kept = 0
    var t = 0L
    for (deg in 0 until 90) {
      t += 100_000_000L
      val a = Math.toRadians(deg.toDouble())
      val c = cand(t, 0f, floatArrayOf(Math.sin(a).toFloat(), 0f, -Math.cos(a).toFloat()))
      if (p.evaluate(c).capture) { p.onCaptured(c, 100f); kept++ }
    }
    assertTrue("kept $kept", kept in 8..10) // every 10°
  }

  @Test
  fun fastTurnsTrackingLossAndBlurAreRejected() {
    val p = ScanPolicy()
    assertTrue(p.evaluate(cand(0, 0f, speed = 80f)).guidance!!.contains("Slow"))
    assertTrue(p.evaluate(cand(1, 0f, tracking = false)).guidance!!.contains("Tracking"))
    assertTrue(p.evaluate(cand(2, 0f)).capture)
    for (i in 0 until 5) p.onCaptured(cand(i * 1_000_000_000L, i.toFloat()), 200f)
    assertFalse(p.acceptSharpness(90f))
    assertTrue(p.acceptSharpness(150f))
  }

  @Test
  fun scanManifestHasSchemaAnchorAndFramesButNoSeed() {
    val identity = FloatArray(16).also { it[0] = 1f; it[5] = 1f; it[10] = 1f; it[15] = 1f }
    val frame = CaptureFormat.Frame(0, 5L, "frames/0000.jpg", 1920, 1080, CaptureFormat.Intrinsics(1400f, 1400f, 960f, 540f),
      identity, identity, CaptureFormat.Depth("depth/0000.bin", 160, 90, 4L, CaptureFormat.Intrinsics(116.7f, 116.7f, 80f, 45f),
        floatArrayOf(12f, 12f, 0f, 0f)), "confidence/0000.bin", 10f, 0f)
    val j: JSONObject = CaptureFormat.scanManifestJson(CaptureFormat.ScanManifest("s1_scan_1", "s1", "Dorm stuff", identity, listOf(frame)))
    assertEquals("housingar.scan.v1", j.getString("schema"))
    assertEquals("s1_scan_1", j.getString("scan_id"))
    assertEquals("meters", j.getString("units"))
    assertEquals(CaptureFormat.CONVENTIONS, j.getString("conventions"))
    assertFalse(j.has("seed"))
    assertFalse(j.has("mode"))
    assertEquals(1, j.getJSONArray("frames").length())
    assertEquals("depth/0000.bin", j.getJSONArray("frames").getJSONObject(0).getJSONObject("depth").getString("file"))
  }
}
