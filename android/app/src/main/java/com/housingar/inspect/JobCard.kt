package com.housingar.inspect

import android.view.View
import android.widget.Button
import android.widget.ProgressBar
import android.widget.TextView

/**
 * The progress card for Mac jobs (scan → proposals, single object → mesh). Whoever shows it last
 * owns its buttons. Callable from any thread.
 */
class JobCard(private val activity: InspectActivity) {
  private val panel: View = activity.findViewById(R.id.job_panel)
  private val status: TextView = activity.findViewById(R.id.job_status)
  private val progress: ProgressBar = activity.findViewById(R.id.job_progress)
  private val retry: Button = activity.findViewById(R.id.job_retry)
  private val cancel: Button = activity.findViewById(R.id.job_cancel)

  @Volatile var busy = false
    private set

  fun show(text: String, fraction: Float, onCancel: () -> Unit) = activity.runOnUiThread {
    busy = true
    panel.visibility = View.VISIBLE
    status.text = text
    progress.visibility = View.VISIBLE
    progress.progress = (fraction.coerceIn(0f, 1f) * 1000).toInt()
    retry.visibility = View.GONE
    cancel.text = activity.getString(R.string.job_cancel)
    cancel.setOnClickListener { onCancel() }
  }

  /** A failure the user can retry (or close). The capture/scan stays saved on the phone. */
  fun fail(text: String, onRetry: (() -> Unit)?, onClose: () -> Unit = {}) = activity.runOnUiThread {
    busy = false
    panel.visibility = View.VISIBLE
    status.text = text
    progress.visibility = View.GONE
    retry.visibility = if (onRetry != null) View.VISIBLE else View.GONE
    retry.setOnClickListener {
      busy = true
      onRetry?.invoke()
    }
    cancel.text = activity.getString(R.string.mesh_close)
    cancel.setOnClickListener {
      panel.visibility = View.GONE
      onClose()
    }
  }

  fun hide(message: String?) = activity.runOnUiThread {
    busy = false
    panel.visibility = View.GONE
    if (message != null) activity.showBanner(message)
  }

  companion object {
    /** Human-readable stage names for both job kinds (the Mac's own message is preferred). */
    val STAGES = mapOf(
      "queued" to "waiting in the queue",
      "validating" to "checking the upload",
      "masking" to "finding the object in every frame",
      "reconstructing_object" to "building the object mesh",
      "reconstructing_background" to "building the surroundings",
      "fusing_scene" to "combining the scan into one 3D scene",
      "finding_objects" to "finding separate objects",
      "segmenting_objects" to "outlining each object",
      "meshing_objects" to "building object meshes",
      "texturing" to "texturing",
      "exporting" to "exporting",
    )
  }
}
