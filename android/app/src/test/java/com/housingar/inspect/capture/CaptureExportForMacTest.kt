package com.housingar.inspect.capture

import org.junit.Assume.assumeTrue
import org.junit.Test
import java.io.File
import kotlin.math.cos
import kotlin.math.sin

/**
 * Writes a complete capture_v1 folder + zip and a scan_v1 folder + zip with the phone's real
 * packaging code, for checking against the Mac's validators. Skipped unless -PcaptureOut=/some/dir is passed.
 */
class CaptureExportForMacTest {
  private fun pose(tx: Float, ty: Float, tz: Float, yawDeg: Float): FloatArray {
    val a = Math.toRadians(yawDeg.toDouble())
    val c = cos(a).toFloat(); val s = sin(a).toFloat()
    // Rotation about +Y, column-major, plus translation.
    return floatArrayOf(c, 0f, -s, 0f, 0f, 1f, 0f, 0f, s, 0f, c, 0f, tx, ty, tz, 1f)
  }

  private fun mul(a: FloatArray, b: FloatArray) = FloatArray(16).also { o ->
    for (r in 0..3) for (c in 0..3) { var v = 0f; for (k in 0..3) v += a[k * 4 + r] * b[c * 4 + k]; o[c * 4 + r] = v }
  }

  private fun inverseRigid(m: FloatArray): FloatArray {
    val o = FloatArray(16)
    for (r in 0..2) for (c in 0..2) o[c * 4 + r] = m[r * 4 + c]
    for (r in 0..2) o[12 + r] = -(o[r] * m[12] + o[4 + r] * m[13] + o[8 + r] * m[14])
    o[15] = 1f
    return o
  }

  @Test
  fun exportCapture() {
    val out = System.getProperty("capture.out").orEmpty()
    assumeTrue("set -PcaptureOut to run", out.isNotBlank())
    val dir = File(out, "phone_capture").also { it.deleteRecursively(); it.mkdirs() }
    File(dir, "frames").mkdirs(); File(dir, "depth").mkdirs(); File(dir, "confidence").mkdirs()
    val jpeg = javaClass.getResourceAsStream("/room_two_tone.jpg")!!.readBytes()
    File(dir, "seed_mask.png").writeBytes(javaClass.getResourceAsStream("/room_two_tone_mask.png")!!.readBytes())

    val anchorToWorld = pose(0.2f, 0.75f, -1.2f, 30f)
    val worldToAnchor = inverseRigid(anchorToWorld)
    val imageK = CaptureFormat.Intrinsics(500f, 500f, 320f, 240f)
    val frames = (0 until 14).map { i ->
      val yaw = i * 25f
      val camToWorld = pose(0.2f + 1.1f * sin(Math.toRadians(yaw.toDouble())).toFloat(), 1.3f, -1.2f + 1.1f * cos(Math.toRadians(yaw.toDouble())).toFloat(), yaw)
      // Depth 160x120 slightly overhanging the 640x480 image, as ARCore may report.
      val crop = CaptureFormat.cropDepthToImage(160, 120, floatArrayOf(-1.5f, -0.2f, 641.5f, 480.3f), 640, 480, imageK)!!
      val name = "%04d".format(i)
      File(dir, "frames/$name.jpg").writeBytes(jpeg)
      File(dir, "depth/$name.bin").writeBytes(CaptureFormat.depthBytes(crop.crop(ShortArray(160 * 120) { 1100 }, 160)))
      val conf = if (i == 0) null else "confidence/$name.bin"
      if (conf != null) File(dir, conf).writeBytes(crop.crop(ByteArray(160 * 120) { 200.toByte() }, 160))
      CaptureFormat.Frame(i, 1_000_000_000L + i * 400_000_000L, "frames/$name.jpg", 640, 480, imageK,
        mul(worldToAnchor, camToWorld), camToWorld,
        CaptureFormat.Depth("depth/$name.bin", crop.width, crop.height, 999_000_000L + i * 400_000_000L, crop.intrinsics, crop.toImage),
        conf, 150f + i, (yaw % 360f))
    }
    val manifest = CaptureFormat.manifestJson(CaptureFormat.Manifest(
      captureId = "phonetest_obj1_1234", sessionId = "phonetest", label = null, anchorToWorld = anchorToWorld,
      seedFrameIndex = 0, seedPoints = listOf(floatArrayOf(160f, 240f, 1f), floatArrayOf(500f, 100f, 0f)), seedMask = "seed_mask.png",
      bboxMin = floatArrayOf(-0.25f, -0.05f, -0.25f), bboxMax = floatArrayOf(0.25f, 0.55f, 0.25f), frames = frames,
    ))
    File(dir, "manifest.json").writeText(manifest.toString(2))
    File(out, "phone_capture.zip").outputStream().use { CaptureFormat.writeZip(dir, it) }
  }

  @Test
  fun exportScan() {
    val out = System.getProperty("capture.out").orEmpty()
    assumeTrue("set -PcaptureOut to run", out.isNotBlank())
    val dir = File(out, "phone_scan").also { it.deleteRecursively(); it.mkdirs() }
    File(dir, "frames").mkdirs(); File(dir, "depth").mkdirs(); File(dir, "confidence").mkdirs()
    val jpeg = javaClass.getResourceAsStream("/room_two_tone.jpg")!!.readBytes()
    // Level scan anchor at the start position (the renderer's AnchorFrames.gravityAligned).
    val anchorToWorld = pose(0.4f, 1.4f, 0.3f, -20f)
    val worldToAnchor = inverseRigid(anchorToWorld)
    val imageK = CaptureFormat.Intrinsics(500f, 500f, 320f, 240f)
    // A walk along a 3 m line with the camera turning slowly: 36 keyframes.
    val frames = (0 until 36).map { i ->
      val camToWorld = pose(0.4f + i * 0.09f, 1.4f, 0.3f - i * 0.02f, -20f + i * 2.5f)
      val crop = CaptureFormat.cropDepthToImage(160, 120, floatArrayOf(-1.5f, -0.2f, 641.5f, 480.3f), 640, 480, imageK)!!
      val name = "%04d".format(i)
      File(dir, "frames/$name.jpg").writeBytes(jpeg)
      File(dir, "depth/$name.bin").writeBytes(CaptureFormat.depthBytes(crop.crop(ShortArray(160 * 120) { 1500 }, 160)))
      File(dir, "confidence/$name.bin").writeBytes(crop.crop(ByteArray(160 * 120) { 220.toByte() }, 160))
      CaptureFormat.Frame(i, 2_000_000_000L + i * 300_000_000L, "frames/$name.jpg", 640, 480, imageK,
        mul(worldToAnchor, camToWorld), camToWorld,
        CaptureFormat.Depth("depth/$name.bin", crop.width, crop.height, 1_999_000_000L + i * 300_000_000L, crop.intrinsics, crop.toImage),
        "confidence/$name.bin", 140f + i, 0f)
    }
    val manifest = CaptureFormat.scanManifestJson(CaptureFormat.ScanManifest("phonetest_scan_1234", "phonetest", null, anchorToWorld, frames))
    File(dir, "manifest.json").writeText(manifest.toString(2))
    File(out, "phone_scan.zip").outputStream().use { CaptureFormat.writeZip(dir, it) }

    // The same walk with two tapped things (scan_v2): one with the outline from tap time, one without.
    val dir2 = File(out, "phone_scan_v2").also { it.deleteRecursively(); dir.copyRecursively(it) }
    File(dir2, "manifest.json").delete()
    File(dir2, "seeds").mkdirs()
    File(dir2, "seeds/obj_01.png").writeBytes(javaClass.getResourceAsStream("/room_two_tone_mask.png")!!.readBytes())
    val objects = listOf(
      CaptureFormat.ScanObjectSeed("obj_01", 4, listOf(floatArrayOf(160f, 240f, 1f)), "seeds/obj_01.png", floatArrayOf(0.3f, -0.6f, -1.2f), 20, 140f),
      CaptureFormat.ScanObjectSeed("obj_02", 20, listOf(floatArrayOf(400f, 260f, 1f)), null, floatArrayOf(1.4f, -0.7f, -1.0f), 9, 60f),
    )
    val manifest2 = CaptureFormat.scanManifestJson(CaptureFormat.ScanManifest("phonetest_scan_v2_1234", "phonetest", null, anchorToWorld, frames, objects))
    File(dir2, "manifest.json").writeText(manifest2.toString(2))
    File(out, "phone_scan_v2.zip").outputStream().use { CaptureFormat.writeZip(dir2, it) }
  }
}
