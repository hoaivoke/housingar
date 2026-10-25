package com.housingar.inspect.mesh

import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Assume.assumeTrue
import org.junit.Test
import java.io.File

/** Loads a GLB written by the Mac's writer (glb.py). Skipped unless -PglbIn=/path/to/file.glb. */
class CodexGlbCompatTest {
  @Test
  fun loadsMacGlb() {
    val path = System.getProperty("glb.in").orEmpty()
    assumeTrue("set -PglbIn to run", path.isNotBlank())
    val m = GlbLoader.load(File(path).readBytes())
    assertEquals(24, m.vertexCount)
    assertEquals(12, m.triangleCount)
    val (mn, mx) = m.bounds()
    assertEquals(0.30f, mx[0] - mn[0], 1e-5f)
    assertEquals(0.40f, mx[1] - mn[1], 1e-5f)
    assertEquals(0.50f, mx[2] - mn[2], 1e-5f)
    assertEquals(0f, mn[1], 1e-6f) // origin at the bottom centre
    assertEquals("image/png", m.baseColorMime)
    val png = m.baseColorImage!!
    assertTrue(png[0] == 0x89.toByte() && png[1] == 'P'.code.toByte() && png[2] == 'N'.code.toByte())
    // IHDR: the writer resamples every texture to 2048 × 2048.
    val w = ((png[16].toInt() and 0xFF) shl 24) or ((png[17].toInt() and 0xFF) shl 16) or ((png[18].toInt() and 0xFF) shl 8) or (png[19].toInt() and 0xFF)
    assertEquals(2048, w)
    assertTrue(m.normals != null && m.uvs != null)
  }
}
