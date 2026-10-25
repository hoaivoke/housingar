package com.housingar.inspect

import org.junit.Assert.assertEquals
import org.junit.Assert.assertNotNull
import org.junit.Assert.assertNull
import org.junit.Test

class DriftMathTest {
  @Test
  fun displacementOnFloorSplitsIntoInPlaneAndNormal() {
    // Floor marker at origin, normal +Y. Mark re-observed 3 cm sideways, 4 cm along, 1 cm up.
    val d = DriftMath.displacement(floatArrayOf(0f, 0f, 0f), floatArrayOf(0f, 2f, 0f), floatArrayOf(0.03f, 0.01f, 0.04f))
    assertEquals(0.05f, d.inPlaneM, 1e-5f)
    assertEquals(0.01f, d.normalM, 1e-5f)
    assertEquals(Math.sqrt(0.0026).toFloat(), d.totalM, 1e-5f)
  }

  @Test
  fun displacementOnWallUsesWallNormal() {
    // Wall facing +Z at z = -2. Mark observed 2 cm into the wall and 1 cm up.
    val d = DriftMath.displacement(floatArrayOf(0f, 1f, -2f), floatArrayOf(0f, 0f, 1f), floatArrayOf(0f, 1.01f, -2.02f))
    assertEquals(0.01f, d.inPlaneM, 1e-5f)
    assertEquals(-0.02f, d.normalM, 1e-5f)
  }

  @Test
  fun projectionUsesTopLeftPixelOrigin() {
    // Identity view-projection: NDC == world xy.
    val identity = FloatArray(16).also { it[0] = 1f; it[5] = 1f; it[10] = 1f; it[15] = 1f }
    val center = DriftMath.projectToView(identity, floatArrayOf(0f, 0f, 0f), 1000, 2000)
    assertNotNull(center)
    assertEquals(500f, center!![0], 1e-3f)
    assertEquals(1000f, center[1], 1e-3f)
    val topRight = DriftMath.projectToView(identity, floatArrayOf(1f, 1f, 0f), 1000, 2000)!!
    assertEquals(1000f, topRight[0], 1e-3f)
    assertEquals(0f, topRight[1], 1e-3f)
  }

  @Test
  fun pointBehindPerspectiveCameraIsRejected() {
    // Perspective-like matrix: w = -z (OpenGL camera looks down -Z).
    val p = FloatArray(16).also { it[0] = 1f; it[5] = 1f; it[10] = -1f; it[11] = -1f; it[14] = -0.2f }
    assertNull(DriftMath.projectToView(p, floatArrayOf(0f, 0f, 1f), 100, 100))
    assertNotNull(DriftMath.projectToView(p, floatArrayOf(0f, 0f, -1f), 100, 100))
  }
}
