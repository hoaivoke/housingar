package com.housingar.inspect.room

import com.housingar.inspect.capture.AnchorFrames
import com.housingar.inspect.library.Placement
import org.json.JSONArray
import org.json.JSONObject
import org.junit.Assert.*
import org.junit.Test
import kotlin.math.abs
import kotlin.math.hypot

internal fun testPlan(): JSONObject = JSONObject("""{
  "schema":"housingar.room.v1","listing_id":"demo","estimated":true,"source":"estimated",
  "units":"meters","ceiling_height_m":2.44,"total_area_m2":40,
  "rooms":[
    {"room_id":"living","name":"Living room","kind":"living","polygon":[[0,0],[4,0],[4,5],[0,5]]},
    {"room_id":"bed","name":"Bedroom","kind":"bedroom","polygon":[[4,0],[8,0],[8,5],[4,5]]}],
  "openings":[
    {"opening_id":"entry","kind":"door","room_ids":["living"],"a":[1,5],"b":[2,5],"height_m":2.1,"sill_m":0},
    {"opening_id":"inside","kind":"door","room_ids":["living","bed"],"a":[4,2],"b":[4,3],"height_m":2.1,"sill_m":0},
    {"opening_id":"w1","kind":"window","room_ids":["living"],"a":[1,0],"b":[2,0],"height_m":1,"sill_m":1},
    {"opening_id":"w2","kind":"window","room_ids":["bed"],"a":[5,0],"b":[6,0],"height_m":1,"sill_m":1}]}
""")

class RoomPlanTest {
  @Test fun metreGeometryHasExactFloorDimensionsAndCeiling() {
    val plan=RoomPlan.from(testPlan());val geometry=RoomGeometry.build(plan)
    assertEquals(40f,plan.area,1e-5f)
    val floor=geometry.floor.positions.toList().chunked(3)
    assertEquals(0f,floor.minOf{it[0]},1e-5f);assertEquals(8f,floor.maxOf{it[0]},1e-5f)
    assertEquals(0f,floor.minOf{it[2]},1e-5f);assertEquals(5f,floor.maxOf{it[2]},1e-5f)
    assertTrue(floor.all{it[1]==0f})
    assertEquals(2.44f,geometry.walls.positions.toList().chunked(3).maxOf{it[1]},1e-5f)
  }

  @Test fun sharedWallsAppearOnceAndDoorAndWindowOpeningsRemainClear() {
    val pieces=RoomPlan.from(testPlan()).wallPieces()
    val shared=pieces.filter{abs(it.a[0]-4)<EPS && abs(it.b[0]-4)<EPS}
    val area=shared.sumOf{(abs(it.a[1]-it.b[1])*(it.high-it.low)).toDouble()}
    assertEquals(5*2.44-2.1,area,1e-5)
    fun solid(x:Float,z:Float,y:Float)=pieces.any { p ->
      val on=if(abs(p.a[0]-p.b[0])<EPS) abs(x-p.a[0])<EPS && z>minOf(p.a[1],p.b[1]) && z<maxOf(p.a[1],p.b[1])
        else abs(z-p.a[1])<EPS && x>minOf(p.a[0],p.b[0]) && x<maxOf(p.a[0],p.b[0])
      on && y>p.low && y<p.high
    }
    assertFalse(solid(4f,2.5f,1f));assertTrue(solid(4f,2.5f,2.3f))
    assertFalse(solid(1.5f,0f,1.5f));assertTrue(solid(1.5f,0f,.5f))
    assertFalse(solid(1.5f,5f,1f))
  }

  @Test fun eachExteriorDoorOrientationFacesViewerAndInteriorExtendsAway() {
    val cases=listOf(
      Triple(floatArrayOf(1f,5f),floatArrayOf(2f,5f),floatArrayOf(1.5f,0f,4f)),
      Triple(floatArrayOf(1f,0f),floatArrayOf(2f,0f),floatArrayOf(1.5f,0f,1f)),
      Triple(floatArrayOf(0f,1f),floatArrayOf(0f,2f),floatArrayOf(1f,0f,1.5f)),
      Triple(floatArrayOf(8f,1f),floatArrayOf(8f,2f),floatArrayOf(7f,0f,1.5f)))
    for((a,b,inside) in cases) {
      val json=testPlan();val door=json.getJSONArray("openings").getJSONObject(0)
      door.put("a",JSONArray(a.toList())).put("b",JSONArray(b.toList()))
        .put("room_ids",JSONArray(listOf(if(a[0]==8f)"bed" else "living")))
      val frame=RoomPlan.from(json).entryFrame()
      assertArrayEquals(floatArrayOf(0f,0f,0f),RoomMath.point(frame,floatArrayOf((a[0]+b[0])/2,0f,(a[1]+b[1])/2)),1e-5f)
      assertArrayEquals(floatArrayOf(0f,0f,-1f),RoomMath.point(frame,inside),1e-5f)
    }
  }

  @Test fun rotatedAnchorAndRoomPreserveFurnitureMetricDistancesAndBottomCentre() {
    val anchor=AnchorFrames.gravityAligned(floatArrayOf(2f,1f,3f),floatArrayOf(-1f,2f,5f)).matrix
    val turn=Placement.model(floatArrayOf(0f,0f,0f),75f)
    val room=RoomMath.multiply(RoomMath.multiply(anchor,turn),RoomPlan.from(testPlan()).entryFrame())
    val furniture=RoomMath.multiply(room,Placement.model(floatArrayOf(2f,0f,3f),31f))
    val bottom=RoomMath.point(furniture,floatArrayOf(0f,0f,0f))
    assertEquals(1f,bottom[1],1e-5f)
    val edge=RoomMath.point(furniture,floatArrayOf(.4f,0f,.3f))
    assertEquals(.5f,hypot(edge[0]-bottom[0],edge[2]-bottom[2]),1e-5f)
    assertArrayEquals(RoomMath.point(room,floatArrayOf(2f,0f,3f)),bottom,1e-5f)
  }

  @Test fun selectedBedroomUsesItsInternalDoorAsEntryWithoutChangingCoordinates() {
    val plan=RoomPlan.from(testPlan()).selected("bed")
    assertEquals("Bedroom",plan.rooms.single().name)
    assertEquals(20f,plan.area,1e-5f)
    assertArrayEquals(floatArrayOf(0f,0f,0f),RoomMath.point(plan.entryFrame(),floatArrayOf(4f,0f,2.5f)),1e-5f)
    assertEquals(-1f,RoomMath.point(plan.entryFrame(),floatArrayOf(5f,0f,2.5f))[2],1e-5f)
    assertTrue(plan.openings.all{it.rooms==listOf("bed")})
    assertThrows(IllegalArgumentException::class.java){plan.selected("missing")}
  }

  @Test fun rejectsMalformedOrUnsafePlans() {
    fun reject(edit:(JSONObject)->Unit) { val json=testPlan();edit(json);assertThrows(Exception::class.java){RoomPlan.from(json)} }
    reject{it.put("units","feet")};reject{it.put("ceiling_height_m",0)}
    reject{it.getJSONArray("rooms").getJSONObject(1).put("room_id","living")}
    reject{it.getJSONArray("rooms").getJSONObject(1).put("polygon",JSONArray("[[3,0],[8,0],[8,5],[3,5]]"))}
    reject{it.getJSONArray("rooms").getJSONObject(1).put("polygon",JSONArray("[[5,0],[8,0],[8,5],[5,5]]"))}
    reject{it.getJSONArray("openings").getJSONObject(1).put("a",JSONArray("[2,2]"))}
    reject{it.getJSONArray("openings").getJSONObject(0).put("sill_m",1)}
    reject{it.getJSONArray("openings").remove(0)}
  }

  @Test fun consumesEverySavedMacFixtureWithoutScaleConversion() {
    val raw=javaClass.getResource("/room/mac-layouts.json")!!.readText()
    val fixtures=JSONArray(raw)
    assertTrue(fixtures.length()>=5)
    for(i in 0 until fixtures.length()) {
      val source=fixtures.getJSONObject(i);val plan=RoomPlan.from(source)
      assertEquals(source.getDouble("total_area_m2"),plan.area.toDouble(),.002)
      val walls=RoomGeometry.build(plan).walls
      assertTrue(walls.triangleCount>0)
      assertTrue(walls.positions.all{it.isFinite()})
      plan.rooms.forEach{plan.selected(it.id).entryFrame()}
    }
  }

  @Test fun safeLinksSupportUsbLanAndSingleRoomWithoutRedirectingScanServer() {
    val link=RoomLink.parse("housingar://room/demo?service=http%3A%2F%2F127.0.0.1%3A8000&room_id=bed")
    assertEquals(RoomLink("demo","http://127.0.0.1:8000","bed"),link)
    assertEquals("https://example.com",RoomLink.origin("https://EXAMPLE.com:443/"))
    assertEquals("http://[::1]:8000",RoomLink.origin("http://[::1]:8000"))
    for(raw in listOf("housingar://room/../x?service=http://localhost", "housingar://room/x?service=file:///x",
      "housingar://room/x?service=http://host/path", "housingar://room/x?service=http://host&service=http://other",
      "housingar://room/x?service=http://host&room_id=../x", "housingar://room/x?service=http://host#fragment",
      "housingar://room/x?service=http://host&extra=1")) assertThrows(Exception::class.java){RoomLink.parse(raw)}
  }
}
