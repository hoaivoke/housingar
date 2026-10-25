package com.housingar.inspect

import android.graphics.Bitmap
import android.opengl.Matrix
import android.util.Log
import android.view.MotionEvent
import androidx.lifecycle.DefaultLifecycleObserver
import androidx.lifecycle.LifecycleOwner
import com.google.ar.core.Anchor
import com.google.ar.core.Camera
import com.google.ar.core.Coordinates2d
import com.google.ar.core.DepthPoint
import com.google.ar.core.Frame
import com.google.ar.core.HitResult
import com.google.ar.core.Plane
import com.google.ar.core.Point
import com.google.ar.core.Pose
import com.google.ar.core.Session
import com.google.ar.core.TrackingFailureReason
import com.google.ar.core.TrackingState
import com.google.ar.core.examples.java.common.helpers.DisplayRotationHelper
import com.google.ar.core.examples.java.common.helpers.TrackingStateHelper
import com.google.ar.core.examples.java.common.samplerender.Framebuffer
import com.google.ar.core.examples.java.common.samplerender.Mesh
import com.google.ar.core.examples.java.common.samplerender.SampleRender
import com.google.ar.core.examples.java.common.samplerender.Shader
import com.google.ar.core.examples.java.common.samplerender.VertexBuffer
import com.google.ar.core.examples.java.common.samplerender.arcore.BackgroundRenderer
import com.google.ar.core.examples.java.common.samplerender.arcore.PlaneRenderer
import com.google.ar.core.exceptions.CameraNotAvailableException
import com.google.ar.core.exceptions.NotYetAvailableException
import com.housingar.inspect.capture.AnchorFrames
import com.housingar.inspect.capture.ObjectCaptureSession
import com.housingar.inspect.geometry.SurfaceGeometry
import com.housingar.inspect.mesh.OrbitRingRenderer
import com.housingar.inspect.capture.ScanSession
import com.housingar.inspect.capture.TappedObject
import com.housingar.inspect.library.Placement
import com.housingar.inspect.mesh.MeshData
import com.housingar.inspect.mesh.TexturedMesh
import com.housingar.inspect.mesh.TexturedMeshRenderer
import org.json.JSONObject
import java.nio.ByteOrder
import java.util.concurrent.ConcurrentLinkedQueue
import kotlin.math.abs
import java.io.IOException
import java.util.Locale
import kotlin.math.hypot

/**
 * Milestone 0/1 renderer, extended from Google's hello_ar_kotlin sample.
 *
 * - Default tap: place an anchored surface marker on a detected plane or depth point.
 * - "Measure drift" tap: re-tap the physical reference mark; logs how far the marker has slid.
 * - "Test frame" tap: capture the CPU camera image + that frame's pose/intrinsics and send it to
 *   the Mac segmentation service.
 */
class InspectRenderer(private val activity: InspectActivity) : SampleRender.Renderer, DefaultLifecycleObserver {
  val roomScene = com.housingar.inspect.room.RoomArScene(activity::onListingRoomGuidance)

  /**
   * DEFAULT: tap a placed object to select it. MARKER: drift-test marker. PLACE_ITEM / MOVE_ITEM:
   * put the pending library object (or the selected one) on a tapped floor or table.
   */
  enum class TapMode { DEFAULT, MARKER, MEASURE, SEGMENT_TEST, SELECT, PLACE_ITEM, MOVE_ITEM }

  /** A library object to place, decoded off the GL thread. */
  class PlaceRequest(val itemId: String, val name: String, val mesh: MeshData, val bitmap: Bitmap?)

  /** A library object standing in the room: upright on [anchor], turned by [yawDeg]. GL thread only. */
  private class PlacedItem(
    val id: Int,
    val itemId: String,
    val name: String,
    var anchor: Anchor,
    var yawDeg: Float,
    val mesh: TexturedMesh,
    val boundsMin: FloatArray,
    val boundsMax: FloatArray,
  ) {
    val model = FloatArray(16)
    fun updateModel(): Boolean {
      if (anchor.trackingState != TrackingState.TRACKING) return false
      Placement.model(anchor.pose.translation, yawDeg).copyInto(model)
      return true
    }
  }

  private class AnchoredSurface(val id: Int, val anchor: Anchor, val meshes: SurfacePatchMeshes, val areaM2: Float)
  private class PendingSurface(val id: Int, val anchor: Anchor, val surface: SurfaceGeometry.Surface)

  private class SurfaceMarker(
    val id: Int,
    val anchor: Anchor,
    val surfaceKind: String,
    val createdFrameNs: Long,
    val resetsAtCreation: Int,
    val initialTranslation: FloatArray,
    val cameraDistanceAtPlacementM: Float,
  )

  private lateinit var backgroundRenderer: BackgroundRenderer
  private lateinit var planeRenderer: PlaneRenderer
  private lateinit var markerRenderer: SurfaceMarkerRenderer
  private lateinit var patchRenderer: SurfacePatchRenderer
  private lateinit var texturedRenderer: TexturedMeshRenderer
  private lateinit var selectionBoxShader: Shader
  private lateinit var selectionBox: Mesh
  private lateinit var virtualSceneFramebuffer: Framebuffer
  private lateinit var pointCloudVertexBuffer: VertexBuffer
  private lateinit var pointCloudMesh: Mesh
  private lateinit var pointCloudShader: Shader
  private var lastPointCloudTimestamp = 0L
  private var hasSetTextureNames = false
  private var rendererReady = false

  private val markers = mutableListOf<SurfaceMarker>()
  private var nextMarkerId = 1

  // Selected surfaces (GL thread) and hand-offs from other threads.
  private val surfaces = mutableListOf<AnchoredSurface>()
  private val pendingSurfaces = ConcurrentLinkedQueue<PendingSurface>()
  private val anchorsToRelease = ConcurrentLinkedQueue<Anchor>()
  private var nextSelectionId = 1

  // Single-object capture ("Create mesh").
  @Volatile private var requestedCapture: ObjectCaptureSession? = null
  @Volatile private var stopCaptureCallback: ((ObjectCaptureSession) -> Unit)? = null
  private var captureSession: ObjectCaptureSession? = null
  private var ringRenderer: OrbitRingRenderer? = null

  // "Scan stuff" walk-through.
  private class ScanStart(val factory: (Anchor, FloatArray) -> ScanSession, val onStarted: (ScanSession?) -> Unit)
  @Volatile private var requestedScan: ScanStart? = null
  @Volatile private var stopScanCallback: ((ScanSession) -> Unit)? = null
  private var scanSession: ScanSession? = null
  /** Things tapped during the scan: a coloured marker on each, and a ring showing which sides are filmed. */
  private class ScanMarker(val obj: TappedObject, val anchor: Anchor)
  private val scanMarkers = mutableListOf<ScanMarker>()
  private var scanRing: OrbitRingRenderer? = null

  // Library objects placed in the room.
  private val placed = mutableListOf<PlacedItem>()
  private var selected: PlacedItem? = null
  @Volatile private var pendingPlace: PlaceRequest? = null
  private val placementOps = ConcurrentLinkedQueue<() -> Unit>()
  private var nextPlacementId = 1
  private val boxModel = FloatArray(16)
  private val boxMvp = FloatArray(16)
  private val inverseViewProjection = FloatArray(16)

  private val viewMatrix = FloatArray(16)
  private val projectionMatrix = FloatArray(16)
  private val viewProjectionMatrix = FloatArray(16)
  private val modelMatrix = FloatArray(16)
  private var viewWidth = 1
  private var viewHeight = 1
  private var frameIndex = 0L
  private var lastHudUpdateNs = 0L

  @Volatile private var requestedMode: TapMode? = null
  @Volatile private var clearRequested = false
  private var tapMode = TapMode.DEFAULT

  private val displayRotationHelper = DisplayRotationHelper(activity)
  private val trackingStateHelper = TrackingStateHelper(activity)
  private val diagnostics get() = activity.diagnostics
  private val settings get() = activity.settings
  private val session: Session? get() = activity.arCoreSessionHelper.session

  fun armMode(mode: TapMode) {
    requestedMode = mode
  }

  fun requestClear() {
    clearRequested = true
  }

  /** Any thread: queue an accepted surface for GPU upload on the GL thread. */
  fun addSurface(id: Int, anchor: Anchor, surface: SurfaceGeometry.Surface) {
    pendingSurfaces.add(PendingSurface(id, anchor, surface))
  }

  /** Any thread: begin running [session] on the next frame. */
  fun startObjectCapture(session: ObjectCaptureSession) {
    requestedCapture = session
  }

  /** Any thread: stop the running capture; [onStopped] runs on the UI thread with the stopped session. */
  fun stopObjectCapture(onStopped: (ObjectCaptureSession) -> Unit) {
    stopCaptureCallback = onStopped
  }

  /**
   * Any thread: start a scan on the next tracked frame. The GL thread creates a level scan anchor
   * at the camera and builds the session with [factory]; [onStarted] runs on the UI thread (null
   * if tracking was not available).
   */
  fun startScan(factory: (Anchor, FloatArray) -> ScanSession, onStarted: (ScanSession?) -> Unit) {
    requestedScan = ScanStart(factory, onStarted)
  }

  /** Any thread: stop the scan; its anchor is released; [onStopped] runs on the UI thread. */
  fun stopScan(onStopped: (ScanSession) -> Unit) {
    stopScanCallback = onStopped
  }

  /** Any thread: undo the last thing tapped in the running scan; [onUndone] gets it (UI thread). */
  fun undoScanObject(onUndone: (ScanSession.UndoResult?) -> Unit) = placementOps.add {
    val r = scanSession?.undoTap()
    if (r != null) scanMarkers.lastOrNull { it.obj === r.obj }?.let { it.anchor.detach(); scanMarkers.remove(it) }
    activity.runOnUiThread { onUndone(r) }
  }

  /** Any thread: the next tap in the running scan starts another thing. */
  fun startNewScanThing() = placementOps.add { scanSession?.startNewThing() }

  /** Any thread: the next floor/table tap places this library object. */
  fun placeItem(request: PlaceRequest) {
    pendingPlace = request
    armMode(TapMode.PLACE_ITEM)
  }

  fun rotateSelected(deg: Float) = placementOps.add { selected?.let { it.yawDeg = (it.yawDeg + deg) % 360f } }

  fun moveSelected() = placementOps.add { if (selected != null) { tapMode = TapMode.MOVE_ITEM } }

  fun deleteSelected() = placementOps.add {
    selected?.let { item ->
      placed.remove(item)
      item.mesh.close()
      item.anchor.detach()
      select(null)
    }
  }

  fun deselect() = placementOps.add { select(null) }

  fun clearPlacements() = placementOps.add {
    placed.forEach { it.mesh.close(); it.anchor.detach() }
    placed.clear()
    select(null)
  }

  /** Any thread: current placements as (item_id, name, column-major pose in world); [onResult] on the UI thread. */
  fun snapshotPlacements(onResult: (List<Triple<String, String, FloatArray>>) -> Unit) = placementOps.add {
    val list = placed.filter { it.updateModel() }.map { Triple(it.itemId, it.name, it.model.copyOf()) }
    activity.runOnUiThread { onResult(list) }
  }

  /** Any thread: detach an anchor on the GL thread. */
  fun releaseAnchor(anchor: Anchor) {
    anchorsToRelease.add(anchor)
  }

  override fun onResume(owner: LifecycleOwner) {
    displayRotationHelper.onResume()
    hasSetTextureNames = false
  }

  override fun onPause(owner: LifecycleOwner) {
    displayRotationHelper.onPause()
  }

  override fun onSurfaceCreated(render: SampleRender) {
    hasSetTextureNames = false
    roomScene.onContextCreated()
    try {
      planeRenderer = PlaneRenderer(render)
      backgroundRenderer = BackgroundRenderer(render)
      virtualSceneFramebuffer = Framebuffer(render, 1, 1)
      markerRenderer = SurfaceMarkerRenderer(render, MARKER_SIZE_M)
      patchRenderer = SurfacePatchRenderer(render)
      texturedRenderer = TexturedMeshRenderer(render)
      selectionBoxShader = Shader.createFromAssets(render, "shaders/surface_patch.vert", "shaders/surface_patch.frag", null)
        .setBlend(Shader.BlendFactor.ONE, Shader.BlendFactor.ONE_MINUS_SRC_ALPHA)
        .setDepthTest(false).setDepthWrite(false).setCullFace(false)
      selectionBox = unitCubeEdges(render)

      pointCloudShader = Shader.createFromAssets(render, "shaders/point_cloud.vert", "shaders/point_cloud.frag", null)
        .setVec4("u_Color", floatArrayOf(31f / 255f, 188f / 255f, 210f / 255f, 1f))
        .setFloat("u_PointSize", 5f)
      pointCloudVertexBuffer = VertexBuffer(render, 4, null)
      pointCloudMesh = Mesh(render, Mesh.PrimitiveMode.POINTS, null, arrayOf(pointCloudVertexBuffer))
      rendererReady = true
    } catch (e: IOException) {
      Log.e(TAG, "Failed to read a required asset file", e)
      activity.showBanner("Failed to load renderer assets: $e", error = true)
    }
  }

  override fun onSurfaceChanged(render: SampleRender, width: Int, height: Int) {
    displayRotationHelper.onSurfaceChanged(width, height)
    virtualSceneFramebuffer.resize(width, height)
    viewWidth = width
    viewHeight = height
  }

  private lateinit var renderRef: SampleRender
  private var lastCamera: Camera? = null

  override fun onDrawFrame(render: SampleRender) {
    val session = session ?: return
    if (!rendererReady) return
    renderRef = render

    if (!hasSetTextureNames) {
      session.setCameraTextureNames(intArrayOf(backgroundRenderer.cameraColorTexture.textureId))
      hasSetTextureNames = true
    }
    displayRotationHelper.updateSessionIfNeeded(session)

    val frame = try {
      session.update()
    } catch (e: CameraNotAvailableException) {
      Log.e(TAG, "Camera not available during onDrawFrame", e)
      activity.showBanner("Camera not available. Try restarting the app.", error = true)
      return
    }
    frameIndex++
    val camera = frame.camera
    lastCamera = camera
    diagnostics.onFrame(frame.timestamp, camera.trackingState, camera.trackingFailureReason)

    consumeRequests()
    roomScene.consume()

    val depthOn = diagnostics.depthEnabled
    try {
      backgroundRenderer.setUseDepthVisualization(render, depthOn && settings.showDepth && !activity.isListingRoomActive)
      backgroundRenderer.setUseOcclusion(render, depthOn && settings.useOcclusion && !activity.isListingRoomActive)
    } catch (e: IOException) {
      Log.e(TAG, "Failed to read a required asset file", e)
      activity.showBanner("Failed to load shader: $e", error = true)
      return
    }
    backgroundRenderer.updateDisplayGeometry(frame)

    if (camera.trackingState == TrackingState.TRACKING && depthOn) {
      updateDepth(frame)
    }

    handleTap(frame, camera)
    trackingStateHelper.updateKeepScreenOnFlag(camera.trackingState)
    updateGuidance(session, camera)

    if (frame.timestamp != 0L) backgroundRenderer.drawBackground(render)

    // Tracking loss: pause spatial content rather than drawing it screen-fixed.
    if (camera.trackingState != TrackingState.TRACKING) {
      roomScene.trackingPaused()
      maybeUpdateHud()
      return
    }

    camera.getProjectionMatrix(projectionMatrix, 0, Z_NEAR, Z_FAR)
    camera.getViewMatrix(viewMatrix, 0)
    Matrix.multiplyMM(viewProjectionMatrix, 0, projectionMatrix, 0, viewMatrix, 0)

    captureSession?.onFrame(frame, camera, viewProjectionMatrix)
    scanSession?.onFrame(frame, camera)

    frame.acquirePointCloud().use { pointCloud ->
      diagnostics.featurePoints = pointCloud.points.remaining() / 4
      if (settings.showPointCloud && !activity.isListingRoomActive) {
        if (pointCloud.timestamp > lastPointCloudTimestamp) {
          pointCloudVertexBuffer.set(pointCloud.points)
          lastPointCloudTimestamp = pointCloud.timestamp
        }
        pointCloudShader.setMat4("u_ModelViewProjection", viewProjectionMatrix)
        render.draw(pointCloudMesh, pointCloudShader)
      }
    }

    val planes = session.getAllTrackables(Plane::class.java)
    diagnostics.planesHorizontal = planes.count { it.trackingState == TrackingState.TRACKING && it.type != Plane.Type.VERTICAL }
    diagnostics.planesVertical = planes.count { it.trackingState == TrackingState.TRACKING && it.type == Plane.Type.VERTICAL }
    if (settings.showPlanes) {
      planeRenderer.drawPlanes(render, planes, camera.displayOrientedPose, projectionMatrix)
    }

    render.clear(virtualSceneFramebuffer, 0f, 0f, 0f, 0f)
    // Selected surfaces: geometry is anchor-relative and redrawn from the current pose every frame.
    for (surface in if (activity.isListingRoomActive) emptyList() else surfaces) {
      if (surface.anchor.trackingState != TrackingState.TRACKING) continue
      surface.anchor.pose.toMatrix(modelMatrix, 0)
      val color = SURFACE_COLORS[(surface.id - 1) % SURFACE_COLORS.size]
      patchRenderer.draw(render, virtualSceneFramebuffer, surface.meshes, modelMatrix, viewProjectionMatrix, color)
    }
    for (marker in if (activity.isListingRoomActive) emptyList() else markers) {
      if (marker.anchor.trackingState != TrackingState.TRACKING) continue
      marker.anchor.pose.toMatrix(modelMatrix, 0)
      val color = MARKER_COLORS[(marker.id - 1) % MARKER_COLORS.size]
      markerRenderer.draw(render, virtualSceneFramebuffer, modelMatrix, viewProjectionMatrix, color)
    }
    // Things tapped during a scan, with their filmed sides.
    var ringed: TappedObject? = null
    for (m in if (activity.isListingRoomActive) emptyList() else scanMarkers) {
      if (m.anchor.trackingState != TrackingState.TRACKING) continue
      m.anchor.pose.toMatrix(modelMatrix, 0)
      markerRenderer.draw(render, virtualSceneFramebuffer, modelMatrix, viewProjectionMatrix, MARKER_COLORS[(m.obj.number - 1) % MARKER_COLORS.size])
      // One ring per thing, around its first tap.
      if (ringed !== m.obj && scanMarkers.first { it.obj === m.obj } === m) {
        ringed = m.obj
        val p = m.anchor.pose.translation
        scanRing?.draw(render, virtualSceneFramebuffer, p, p[1], m.obj.coverage::isCovered, viewProjectionMatrix)
      }
    }
    // Walk-around guide ring.
    val cs = captureSession
    val ring = ringRenderer
    if (cs != null && ring != null) {
      ring.draw(render, virtualSceneFramebuffer, cs.extent.centerWorld, cs.extent.bottomWorldY, cs.coverage::isCovered, viewProjectionMatrix)
    }
    // Library objects placed in the room, at real size.
    for (item in if (activity.isListingRoomActive) emptyList() else placed) {
      if (!item.updateModel()) continue
      texturedRenderer.drawInScene(render, virtualSceneFramebuffer, item.mesh, item.model, viewProjectionMatrix, 1f)
    }
    roomScene.draw(render, virtualSceneFramebuffer, texturedRenderer, viewProjectionMatrix, camera)
    backgroundRenderer.drawVirtualScene(render, virtualSceneFramebuffer, Z_NEAR, Z_FAR)
    // Outline of the selected object, drawn on top so it stays visible behind real occluders.
    selected?.takeUnless { activity.isListingRoomActive }?.let { item ->
      if (item.updateModel()) {
        Matrix.setIdentityM(boxModel, 0)
        Matrix.translateM(boxModel, 0, (item.boundsMin[0] + item.boundsMax[0]) / 2f, (item.boundsMin[1] + item.boundsMax[1]) / 2f, (item.boundsMin[2] + item.boundsMax[2]) / 2f)
        Matrix.scaleM(boxModel, 0, item.boundsMax[0] - item.boundsMin[0] + 0.02f, item.boundsMax[1] - item.boundsMin[1] + 0.02f, item.boundsMax[2] - item.boundsMin[2] + 0.02f)
        Matrix.multiplyMM(modelMatrix, 0, item.model, 0, boxModel, 0)
        Matrix.multiplyMM(boxMvp, 0, viewProjectionMatrix, 0, modelMatrix, 0)
        selectionBoxShader.setMat4("u_ModelViewProjection", boxMvp).setVec4("u_Color", floatArrayOf(1f, 0.84f, 0.31f, 1f))
        render.draw(selectionBox, selectionBoxShader)
      }
    }

    diagnostics.markerCount = markers.size
    if (cs != null && frameIndex % 10L == 0L) activity.updateCaptureProgress(cs.frameCount, cs.coverage.coveredCount, cs.coverage.sectors)
    scanSession?.let { if (frameIndex % 10L == 0L) activity.updateScanProgress(it.frameCount, it.distanceWalkedM) }
    maybeUpdateHud()
  }

  private fun consumeRequests() {
    requestedMode?.let {
      tapMode = it
      requestedMode = null
      // Drop taps queued before the mode button was pressed.
      while (activity.tapHelper.poll() != null) { /* discard */ }
    }
    while (true) {
      val a = anchorsToRelease.poll() ?: break
      a.detach()
    }
    while (true) {
      val p = pendingSurfaces.poll() ?: break
      if (p.anchor.trackingState == TrackingState.STOPPED) continue
      if (surfaces.size >= MAX_SURFACES) surfaces.removeAt(0).let { it.meshes.close(); it.anchor.detach() }
      surfaces.add(AnchoredSurface(p.id, p.anchor, SurfacePatchMeshes(renderRef, p.surface), p.surface.areaM2))
    }
    if (clearRequested) {
      clearRequested = false
      markers.forEach { it.anchor.detach() }
      markers.clear()
      surfaces.forEach { it.meshes.close(); it.anchor.detach() }
      surfaces.clear()
      pendingSurfaces.clear()
      placed.forEach { it.mesh.close(); it.anchor.detach() }
      placed.clear()
      select(null)
      tapMode = TapMode.DEFAULT
      activity.showBanner("Markers, surfaces and placed objects cleared.")
    }
    diagnostics.surfaceCount = surfaces.size

    requestedCapture?.let { s ->
      requestedCapture = null
      captureSession = s
      ringRenderer?.close()
      ringRenderer = OrbitRingRenderer(renderRef, s.extent.ringRadiusM, s.coverage.sectors)
    }
    stopCaptureCallback?.let { cb ->
      stopCaptureCallback = null
      val s = captureSession
      captureSession = null
      ringRenderer?.close()
      ringRenderer = null
      if (s != null) activity.runOnUiThread { cb(s) }
    }
    requestedScan?.let { req ->
      val session = session
      val cam = lastCamera
      if (session != null && cam != null && cam.trackingState == TrackingState.TRACKING) {
        requestedScan = null
        val camPos = cam.pose.translation
        val z = cam.pose.zAxis
        // Level anchor at the camera; +Z points back towards the user (opposite the view).
        val f = AnchorFrames.gravityAligned(camPos, floatArrayOf(camPos[0] + z[0], camPos[1], camPos[2] + z[2]))
        val anchor = session.createAnchor(Pose(camPos, f.rotation))
        val anchorToWorld = FloatArray(16).also { anchor.pose.toMatrix(it, 0) }
        val s = req.factory(anchor, anchorToWorld)
        scanSession = s
        scanRing?.close()
        scanRing = OrbitRingRenderer(renderRef, SCAN_RING_RADIUS_M, 24)
        activity.runOnUiThread { req.onStarted(s) }
      } else if (cam != null && cam.trackingState != TrackingState.TRACKING) {
        requestedScan = null
        activity.runOnUiThread { req.onStarted(null) }
      }
    }
    stopScanCallback?.let { cb ->
      stopScanCallback = null
      val s = scanSession
      scanSession = null
      scanMarkers.forEach { it.anchor.detach() }
      scanMarkers.clear()
      scanRing?.close()
      scanRing = null
      if (s != null) {
        s.anchor.detach()
        activity.runOnUiThread { cb(s) }
      }
    }
    while (true) {
      val op = placementOps.poll() ?: break
      op()
    }
    diagnostics.mode = tapMode.name.lowercase(Locale.US)
  }

  private fun updateDepth(frame: Frame) {
    try {
      frame.acquireDepthImage16Bits().use { depth ->
        diagnostics.onDepth(depth.width, depth.height, depth.timestamp, frame.timestamp)
        if (settings.showDepth || settings.useOcclusion) backgroundRenderer.updateCameraDepthTexture(depth)
      }
    } catch (_: NotYetAvailableException) {
      // Normal until enough motion has been observed.
    }
    if (diagnostics.rawDepthSupported && frameIndex % 30L == 0L) {
      try {
        frame.acquireRawDepthImage16Bits().use { raw -> diagnostics.onRawDepth(raw.width, raw.height) }
      } catch (_: NotYetAvailableException) {
      } catch (e: IllegalStateException) {
        Log.w(TAG, "Raw depth unavailable: $e")
      }
    }
  }

  private fun handleTap(frame: Frame, camera: Camera) {
    val tap = activity.tapHelper.poll() ?: return
    if (activity.isListingRoomActive) { roomScene.tap(frame, camera, tap); return }
    if (camera.trackingState != TrackingState.TRACKING) {
      activity.showBanner("Wait for tracking before tapping. Move the phone slowly sideways.")
      return
    }
    when (tapMode) {
      TapMode.DEFAULT -> if (scanSession != null) scanTap(frame, camera, tap) else pickPlaced(tap)
      TapMode.PLACE_ITEM -> {
        val req = pendingPlace
        val hit = supportHit(frame, camera, tap)
        if (req == null) {
          tapMode = TapMode.DEFAULT
        } else if (hit == null) {
          activity.showBanner("No floor or table found there yet. Move sideways slowly, then tap again.")
        } else {
          tapMode = TapMode.DEFAULT
          pendingPlace = null
          val pos = hit.hitPose.translation
          val cam = camera.pose.translation
          // Face the user: the object's +Z (its front, by the Mac's convention) towards the camera.
          val yaw = Math.toDegrees(kotlin.math.atan2((cam[0] - pos[0]).toDouble(), (cam[2] - pos[2]).toDouble())).toFloat()
          val (mn, mx) = req.mesh.bounds()
          val item = PlacedItem(nextPlacementId++, req.itemId, req.name, hit.createAnchor(), yaw, TexturedMesh(renderRef, req.mesh, req.bitmap), mn, mx)
          placed.add(item)
          select(item)
          activity.showBanner(String.format(Locale.US, "%s placed (%.0f × %.0f × %.0f cm). Tap it to select it.",
            req.name, (mx[0] - mn[0]) * 100, (mx[1] - mn[1]) * 100, (mx[2] - mn[2]) * 100))
        }
      }
      TapMode.MOVE_ITEM -> {
        val item = selected
        val hit = supportHit(frame, camera, tap)
        if (item == null) {
          tapMode = TapMode.DEFAULT
        } else if (hit == null) {
          activity.showBanner("No floor or table found there yet. Tap another spot.")
        } else {
          tapMode = TapMode.DEFAULT
          item.anchor.detach()
          item.anchor = hit.createAnchor()
          activity.showBanner("${item.name} moved.")
        }
      }
      TapMode.SELECT -> {
        tapMode = TapMode.DEFAULT
        captureForSelection(frame, camera, tap)
      }
      TapMode.SEGMENT_TEST -> {
        tapMode = TapMode.DEFAULT
        captureForSegmentation(frame, camera, tap)
      }
      TapMode.MARKER -> {
        val hit = bestHit(frame.hitTest(tap), camera)
        if (hit == null) {
          activity.showBanner("No mapped surface there yet. Move sideways slowly, then tap again.")
          return
        }
        tapMode = TapMode.DEFAULT
        placeMarker(hit, frame, camera)
      }
      TapMode.MEASURE -> {
        val hit = bestHit(frame.hitTest(tap), camera)
        if (hit == null) {
          activity.showBanner("No surface under that point. Tap the reference mark again.")
          return
        }
        tapMode = TapMode.DEFAULT
        measureDrift(hit, frame, camera, tap)
      }
    }
  }

  private fun bestHit(hits: List<HitResult>, camera: Camera): HitResult? = hits.firstOrNull { hit ->
    when (val t = hit.trackable) {
      is Plane -> t.isPoseInPolygon(hit.hitPose) && PlaneRenderer.calculateDistanceToPlane(hit.hitPose, camera.pose) > 0
      is DepthPoint -> true
      is Point -> t.orientationMode == Point.OrientationMode.ESTIMATED_SURFACE_NORMAL
      else -> false
    }
  }

  private fun surfaceKind(hit: HitResult): String = when (val t = hit.trackable) {
    is Plane -> when (t.type) {
      Plane.Type.VERTICAL -> "vertical_plane"
      Plane.Type.HORIZONTAL_DOWNWARD_FACING -> "ceiling_plane"
      else -> "horizontal_plane"
    }
    is DepthPoint -> "depth_point"
    is Point -> "feature_point"
    else -> "unknown"
  }

  private fun placeMarker(hit: HitResult, frame: Frame, camera: Camera) {
    if (markers.size >= MAX_MARKERS) {
      markers.removeAt(0).anchor.detach()
    }
    val anchor = hit.createAnchor()
    val t = anchor.pose.translation
    val marker = SurfaceMarker(
      id = nextMarkerId++,
      anchor = anchor,
      surfaceKind = surfaceKind(hit),
      createdFrameNs = frame.timestamp,
      resetsAtCreation = diagnostics.trackingResets,
      initialTranslation = t.copyOf(),
      cameraDistanceAtPlacementM = DriftMath.distance(camera.pose.translation, t),
    )
    markers.add(marker)
    activity.showBanner(
      String.format(Locale.US, "Marker %d on %s at %.2f m. Pan away, step sideways, return, then Measure drift.",
        marker.id, marker.surfaceKind.replace('_', ' '), marker.cameraDistanceAtPlacementM)
    )
  }

  private fun measureDrift(hit: HitResult, frame: Frame, camera: Camera, tap: MotionEvent) {
    val observed = hit.hitPose.translation
    val marker = markers.filter { it.anchor.trackingState == TrackingState.TRACKING }
      .minByOrNull { DriftMath.distance(it.anchor.pose.translation, observed) }
    if (marker == null) {
      activity.showBanner("Place a marker first (tap a surface), then measure.")
      return
    }
    val pose = marker.anchor.pose
    val center = pose.translation
    val normal = pose.yAxis
    val d = DriftMath.displacement(center, normal, observed)

    camera.getProjectionMatrix(projectionMatrix, 0, Z_NEAR, Z_FAR)
    camera.getViewMatrix(viewMatrix, 0)
    Matrix.multiplyMM(viewProjectionMatrix, 0, projectionMatrix, 0, viewMatrix, 0)
    val screen = DriftMath.projectToView(viewProjectionMatrix, center, viewWidth, viewHeight)
    val pixelError = screen?.let { hypot(it[0] - tap.x, it[1] - tap.y) } ?: -1f

    val elapsedS = (frame.timestamp - marker.createdFrameNs) / 1e9
    val result = JSONObject()
      .put("kind", "m1_drift_measurement")
      .put("session_id", diagnostics.sessionId)
      .put("created_ms", System.currentTimeMillis())
      .put("marker_id", marker.id)
      .put("surface_kind", marker.surfaceKind)
      .put("observed_on", surfaceKind(hit))
      .put("elapsed_s", elapsedS)
      .put("tracking_resets_since_placement", diagnostics.trackingResets - marker.resetsAtCreation)
      .put("camera_distance_at_placement_m", marker.cameraDistanceAtPlacementM.toDouble())
      .put("camera_distance_now_m", DriftMath.distance(camera.pose.translation, center).toDouble())
      .put("displacement_3d_cm", d.totalM * 100.0)
      .put("displacement_in_plane_cm", d.inPlaneM * 100.0)
      .put("displacement_normal_cm", d.normalM * 100.0)
      .put("screen_error_px", pixelError.toDouble())
      .put("anchor_refinement_cm", DriftMath.distance(marker.initialTranslation, center) * 100.0)
      .put("render_fps", diagnostics.fps().toDouble())
      .put("depth_enabled", diagnostics.depthEnabled)
      .put("target_cm_at_2m", 5.0)
    activity.recordDrift(result)
    activity.showBanner(
      String.format(Locale.US, "Marker %d: %.1f cm along surface (3D %.1f cm, %.0f px) after %.0f s, resets %d",
        marker.id, d.inPlaneM * 100, d.totalM * 100, pixelError, elapsedS, diagnostics.trackingResets - marker.resetsAtCreation)
    )
  }

  private fun captureForSegmentation(frame: Frame, camera: Camera, tap: MotionEvent) {
    val captured = captureCpuFrame(frame, camera, tap) ?: return
    activity.sendFrameToMac(captured)
  }

  /** CPU camera image + tap location + this frame's intrinsics and pose. */
  private fun captureCpuFrame(frame: Frame, camera: Camera, tap: MotionEvent): CapturedFrame? {
    val image = try {
      frame.acquireCameraImage()
    } catch (e: NotYetAvailableException) {
      activity.showBanner("Camera image not ready yet. Try again.")
      return null
    } catch (e: Exception) {
      // e.g. ResourceExhaustedException / DeadlineExceededException
      Log.w(TAG, "acquireCameraImage failed", e)
      activity.showBanner("Could not read the camera image: ${e.javaClass.simpleName}", error = true)
      return null
    }
    val nv21: ByteArray
    val width: Int
    val height: Int
    image.use {
      width = it.width
      height = it.height
      nv21 = CameraImages.yuv420ToNv21(it)
    }
    val imagePoint = FloatArray(2)
    frame.transformCoordinates2d(Coordinates2d.VIEW, floatArrayOf(tap.x, tap.y), Coordinates2d.IMAGE_PIXELS, imagePoint)
    val intr = camera.imageIntrinsics
    val f = intr.focalLength
    val c = intr.principalPoint
    val pose = camera.pose
    val q = pose.rotationQuaternion
    val t = pose.translation
    return CapturedFrame(
      frameId = "${diagnostics.sessionId}-${frame.timestamp}",
      sessionId = diagnostics.sessionId,
      timestampNs = frame.timestamp,
      width = width,
      height = height,
      nv21 = nv21,
      pointX = imagePoint[0].coerceIn(0f, width - 1f),
      pointY = imagePoint[1].coerceIn(0f, height - 1f),
      intrinsics = floatArrayOf(f[0], f[1], c[0], c[1]),
      cameraPose = floatArrayOf(t[0], t[1], t[2], q[0], q[1], q[2], q[3]),
      sensorToDisplayRotation = displayRotationHelper.getCameraSensorToDisplayRotation(diagnostics.cameraId),
      depthTimestampNs = diagnostics.lastDepthTimestampNs,
    )
  }

  /**
   * Captures everything the selection needs from *this* frame: CPU image, tap in image pixels,
   * intrinsics, physical camera pose, depth (with its region of the CPU image) and an anchor.
   */
  private fun captureForSelection(frame: Frame, camera: Camera, tap: MotionEvent) {
    val session = session ?: return
    val cpu = captureCpuFrame(frame, camera, tap) ?: return

    // Anchor: on the tapped surface if ARCore can hit it, otherwise at the camera position. It is
    // level and faces the camera (see AnchorFrames) so the capture's seed box sits flat on the
    // support and follows the view; it stays attached to the tapped trackable.
    val hits = frame.hitTest(tap)
    val hit = bestHit(hits, camera)
    val anchor: Anchor
    val anchorSource: String
    val camPos = camera.pose.translation
    if (hit != null) {
      val f = AnchorFrames.gravityAligned(hit.hitPose.translation, camPos)
      anchor = hit.trackable.createAnchor(Pose(hit.hitPose.translation, f.rotation))
      anchorSource = surfaceKind(hit) + "_level"
    } else {
      val f = AnchorFrames.gravityAligned(camPos, floatArrayOf(camPos[0] - camera.pose.zAxis[0], camPos[1], camPos[2] - camera.pose.zAxis[2]))
      anchor = session.createAnchor(Pose(camPos, f.rotation))
      anchorSource = "camera_position_level"
    }
    val anchorPose = anchor.pose
    val anchorWorld = FloatArray(16).also { anchorPose.toMatrix(it, 0) }
    val worldToAnchor = FloatArray(16).also { anchorPose.inverse().toMatrix(it, 0) }
    val cameraToWorld = FloatArray(16).also { camera.pose.toMatrix(it, 0) }

    val fallback = hits.firstOrNull { h ->
      val t = h.trackable
      t is Plane && t.isPoseInPolygon(h.hitPose)
    }?.let { SurfaceGeometry.PlaneEstimate(it.hitPose.translation, it.hitPose.yAxis) }

    var depthNote = if (diagnostics.depthEnabled) "not available yet" else "disabled"
    var depth: SurfaceGeometry.DepthImage? = null
    if (diagnostics.depthEnabled) {
      try {
        frame.acquireDepthImage16Bits().use { img ->
          val corners = FloatArray(8)
          frame.transformCoordinates2d(Coordinates2d.TEXTURE_NORMALIZED, floatArrayOf(0f, 0f, 0f, 1f, 1f, 0f, 1f, 1f), Coordinates2d.IMAGE_PIXELS, corners)
          // Expect an axis-aligned region: (0,1) shares x with (0,0); (1,0) shares y with (0,0).
          val aligned = abs(corners[2] - corners[0]) < 2f && abs(corners[5] - corners[1]) < 2f
          if (!aligned) {
            depthNote = "alignment unsupported"
          } else {
            val ti = camera.textureIntrinsics
            val tf = ti.focalLength
            val tc = ti.principalPoint
            val td = ti.imageDimensions
            depth = SurfaceGeometry.DepthImage(
              img.width, img.height, copyDepth(img),
              SurfaceGeometry.Intrinsics(tf[0], tf[1], tc[0], tc[1], td[0], td[1]),
              floatArrayOf(corners[0], corners[1], corners[6], corners[7]),
            )
            val ageMs = (frame.timestamp - img.timestamp) / 1_000_000
            depthNote = "${img.width}x${img.height}, ${ageMs} ms old"
          }
        }
      } catch (_: NotYetAvailableException) {
      }
    }

    activity.beginSelection(
      SelectionCapture(
        selectionId = nextSelectionId++,
        frame = cpu,
        anchor = anchor,
        anchorSource = anchorSource,
        anchorPoseWorld = anchorWorld,
        worldToAnchor = worldToAnchor,
        cameraToWorld = cameraToWorld,
        depth = depth,
        depthNote = depthNote,
        fallbackPlane = fallback,
      )
    )
  }

  /** Copies a DEPTH16 image into a tightly packed array of millimetres. */
  private fun copyDepth(img: android.media.Image): ShortArray {
    val plane = img.planes[0]
    val buf = plane.buffer.order(ByteOrder.nativeOrder())
    val out = ShortArray(img.width * img.height)
    val rowStride = plane.rowStride
    val pixelStride = plane.pixelStride
    for (y in 0 until img.height) for (x in 0 until img.width) {
      out[y * img.width + x] = buf.getShort(y * rowStride + x * pixelStride)
    }
    return out
  }

  /**
   * Scan running, plain tap: the user picked a thing to keep. Its 3D point comes from ARCore's hit
   * test (depth or planes); this frame becomes its seed keyframe; the activity asks the Mac for
   * an outline preview when it can.
   */
  private fun scanTap(frame: Frame, camera: Camera, tap: MotionEvent) {
    val s = scanSession ?: return
    val hit = bestHit(frame.hitTest(tap), camera)
    if (hit == null) {
      activity.showBanner("Couldn't place that yet: move sideways a little so depth catches up, then tap the thing again.")
      return
    }
    val imagePoint = FloatArray(2)
    frame.transformCoordinates2d(Coordinates2d.VIEW, floatArrayOf(tap.x, tap.y), Coordinates2d.IMAGE_PIXELS, imagePoint)
    when (val r = s.addObject(frame, camera, imagePoint, hit.hitPose.translation)) {
      is ScanSession.AddResult.Failed -> activity.showBanner(r.message)
      is ScanSession.AddResult.Ok -> {
        scanMarkers.add(ScanMarker(r.obj, hit.createAnchor()))
        val kf = r.keyframe
        val pose = camera.pose
        val q = pose.rotationQuaternion
        val t = pose.translation
        val captured = CapturedFrame(
          frameId = "${diagnostics.sessionId}-${frame.timestamp}", sessionId = diagnostics.sessionId, timestampNs = kf.timestampNs,
          width = kf.width, height = kf.height, nv21 = kf.nv21,
          pointX = imagePoint[0].coerceIn(0f, kf.width - 1f), pointY = imagePoint[1].coerceIn(0f, kf.height - 1f),
          intrinsics = floatArrayOf(kf.imageK.fx, kf.imageK.fy, kf.imageK.cx, kf.imageK.cy),
          cameraPose = floatArrayOf(t[0], t[1], t[2], q[0], q[1], q[2], q[3]),
          sensorToDisplayRotation = displayRotationHelper.getCameraSensorToDisplayRotation(diagnostics.cameraId),
          depthTimestampNs = kf.depthTimestampNs,
        )
        activity.runOnUiThread { activity.onScanObjectAdded(r.obj, captured, r.newThing) }
      }
    }
  }

  /** Upward-facing plane under the tap (floor, table), else any surface hit. */
  private fun supportHit(frame: Frame, camera: Camera, tap: MotionEvent): HitResult? {
    val hits = frame.hitTest(tap)
    return hits.firstOrNull { h ->
      val t = h.trackable
      t is Plane && t.type == Plane.Type.HORIZONTAL_UPWARD_FACING && t.isPoseInPolygon(h.hitPose)
    } ?: bestHit(hits, camera)
  }

  /** Default tap: select the nearest placed object under the finger, or deselect. */
  private fun pickPlaced(tap: MotionEvent) {
    if (placed.isEmpty()) {
      activity.showBanner("Open My stuff to place your things, or Scan stuff to add more.")
      return
    }
    if (!Matrix.invertM(inverseViewProjection, 0, viewProjectionMatrix, 0)) return
    val (origin, dir) = Placement.rayFromView(inverseViewProjection, tap.x, tap.y, viewWidth, viewHeight)
    val hit = placed.filter { it.updateModel() }
      .mapNotNull { item -> Placement.hitBox(origin, dir, item.model, item.boundsMin, item.boundsMax)?.let { item to it } }
      .minByOrNull { it.second }?.first
    select(hit)
  }

  private fun select(item: PlacedItem?) {
    selected = item
    val name = item?.name
    activity.runOnUiThread { activity.onPlacementSelected(name) }
  }

  private fun unitCubeEdges(render: SampleRender): Mesh {
    val c = listOf(
      floatArrayOf(-0.5f, -0.5f, -0.5f), floatArrayOf(0.5f, -0.5f, -0.5f), floatArrayOf(0.5f, 0.5f, -0.5f), floatArrayOf(-0.5f, 0.5f, -0.5f),
      floatArrayOf(-0.5f, -0.5f, 0.5f), floatArrayOf(0.5f, -0.5f, 0.5f), floatArrayOf(0.5f, 0.5f, 0.5f), floatArrayOf(-0.5f, 0.5f, 0.5f),
    )
    val edges = intArrayOf(0, 1, 1, 2, 2, 3, 3, 0, 4, 5, 5, 6, 6, 7, 7, 4, 0, 4, 1, 5, 2, 6, 3, 7)
    val v = FloatArray(edges.size * 3).also { out -> edges.forEachIndexed { i, e -> c[e].copyInto(out, i * 3) } }
    val vb = VertexBuffer(render, 3, java.nio.ByteBuffer.allocateDirect(v.size * 4).order(java.nio.ByteOrder.nativeOrder()).asFloatBuffer().apply { put(v); rewind() })
    return Mesh(render, Mesh.PrimitiveMode.LINES, null, arrayOf(vb))
  }

  private fun updateGuidance(session: Session, camera: Camera) {
    if (activity.isListingRoomActive) { activity.showGuidance(null); return }
    val message: String? = when {
      camera.trackingState == TrackingState.PAUSED && camera.trackingFailureReason == TrackingFailureReason.NONE ->
        "Initialising: move the phone slowly sideways around the room."
      camera.trackingState != TrackingState.TRACKING ->
        "Tracking lost: ${TrackingStateHelper.getTrackingFailureReasonString(camera)} Markers are paused until tracking recovers."
      tapMode == TapMode.MEASURE -> "Measure: tap exactly on the physical reference mark."
      tapMode == TapMode.SEGMENT_TEST -> "Test frame: tap a wall or tabletop to send this frame to the Mac."
      tapMode == TapMode.SELECT -> "Select: tap a wall section, tabletop or object. Hold still for a moment."
      tapMode == TapMode.PLACE_ITEM -> "Place ${pendingPlace?.name ?: "object"}: tap the floor or a table."
      tapMode == TapMode.MOVE_ITEM -> "Move ${selected?.name ?: "object"}: tap where it should go."
      tapMode == TapMode.MARKER -> "Test marker: tap a surface to place it."
      captureSession != null -> captureSession?.guidance
      scanSession != null -> scanSession?.guidance
      markers.isEmpty() && session.getAllTrackables(Plane::class.java).none { it.trackingState == TrackingState.TRACKING } &&
        diagnostics.depthFrames == 0L -> "Searching for surfaces… move sideways slowly."
      else -> null
    }
    activity.showGuidance(message)
  }

  private fun maybeUpdateHud() {
    val now = System.nanoTime()
    if (now - lastHudUpdateNs < 250_000_000L) return
    lastHudUpdateNs = now
    activity.updateHud(diagnostics.hudText())
  }

  companion object {
    private const val TAG = "InspectRenderer"
    private const val Z_NEAR = 0.1f
    private const val Z_FAR = 100f
    private const val MAX_MARKERS = 5
    private const val MAX_SURFACES = 8
    private val SURFACE_COLORS = arrayOf(
      floatArrayOf(0.98f, 0.80f, 0.20f, 0.38f),
      floatArrayOf(0.35f, 0.85f, 0.55f, 0.38f),
      floatArrayOf(0.95f, 0.45f, 0.40f, 0.38f),
      floatArrayOf(0.45f, 0.65f, 1.00f, 0.38f),
    )
    const val MARKER_SIZE_M = 0.20f
    private const val SCAN_RING_RADIUS_M = 0.25f
    private val MARKER_COLORS = arrayOf(
      floatArrayOf(0.21f, 0.77f, 0.84f, 0.30f),
      floatArrayOf(0.98f, 0.62f, 0.16f, 0.30f),
      floatArrayOf(0.55f, 0.85f, 0.35f, 0.30f),
      floatArrayOf(0.90f, 0.35f, 0.65f, 0.30f),
      floatArrayOf(0.60f, 0.55f, 0.95f, 0.30f),
    )
  }
}
