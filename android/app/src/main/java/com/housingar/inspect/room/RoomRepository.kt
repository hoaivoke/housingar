package com.housingar.inspect.room

import android.graphics.Bitmap
import android.graphics.BitmapFactory
import com.housingar.inspect.library.Placement
import com.housingar.inspect.mesh.GlbLoader
import com.housingar.inspect.mesh.MeshData
import org.json.JSONObject
import java.io.ByteArrayOutputStream
import java.io.File
import java.io.InterruptedIOException
import java.net.HttpURLConnection
import java.net.URI
import java.security.MessageDigest

interface RoomTransport {
  fun get(path: String, maximumBytes: Int): ByteArray
}

/** All reads are same-origin GETs. Opening an AR room never uploads camera images. */
class RoomHttp(private val origin: String): RoomTransport {
  override fun get(path: String, maximumBytes: Int): ByteArray {
    require(path.startsWith("/v1/") && !path.contains("..") && !path.contains('\\'))
    val connection=URI(origin+path).toURL().openConnection() as HttpURLConnection
    try {
      connection.instanceFollowRedirects=false
      connection.connectTimeout=5000;connection.readTimeout=15000
      require(connection.responseCode in 200..299) { "Mac returned HTTP ${connection.responseCode}" }
      val result=ByteArrayOutputStream()
      connection.inputStream.use { input ->
        val buffer=ByteArray(64*1024)
        while(true) {
          if(Thread.currentThread().isInterrupted)throw InterruptedIOException("Room loading cancelled")
          val n=input.read(buffer);if(n<0)break
          require(result.size()+n<=maximumBytes) { "Room download is too large" }
          result.write(buffer,0,n)
        }
      }
      return result.toByteArray()
    } finally { connection.disconnect() }
  }
}

data class RoomInstance(val itemId: String,val model: FloatArray)
class RoomAsset(val data: MeshData,var bitmap: Bitmap?,val outline: Boolean=false)
class RoomPreview(val plan: RoomPlan,val fullPlan: RoomPlan,val title: String,val geometry: RoomGeometry.Result,val instances: List<RoomInstance>,val assets: Map<String,RoomAsset>,val warnings: List<String>) {
  fun releaseBitmaps() { assets.values.forEach { a -> a.bitmap?.takeUnless{it.isRecycled}?.recycle();a.bitmap=null } }
}

class RoomRepository(private val cache: File,private val transport: RoomTransport) {
  fun load(id: String,roomId: String? = null,progress: (String)->Unit): RoomPreview {
    safeId(id)
    fun json(path: String)=JSONObject(String(transport.get(path,2_000_000),Charsets.UTF_8))
    progress("Loading the room…")
    val listing=json("/v1/listings/$id")
    val fullPlan=RoomPlan.from(json("/v1/listings/$id/layout"));require(fullPlan.listingId==id)
    val plan=roomId?.let { fullPlan.selected(it) } ?: fullPlan
    val warnings=mutableListOf<String>()
    val instances=mutableListOf<RoomInstance>();val assets=linkedMapOf<String,RoomAsset>()
    try {
      val furniture=json("/v1/listings/$id/furniture")
      require(furniture.getString("schema")=="housingar.room_furniture.v1" && furniture.getString("listing_id")==id)
      val placements=furniture.getJSONArray("placements");require(placements.length()<=200)
      val library=if(placements.length()>0)json("/v1/library").getJSONArray("items") else org.json.JSONArray()
      val items=(0 until library.length()).map{library.getJSONObject(it)}.associateBy{it.getString("item_id")}
      var realModels=0
      for(i in 0 until placements.length()) {
        if(Thread.currentThread().isInterrupted)throw InterruptedIOException("Room loading cancelled")
        val p=placements.getJSONObject(i);val itemId=safeId(p.getString("item_id"))
        try {
          val position=p.getJSONArray("position");require(position.length()==3)
          val point=FloatArray(3){finite(position.getDouble(it))}
          if(roomId!=null && !plan.rooms.single().contains(point[0],point[2]))continue
          if(!assets.containsKey(itemId)) {
            val item=items[itemId] ?: throw IllegalArgumentException("Item is no longer in My stuff")
            progress("Loading furniture ${i+1} of ${placements.length()}…")
            var asset: RoomAsset?=null
            if(realModels<MAX_TEXTURED_MODELS) {
              try {
                val path=item.getString("mesh_url")
                require(path=="/v1/library/$itemId/object.glb") { "Unsupported furniture address" }
                cache.mkdirs();val file=File(cache,"$itemId.glb");val signature=File(cache,"$itemId.json")
                val metadata=item.toString()
                val bytes=if(file.isFile && signature.isFile && signature.readText()==metadata)file.readBytes()
                  else transport.get(path,MAX_MODEL_BYTES).also { file.writeBytes(it);signature.writeText(metadata) }
                val mesh=GlbLoader.load(bytes);require(mesh.triangleCount<=50000 && mesh.vertexCount<=200000)
                asset=RoomAsset(mesh,decodeTexture(mesh));realModels++
              } catch(e: Exception) {
                if(e is InterruptedIOException)throw e
                warnings.add("${item.optString("name","Furniture")} is shown as a size outline")
              }
            } else warnings.add("${item.optString("name","Furniture")} is shown as a size outline")
            if(asset==null) {
              val d=item.getJSONArray("dimensions_m");require(d.length()==3)
              val dimensions=FloatArray(3){finite(d.getDouble(it))};require(dimensions.all{it>0 && it<=20})
              asset=RoomAsset(RoomGeometry.placeholder(dimensions),null,true)
            }
            assets[itemId]=asset
          }
          val yaw=p.getDouble("yaw_deg");require(yaw.isFinite())
          instances.add(RoomInstance(itemId,Placement.model(point,(yaw%360).toFloat())))
        } catch(e: Exception) {
          if(e is InterruptedIOException)throw e
          warnings.add("A saved furniture item could not be shown")
        }
      }
    } catch(e: Exception) {
      if(e is InterruptedIOException) { assets.values.forEach{it.bitmap?.recycle()};throw e }
      warnings.add("Saved furniture could not be loaded")
    }
    return RoomPreview(plan,fullPlan,listing.optString("name").takeIf{it.isNotBlank() && it!="null"} ?: listing.optString("title","Listing room"),RoomGeometry.build(plan),instances,assets,warnings.distinct())
  }

  companion object {
    const val MAX_TEXTURED_MODELS=16
    const val MAX_MODEL_BYTES=40_000_000
    fun cacheName(origin: String)=MessageDigest.getInstance("SHA-256").digest(origin.toByteArray()).take(12).joinToString(""){"%02x".format(it)}
    fun decodeTexture(mesh: MeshData): Bitmap? {
      val bytes=mesh.baseColorImage ?: return null
      val size=BitmapFactory.Options().apply{inJustDecodeBounds=true}
      BitmapFactory.decodeByteArray(bytes,0,bytes.size,size)
      require(size.outWidth in 1..4096 && size.outHeight in 1..4096) { "Unsupported furniture texture" }
      val options=BitmapFactory.Options().apply{inSampleSize=1};while(maxOf(size.outWidth,size.outHeight)/options.inSampleSize>512)options.inSampleSize*=2
      return BitmapFactory.decodeByteArray(bytes,0,bytes.size,options) ?: throw IllegalArgumentException("Furniture texture did not decode")
    }
  }
}
