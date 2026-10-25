package com.housingar.inspect.mesh

import org.junit.Assert.assertEquals
import org.junit.Assert.assertNotNull
import org.junit.Assert.assertTrue
import org.junit.Assume.assumeTrue
import org.junit.Test
import java.io.File

/**
 * Loads every .glb under -PglbDir=<dir> with the phone's own loader and checks what placing needs:
 * one textured mesh, at most 50k triangles, origin at the bottom centre, metres.
 */
class PhoneGlbLoadTest {
  @Test
  fun macMeshesLoadOnThePhone() {
    val dir = System.getProperty("glb.dir").orEmpty()
    assumeTrue("set -PglbDir to run", dir.isNotBlank())
    val files = File(dir).walkTopDown().filter { it.isFile && it.extension == "glb" }.sortedBy { it.path }.toList()
    assertTrue("no .glb files in $dir", files.isNotEmpty())
    for (f in files) {
      val m = GlbLoader.load(f.readBytes())
      val (mn, mx) = m.bounds()
      println(String.format(java.util.Locale.US, "%s: %d vertices, %d triangles, texture %s (%d bytes), %.1f × %.1f × %.1f cm, min y %.3f, centre x %.3f z %.3f",
        f.name, m.vertexCount, m.triangleCount, m.baseColorMime, m.baseColorImage?.size ?: 0,
        (mx[0] - mn[0]) * 100, (mx[1] - mn[1]) * 100, (mx[2] - mn[2]) * 100, mn[1], (mn[0] + mx[0]) / 2, (mn[2] + mx[2]) / 2))
      assertTrue("${f.name}: too many triangles", m.triangleCount in 1..50_000)
      assertNotNull("${f.name}: no texture", m.baseColorImage)
      assertNotNull("${f.name}: no UVs", m.uvs)
      assertEquals("${f.name}: bottom at y=0", 0f, mn[1], 0.01f)
      assertEquals("${f.name}: centred in x", 0f, (mn[0] + mx[0]) / 2, 0.02f)
      assertEquals("${f.name}: centred in z", 0f, (mn[2] + mx[2]) / 2, 0.02f)
    }
  }
}
