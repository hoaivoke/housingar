package com.housingar.inspect.room

import com.housingar.inspect.library.Placement
import com.housingar.inspect.mesh.MeshData
import org.json.JSONObject
import java.net.URI
import java.net.URLDecoder
import java.util.Locale
import kotlin.math.abs
import kotlin.math.atan2

/** The listing's metre-scale plan. No display or miniature scale is applied. */
data class RoomRect(val id: String, val x0: Float, val z0: Float, val x1: Float, val z1: Float, val name: String = id) {
  val area get() = (x1 - x0) * (z1 - z0)
  fun contains(x: Float, z: Float) = x >= x0 && x <= x1 && z >= z0 && z <= z1
  fun wall(a: FloatArray, b: FloatArray): Boolean =
    if (abs(a[0] - b[0]) < EPS) (abs(a[0] - x0) < EPS || abs(a[0] - x1) < EPS) && minOf(a[1], b[1]) >= z0-EPS && maxOf(a[1], b[1]) <= z1+EPS
    else abs(a[1] - b[1]) < EPS && (abs(a[1] - z0) < EPS || abs(a[1] - z1) < EPS) && minOf(a[0], b[0]) >= x0-EPS && maxOf(a[0], b[0]) <= x1+EPS
}
data class RoomOpening(val kind: String, val rooms: List<String>, val a: FloatArray, val b: FloatArray, val sill: Float, val height: Float)
data class WallPiece(val a: FloatArray, val b: FloatArray, val low: Float, val high: Float)

class RoomPlan(val listingId: String, val estimated: Boolean, val height: Float, val rooms: List<RoomRect>, val openings: List<RoomOpening>) {
  val bounds = RoomRect("bounds", rooms.minOf { it.x0 }, rooms.minOf { it.z0 }, rooms.maxOf { it.x1 }, rooms.maxOf { it.z1 })
  val area get() = rooms.sumOf { it.area.toDouble() }.toFloat()

  fun selected(id: String): RoomPlan {
    val room = rooms.firstOrNull { it.id == id } ?: throw IllegalArgumentException("This room is no longer in the plan")
    val doors = openings.filter { id in it.rooms }.map { it.copy(rooms = listOf(id)) }
    return RoomPlan(listingId, estimated, height, listOf(room), doors).also { it.entryFrame() }
  }

  /** Move the entry door to the anchor; its outward normal faces the viewer (+Z). */
  fun entryFrame(): FloatArray {
    val door = openings.firstOrNull { it.kind == "door" && it.rooms.size == 1 }
      ?: throw IllegalArgumentException("The room needs an exterior entry door")
    val x=(door.a[0]+door.b[0])/2; val z=(door.a[1]+door.b[1])/2
    val normal = when {
      abs(x-bounds.x0)<EPS -> floatArrayOf(-1f,0f)
      abs(x-bounds.x1)<EPS -> floatArrayOf(1f,0f)
      abs(z-bounds.z0)<EPS -> floatArrayOf(0f,-1f)
      else -> floatArrayOf(0f,1f)
    }
    val yaw=Math.toDegrees(atan2(-normal[0],normal[1]).toDouble()).toFloat()
    val rotation=Placement.model(floatArrayOf(0f,0f,0f),yaw)
    rotation[12]=-(rotation[0]*x+rotation[8]*z)
    rotation[14]=-(rotation[2]*x+rotation[10]*z)
    return rotation
  }

  /** Split collinear shared walls once, then cut doors/windows at their exact heights. */
  fun wallPieces(): List<WallPiece> {
    data class Edge(val vertical: Boolean,val constant: Float,val lo: Float,val hi: Float,val owner: String)
    val edges=rooms.flatMap { r -> listOf(Edge(true,r.x0,r.z0,r.z1,r.id),Edge(true,r.x1,r.z0,r.z1,r.id),Edge(false,r.z0,r.x0,r.x1,r.id),Edge(false,r.z1,r.x0,r.x1,r.id)) }
    val groups=edges.groupBy { "${it.vertical}:${kotlin.math.round(it.constant/EPS).toLong()}" }
    val result=mutableListOf<WallPiece>()
    for (group in groups.values) {
      val edge=group.first()
      val holes=openings.filter { o ->
        if (edge.vertical) abs(o.a[0]-edge.constant)<EPS && abs(o.b[0]-edge.constant)<EPS
        else abs(o.a[1]-edge.constant)<EPS && abs(o.b[1]-edge.constant)<EPS
      }
      fun axis(p: FloatArray)=if(edge.vertical)p[1] else p[0]
      val cuts=(group.flatMap { listOf(it.lo,it.hi) }+holes.flatMap { listOf(axis(it.a),axis(it.b)) }).distinct().sorted()
      fun point(t: Float)=if(edge.vertical)floatArrayOf(edge.constant,t) else floatArrayOf(t,edge.constant)
      for ((lo,hi) in cuts.zipWithNext()) {
        if (hi-lo<EPS) continue
        val middle=(lo+hi)/2
        val owners=group.filter { middle>it.lo-EPS && middle<it.hi+EPS }.map { it.owner }.toSet()
        if (owners.isEmpty()) continue
        val openingsHere=holes.filter { middle>minOf(axis(it.a),axis(it.b))-EPS && middle<maxOf(axis(it.a),axis(it.b))+EPS && owners.containsAll(it.rooms) }
        val heights=(listOf(0f,height)+openingsHere.flatMap { listOf(it.sill,it.sill+it.height) }).distinct().sorted()
        for ((low,high) in heights.zipWithNext()) {
          if (high-low<EPS) continue
          val y=(low+high)/2
          if (openingsHere.none { y>it.sill-EPS && y<it.sill+it.height+EPS }) result.add(WallPiece(point(lo),point(hi),low,high))
        }
      }
    }
    return result
  }

  companion object {
    fun from(json: JSONObject): RoomPlan {
      require(json.getString("schema")=="housingar.room.v1" && json.getString("units")=="meters") { "Unsupported room format" }
      val id=safeId(json.getString("listing_id")); val height=finite(json.getDouble("ceiling_height_m"))
      require(height in .6f..20f) { "Invalid ceiling height" }
      val array=json.getJSONArray("rooms"); require(array.length() in 1..20) { "A room plan needs 1–20 rooms" }
      val rooms=(0 until array.length()).map { i ->
        val r=array.getJSONObject(i); val p=r.getJSONArray("polygon"); require(p.length()==4) { "Rooms must be rectangles" }
        val points=(0..3).map { k -> val q=p.getJSONArray(k); require(q.length()==2); floatArrayOf(finite(q.getDouble(0)),finite(q.getDouble(1))) }
        val rect=RoomRect(safeId(r.getString("room_id")),points.minOf { it[0] },points.minOf { it[1] },points.maxOf { it[0] },points.maxOf { it[1] },r.getString("name").take(40))
        require(rect.x1-rect.x0 in .5999f..20.0001f && rect.z1-rect.z0 in .5999f..20.0001f) { "Invalid room dimensions" }
        require(listOf(rect.x0 to rect.z0,rect.x0 to rect.z1,rect.x1 to rect.z0,rect.x1 to rect.z1).all { c -> points.any { abs(it[0]-c.first)<EPS && abs(it[1]-c.second)<EPS } }) { "Room must be an axis-aligned rectangle" }
        rect
      }
      require(rooms.map { it.id }.toSet().size==rooms.size) { "Duplicate room IDs" }
      for (i in rooms.indices) for (j in i+1 until rooms.size) {
        val a=rooms[i];val b=rooms[j]
        require(minOf(a.x1,b.x1)-maxOf(a.x0,b.x0)<=EPS || minOf(a.z1,b.z1)-maxOf(a.z0,b.z0)<=EPS) { "Room polygons overlap" }
      }
      val outer=RoomRect("outer",rooms.minOf{it.x0},rooms.minOf{it.z0},rooms.maxOf{it.x1},rooms.maxOf{it.z1})
      require(abs(rooms.sumOf{it.area.toDouble()}-outer.area)<maxOf(.01,outer.area*.0001)) { "Room polygons contain gaps" }
      val byId=rooms.associateBy{it.id};val o=json.getJSONArray("openings");require(o.length()<=60)
      val ids=mutableSetOf<String>()
      val openings=(0 until o.length()).map { i ->
        val raw=o.getJSONObject(i);require(ids.add(safeId(raw.getString("opening_id"))))
        val names=raw.getJSONArray("room_ids");require(names.length() in 1..2)
        val refs=(0 until names.length()).map { safeId(names.getString(it)) };require(refs.toSet().size==refs.size && refs.all { byId.containsKey(it) })
        fun point(key: String): FloatArray { val q=raw.getJSONArray(key);require(q.length()==2);return floatArrayOf(finite(q.getDouble(0)),finite(q.getDouble(1))) }
        val opening=RoomOpening(raw.getString("kind"),refs,point("a"),point("b"),finite(raw.getDouble("sill_m")),finite(raw.getDouble("height_m")))
        require(opening.kind in listOf("door","window") && opening.sill>=0 && opening.height>0 && opening.sill+opening.height<=height+EPS)
        require(kotlin.math.hypot(opening.a[0]-opening.b[0],opening.a[1]-opening.b[1])>=.0999f)
        require(refs.all { byId.getValue(it).wall(opening.a,opening.b) }) { "Opening is not on a shared wall" }
        require(refs.size!=1 || outer.wall(opening.a,opening.b)) { "Opening must be exterior" }
        require(opening.kind!="window" || refs.size==1)
        require(opening.kind!="door" || opening.sill<EPS)
        opening
      }
      val plan=RoomPlan(id,json.getBoolean("estimated"),height,rooms,openings)
      plan.entryFrame() // An explicit entry is needed to orient the room.
      return plan
    }
  }
}

/** A link never changes the Mac used for scanning. The UI confirms unfamiliar origins. */
data class RoomLink(val listingId: String,val service: String,val roomId: String? = null) {
  companion object {
    fun parse(value: String): RoomLink {
      val uri=URI(value);require(uri.scheme=="housingar" && uri.host=="room") { "Invalid room link" }
      val id=safeId(uri.path.removePrefix("/"))
      require(uri.userInfo == null && uri.port == -1 && uri.rawFragment == null)
      val pairs=uri.rawQuery?.split('&')?.map { part ->
        val pair=part.split('=',limit=2);require(pair.size==2);URLDecoder.decode(pair[0],"UTF-8") to URLDecoder.decode(pair[1],"UTF-8")
      } ?: emptyList()
      val query=pairs.toMap()
      require(query.size == pairs.size && "service" in query && query.keys.all { it in setOf("service","room_id") }) { "Room link must include the Mac address" }
      return RoomLink(id,origin(query.getValue("service")),query["room_id"]?.let(::safeId))
    }
    fun origin(value: String): String {
      val uri=URI(value); val scheme=uri.scheme?.lowercase(Locale.ROOT)
      require(scheme in listOf("http","https") && uri.host!=null && uri.userInfo==null && uri.rawQuery==null && uri.rawFragment==null && (uri.path.isNullOrEmpty() || uri.path=="/")) { "Invalid Mac address" }
      require(uri.port==-1 || uri.port in 1..65535)
      val port=if(uri.port==(if(scheme=="https")443 else 80))-1 else uri.port
      return URI(scheme,null,uri.host.lowercase(Locale.ROOT),port,null,null,null).toASCIIString()
    }
  }
}

internal const val EPS=.0001f
internal fun safeId(value: String): String { require(value.matches(Regex("[A-Za-z0-9_-]{1,64}"))) { "Invalid identifier" };return value }
internal fun finite(value: Double): Float { val result=value.toFloat();require(result.isFinite() && abs(result)<=10000) { "Invalid room coordinate" };return result }

/** Column-major matrices, shared by the CPU tests and AR renderer. */
object RoomMath {
  fun multiply(a: FloatArray,b: FloatArray): FloatArray = FloatArray(16) { i -> val row=i%4;val col=i/4;(0..3).sumOf { k -> (a[k*4+row]*b[col*4+k]).toDouble() }.toFloat() }
  fun point(m: FloatArray,p: FloatArray)=floatArrayOf(m[0]*p[0]+m[4]*p[1]+m[8]*p[2]+m[12],m[1]*p[0]+m[5]*p[1]+m[9]*p[2]+m[13],m[2]*p[0]+m[6]*p[1]+m[10]*p[2]+m[14])
}

/** Zero-thickness wall planes preserve plan dimensions; outlines show their boundaries. */
object RoomGeometry {
  class Result(val floor: MeshData,val walls: MeshData,val outlines: MeshData)
  private class Builder(private val color: FloatArray) {
    val vertices=mutableListOf<Float>();val indices=mutableListOf<Int>()
    fun quad(a: FloatArray,b: FloatArray,c: FloatArray,d: FloatArray) {
      val base=vertices.size/3;for(p in listOf(a,b,c,d))vertices.addAll(p.toList())
      indices.addAll(listOf(base,base+1,base+2,base,base+2,base+3))
    }
    fun box(a: FloatArray,b: FloatArray,radius: Float=.008f) {
      val lo=FloatArray(3){minOf(a[it],b[it])-radius};val hi=FloatArray(3){maxOf(a[it],b[it])+radius}
      val v=(0..7).map { k -> floatArrayOf(if(k and 1==0)lo[0] else hi[0],if(k and 2==0)lo[1] else hi[1],if(k and 4==0)lo[2] else hi[2]) }
      for(f in listOf(intArrayOf(0,1,3,2),intArrayOf(4,6,7,5),intArrayOf(0,4,5,1),intArrayOf(2,3,7,6),intArrayOf(0,2,6,4),intArrayOf(1,5,7,3)))quad(v[f[0]],v[f[1]],v[f[2]],v[f[3]])
    }
    fun mesh()=MeshData(vertices.toFloatArray(),null,null,indices.toIntArray(),null,null,color)
  }
  fun build(plan: RoomPlan): Result {
    val floor=Builder(floatArrayOf(.2f,.8f,.6f,1f));val wall=Builder(floatArrayOf(.35f,.65f,1f,1f));val lines=Builder(floatArrayOf(.2f,1f,.7f,1f))
    for(r in plan.rooms)floor.quad(floatArrayOf(r.x0,0f,r.z0),floatArrayOf(r.x0,0f,r.z1),floatArrayOf(r.x1,0f,r.z1),floatArrayOf(r.x1,0f,r.z0))
    for(w in plan.wallPieces()) {
      val a=floatArrayOf(w.a[0],w.low,w.a[1]);val b=floatArrayOf(w.b[0],w.low,w.b[1]);val c=floatArrayOf(w.b[0],w.high,w.b[1]);val d=floatArrayOf(w.a[0],w.high,w.a[1])
      wall.quad(a,b,c,d);for(pair in listOf(a to b,b to c,c to d,d to a))lines.box(pair.first,pair.second)
    }
    return Result(floor.mesh(),wall.mesh(),lines.mesh())
  }
  fun placeholder(dimensions: FloatArray): MeshData {
    val (w,h,d)=dimensions
    val builder=Builder(floatArrayOf(1f,.65f,.15f,1f))
    val v=(0..7).map { k -> floatArrayOf(if(k and 1==0)-w/2 else w/2,if(k and 2==0)0f else h,if(k and 4==0)-d/2 else d/2) }
    for(i in 0..7)for(bit in listOf(1,2,4))if(i and bit==0)builder.box(v[i],v[i or bit],.012f)
    return builder.mesh()
  }
}
