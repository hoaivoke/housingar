package com.housingar.inspect

import android.graphics.Color
import android.content.Intent
import android.net.Uri
import android.opengl.GLSurfaceView
import android.os.Build
import android.os.Bundle
import android.os.SystemClock
import android.text.Editable
import android.text.InputFilter
import android.text.InputType
import android.text.TextWatcher
import android.util.Log
import android.view.View
import android.view.WindowManager
import android.widget.Button
import android.widget.CheckBox
import android.widget.EditText
import android.widget.ImageButton
import android.widget.ImageView
import android.widget.LinearLayout
import android.widget.PopupMenu
import android.widget.TextView
import android.widget.Toast
import androidx.activity.OnBackPressedCallback
import androidx.appcompat.app.AlertDialog
import androidx.appcompat.app.AppCompatActivity
import androidx.lifecycle.DefaultLifecycleObserver
import androidx.lifecycle.LifecycleOwner
import com.google.ar.core.Anchor
import com.google.ar.core.CameraConfig
import com.google.ar.core.CameraConfigFilter
import com.google.ar.core.Config
import com.google.ar.core.Session
import com.google.ar.core.examples.java.common.helpers.CameraPermissionHelper
import com.google.ar.core.examples.java.common.helpers.FullScreenHelper
import com.google.ar.core.examples.java.common.helpers.TapHelper
import com.google.ar.core.examples.java.common.samplerender.SampleRender
import com.google.ar.core.examples.kotlin.common.helpers.ARCoreSessionLifecycleHelper
import com.google.ar.core.exceptions.CameraNotAvailableException
import com.google.ar.core.exceptions.UnavailableApkTooOldException
import com.google.ar.core.exceptions.UnavailableDeviceNotCompatibleException
import com.google.ar.core.exceptions.UnavailableSdkTooOldException
import com.google.ar.core.exceptions.UnavailableUserDeclinedInstallationException
import com.housingar.inspect.capture.ObjectCaptureSession
import com.housingar.inspect.capture.ScanSession
import com.housingar.inspect.geometry.SurfaceGeometry
import com.housingar.inspect.library.LibraryController
import com.housingar.inspect.library.Placement
import com.housingar.inspect.room.RoomArController
import org.json.JSONObject
import java.io.File
import java.text.SimpleDateFormat
import java.util.Date
import java.util.EnumSet
import java.util.Locale
import java.util.concurrent.Executors
import java.util.concurrent.ScheduledFuture
import java.util.concurrent.TimeUnit

/**
 * The AR screen. Main flow (since 24 Oct): "Scan stuff" films your belongings and the Mac turns
 * them into separate object meshes; you name the ones to keep and they go into "My stuff"; in a
 * new room you place them at real size on the live camera. "Capture one" covers an object the
 * scan missed (select it, then walk around it). Milestone 0/1 tools are under More.
 */
class InspectActivity : AppCompatActivity() {

  lateinit var arCoreSessionHelper: ARCoreSessionLifecycleHelper
  lateinit var settings: AppSettings
  lateinit var tapHelper: TapHelper
  val diagnostics = Diagnostics()

  private lateinit var renderer: InspectRenderer
  private lateinit var surfaceView: GLSurfaceView
  private lateinit var hud: TextView
  private lateinit var banner: TextView
  private lateinit var thumbnail: ImageView
  private lateinit var client: MacServiceClient
  lateinit var selection: SurfaceSelectionController
  lateinit var meshes: MeshController
  lateinit var scan: ScanController
  lateinit var library: LibraryController
  private lateinit var roomAr: RoomArController
  val isListingRoomActive get() = ::roomAr.isInitialized && roomAr.active
  private lateinit var jobCard: JobCard
  private lateinit var placeBar: View

  private val network = Executors.newSingleThreadScheduledExecutor()
  private var healthTask: ScheduledFuture<*>? = null

  // Banner state (UI thread only): transient results take priority over continuous guidance.
  private var transientText: String? = null
  private var transientIsError = false
  private var transientUntil = 0L
  private var guidanceText: String? = null
  @Volatile private var lastGuidancePosted: String? = null

  override fun onCreate(savedInstanceState: Bundle?) {
    super.onCreate(savedInstanceState)
    settings = AppSettings(this)
    client = MacServiceClient { settings.serviceUrl }
    diagnostics.serviceUrl = settings.serviceUrl

    setContentView(R.layout.activity_inspect)
    surfaceView = findViewById(R.id.surfaceview)
    hud = findViewById(R.id.hud)
    banner = findViewById(R.id.banner)
    thumbnail = findViewById(R.id.thumbnail)
    tapHelper = TapHelper(this).also { surfaceView.setOnTouchListener(it) }
    thumbnail.setOnClickListener { it.visibility = View.GONE }

    arCoreSessionHelper = ARCoreSessionLifecycleHelper(this)
    arCoreSessionHelper.exceptionCallback = { exception ->
      val message = when (exception) {
        is UnavailableUserDeclinedInstallationException -> "Please install Google Play Services for AR"
        is UnavailableApkTooOldException -> "Please update Google Play Services for AR"
        is UnavailableSdkTooOldException -> "Please update this app"
        is UnavailableDeviceNotCompatibleException -> "This device does not support AR"
        is CameraNotAvailableException -> "Camera not available. Try restarting the app."
        else -> "Failed to create AR session: $exception"
      }
      Log.e(TAG, "ARCore threw an exception", exception)
      showBanner(message, error = true)
    }
    arCoreSessionHelper.beforeSessionResume = ::configureSession
    lifecycle.addObserver(arCoreSessionHelper)

    renderer = InspectRenderer(this)
    lifecycle.addObserver(renderer)
    lifecycle.addObserver(object : DefaultLifecycleObserver {
      override fun onResume(owner: LifecycleOwner) = surfaceView.onResume()
      override fun onPause(owner: LifecycleOwner) = surfaceView.onPause()
    })
    SampleRender(surfaceView, renderer, assets)

    jobCard = JobCard(this)
    selection = SurfaceSelectionController(this, client, network)
    meshes = MeshController(this, client, jobCard)
    scan = ScanController(this, client, jobCard)
    library = LibraryController(this, client, jobCard)
    roomAr = RoomArController(this, renderer.roomScene)
    // Back (button or gesture) closes the open sheet or selection review; otherwise it behaves normally.
    val backCancelsSelection = object : OnBackPressedCallback(false) {
      override fun handleOnBackPressed() = selection.cancel("Selection cancelled.")
    }
    val backClosesSheet = object : OnBackPressedCallback(false) {
      override fun handleOnBackPressed() = library.close()
    }
    onBackPressedDispatcher.addCallback(this, backCancelsSelection)
    onBackPressedDispatcher.addCallback(this, backClosesSheet)
    selection.onActiveChanged = { active -> backCancelsSelection.isEnabled = active }
    library.onOpenChanged = { open -> backClosesSheet.isEnabled = open }

    findViewById<Button>(R.id.btn_scan).setOnClickListener { startScanIfIdle() }
    findViewById<Button>(R.id.btn_library).setOnClickListener { library.showLibrary() }
    findViewById<Button>(R.id.btn_select).setOnClickListener { armCaptureOne() }
    findViewById<Button>(R.id.btn_more).setOnClickListener { v ->
      PopupMenu(this, v).apply {
        menu.add(0, MENU_SAVE_LAYOUT, 0, "Save layout of placed objects")
        menu.add(0, MENU_CLEAR, 1, "Clear room (placed objects, surfaces, markers)")
        menu.add(0, MENU_MEASURE, 2, "Measure drift (M1)")
        menu.add(0, MENU_MARKER, 3, "Place test marker (M1)")
        menu.add(0, MENU_TEST_FRAME, 4, "Test frame round trip (M0)")
        menu.add(0, MENU_REPORT, 5, "Save readiness report (M0)")
        menu.add(0, MENU_RENTALS, 6, "Find a rental room")
        setOnMenuItemClickListener {
          when (it.itemId) {
            MENU_SAVE_LAYOUT -> library.saveLayout()
            MENU_CLEAR -> {
              selection.cancel(null)
              renderer.requestClear()
            }
            MENU_MEASURE -> renderer.armMode(InspectRenderer.TapMode.MEASURE)
            MENU_MARKER -> renderer.armMode(InspectRenderer.TapMode.MARKER)
            MENU_TEST_FRAME -> renderer.armMode(InspectRenderer.TapMode.SEGMENT_TEST)
            MENU_REPORT -> writeReadinessReport()
            MENU_RENTALS -> startActivity(Intent(Intent.ACTION_VIEW, Uri.parse(settings.serviceUrl)))
          }
          true
        }
        show()
      }
    }

    placeBar = findViewById(R.id.place_bar)
    findViewById<Button>(R.id.place_rotate_left).setOnClickListener { renderer.rotateSelected(15f) }
    findViewById<Button>(R.id.place_rotate_right).setOnClickListener { renderer.rotateSelected(-15f) }
    findViewById<Button>(R.id.place_move).setOnClickListener { renderer.moveSelected() }
    findViewById<Button>(R.id.place_delete).setOnClickListener { renderer.deleteSelected() }
    findViewById<Button>(R.id.place_done).setOnClickListener { renderer.deselect() }
    findViewById<ImageButton>(R.id.settings_button).setOnClickListener { showSettings() }
    intent.dataString?.takeIf { it.startsWith("housingar:") }?.let { roomAr.open(it) }
  }

  override fun onNewIntent(intent: Intent) {
    super.onNewIntent(intent)
    setIntent(intent)
    intent.dataString?.let { roomAr.open(it) }
  }

  fun canOpenListingRoom() = !selection.isActive && !meshes.isCapturing && !scan.isScanning && !jobCard.busy && !library.isOpen

  fun prepareListingRoom() {
    renderer.armMode(InspectRenderer.TapMode.DEFAULT)
    renderer.deselect()
    findViewById<View>(R.id.main_bar).visibility = View.GONE
    findViewById<View>(R.id.place_bar).visibility = View.GONE
    thumbnail.visibility = View.GONE
    showGuidance(null)
  }

  fun finishListingRoom() { findViewById<View>(R.id.main_bar).visibility = View.VISIBLE }
  fun onListingRoomGuidance(text: String) = runOnUiThread { if (::roomAr.isInitialized) roomAr.guidance(text) }

  /**
   * Uses the highest-resolution CPU image the back camera offers at 30 fps (object reconstruction
   * needs detailed keyframes; ARCore's default is 640x480). Depth comes from motion on the Pixel,
   * so no depth-sensor filter is applied.
   */
  private fun selectCameraConfig(session: Session) {
    try {
      val filter = CameraConfigFilter(session)
        .setFacingDirection(CameraConfig.FacingDirection.BACK)
        .setTargetFps(EnumSet.of(CameraConfig.TargetFps.TARGET_FPS_30))
      val best = session.getSupportedCameraConfigs(filter)
        .filter { it.imageSize.width * it.imageSize.height <= 1920 * 1440 }
        .maxByOrNull { it.imageSize.width * it.imageSize.height } ?: return
      val current = session.cameraConfig
      if (current.imageSize != best.imageSize || current.cameraId != best.cameraId) session.cameraConfig = best
    } catch (e: Exception) {
      Log.w(TAG, "Camera config selection failed; keeping the default", e)
    }
  }

  /** Session configuration for spatial inspection. */
  private fun configureSession(session: Session) {
    selectCameraConfig(session)
    val config = session.config.apply {
      depthMode = if (session.isDepthModeSupported(Config.DepthMode.AUTOMATIC)) Config.DepthMode.AUTOMATIC else Config.DepthMode.DISABLED
      planeFindingMode = Config.PlaneFindingMode.HORIZONTAL_AND_VERTICAL
      focusMode = Config.FocusMode.AUTO
      lightEstimationMode = Config.LightEstimationMode.DISABLED
      instantPlacementMode = Config.InstantPlacementMode.DISABLED
      updateMode = Config.UpdateMode.BLOCKING
    }
    session.configure(config)
    diagnostics.onSessionConfigured(session, config)
  }

  override fun onResume() {
    super.onResume()
    healthTask = network.scheduleWithFixedDelay({
      diagnostics.serviceUrl = settings.serviceUrl
      diagnostics.health = client.health()
    }, 0, 3, TimeUnit.SECONDS)
  }

  override fun onPause() {
    super.onPause()
    healthTask?.cancel(false)
  }

  override fun onDestroy() {
    if (::roomAr.isInitialized) roomAr.destroy()
    super.onDestroy()
    network.shutdownNow()
  }

  override fun onRequestPermissionsResult(requestCode: Int, permissions: Array<String>, results: IntArray) {
    super.onRequestPermissionsResult(requestCode, permissions, results)
    if (!CameraPermissionHelper.hasCameraPermission(this)) {
      Toast.makeText(this, "Camera permission is needed for room inspection", Toast.LENGTH_LONG).show()
      if (!CameraPermissionHelper.shouldShowRequestPermissionRationale(this)) {
        CameraPermissionHelper.launchPermissionSettings(this)
      }
      finish()
    }
  }

  override fun onWindowFocusChanged(hasFocus: Boolean) {
    super.onWindowFocusChanged(hasFocus)
    FullScreenHelper.setFullScreenOnWindowFocusChanged(this, hasFocus)
  }

  // ---- UI updates (callable from any thread) ----

  fun updateHud(text: String) = runOnUiThread { hud.text = text }

  /** Short-lived result or error message; shown for a few seconds over the continuous guidance. */
  fun showBanner(text: String, error: Boolean = false) = runOnUiThread {
    transientText = text
    transientIsError = error
    transientUntil = SystemClock.uptimeMillis() + if (error) 10_000 else 6_000
    refreshBanner()
    banner.postDelayed({ refreshBanner() }, if (error) 10_050L else 6_050L)
  }

  /** Continuous guidance from the renderer, or null when nothing needs saying. Posted only on change. */
  fun showGuidance(text: String?) {
    if (text == lastGuidancePosted) return
    lastGuidancePosted = text
    runOnUiThread {
      guidanceText = text
      refreshBanner()
    }
  }

  private fun refreshBanner() {
    val transient = transientText?.takeIf { SystemClock.uptimeMillis() < transientUntil }
    val text = transient ?: guidanceText
    banner.visibility = if (text == null) View.GONE else View.VISIBLE
    banner.text = text ?: ""
    banner.setBackgroundColor(if (transient != null && transientIsError) Color.parseColor("#E6A12A2A") else Color.parseColor("#E6202A44"))
  }

  // ---- Milestone 0: frame round trip to the Mac ----

  fun sendFrameToMac(frame: CapturedFrame) {
    showBanner("Sending frame to ${settings.serviceUrl} …")
    network.execute {
      val start = System.nanoTime()
      val summary = JSONObject().put("frame_id", frame.frameId).put("image_size", "${frame.width}x${frame.height}")
      try {
        val jpeg = frame.toJpeg()
        val response = client.segment(frame.frameId, jpeg, frame.pointX, frame.pointY, frame.metadata())
        val roundTripMs = (System.nanoTime() - start) / 1_000_000
        val masks = response.getJSONArray("masks")
        val best = masks.getJSONObject(0)
        summary.put("ok", true)
          .put("round_trip_ms", roundTripMs)
          .put("backend", response.optString("backend"))
          .put("model", response.optString("model"))
          .put("score", best.optDouble("score"))
          .put("area_fraction", best.optDouble("area_fraction"))
          .put("server_timing_ms", response.optJSONObject("timing_ms") ?: JSONObject())
          .put("jpeg_bytes", jpeg.size)
        diagnostics.lastRoundTrip = summary
        val preview = CameraImages.maskPreview(jpeg, best.getString("mask_png_b64"), listOf(floatArrayOf(frame.pointX, frame.pointY, 1f)), frame.sensorToDisplayRotation)
        runOnUiThread {
          thumbnail.setImageBitmap(preview)
          thumbnail.visibility = View.VISIBLE
        }
        showBanner(String.format(Locale.US, "Mask returned in %d ms (%s, score %.2f). 2D preview only; tap it to hide.",
          roundTripMs, response.optString("backend"), best.optDouble("score")))
      } catch (e: Exception) {
        Log.w(TAG, "Frame round trip failed", e)
        summary.put("ok", false).put("error", e.toString())
        diagnostics.lastRoundTrip = summary
        showBanner("Frame round trip failed: ${e.message?.take(120)}. Check the service URL in settings.", error = true)
      }
    }
  }

  // ---- Milestones 2–3: surface selection ----

  /** GL thread → UI: a selection tap was captured. */
  fun beginSelection(capture: SelectionCapture) = selection.begin(capture)

  /** Hands an accepted surface (and ownership of its anchor) to the renderer. */
  fun addAnchoredSurface(id: Int, anchor: Anchor, surface: SurfaceGeometry.Surface) = renderer.addSurface(id, anchor, surface)

  /** Detaches a capture anchor that will not be used (cancelled or superseded selection). */
  fun releaseSelectionAnchor(anchor: Anchor) = renderer.releaseAnchor(anchor)

  // ---- Scan stuff ----

  private fun startScanIfIdle() {
    when {
      meshes.isCapturing -> showBanner("Finish or cancel the object capture first.")
      selection.isActive -> showBanner("Close the selection review first.")
      else -> scan.start()
    }
  }

  fun startScan(factory: (Anchor, FloatArray) -> ScanSession, onStarted: (ScanSession?) -> Unit) = renderer.startScan(factory, onStarted)
  fun stopScan(onStopped: (ScanSession) -> Unit) = renderer.stopScan(onStopped)
  /** Renderer → UI, a few times per second while scanning. */
  fun updateScanProgress(frames: Int, distanceM: Float) = scan.updateProgress(frames, distanceM)

  /** Renderer → UI: a thing was tapped during the scan ([frame] is its seed keyframe). */
  fun onScanObjectAdded(obj: com.housingar.inspect.capture.TappedObject, frame: CapturedFrame, newThing: Boolean) = scan.onObjectAdded(obj, frame, newThing)

  fun undoScanObject(onUndone: (com.housingar.inspect.capture.ScanSession.UndoResult?) -> Unit) = renderer.undoScanObject(onUndone)

  fun startNewScanThing() = renderer.startNewScanThing()

  /** Any thread: a small picture in the corner (tap it to hide) plus a message. */
  fun showPreview(bitmap: android.graphics.Bitmap, message: String) = runOnUiThread {
    thumbnail.setImageBitmap(bitmap)
    thumbnail.visibility = View.VISIBLE
    showBanner(message)
    thumbnail.postDelayed({ if (thumbnail.drawable != null) thumbnail.visibility = View.GONE }, 8000)
  }

  // ---- Capture one object (the scan missed it): select → review → Create mesh → walk around ----

  fun armCaptureOne() {
    if (scan.isScanning || meshes.isCapturing) {
      showBanner("Finish the current scan or capture first.")
      return
    }
    renderer.armMode(InspectRenderer.TapMode.SELECT)
    showBanner("Tap the object to capture. Check its outline, then press Create mesh and walk around it.")
  }

  fun startObjectCapture(s: ObjectCaptureSession) = renderer.startObjectCapture(s)
  fun stopObjectCapture(onStopped: (ObjectCaptureSession) -> Unit) = renderer.stopObjectCapture(onStopped)
  fun updateCaptureProgress(frames: Int, covered: Int, sectors: Int) = meshes.updateCaptureProgress(frames, covered, sectors)

  // ---- Placing My stuff in the room ----

  /** Any thread: the next floor/table tap places this object. */
  fun placeItem(request: InspectRenderer.PlaceRequest) = renderer.placeItem(request)

  fun snapshotPlacements(onResult: (List<Triple<String, String, FloatArray>>) -> Unit) = renderer.snapshotPlacements(onResult)

  /** UI thread: a placed object was selected (show rotate / move / delete) or deselected (null). */
  fun onPlacementSelected(name: String?) {
    placeBar.visibility = if (name != null) View.VISIBLE else View.GONE
    if (name != null) showBanner("$name selected: rotate, move or delete it, or press Done.")
  }

  /** UI thread: a one-line text prompt. [onName] receives the trimmed, non-blank text. */
  fun askName(title: String, hint: String, initial: String = "", onCancel: (() -> Unit)? = null, onName: (String) -> Unit) {
    val pad = (16 * resources.displayMetrics.density).toInt()
    val input = EditText(this).apply {
      this.hint = hint
      setText(initial)
      setSelection(initial.length)
      inputType = InputType.TYPE_CLASS_TEXT or InputType.TYPE_TEXT_FLAG_CAP_SENTENCES
      isSingleLine = true
      filters = arrayOf(InputFilter.LengthFilter(Placement.MAX_NAME_LENGTH))
    }
    val frame = LinearLayout(this).apply {
      setPadding(pad, pad / 2, pad, 0)
      addView(input, LinearLayout.LayoutParams(LinearLayout.LayoutParams.MATCH_PARENT, LinearLayout.LayoutParams.WRAP_CONTENT))
    }
    val dialog = AlertDialog.Builder(this)
      .setTitle(title)
      .setView(frame)
      .setPositiveButton("OK") { _, _ -> onName(input.text.toString().trim()) }
      .setNegativeButton("Cancel") { _, _ -> onCancel?.invoke() }
      .setOnCancelListener { onCancel?.invoke() }
      .create()
    dialog.setOnShowListener {
      val ok = dialog.getButton(AlertDialog.BUTTON_POSITIVE)
      ok.isEnabled = input.text.isNotBlank()
      input.addTextChangedListener(object : TextWatcher {
        override fun beforeTextChanged(s: CharSequence?, start: Int, count: Int, after: Int) {}
        override fun onTextChanged(s: CharSequence?, start: Int, before: Int, count: Int) {}
        override fun afterTextChanged(s: Editable?) {
          ok.isEnabled = !s.isNullOrBlank()
        }
      })
      input.requestFocus()
    }
    dialog.window?.setSoftInputMode(WindowManager.LayoutParams.SOFT_INPUT_STATE_VISIBLE)
    dialog.show()
  }

  // ---- Milestone 1: drift log ----

  fun recordDrift(measurement: JSONObject) {
    network.execute {
      val dir = getExternalFilesDir("drift") ?: filesDir
      File(dir, "drift_${diagnostics.sessionId}.jsonl").appendText(measurement.toString() + "\n")
      client.postJson("/v1/logs/drift", measurement)
    }
  }

  // ---- Milestone 0: readiness report ----

  private fun writeReadinessReport() {
    val device = JSONObject()
      .put("manufacturer", Build.MANUFACTURER)
      .put("model", Build.MODEL)
      .put("android_release", Build.VERSION.RELEASE)
      .put("sdk_int", Build.VERSION.SDK_INT)
      .put("arcore_version", arCoreVersion())
      .put("app_version", BuildConfig.VERSION_NAME)
    val report = diagnostics.report(device)
    network.execute {
      val dir = getExternalFilesDir("reports") ?: filesDir
      val stamp = SimpleDateFormat("yyyyMMdd_HHmmss", Locale.US).format(Date())
      val file = File(dir, "m0_${stamp}_${diagnostics.sessionId}.json")
      file.writeText(report.toString(2))
      val posted = client.postJson("/v1/diagnostics", report)
      val checks = report.getJSONObject("m0_checks")
      val passed = checks.keys().asSequence().count { checks.getBoolean(it) }
      val total = checks.length()
      showBanner("Report saved ($passed/$total checks pass)" + if (posted) " and sent to the Mac." else ". Mac not reachable; copy it with adb.")
    }
  }

  private fun arCoreVersion(): String = try {
    packageManager.getPackageInfo("com.google.ar.core", 0).versionName ?: "unknown"
  } catch (e: Exception) {
    "not installed"
  }

  private fun showSettings() {
    val pad = (16 * resources.displayMetrics.density).toInt()
    val layout = LinearLayout(this).apply {
      orientation = LinearLayout.VERTICAL
      setPadding(pad, pad / 2, pad, 0)
    }
    val urlLabel = TextView(this).apply { text = "Mac service URL (USB: http://127.0.0.1:8000)" }
    val url = EditText(this).apply {
      setText(settings.serviceUrl)
      inputType = InputType.TYPE_CLASS_TEXT or InputType.TYPE_TEXT_VARIATION_URI
      isSingleLine = true
    }
    val planes = CheckBox(this).apply { text = "Show detected planes"; isChecked = settings.showPlanes }
    val points = CheckBox(this).apply { text = "Show feature points"; isChecked = settings.showPointCloud }
    val depthVis = CheckBox(this).apply { text = "Show depth map"; isChecked = settings.showDepth; isEnabled = diagnostics.depthAutomaticSupported }
    val occlusion = CheckBox(this).apply { text = "Depth occlusion for markers"; isChecked = settings.useOcclusion; isEnabled = diagnostics.depthAutomaticSupported }
    listOf(urlLabel, url, planes, points, depthVis, occlusion).forEach { layout.addView(it) }

    AlertDialog.Builder(this)
      .setTitle("Room inspection settings")
      .setView(layout)
      .setPositiveButton("Save") { _, _ ->
        settings.serviceUrl = url.text.toString().ifBlank { AppSettings.DEFAULT_URL }
        settings.showPlanes = planes.isChecked
        settings.showPointCloud = points.isChecked
        settings.showDepth = depthVis.isChecked
        settings.useOcclusion = occlusion.isChecked
        diagnostics.serviceUrl = settings.serviceUrl
        diagnostics.health = null
      }
      .setNegativeButton("Cancel", null)
      .show()
  }

  companion object {
    private const val TAG = "InspectActivity"
    private const val MENU_TEST_FRAME = 1
    private const val MENU_REPORT = 2
    private const val MENU_SAVE_LAYOUT = 3
    private const val MENU_CLEAR = 4
    private const val MENU_MEASURE = 5
    private const val MENU_MARKER = 6
    private const val MENU_RENTALS = 7
  }
}
