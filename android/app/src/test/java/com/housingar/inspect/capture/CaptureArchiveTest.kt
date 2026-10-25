package com.housingar.inspect.capture

import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Test
import java.io.ByteArrayInputStream
import java.io.ByteArrayOutputStream
import java.io.File
import java.nio.ByteBuffer
import java.nio.ByteOrder
import java.nio.file.Files
import java.util.zip.ZipInputStream

class CaptureArchiveTest {
  private val identity = FloatArray(16).also { it[0] = 1f; it[5] = 1f; it[10] = 1f; it[15] = 1f }

  private fun frame(i: Int) = CaptureFormat.Frame(
    index = i, timestampNs = 1000L + i, image = "frames/%04d.jpg".format(i), width = 1920, height = 1080,
    intrinsics = CaptureFormat.Intrinsics(1400f, 1400f, 960f, 540f), cameraToAnchor = identity, cameraToWorld = identity,
    depth = CaptureFormat.Depth("depth/%04d.bin".format(i), 160, 90, 990L + i, CaptureFormat.Intrinsics(116.7f, 116.7f, 80f, 45f),
      CaptureFormat.depthToImage(0f, 0f, 1920f, 1080f, 160, 90)),
    confidenceFile = if (i == 0) null else "confidence/%04d.bin".format(i), sharpness = 123f, orbitDeg = i * 7f,
  )

  private fun manifest() = CaptureFormat.Manifest(
    captureId = "abc123_obj2_99", sessionId = "abc123", label = "lamp", anchorToWorld = identity, seedFrameIndex = 0,
    seedPoints = listOf(floatArrayOf(10f, 20f, 1f), floatArrayOf(30f, 40f, 0f)), seedMask = "seed_mask.png",
    bboxMin = floatArrayOf(-0.1f, 0f, -0.1f), bboxMax = floatArrayOf(0.1f, 0.4f, 0.1f), frames = (0 until 3).map(::frame),
  )

  @Test
  fun manifestMatchesCaptureV1() {
    val j = CaptureFormat.manifestJson(manifest())
    assertEquals("housingar.capture.v1", j.getString("schema"))
    assertEquals("object", j.getString("mode"))
    assertEquals("meters", j.getString("units"))
    assertEquals(16, j.getJSONArray("anchor_to_world").length())
    val seed = j.getJSONObject("seed")
    assertEquals(0, seed.getJSONArray("points").getJSONArray(1).getInt(2))
    assertEquals(3, seed.getJSONObject("bbox_anchor").getJSONArray("max").length())
    val f1 = j.getJSONArray("frames").getJSONObject(1)
    for (k in listOf("index", "timestamp_ns", "tracking", "image", "width", "height", "intrinsics", "camera_to_anchor",
      "camera_to_world", "depth", "confidence", "sharpness", "orbit_deg")) assertTrue("missing $k", f1.has(k))
    val toImage = f1.getJSONObject("depth").getJSONArray("to_image")
    assertEquals(12.0, toImage.getDouble(0), 1e-6) // 1920 / 160
    assertTrue(j.getJSONArray("frames").getJSONObject(0).isNull("confidence"))
  }

  @Test
  fun depthIsLittleEndianUint16() {
    val b = CaptureFormat.depthBytes(shortArrayOf(1, 0x1234, 0xFFFF.toShort()))
    val bb = ByteBuffer.wrap(b).order(ByteOrder.LITTLE_ENDIAN)
    assertEquals(1, bb.getShort(0).toInt())
    assertEquals(0x1234, bb.getShort(2).toInt())
    assertEquals(65535, bb.getShort(4).toInt() and 0xFFFF)
  }

  @Test
  fun zipContainsCaptureFilesAndManifest() {
    val dir = Files.createTempDirectory("cap").toFile()
    File(dir, "frames").mkdirs(); File(dir, "depth").mkdirs()
    File(dir, "frames/0000.jpg").writeBytes(byteArrayOf(1, 2, 3))
    File(dir, "depth/0000.bin").writeBytes(ByteArray(8))
    File(dir, "seed_mask.png").writeBytes(byteArrayOf(9))
    File(dir, "manifest.json").writeText(CaptureFormat.manifestJson(manifest()).toString())
    File(dir, "outputs").mkdirs(); File(dir, "outputs/object.glb").writeBytes(byteArrayOf(1))
    val out = ByteArrayOutputStream()
    CaptureFormat.writeZip(dir, out)
    val names = mutableListOf<String>()
    ZipInputStream(ByteArrayInputStream(out.toByteArray())).use { z ->
      while (true) {
        val e = z.nextEntry ?: break
        names.add(e.name)
        z.readBytes()
      }
    }
    // Only the capture_v1 archive contents: no manifest copy, no downloaded outputs.
    assertEquals(listOf("depth/0000.bin", "frames/0000.jpg", "seed_mask.png"), names)
    dir.deleteRecursively()
  }

  @Test(expected = IllegalArgumentException::class)
  fun unsafeIdsAreRejected() {
    CaptureFormat.Manifest("../x", "s", null, identity, 0, emptyList(), "seed_mask.png", FloatArray(3), FloatArray(3), emptyList())
  }
}

class DepthCropTest {
  private val image = CaptureFormat.Intrinsics(1400f, 1400f, 960f, 540f)

  /** Mirrors the Mac validator (format.py) checks on to_image and intrinsics. */
  private fun assertMacAccepts(c: CaptureFormat.DepthCrop, imageW: Int, imageH: Int) {
    val (sx, sy, tx, ty) = c.toImage.toList()
    org.junit.Assert.assertTrue(sx > 0 && sy > 0 && tx >= -0.01f && ty >= -0.01f)
    org.junit.Assert.assertTrue("right edge ${sx * c.width + tx}", sx * c.width + tx <= imageW + 0.01f)
    org.junit.Assert.assertTrue("bottom edge ${sy * c.height + ty}", sy * c.height + ty <= imageH + 0.01f)
    org.junit.Assert.assertEquals(image.fx, sx * c.intrinsics.fx, 1e-2f)
    org.junit.Assert.assertEquals(image.cx, sx * c.intrinsics.cx + tx, 1e-2f)
    org.junit.Assert.assertEquals(image.cy, sy * c.intrinsics.cy + ty, 1e-2f)
  }

  @Test
  fun depthCoveringTheImageExactlyIsKeptWhole() {
    val c = CaptureFormat.cropDepthToImage(160, 90, floatArrayOf(0f, 0f, 1920f, 1080f), 1920, 1080, image)!!
    assertEquals(160, c.width)
    assertEquals(90, c.height)
    assertMacAccepts(c, 1920, 1080)
  }

  @Test
  fun depthOverhangingTheImageIsCroppedToFit() {
    // Depth covers a slightly larger area than the RGB image (rounding or a wider texture FOV).
    val c = CaptureFormat.cropDepthToImage(160, 120, floatArrayOf(-24f, -0.3f, 1944f, 1080.4f), 1920, 1080, image)!!
    org.junit.Assert.assertTrue(c.i0 >= 2 && c.width <= 158)
    assertMacAccepts(c, 1920, 1080)
    val src = ShortArray(160 * 120) { it.toShort() }
    val out = c.crop(src, 160)
    assertEquals((c.j0 * 160 + c.i0).toShort(), out[0])
  }

  @Test
  fun depthInsideACroppedImageRegionMapsWithAnOffset() {
    val c = CaptureFormat.cropDepthToImage(160, 90, floatArrayOf(0f, 60f, 640f, 420f), 640, 480, CaptureFormat.Intrinsics(500f, 500f, 320f, 240f))!!
    assertEquals(60f, c.toImage[3], 1e-4f)
    assertEquals(4f, c.toImage[0], 1e-4f)
  }

  @Test
  fun noOverlapReturnsNull() {
    org.junit.Assert.assertNull(CaptureFormat.cropDepthToImage(160, 90, floatArrayOf(2000f, 0f, 3000f, 1080f), 1920, 1080, image))
  }
}
