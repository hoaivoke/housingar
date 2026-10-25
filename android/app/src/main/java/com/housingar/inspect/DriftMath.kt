package com.housingar.inspect

import kotlin.math.sqrt

/**
 * Pure geometry used by the pan-and-return drift test. Kept free of Android/ARCore types so it
 * can be unit tested on the JVM.
 */
object DriftMath {

  /** Result of comparing a marker's current position with where the physical mark was re-observed. */
  data class Displacement(
    /** Full 3D distance between marker centre and re-observed mark, in metres. */
    val totalM: Float,
    /** Component lying in the marker's surface plane: the apparent slide along the surface. */
    val inPlaneM: Float,
    /** Signed component along the surface normal (positive = mark is in front of the marker). */
    val normalM: Float,
  )

  /**
   * @param markerCenter current world position of the marker anchor
   * @param markerNormal world-space surface normal of the marker (any length, non-zero)
   * @param observed world position where the user re-tapped the physical reference mark
   */
  fun displacement(markerCenter: FloatArray, markerNormal: FloatArray, observed: FloatArray): Displacement {
    val d = sub(observed, markerCenter)
    val n = normalize(markerNormal)
    val along = dot(d, n)
    val inPlane = floatArrayOf(d[0] - along * n[0], d[1] - along * n[1], d[2] - along * n[2])
    return Displacement(totalM = length(d), inPlaneM = length(inPlane), normalM = along)
  }

  /**
   * Projects a world point to view pixel coordinates (origin top-left), or null when the point is
   * behind the camera.
   */
  fun projectToView(viewProjection: FloatArray, world: FloatArray, width: Int, height: Int): FloatArray? {
    val x = world[0]
    val y = world[1]
    val z = world[2]
    // Column-major 4x4 (OpenGL / android.opengl.Matrix convention).
    val cx = viewProjection[0] * x + viewProjection[4] * y + viewProjection[8] * z + viewProjection[12]
    val cy = viewProjection[1] * x + viewProjection[5] * y + viewProjection[9] * z + viewProjection[13]
    val cw = viewProjection[3] * x + viewProjection[7] * y + viewProjection[11] * z + viewProjection[15]
    if (cw <= 1e-6f) return null
    val ndcX = cx / cw
    val ndcY = cy / cw
    return floatArrayOf((ndcX + 1f) * 0.5f * width, (1f - ndcY) * 0.5f * height)
  }

  fun distance(a: FloatArray, b: FloatArray): Float = length(sub(a, b))

  private fun sub(a: FloatArray, b: FloatArray) = floatArrayOf(a[0] - b[0], a[1] - b[1], a[2] - b[2])

  private fun dot(a: FloatArray, b: FloatArray) = a[0] * b[0] + a[1] * b[1] + a[2] * b[2]

  private fun length(a: FloatArray) = sqrt(dot(a, a))

  private fun normalize(a: FloatArray): FloatArray {
    val l = length(a)
    require(l > 1e-9f) { "normal must be non-zero" }
    return floatArrayOf(a[0] / l, a[1] / l, a[2] / l)
  }
}
