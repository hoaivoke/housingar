package com.housingar.inspect.room

import android.view.MotionEvent
import com.google.ar.core.Anchor
import com.google.ar.core.Camera
import com.google.ar.core.Frame
import com.google.ar.core.Plane
import com.google.ar.core.Pose
import com.google.ar.core.TrackingState
import com.google.ar.core.examples.java.common.samplerender.Framebuffer
import com.google.ar.core.examples.java.common.samplerender.SampleRender
import com.housingar.inspect.capture.AnchorFrames
import com.housingar.inspect.library.Placement
import com.housingar.inspect.mesh.TexturedMesh
import com.housingar.inspect.mesh.TexturedMeshRenderer
import java.util.concurrent.ConcurrentLinkedQueue

/** One ARCore anchor owns the entire room: walls and all furniture remain rigid together. */
class RoomArScene(private val status: (String)->Unit) {
  private val operations=ConcurrentLinkedQueue<()->Unit>()
  private var preview: RoomPreview?=null
  private var anchor: Anchor?=null
  private var floor: TexturedMesh?=null
  private var walls: TexturedMesh?=null
  private var outlines: TexturedMesh?=null
  private val meshes=mutableMapOf<String,TexturedMesh>()
  private var yaw=0f
  private var waiting=true
  private var lastStatus=""
  private var failed=false
  val isActive get()=preview!=null

  fun open(next: RoomPreview)=operations.add { clear();preview=next;waiting=true;setStatus("Move the phone slowly, then tap the floor to place the doorway.") }
  fun close()=operations.add { clear();setStatus("") }
  fun rotate(degrees: Float)=operations.add { yaw=(yaw+degrees)%360 }
  fun move()=operations.add { waiting=true;setStatus("Tap the floor to move the room's doorway.") }

  /** Old GL handles belong to a discarded context; do not delete new-context handles. */
  fun onContextCreated() { floor=null;walls=null;outlines=null;meshes.clear();failed=false }
  fun consume() { while(true)(operations.poll() ?: break).invoke() }
  fun trackingPaused() { if(preview!=null)setStatus("Tracking paused. Move the phone slowly until it recovers.") }

  fun tap(frame: Frame,camera: Camera,event: MotionEvent) {
    if(preview==null || !waiting)return
    if(camera.trackingState!=TrackingState.TRACKING) { setStatus("Tracking paused. Move the phone slowly until it recovers.");return }
    val hit=frame.hitTest(event).firstOrNull { h ->
      val plane=h.trackable as? Plane
      plane!=null && plane.trackingState==TrackingState.TRACKING && plane.type==Plane.Type.HORIZONTAL_UPWARD_FACING && plane.isPoseInPolygon(h.hitPose)
    }
    if(hit==null) { setStatus("No floor found there yet. Move sideways slowly and tap the floor again.");return }
    val frameAxes=AnchorFrames.gravityAligned(hit.hitPose.translation,camera.pose.translation)
    val next=hit.trackable.createAnchor(Pose(hit.hitPose.translation,frameAxes.rotation))
    anchor?.detach();anchor=next;waiting=false
    setStatus("Room placed at full size. Move around it, or turn or move the doorway.")
  }

  fun draw(render: SampleRender,fb: Framebuffer,renderer: TexturedMeshRenderer,viewProjection: FloatArray,camera: Camera) {
    val p=preview ?: return
    if(failed)return
    if(camera.trackingState!=TrackingState.TRACKING) { setStatus("Tracking paused. Move the phone slowly until it recovers.");return }
    val a=anchor
    if(a==null) { setStatus("Tap the floor to place the room's doorway. Keep an open space in front of you.");return }
    if(a.trackingState!=TrackingState.TRACKING) { setStatus("The room anchor is paused. Move slowly to recover tracking, or tap Move.");return }
    if(floor==null) {
      try {
      floor=TexturedMesh(render,p.geometry.floor,null);walls=TexturedMesh(render,p.geometry.walls,null);outlines=TexturedMesh(render,p.geometry.outlines,null)
      for((id,asset) in p.assets) {
        val bitmap=asset.bitmap?.takeUnless{it.isRecycled} ?: RoomRepository.decodeTexture(asset.data)
        meshes[id]=TexturedMesh(render,asset.data,bitmap);bitmap?.recycle();asset.bitmap=null
      }
      } catch(e: Exception) {
        failed=true
        setStatus("This room couldn't be displayed on the phone. Close it and try a room with fewer furniture items.")
        return
      }
    }
    val world=FloatArray(16);a.pose.toMatrix(world,0)
    val userTurn=Placement.model(floatArrayOf(0f,0f,0f),yaw)
    val model=RoomMath.multiply(RoomMath.multiply(world,userTurn),p.plan.entryFrame())
    for(instance in p.instances) {
      val mesh=meshes[instance.itemId] ?: continue
      renderer.drawInScene(render,fb,mesh,RoomMath.multiply(model,instance.model),viewProjection,if(p.assets[instance.itemId]?.outline==true).7f else 1f)
    }
    // Ghost walls preserve the camera view and let users see the arrangement
    // through partitions; they do not write depth over virtual furniture.
    renderer.drawInScene(render,fb,floor!!,model,viewProjection,.13f,false)
    renderer.drawInScene(render,fb,walls!!,model,viewProjection,.22f,false)
    renderer.drawInScene(render,fb,outlines!!,model,viewProjection,.85f,false)
    if(!waiting)setStatus("Full-size room · walk only where your real surroundings are clear.")
  }

  private fun setStatus(value: String) { if(value!=lastStatus){lastStatus=value;status(value)} }
  private fun clear() {
    anchor?.detach();anchor=null
    floor?.close();walls?.close();outlines?.close();meshes.values.forEach{it.close()};meshes.clear()
    floor=null;walls=null;outlines=null;preview?.releaseBitmaps();preview=null;yaw=0f;waiting=true;failed=false
  }
}
