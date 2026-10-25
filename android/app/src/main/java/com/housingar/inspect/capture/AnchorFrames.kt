package com.housingar.inspect.capture

import kotlin.math.atan2
import kotlin.math.cos
import kotlin.math.sin
import kotlin.math.sqrt

/**
 * Orientation for selection anchors. The capture's seed box is axis-aligned in the anchor frame
 * (capture_v1 `bbox_anchor`), and the Mac treats depth inside that box as object. If the anchor is
 * tilted, the box's bottom face tilts into the supporting surface; if it is turned arbitrarily, the
 * box is loose around the object. So selection anchors are level (+Y = world up) and face the
 * camera (+Z horizontal, towards the viewer), which also makes the box follow the view direction.
 */
object AnchorFrames {
  class Frame(
    /** Quaternion x, y, z, w (ARCore Pose order). */
    val rotation: FloatArray,
    /** Column-major anchor → world. */
    val matrix: FloatArray,
  )

  fun gravityAligned(position: FloatArray, viewer: FloatArray): Frame {
    var dx = viewer[0] - position[0]
    var dz = viewer[2] - position[2]
    val len = sqrt(dx * dx + dz * dz)
    if (len < 1e-4f) { dx = 0f; dz = 1f } else { dx /= len; dz /= len }
    // Rotation about +Y by yaw maps +Z to (sin yaw, 0, cos yaw) = direction to the viewer.
    val yaw = atan2(dx, dz)
    val s = sin(yaw / 2f)
    val c = cos(yaw / 2f)
    val cy = cos(yaw)
    val sy = sin(yaw)
    val m = floatArrayOf(
      cy, 0f, -sy, 0f,
      0f, 1f, 0f, 0f,
      sy, 0f, cy, 0f,
      position[0], position[1], position[2], 1f,
    )
    return Frame(floatArrayOf(0f, s, 0f, c), m)
  }
}
