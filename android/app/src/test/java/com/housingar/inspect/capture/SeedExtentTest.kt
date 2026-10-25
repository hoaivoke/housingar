package com.housingar.inspect.capture

import com.housingar.inspect.geometry.SurfaceGeometry
import org.junit.Assert.assertEquals
import org.junit.Assert.assertNotNull
import org.junit.Assert.assertTrue
import org.junit.Test
import kotlin.math.abs
import kotlin.math.roundToInt
import kotlin.math.sqrt

/**
 * A 40 × 30 × 30 cm box on a table (top at y = 0.61) with a wall behind it. The Mac treats all depth
 * inside the seed box as object, so the box must clear the table and the wall yet contain the object.
 */
class SeedExtentTest {
  private val boxMin = floatArrayOf(-0.2f, 0.61f, -0.15f)
  private val boxMax = floatArrayOf(0.2f, 0.91f, 0.15f)
  private val tableY = 0.61f
  private val identity = FloatArray(16).also { it[0] = 1f; it[5] = 1f; it[10] = 1f; it[15] = 1f }
  private val k = SurfaceGeometry.Intrinsics(500f, 500f, 320f, 240f, 640, 480)

  private fun lookAt(eye: FloatArray, target: FloatArray): FloatArray {
    val f = norm(floatArrayOf(target[0] - eye[0], target[1] - eye[1], target[2] - eye[2]))
    val r = norm(cross(f, floatArrayOf(0f, 1f, 0f)))
    val u = cross(r, f)
    return floatArrayOf(r[0], r[1], r[2], 0f, u[0], u[1], u[2], 0f, -f[0], -f[1], -f[2], 0f, eye[0], eye[1], eye[2], 1f)
  }

  private fun cross(a: FloatArray, b: FloatArray) = floatArrayOf(a[1] * b[2] - a[2] * b[1], a[2] * b[0] - a[0] * b[2], a[0] * b[1] - a[1] * b[0])
  private fun norm(a: FloatArray): FloatArray { val l = sqrt(a[0] * a[0] + a[1] * a[1] + a[2] * a[2]); return floatArrayOf(a[0] / l, a[1] / l, a[2] / l) }

  /** Nearest hit along a camera ray: returns (t, isObject). dirCam has z = -1, so t is the depth. */
  private fun trace(cam: FloatArray, dirCam: FloatArray, wallZ: Float): Pair<Float, Boolean>? {
    val o = floatArrayOf(cam[12], cam[13], cam[14])
    val d = floatArrayOf(
      cam[0] * dirCam[0] + cam[4] * dirCam[1] + cam[8] * dirCam[2],
      cam[1] * dirCam[0] + cam[5] * dirCam[1] + cam[9] * dirCam[2],
      cam[2] * dirCam[0] + cam[6] * dirCam[1] + cam[10] * dirCam[2])
    var best = Float.MAX_VALUE
    var isObj = false
    // Box (slab method).
    var t0 = -Float.MAX_VALUE
    var t1 = Float.MAX_VALUE
    var hitBox = true
    for (a in 0..2) {
      if (abs(d[a]) < 1e-9f) { if (o[a] < boxMin[a] || o[a] > boxMax[a]) hitBox = false; continue }
      var ta = (boxMin[a] - o[a]) / d[a]
      var tb = (boxMax[a] - o[a]) / d[a]
      if (ta > tb) { val s = ta; ta = tb; tb = s }
      t0 = maxOf(t0, ta); t1 = minOf(t1, tb)
    }
    if (hitBox && t0 <= t1 && t0 > 0f) { best = t0; isObj = true }
    if (d[1] < 0f) {
      val t = (tableY - o[1]) / d[1]
      val z = o[2] + d[2] * t
      if (t > 0f && z > wallZ && t < best) { best = t; isObj = false }
    }
    if (d[2] < 0f) {
      val t = (wallZ - o[2]) / d[2]
      if (t > 0f && o[1] + d[1] * t > tableY - 1f && t < best) { best = t; isObj = false }
    }
    return if (best == Float.MAX_VALUE) null else best to isObj
  }

  private fun scene(eye: FloatArray, wallZ: Float): Triple<SurfaceGeometry.Mask, SurfaceGeometry.DepthImage, FloatArray> {
    val cam = lookAt(eye, floatArrayOf(0f, 0.76f, 0f))
    val bits = BooleanArray(640 * 480)
    for (v in 0 until 480) for (u in 0 until 640) {
      val hit = trace(cam, floatArrayOf((u + 0.5f - k.cx) / k.fx, (k.cy - (v + 0.5f)) / k.fy, -1f), wallZ)
      bits[v * 640 + u] = hit?.second == true
    }
    val dw = 160
    val dh = 120
    val dk = k.scaledTo(dw, dh)
    val mm = ShortArray(dw * dh)
    for (y in 0 until dh) for (x in 0 until dw) {
      val hit = trace(cam, floatArrayOf((x + 0.5f - dk.cx) / dk.fx, (dk.cy - (y + 0.5f)) / dk.fy, -1f), wallZ) ?: continue
      mm[y * dw + x] = (hit.first * 1000f).roundToInt().toShort()
    }
    return Triple(SurfaceGeometry.Mask(640, 480, bits), SurfaceGeometry.DepthImage(dw, dh, mm, k, floatArrayOf(0f, 0f, 640f, 480f)), cam)
  }

  private fun assertClearOfBackgroundAndContainsObject(e: SeedExtent, wallZ: Float, label: String) {
    val mn = e.bboxMinAnchor
    val mx = e.bboxMaxAnchor
    // Clears the table, but cuts off at most ~2.5 cm of the object's bottom.
    assertTrue("$label bottom ${mn[1]}", mn[1] >= tableY + 0.01f && mn[1] <= tableY + 0.025f)
    // Clears the wall behind.
    assertTrue("$label back ${mn[2]} vs wall $wallZ", mn[2] >= wallZ + 0.01f)
    // Contains the rest of the object.
    assertTrue("$label x ${mn[0]}..${mx[0]}", mn[0] <= boxMin[0] && mx[0] >= boxMax[0])
    assertTrue("$label top ${mx[1]}", mx[1] >= boxMax[1])
    assertTrue("$label front ${mx[2]}", mx[2] >= boxMax[2])
    assertNotNull(e.supportAnchor)
    assertEquals(tableY, e.supportAnchor!!, 0.01f)
  }

  @Test
  fun viewFromAboveSeesTheTopAndClearsTableAndWall() {
    val wallZ = -0.40f
    val (mask, depth, cam) = scene(floatArrayOf(0.35f, 1.35f, 1.2f), wallZ)
    val e = SeedExtent.compute(mask, depth, cam, identity, identity)!!
    assertClearOfBackgroundAndContainsObject(e, wallZ, "above")
    assertTrue("back of the object kept: ${e.bboxMinAnchor[2]}", e.bboxMinAnchor[2] <= boxMin[2] + 0.01f)
  }

  @Test
  fun straightOnViewSquaresTheUnseenDepthButStopsBeforeTheWall() {
    // Eye level with the box's middle: only the front face is visible, so depth must be guessed.
    val wallZ = -0.22f
    val (mask, depth, cam) = scene(floatArrayOf(0f, 0.76f, 1.0f), wallZ)
    val e = SeedExtent.compute(mask, depth, cam, identity, identity)!!
    assertClearOfBackgroundAndContainsObject(e, wallZ, "straight-on")
    // Without the wall the squared footprint would reach z = 0.15 - 0.40 - 0.03 = -0.28.
    assertTrue("pulled in front of the wall: ${e.bboxMinAnchor[2]}", e.bboxMinAnchor[2] in (wallZ + 0.01f)..(wallZ + 0.03f))
  }

  @Test
  fun noDepthOnTheObjectGivesNull() {
    val (mask, depth, cam) = scene(floatArrayOf(0.35f, 1.35f, 1.2f), -0.4f)
    val empty = SurfaceGeometry.Mask(640, 480, BooleanArray(640 * 480))
    assertEquals(null, SeedExtent.compute(empty, depth, cam, identity, identity))
    assertTrue(mask.bits.any { it })
  }
}
