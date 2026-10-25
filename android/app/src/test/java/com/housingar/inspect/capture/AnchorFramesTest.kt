package com.housingar.inspect.capture

import org.junit.Assert.assertEquals
import org.junit.Test
import kotlin.math.sqrt

class AnchorFramesTest {
  private fun col(m: FloatArray, c: Int) = floatArrayOf(m[c * 4], m[c * 4 + 1], m[c * 4 + 2])

  @Test
  fun anchorIsLevelAndFacesTheViewer() {
    val p = floatArrayOf(0.3f, 0.7f, -1.0f)
    val viewer = floatArrayOf(1.3f, 1.5f, 0.2f)
    val f = AnchorFrames.gravityAligned(p, viewer)
    val y = col(f.matrix, 1)
    val z = col(f.matrix, 2)
    assertEquals(1f, y[1], 1e-6f) // +Y is world up
    assertEquals(0f, z[1], 1e-6f) // +Z is horizontal
    val dx = viewer[0] - p[0]
    val dz = viewer[2] - p[2]
    val l = sqrt(dx * dx + dz * dz)
    assertEquals(dx / l, z[0], 1e-5f)
    assertEquals(dz / l, z[2], 1e-5f)
    // Rigid, right-handed, and the quaternion matches the matrix.
    val x = col(f.matrix, 0)
    val cross = floatArrayOf(x[1] * y[2] - x[2] * y[1], x[2] * y[0] - x[0] * y[2], x[0] * y[1] - x[1] * y[0])
    for (i in 0..2) assertEquals(z[i], cross[i], 1e-5f)
    val q = f.rotation
    val qz = floatArrayOf(2 * (q[0] * q[2] + q[1] * q[3]), 2 * (q[1] * q[2] - q[0] * q[3]), 1 - 2 * (q[0] * q[0] + q[1] * q[1]))
    for (i in 0..2) assertEquals(z[i], qz[i], 1e-5f)
    assertEquals(0.3f, f.matrix[12], 0f)
  }
}
