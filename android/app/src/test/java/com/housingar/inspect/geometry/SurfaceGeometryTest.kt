package com.housingar.inspect.geometry

import com.housingar.inspect.geometry.SurfaceGeometry.DepthImage
import com.housingar.inspect.geometry.SurfaceGeometry.Failure
import com.housingar.inspect.geometry.SurfaceGeometry.Input
import com.housingar.inspect.geometry.SurfaceGeometry.Intrinsics
import com.housingar.inspect.geometry.SurfaceGeometry.Mask
import com.housingar.inspect.geometry.SurfaceGeometry.Outcome
import com.housingar.inspect.geometry.SurfaceGeometry.PlaneEstimate
import org.junit.Assert.assertEquals
import org.junit.Assert.assertNotNull
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test
import kotlin.math.abs
import kotlin.math.acos
import kotlin.math.cos
import kotlin.math.roundToInt
import kotlin.math.sin
import kotlin.random.Random

/**
 * Synthetic scenes with known geometry. The CPU image is 640x480 (4:3); the camera texture and
 * depth image are 16:9 and cover CPU rows 60..420, as on many ARCore devices, so the
 * depth→CPU-image region mapping is exercised.
 */
class SurfaceGeometryTest {

  private val cpu = Intrinsics(500f, 500f, 320f, 240f, 640, 480)
  private val region = floatArrayOf(0f, 60f, 640f, 420f)
  // Texture intrinsics consistent with the CPU intrinsics and the region above.
  private val texture = Intrinsics(1500f, 1500f, 960f, 540f, 1920, 1080)
  private val depthW = 160
  private val depthH = 90

  /** Camera 1.5 m above the floor, pitched down 30°. */
  private val floorCamera = poseMatrix(floatArrayOf(0f, 1.5f, 0f), pitchDeg = -30f)
  private val floor = PlaneEstimate(floatArrayOf(0f, 0f, 0f), floatArrayOf(0f, 1f, 0f))

  // ---------------------------------------------------------------- scene helpers

  private fun poseMatrix(t: FloatArray, pitchDeg: Float = 0f, yawDeg: Float = 0f): FloatArray {
    // R = Ry(yaw) * Rx(pitch), column-major.
    val p = Math.toRadians(pitchDeg.toDouble())
    val y = Math.toRadians(yawDeg.toDouble())
    val cp = cos(p).toFloat(); val sp = sin(p).toFloat()
    val cy = cos(y).toFloat(); val sy = sin(y).toFloat()
    val r = arrayOf(
      floatArrayOf(cy, sy * sp, sy * cp),
      floatArrayOf(0f, cp, -sp),
      floatArrayOf(-sy, cy * sp, cy * cp),
    )
    val m = FloatArray(16)
    for (row in 0..2) for (col in 0..2) m[col * 4 + row] = r[row][col]
    m[12] = t[0]; m[13] = t[1]; m[14] = t[2]; m[15] = 1f
    return m
  }

  private fun translation(t: FloatArray) = poseMatrix(t)

  private fun inverseRigid(m: FloatArray): FloatArray {
    val out = FloatArray(16)
    for (r in 0..2) for (c in 0..2) out[c * 4 + r] = m[r * 4 + c]
    for (r in 0..2) out[12 + r] = -(out[r] * m[12] + out[4 + r] * m[13] + out[8 + r] * m[14])
    out[15] = 1f
    return out
  }

  private fun rayDepth(cameraToWorld: FloatArray, dirCam: FloatArray, plane: PlaneEstimate): Float? {
    val o = SurfaceGeometry.transformPoint(cameraToWorld, floatArrayOf(0f, 0f, 0f))
    val rot = cameraToWorld.copyOf().also { it[12] = 0f; it[13] = 0f; it[14] = 0f }
    val d = SurfaceGeometry.transformPoint(rot, dirCam)
    val denom = SurfaceGeometry.dot(d, plane.normal)
    if (abs(denom) < 1e-6f) return null
    val t = SurfaceGeometry.dot(SurfaceGeometry.sub(plane.point, o), plane.normal) / denom
    return if (t > 0f) t else null // dirCam has z = -1, so t is the depth
  }

  private fun renderDepth(
    cameraToWorld: FloatArray,
    planeFor: (x: Int, y: Int) -> PlaneEstimate = { _, _ -> floor },
    noiseM: Float = 0f,
    random: Random = Random(1),
  ): DepthImage {
    val k = texture.scaledTo(depthW, depthH)
    val mm = ShortArray(depthW * depthH)
    for (y in 0 until depthH) for (x in 0 until depthW) {
      val dir = floatArrayOf((x + 0.5f - k.cx) / k.fx, (k.cy - (y + 0.5f)) / k.fy, -1f)
      val z = rayDepth(cameraToWorld, dir, planeFor(x, y)) ?: continue
      val noisy = z + if (noiseM > 0f) (random.nextFloat() * 2f - 1f) * noiseM else 0f
      val v = (noisy * 1000f).roundToInt()
      if (v in 1..65535) mm[y * depthW + x] = v.toShort()
    }
    return DepthImage(depthW, depthH, mm, texture, region)
  }

  private fun rectMask(u0: Int, v0: Int, u1: Int, v1: Int): Pair<Mask, List<FloatArray>> {
    val bits = BooleanArray(640 * 480)
    for (v in v0 until v1) for (u in u0 until u1) bits[v * 640 + u] = true
    val outline = listOf(
      floatArrayOf(u0.toFloat(), v0.toFloat()), floatArrayOf(u0.toFloat(), v1.toFloat()),
      floatArrayOf(u1.toFloat(), v1.toFloat()), floatArrayOf(u1.toFloat(), v0.toFloat()),
    )
    return Mask(640, 480, bits) to outline
  }

  private fun angleDeg(a: FloatArray, b: FloatArray): Double {
    val c = SurfaceGeometry.dot(SurfaceGeometry.normalize(a), SurfaceGeometry.normalize(b)).toDouble().coerceIn(-1.0, 1.0)
    return Math.toDegrees(acos(c))
  }

  private fun success(o: Outcome): SurfaceGeometry.Surface {
    assertTrue("expected success, got ${(o as? Outcome.Failed)?.reason}", o is Outcome.Success)
    return (o as Outcome.Success).surface
  }

  /** Analytic projection of a CPU pixel onto a world plane. */
  private fun projectPixel(cameraToWorld: FloatArray, u: Float, v: Float, plane: PlaneEstimate): FloatArray {
    val dir = floatArrayOf((u - cpu.cx) / cpu.fx, (cpu.cy - v) / cpu.fy, -1f)
    val t = rayDepth(cameraToWorld, dir, plane)!!
    val camPt = SurfaceGeometry.scale(dir, t)
    return SurfaceGeometry.transformPoint(cameraToWorld, camPt)
  }

  // ---------------------------------------------------------------- tests

  @Test
  fun floorIsRecoveredFromDepthAndExpressedRelativeToAnchor() {
    val (mask, outline) = rectMask(200, 300, 440, 420)
    val anchorPos = floatArrayOf(0.1f, 0f, -2.6f)
    val result = SurfaceGeometry.build(
      Input(mask, outline, cpu, renderDepth(floorCamera), floorCamera, inverseRigid(translation(anchorPos))),
    )
    val s = success(result)
    assertEquals("depth_plane_fit", s.stats.source)
    assertTrue("normal ${s.normalAnchor.toList()}", angleDeg(s.normalAnchor, floatArrayOf(0f, 1f, 0f)) < 1.0)
    for (i in 0 until s.vertices.size / 3) assertEquals(0f, s.vertices[i * 3 + 1], 0.005f)

    // Each outline corner lands where the analytic ray hits the floor (anchor-relative).
    val expected = projectPixel(floorCamera, 200f, 300f, floor)
    val nearest = (0 until s.vertices.size / 3).minOf { i ->
      SurfaceGeometry.length(SurfaceGeometry.sub(floatArrayOf(s.vertices[i * 3], s.vertices[i * 3 + 1], s.vertices[i * 3 + 2]), SurfaceGeometry.sub(expected, anchorPos)))
    }
    assertTrue("corner error $nearest m", nearest < 0.01f)
    assertEquals(4, s.vertices.size / 3)
    assertEquals(6, s.triangles.size)
    assertTrue(s.stats.inliers > 0.95f * s.stats.validDepthPixels)
  }

  @Test
  fun depthSamplesComeFromTheMaskedImageRows() {
    // A horizontal band of the CPU image near the top of the depth region.
    val (mask, _) = rectMask(0, 100, 640, 140)
    val samples = SurfaceGeometry.maskedDepthPoints(mask, renderDepth(floorCamera))
    assertTrue(samples.points.isNotEmpty())
    for (i in 0 until samples.points.size / 3) {
      val y = samples.points[i * 3 + 1]
      val z = -samples.points[i * 3 + 2]
      val vCpu = cpu.cy - cpu.fy * (y / z)
      assertTrue("sample maps to CPU row $vCpu", vCpu in 97f..143f)
    }
  }

  @Test
  fun noiseAndAnObjectOnTheFloorAreRejectedAsOutliers() {
    val box = PlaneEstimate(floatArrayOf(0f, 0.25f, 0f), floatArrayOf(0f, 1f, 0f))
    val depth = renderDepth(floorCamera, planeFor = { x, _ -> if (x in 70 until 85) box else floor }, noiseM = 0.006f)
    val (mask, outline) = rectMask(200, 300, 440, 420)
    val s = success(SurfaceGeometry.build(Input(mask, outline, cpu, depth, floorCamera, inverseRigid(translation(floatArrayOf(0f, 0f, -2.5f))))))
    assertTrue("normal error ${angleDeg(s.normalAnchor, floatArrayOf(0f, 1f, 0f))}", angleDeg(s.normalAnchor, floatArrayOf(0f, 1f, 0f)) < 2.0)
    assertTrue("rms ${s.stats.rmsM}", s.stats.rmsM < 0.01f)
    assertTrue(s.stats.inliers < s.stats.validDepthPixels)
  }

  @Test
  fun wallSeenAtAnAngleGetsANormalFacingTheCamera() {
    val wall = PlaneEstimate(floatArrayOf(0f, 0f, -2f), floatArrayOf(0f, 0f, 1f))
    val camera = poseMatrix(floatArrayOf(0.3f, 1.4f, 0f), yawDeg = 25f)
    val (mask, outline) = rectMask(180, 150, 460, 330)
    val s = success(SurfaceGeometry.build(Input(mask, outline, cpu, renderDepth(camera, { _, _ -> wall }), camera, inverseRigid(translation(floatArrayOf(0f, 1.4f, -2f))))))
    assertTrue(angleDeg(s.normalAnchor, floatArrayOf(0f, 0f, 1f)) < 1.0)
    for (i in 0 until s.vertices.size / 3) assertEquals(0f, s.vertices[i * 3 + 2], 0.005f)
  }

  @Test
  fun withoutDepthTheArcorePlaneUnderTheTapIsUsed() {
    val (mask, outline) = rectMask(200, 300, 440, 420)
    val s = success(SurfaceGeometry.build(Input(mask, outline, cpu, null, floorCamera, inverseRigid(translation(floatArrayOf(0f, 0f, -2.5f))), fallbackPlane = floor)))
    assertEquals("arcore_plane", s.stats.source)
    for (i in 0 until s.vertices.size / 3) assertEquals(0f, s.vertices[i * 3 + 1], 1e-4f)
  }

  @Test
  fun withoutDepthOrPlaneTheFailureIsExplicit() {
    val (mask, outline) = rectMask(200, 300, 440, 420)
    val o = SurfaceGeometry.build(Input(mask, outline, cpu, null, floorCamera, inverseRigid(translation(floatArrayOf(0f, 0f, -2.5f)))))
    assertEquals(Failure.NOT_ENOUGH_DEPTH, (o as Outcome.Failed).reason)
  }

  @Test
  fun twoDifferentSurfacesAreReportedAsNotFlat() {
    // Right half of the selection is a raised platform: no plane explains 60% of samples.
    val raised = PlaneEstimate(floatArrayOf(0f, 0.4f, 0f), floatArrayOf(0f, 1f, 0f))
    val depth = renderDepth(floorCamera, planeFor = { x, _ -> if (x >= 80) raised else floor })
    val (mask, outline) = rectMask(200, 300, 440, 420)
    val o = SurfaceGeometry.build(Input(mask, outline, cpu, depth, floorCamera, inverseRigid(translation(floatArrayOf(0f, 0f, -2.5f)))))
    assertTrue(o is Outcome.Failed)
    assertEquals(Failure.NOT_FLAT, (o as Outcome.Failed).reason)
  }

  @Test
  fun tinySelectionIsRejected() {
    val (mask, outline) = rectMask(318, 318, 322, 322)
    // Fallback plane so the size check, not depth coverage, is what fails.
    val o = SurfaceGeometry.build(Input(mask, outline, cpu, null, floorCamera, inverseRigid(translation(floatArrayOf(0f, 0f, -2.5f))), fallbackPlane = floor))
    assertEquals(Failure.TOO_SMALL, (o as Outcome.Failed).reason)
  }

  @Test
  fun earClippingHandlesConcaveAndRejectsSelfIntersecting() {
    val lShape = listOf(
      floatArrayOf(0f, 0f), floatArrayOf(2f, 0f), floatArrayOf(2f, 1f),
      floatArrayOf(1f, 1f), floatArrayOf(1f, 2f), floatArrayOf(0f, 2f),
    )
    val tris = SurfaceGeometry.earClip(lShape)
    assertNotNull(tris)
    assertEquals((lShape.size - 2) * 3, tris!!.size)
    var area = 0f
    for (t in 0 until tris.size / 3) {
      area += abs(SurfaceGeometry.signedArea(listOf(lShape[tris[t * 3]], lShape[tris[t * 3 + 1]], lShape[tris[t * 3 + 2]])))
    }
    assertEquals(3f, area, 1e-5f)

    val bowtie = listOf(floatArrayOf(0f, 0f), floatArrayOf(1f, 1f), floatArrayOf(1f, 0f), floatArrayOf(0f, 1f))
    assertNull(SurfaceGeometry.earClip(bowtie))
    assertEquals(4, SurfaceGeometry.convexHull(bowtie).size)
  }

  @Test
  fun jacobiEigenDecomposesSymmetricMatrices() {
    val a = arrayOf(doubleArrayOf(4.0, 1.0, 0.5), doubleArrayOf(1.0, 3.0, 0.2), doubleArrayOf(0.5, 0.2, 0.1))
    val (values, vectors) = SurfaceGeometry.jacobiEigen(a)
    for (k in 0..2) {
      val v = DoubleArray(3) { vectors[it][k] }
      for (r in 0..2) {
        val av = a[r][0] * v[0] + a[r][1] * v[1] + a[r][2] * v[2]
        assertEquals(values[k] * v[r], av, 1e-9)
      }
    }
  }
}
