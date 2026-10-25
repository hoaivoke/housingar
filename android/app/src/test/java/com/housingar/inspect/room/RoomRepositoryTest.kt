package com.housingar.inspect.room

import org.json.JSONArray
import org.json.JSONObject
import org.junit.Assert.*
import org.junit.Test
import java.io.File
import java.nio.ByteBuffer
import java.nio.ByteOrder
import java.nio.file.Files

class RoomRepositoryTest {
  private class Fake : RoomTransport {
    val bodies=mutableMapOf<String,ByteArray>()
    val reads=mutableListOf<String>()
    override fun get(path:String,maximumBytes:Int):ByteArray { reads.add(path);val body=bodies[path] ?: error("missing $path");require(body.size<=maximumBytes);return body }
    fun put(path:String,body:Any){ bodies[path]=body.toString().toByteArray() }
  }
  private fun service():Fake=Fake().apply {
    put("/v1/listings/demo",JSONObject().put("title","Two-room apartment"))
    put("/v1/listings/demo/layout",testPlan())
    put("/v1/listings/demo/furniture",JSONObject().put("schema","housingar.room_furniture.v1").put("listing_id","demo").put("placements",JSONArray()))
  }
  private fun placements(fake:Fake,count:Int=1) {
    val p=JSONArray();val items=JSONArray()
    repeat(count){i->
      p.put(JSONObject().put("item_id","chair_$i").put("position",JSONArray(listOf(if(i==0)2 else 6,0,3))).put("yaw_deg",90))
      items.put(JSONObject().put("item_id","chair_$i").put("name","Chair $i").put("dimensions_m",JSONArray("[0.4,0.8,0.5]"))
        .put("mesh_url","/v1/library/chair_$i/object.glb"))
    }
    fake.put("/v1/listings/demo/furniture",JSONObject().put("schema","housingar.room_furniture.v1").put("listing_id","demo").put("placements",p))
    fake.put("/v1/library",JSONObject().put("items",items))
  }
  private fun <T> cache(run:(File)->T):T { val dir=Files.createTempDirectory("room-test").toFile();try{return run(dir)}finally{dir.deleteRecursively()} }

  @Test fun emptyRoomDoesNotDownloadLibrary()=cache<Unit> { dir ->
    val fake=service();val preview=RoomRepository(dir,fake).load("demo"){}
    assertEquals("Two-room apartment",preview.title);assertTrue(preview.warnings.isEmpty())
    assertTrue(preview.instances.isEmpty());assertFalse(fake.reads.contains("/v1/library"))
  }
  @Test fun failedFurnitureIsVisibleAsMetricOutlineAndUnknownItemsAreReported()=cache<Unit> { dir ->
    val fake=service();placements(fake)
    val preview=RoomRepository(dir,fake).load("demo"){}
    assertEquals(1,preview.instances.size);assertTrue(preview.assets.getValue("chair_0").outline)
    assertTrue(preview.warnings.any{it.contains("size outline")})
    val bottom=RoomMath.point(preview.instances.single().model,floatArrayOf(0f,0f,0f))
    assertArrayEquals(floatArrayOf(2f,0f,3f),bottom,1e-5f)
    fake.put("/v1/library",JSONObject().put("items",JSONArray()))
    val missing=RoomRepository(dir,fake).load("demo"){}
    assertTrue(missing.instances.isEmpty());assertTrue(missing.warnings.isNotEmpty())
  }
  @Test fun bedroomOnlyDownloadsItsFurniture()=cache<Unit> { dir ->
    val fake=service();placements(fake,2)
    val preview=RoomRepository(dir,fake).load("demo","bed"){}
    assertEquals(1,preview.plan.rooms.size);assertEquals(2,preview.fullPlan.rooms.size)
    assertEquals("chair_1",preview.instances.single().itemId)
    assertFalse(fake.reads.contains("/v1/library/chair_0/object.glb"))
  }
  @Test fun downloadedGlbIsCachedAndFurnitureKeepsItsYaw()=cache<Unit> { dir ->
    val fake=service();placements(fake)
    fake.bodies["/v1/library/chair_0/object.glb"]=triangle()
    val repo=RoomRepository(dir,fake);val preview=repo.load("demo"){}
    assertFalse(preview.assets.getValue("chair_0").outline)
    assertEquals(1,preview.assets.getValue("chair_0").data.triangleCount)
    assertArrayEquals(floatArrayOf(2f,0f,2f),RoomMath.point(preview.instances.single().model,floatArrayOf(1f,0f,0f)),1e-5f)
    repo.load("demo"){}
    assertEquals(1,fake.reads.count{it=="/v1/library/chair_0/object.glb"})
  }
  @Test fun capsDetailedFurnitureInsteadOfAllocatingUnlimitedMeshes()=cache<Unit> { dir ->
    val fake=service();placements(fake,18)
    repeat(18){fake.bodies["/v1/library/chair_$it/object.glb"]=triangle()}
    val preview=RoomRepository(dir,fake).load("demo"){}
    assertEquals(18,preview.instances.size)
    assertEquals(16,preview.assets.values.count{!it.outline})
    assertEquals(2,preview.assets.values.count{it.outline})
    assertEquals(16,fake.reads.count{it.endsWith(".glb")})
  }
  @Test fun furnitureFailureDoesNotHideTheRoom()=cache<Unit> { dir ->
    val fake=service();fake.bodies.remove("/v1/listings/demo/furniture")
    val preview=RoomRepository(dir,fake).load("demo"){}
    assertEquals(40f,preview.plan.area,1e-5f);assertEquals(listOf("Saved furniture could not be loaded"),preview.warnings)
  }
  @Test fun wrongListingAndUnknownSelectedRoomFailClearly()=cache<Unit> { dir ->
    val fake=service()
    assertThrows(Exception::class.java){RoomRepository(dir,fake).load("demo","unknown"){} }
    fake.put("/v1/listings/demo/layout",testPlan().put("listing_id","other"))
    assertThrows(Exception::class.java){RoomRepository(dir,fake).load("demo"){} }
  }
  @Test fun glbDownloadsCannotSwitchOrigins()=cache<Unit> { dir ->
    val fake=service();placements(fake)
    val library=JSONObject(String(fake.bodies.getValue("/v1/library")))
    library.getJSONArray("items").getJSONObject(0).put("mesh_url","https://other.example/model.glb")
    fake.put("/v1/library",library)
    val preview=RoomRepository(dir,fake).load("demo"){}
    assertTrue(preview.assets.getValue("chair_0").outline)
    assertFalse(fake.reads.any{it.startsWith("http")})
  }

  /** A real untextured GLB triangle, allowing decoder/cache tests without Android bitmap stubs. */
  private fun triangle():ByteArray {
    val binary=ByteBuffer.allocate(48).order(ByteOrder.LITTLE_ENDIAN)
    floatArrayOf(0f,0f,0f,.4f,0f,0f,0f,.8f,0f).forEach{binary.putFloat(it)}
    intArrayOf(0,1,2).forEach{binary.putInt(it)}
    val raw="""{"asset":{"version":"2.0"},"buffers":[{"byteLength":48}],"bufferViews":[{"buffer":0,"byteOffset":0,"byteLength":36},{"buffer":0,"byteOffset":36,"byteLength":12}],"accessors":[{"bufferView":0,"componentType":5126,"count":3,"type":"VEC3"},{"bufferView":1,"componentType":5125,"count":3,"type":"SCALAR"}],"meshes":[{"primitives":[{"attributes":{"POSITION":0},"indices":1}]}],"nodes":[{"mesh":0}],"scenes":[{"nodes":[0]}],"scene":0}""".toByteArray()
    val json=raw.copyOf((raw.size+3)/4*4).also{for(i in raw.size until it.size)it[i]=32}
    return ByteBuffer.allocate(12+8+json.size+8+48).order(ByteOrder.LITTLE_ENDIAN)
      .putInt(0x46546c67).putInt(2).putInt(12+8+json.size+8+48)
      .putInt(json.size).putInt(0x4e4f534a).put(json).putInt(48).putInt(0x004e4942).put(binary.array()).array()
  }
}
