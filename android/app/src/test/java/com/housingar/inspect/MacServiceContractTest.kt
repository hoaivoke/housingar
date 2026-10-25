package com.housingar.inspect

import org.json.JSONObject
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Assume.assumeTrue
import org.junit.Before
import org.junit.Test
import java.util.Base64

/**
 * Exercises the phone's real HTTP client and metadata against a running segmentation service.
 * Skipped unless -PsegmentationUrl=... is passed. Use the floodfill backend for predictable masks.
 */
class MacServiceContractTest {
  private lateinit var client: MacServiceClient
  private val sessionId = "contract" + (System.currentTimeMillis() % 100000)

  @Before
  fun setUp() {
    val url = System.getProperty("segmentation.url").orEmpty()
    assumeTrue("set -PsegmentationUrl to run", url.isNotBlank())
    client = MacServiceClient { url }
  }

  /** 640x480: left half light grey "wall", right half blue; floodfill from the left stays left. */
  private fun roomJpeg(): ByteArray = javaClass.getResourceAsStream("/room_two_tone.jpg")!!.use { it.readBytes() }

  /** Width and height from a PNG's IHDR chunk. */
  private fun pngSize(png: ByteArray): Pair<Int, Int> {
    fun be(o: Int) = ((png[o].toInt() and 0xFF) shl 24) or ((png[o + 1].toInt() and 0xFF) shl 16) or
      ((png[o + 2].toInt() and 0xFF) shl 8) or (png[o + 3].toInt() and 0xFF)
    return be(16) to be(20)
  }

  private fun frame(depthTs: Long?) = CapturedFrame(
    frameId = "$sessionId-1", sessionId = sessionId, timestampNs = 123_456_789L, width = 640, height = 480,
    nv21 = ByteArray(0), pointX = 100f, pointY = 200f, intrinsics = floatArrayOf(500f, 500f, 320f, 240f),
    cameraPose = floatArrayOf(0f, 1.4f, 0f, 0f, 0f, 0f, 1f), sensorToDisplayRotation = 90, depthTimestampNs = depthTs,
  )

  @Test
  fun healthSegmentCorrectionAndLogsMatchThePhoneClient() {
    val health = client.health()
    assertTrue("health: ${health.error}", health.ok)

    val jpeg = roomJpeg()
    val f = frame(depthTs = null) // no depth yet must still be accepted
    val first = client.segment(f.frameId, jpeg, f.pointX, f.pointY, f.metadata())
    assertEquals(640, first.getInt("width"))
    assertEquals(480, first.getInt("height"))
    assertFalse(first.getBoolean("embedding_cached"))
    val masks = first.getJSONArray("masks")
    assertTrue(masks.length() >= 1)
    val best = masks.getJSONObject(0)
    for (k in listOf("score", "area_fraction", "mask_png_b64", "polygon", "bbox")) assertTrue("missing $k", best.has(k))
    assertEquals(640 to 480, pngSize(Base64.getDecoder().decode(best.getString("mask_png_b64"))))
    // Floodfill from (100, 200) should cover exactly the left half.
    val bbox = best.getJSONArray("bbox")
    assertEquals(0, bbox.getInt(0))
    assertTrue("bbox right edge ${bbox.getInt(2)}", bbox.getInt(2) in 315..325)
    assertEquals(0.5, best.getDouble("area_fraction"), 0.02)
    assertTrue(best.getJSONArray("polygon").length() >= 4)

    // Correction on the same frame: identical bytes reuse the cached embedding.
    val corrected = client.segment(f.frameId, jpeg, listOf(floatArrayOf(100f, 200f, 1f), floatArrayOf(250f, 50f, 0f)), frame(999L).metadata())
    assertTrue(corrected.getBoolean("embedding_cached"))

    // Reusing the frame id with different bytes is a client bug the service must reject.
    val other = roomJpeg().also { it[it.size / 2] = (it[it.size / 2] + 1).toByte() }
    try {
      client.segment(f.frameId, other, f.pointX, f.pointY, f.metadata())
      throw AssertionError("expected HTTP 409")
    } catch (e: MacServiceClient.ServiceException) {
      assertEquals(409, e.code)
    }

    assertTrue(client.postJson("/v1/logs/drift", JSONObject().put("kind", "m1_drift_measurement").put("session_id", sessionId).put("displacement_in_plane_cm", 1.2)))
    val report = Diagnostics().report(JSONObject().put("model", "contract-test"))
    assertTrue(client.postJson("/v1/diagnostics", report))
  }
}
