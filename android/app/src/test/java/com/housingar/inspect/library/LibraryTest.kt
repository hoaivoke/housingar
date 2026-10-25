package com.housingar.inspect.library

import org.json.JSONObject
import org.junit.Assert.assertEquals
import org.junit.Assert.assertNotNull
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test
import java.nio.file.Files

class LibraryTest {
  @Test
  fun proposalsParseIncludingUnmeshableOnes() {
    val j = JSONObject("""{"scan_id":"s","proposals":[
      {"proposal_id":"p1","parent_id":null,"thumbnail_url":"/t1.jpg","mesh_url":"/m1.glb","dimensions_m":[1.2,0.75,0.6],
       "object_to_anchor":[1,0,0,0,0,1,0,0,0,0,1,0,0,0,0,1],"method":"depth_fusion",
       "quality":{"views":40,"coverage_deg":300,"observed_fraction":0.8,"low_quality":false,"warnings":[]}},
      {"proposal_id":"p2","parent_id":"p1","thumbnail_url":"/t2.jpg","mesh_url":null,"dimensions_m":null,
       "quality":{"views":3,"coverage_deg":40,"observed_fraction":0.2,"low_quality":true,"warnings":["too few views"]}}]}""")
    val ps = Proposal.listFrom(j)
    assertEquals(2, ps.size)
    assertTrue(ps[0].usable)
    assertNull(ps[0].parentId)
    assertEquals(0.75f, ps[0].dimensionsM!![1], 1e-6f)
    assertEquals("p1", ps[1].parentId)
    assertTrue(!ps[1].usable && ps[1].lowQuality)
    assertEquals(listOf("too few views"), ps[1].warnings)
  }

  @Test
  fun storeRoundTripsAndPlansSync() {
    val dir = Files.createTempDirectory("lib").toFile()
    val store = LibraryStore(dir)
    fun item(id: String, name: String) = LibraryItem(id, name, floatArrayOf(0.4f, 0.3f, 0.3f), "/v1/library/$id/object.glb", null, "depth_fusion", false, "2026-10-24")
    store.save(listOf(item("a", "Lamp"), item("b", "Chair")))
    assertEquals(listOf("Lamp", "Chair"), store.load().map { it.name })
    store.meshFile("a").parentFile.mkdirs(); store.meshFile("a").writeBytes(byteArrayOf(1))
    // Remote: a unchanged, b renamed, c new; local b must be refreshed, nothing else deleted.
    val plan = store.plan(listOf(item("a", "Lamp"), item("b", "Desk chair"), item("c", "Box")))
    assertEquals(listOf("b", "c"), plan.download.map { it.itemId })
    assertEquals(listOf("a"), plan.keep.map { it.itemId })
    assertTrue(plan.delete.isEmpty())
    // Remote without a: delete it locally.
    assertEquals(listOf("a"), store.plan(listOf(item("b", "Chair"))).delete)
    dir.deleteRecursively()
  }

  @Test
  fun placementModelKeepsObjectsUprightAndTapsPickTheNearestBox() {
    val m = Placement.model(floatArrayOf(1f, 0f, -2f), 90f)
    // +X of the object maps to world -Z when turned 90° about +Y.
    assertEquals(0f, m[0], 1e-6f); assertEquals(-1f, m[2], 1e-6f)
    assertEquals(1f, m[5], 0f)
    // Ray from (1, 0.2, 0) looking down -Z hits a 40×30×30 box at z = -2 (its local x span is along world z).
    val hit = Placement.hitBox(floatArrayOf(1f, 0.2f, 0f), floatArrayOf(0f, 0f, -1f), m, floatArrayOf(-0.2f, 0f, -0.15f), floatArrayOf(0.2f, 0.3f, 0.15f))
    assertNotNull(hit)
    assertEquals(1.8f, hit!!, 1e-4f)
    // Missing above the box.
    assertNull(Placement.hitBox(floatArrayOf(1f, 0.5f, 0f), floatArrayOf(0f, 0f, -1f), m, floatArrayOf(-0.2f, 0f, -0.15f), floatArrayOf(0.2f, 0.3f, 0.15f)))
  }

  @Test
  fun rayFromViewGoesThroughTheTappedPixel() {
    // Orthographic-like identity: inverse VP = identity, so NDC maps straight to world.
    val id = FloatArray(16).also { it[0] = 1f; it[5] = 1f; it[10] = 1f; it[15] = 1f }
    val (o, d) = Placement.rayFromView(id, 750f, 250f, 1000, 1000)
    assertEquals(0.5f, o[0], 1e-6f)
    assertEquals(0.5f, o[1], 1e-6f)
    assertEquals(1f, d[2], 1e-6f)
  }

  @Test
  fun layoutJsonListsPlacements() {
    val j = Placement.layoutJson("L1", "s1", "Apartment 4B", listOf(Triple("a", "Lamp", Placement.model(floatArrayOf(0f, 0f, 0f), 0f))))
    assertEquals("Apartment 4B", j.getString("name"))
    val placement = j.getJSONArray("placements").getJSONObject(0)
    assertEquals(16, placement.getJSONArray("pose_world").length())
    // The Mac rejects unknown fields: exactly item_id + pose_world per placement.
    assertEquals(setOf("item_id", "pose_world"), placement.keys().asSequence().toSet())
    val local = Placement.layoutJson("L1", "s1", "Apartment 4B", listOf(Triple("a", "Lamp", Placement.model(floatArrayOf(0f, 0f, 0f), 0f))), withNames = true)
    assertEquals("Lamp", local.getJSONArray("placements").getJSONObject(0).getString("name"))
  }
}
