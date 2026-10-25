package com.housingar.inspect.capture

import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test
import java.io.ByteArrayInputStream
import java.io.ByteArrayOutputStream
import java.io.File
import java.nio.file.Files
import java.util.zip.ZipInputStream
import kotlin.math.cos
import kotlin.math.sin

class TappedObjectsTest {
  private val center = floatArrayOf(1f, 0.5f, -2f)
  private fun around(deg: Float, r: Float = 1.5f) = floatArrayOf(center[0] + r * sin(Math.toRadians(deg.toDouble())).toFloat(), 1.4f, center[2] + r * cos(Math.toRadians(deg.toDouble())).toFloat())

  @Test
  fun coverageIsThreeSixtyMinusLargestGap() {
    val c = ViewCoverage(center)
    assertEquals(0f, c.coverageDeg, 0f)
    c.add(around(0f))
    assertEquals("one view covers nothing", 0f, c.coverageDeg, 0f)
    c.add(around(90f))
    assertEquals(90f, c.coverageDeg, 0.5f)
    c.add(around(180f)); c.add(around(270f))
    assertEquals(270f, c.coverageDeg, 0.5f)
    // Wrap-around: 350° and 10° are 20° apart, not 340°.
    val w = ViewCoverage(center)
    w.add(around(350f)); w.add(around(10f))
    assertEquals(20f, w.coverageDeg, 0.5f)
    assertTrue(c.isCovered(0) && c.isCovered(6) && !c.isCovered(3))
  }

  @Test
  fun readyNeedsEightViewsAndHalfwayRound() {
    val c = ViewCoverage(center)
    repeat(10) { c.add(around(it * 5f)) } // 45° only
    assertFalse(c.ready)
    val d = ViewCoverage(center)
    listOf(0f, 200f).forEach { d.add(around(it)) }
    assertFalse("two views", d.ready)
    val e = ViewCoverage(center)
    repeat(8) { e.add(around(it * 30f)) } // 0..210°
    assertTrue(e.ready)
  }

  /** Camera at [pos] looking at [target], +Y up (ARCore: camera looks down −Z). */
  private fun lookAt(pos: FloatArray, target: FloatArray): FloatArray {
    val f = floatArrayOf(target[0] - pos[0], target[1] - pos[1], target[2] - pos[2]).let { v ->
      val l = kotlin.math.sqrt(v[0] * v[0] + v[1] * v[1] + v[2] * v[2]); floatArrayOf(v[0] / l, v[1] / l, v[2] / l)
    }
    val zc = floatArrayOf(-f[0], -f[1], -f[2])
    // Right = forward × up.
    val x = floatArrayOf(-f[2], 0f, f[0]).let { v -> val l = kotlin.math.sqrt(v[0] * v[0] + v[2] * v[2]); floatArrayOf(v[0] / l, 0f, v[2] / l) }
    val y = floatArrayOf(zc[1] * x[2] - zc[2] * x[1], zc[2] * x[0] - zc[0] * x[2], zc[0] * x[1] - zc[1] * x[0])
    return floatArrayOf(x[0], x[1], x[2], 0f, y[0], y[1], y[2], 0f, zc[0], zc[1], zc[2], 0f, pos[0], pos[1], pos[2], 1f)
  }

  private val k = CaptureFormat.Intrinsics(500f, 500f, 320f, 240f)

  @Test
  fun visibilityChecksFrustumDistanceAndOcclusion() {
    val cam = floatArrayOf(1f, 1.4f, 0f)
    val toward = lookAt(cam, center)
    assertTrue(PointVisibility.visible(center, toward, k, 640, 480))
    // Looking the other way.
    assertFalse(PointVisibility.visible(center, lookAt(cam, floatArrayOf(1f, 1.4f, 3f)), k, 640, 480))
    // Turned 45° sideways: off the side of the image.
    assertFalse(PointVisibility.visible(center, lookAt(cam, floatArrayOf(3f, 1.4f, -2f)), k, 640, 480))
    // Too far away.
    assertFalse(PointVisibility.visible(center, lookAt(floatArrayOf(1f, 1.4f, 6f), center), k, 640, 480))
    // Depth says something is 1.0 m from the camera where the point (≈2.2 m) projects: hidden.
    val toImage = floatArrayOf(4f, 4f, 0f, 0f) // 160×120 depth over 640×480
    val near = ShortArray(160 * 120) { 1000 }
    assertFalse(PointVisibility.visible(center, toward, k, 640, 480, near, 160, 120, toImage))
    val at = ShortArray(160 * 120) { 2150 }
    assertTrue("depth at the point's own distance", PointVisibility.visible(center, toward, k, 640, 480, at, 160, 120, toImage))
    // The thing's own far side, 25 cm nearer than the tapped point (seen from behind): still in view.
    val ownBulk = ShortArray(160 * 120) { 1940 }
    assertTrue("the thing itself doesn't hide it", PointVisibility.visible(center, toward, k, 640, 480, ownBulk, 160, 120, toImage))
    val holes = ShortArray(160 * 120)
    assertTrue("no depth reading → not treated as hidden", PointVisibility.visible(center, toward, k, 640, 480, holes, 160, 120, toImage))
  }

  @Test
  fun guidanceNamesTheWorstCoveredThing() {
    assertTrue(ScanSession.coverageGuidance(emptyList()).startsWith("Tap each thing"))
    val a = TappedObject(1, center, center, 0, floatArrayOf(10f, 10f))
    val b = TappedObject(2, center, center, 1, floatArrayOf(10f, 10f))
    repeat(9) { a.coverage.add(around(it * 30f)) }
    b.coverage.add(around(0f)); b.coverage.add(around(40f))
    assertTrue(ScanSession.coverageGuidance(listOf(a, b)).startsWith("Thing 2: seen from 40°"))
    repeat(9) { b.coverage.add(around(it * 30f)) }
    assertTrue(ScanSession.coverageGuidance(listOf(a, b)).startsWith("All 2 covered"))
    assertEquals("obj_02", b.objectId)
  }

  @Test
  fun moreTapsReprojectIntoTheLatestSeedAndUndoRestoresIt() {
    val first = floatArrayOf(0f, 0.45f, -2f)
    val o = TappedObject(1, first, first, 3, floatArrayOf(100f, 200f))
    assertEquals(1, o.tapCount)
    // Second tap in keyframe 9: the first tap is still in view there (projects to 320, 250).
    o.maskFile = "seeds/obj_01.png"
    o.addTap(floatArrayOf(0.1f, 0.8f, -2.2f), 9, floatArrayOf(400f, 120f)) { p -> if (p === first || p.contentEquals(first)) floatArrayOf(320f, 250f) else null }
    assertEquals(2, o.tapCount)
    assertEquals(9, o.seedFrameIndex)
    assertEquals(listOf(listOf(320f, 250f, 1f), listOf(400f, 120f, 1f)), o.seed.points.map { it.toList() })
    assertEquals("a new seed needs a new outline", null, o.maskFile)
    // Third tap where neither earlier tap is visible: only the new point.
    o.addTap(floatArrayOf(0.5f, 0.5f, -2f), 15, floatArrayOf(50f, 60f)) { null }
    assertEquals(listOf(listOf(50f, 60f, 1f)), o.seed.points.map { it.toList() })
    assertEquals(3, o.tapPoints.size)
    // Undo goes back one seed at a time; the last tap can't be undone here (the thing is removed instead).
    assertTrue(o.undoTap())
    assertEquals(9, o.seedFrameIndex)
    assertTrue(o.undoTap())
    assertEquals(3, o.seedFrameIndex)
    assertEquals(listOf(listOf(100f, 200f, 1f)), o.seed.points.map { it.toList() })
    assertFalse(o.undoTap())
    // The manifest carries the current seed.
    o.addTap(floatArrayOf(0.1f, 0.8f, -2.2f), 9, floatArrayOf(400f, 120f)) { floatArrayOf(320f, 250f) }
    val seed = o.toSeed()
    assertEquals(9, seed.seedFrameIndex)
    assertEquals(2, seed.points.size)
  }

  @Test
  fun projectReturnsThePixel() {
    val cam = floatArrayOf(1f, 1.4f, 0f)
    val uv = PointVisibility.project(center, lookAt(cam, center), k, 640, 480)!!
    assertEquals(320f, uv[0], 0.5f)
    assertEquals(240f, uv[1], 0.5f)
  }

  private val identity = FloatArray(16).also { it[0] = 1f; it[5] = 1f; it[10] = 1f; it[15] = 1f }
  private fun frame(i: Int) = CaptureFormat.Frame(
    index = i, timestampNs = 1000L + i, image = "frames/%04d.jpg".format(i), width = 640, height = 480,
    intrinsics = k, cameraToAnchor = identity, cameraToWorld = identity,
    depth = CaptureFormat.Depth("depth/%04d.bin".format(i), 160, 120, 990L + i, CaptureFormat.Intrinsics(125f, 125f, 80f, 60f),
      CaptureFormat.depthToImage(0f, 0f, 640f, 480f, 160, 120)),
    confidenceFile = null, sharpness = 100f, orbitDeg = 0f,
  )

  @Test
  fun scanV2ManifestCarriesTheTappedObjects() {
    val a = TappedObject(1, center, floatArrayOf(0.1f, 0.2f, -1.5f), 3, floatArrayOf(321.5f, 200f)).also { it.maskFile = "seeds/obj_01.png" }
    repeat(3) { a.coverage.add(around(it * 60f)) }
    val b = TappedObject(2, center, floatArrayOf(0.5f, 0f, -1f), 7, floatArrayOf(10f, 20f))
    val j = CaptureFormat.scanManifestJson(CaptureFormat.ScanManifest("s_scan_1", "s", null, identity, (0 until 10).map(::frame), listOf(a.toSeed(), b.toSeed())))
    assertEquals("housingar.scan.v2", j.getString("schema"))
    val objs = j.getJSONArray("objects")
    assertEquals(2, objs.length())
    val o = objs.getJSONObject(0)
    assertEquals("obj_01", o.getString("object_id"))
    assertEquals(3, o.getInt("seed_frame_index"))
    assertEquals(321.5, o.getJSONArray("points").getJSONArray(0).getDouble(0), 1e-4)
    assertEquals(1, o.getJSONArray("points").getJSONArray(0).getInt(2))
    assertEquals("seeds/obj_01.png", o.getString("mask"))
    assertEquals(3, o.getJSONArray("point_anchor").length())
    assertEquals(3, o.getJSONObject("phone_coverage").getInt("views"))
    assertTrue(objs.getJSONObject(1).isNull("mask"))
    // Nothing tapped: plain scan_v1, no objects key.
    val v1 = CaptureFormat.scanManifestJson(CaptureFormat.ScanManifest("s_scan_2", "s", null, identity, (0 until 10).map(::frame)))
    assertEquals("housingar.scan.v1", v1.getString("schema"))
    assertFalse(v1.has("objects"))
  }

  @Test(expected = IllegalArgumentException::class)
  fun seedFrameMustBeInTheScan() {
    val a = TappedObject(1, center, center, 42, floatArrayOf(1f, 1f))
    CaptureFormat.ScanManifest("s_scan_1", "s", null, identity, (0 until 10).map(::frame), listOf(a.toSeed()))
  }

  @Test
  fun zipIncludesSeedMasks() {
    val dir = Files.createTempDirectory("scan").toFile()
    File(dir, "frames").mkdirs(); File(dir, "seeds").mkdirs()
    File(dir, "frames/0000.jpg").writeBytes(byteArrayOf(1, 2))
    File(dir, "seeds/obj_01.png").writeBytes(byteArrayOf(3, 4))
    File(dir, "seeds/notes.txt").writeText("x")
    val out = ByteArrayOutputStream()
    CaptureFormat.writeZip(dir, out)
    val names = mutableListOf<String>()
    ZipInputStream(ByteArrayInputStream(out.toByteArray())).use { z -> while (true) { names += (z.nextEntry ?: break).name } }
    assertEquals(listOf("frames/0000.jpg", "seeds/obj_01.png"), names)
  }
}
