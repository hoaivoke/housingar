package com.housingar.inspect.geometry

import org.junit.Assert.assertEquals
import org.junit.Test

class ImageRotationTest {
  @Test
  fun roundTripsForAllRightAngles() {
    for (deg in intArrayOf(0, 90, 180, 270, -90, 450)) {
      val r = ImageRotation.toRotated(123f, 45f, 640, 480, deg)
      val back = ImageRotation.fromRotated(r[0], r[1], 640, 480, deg)
      assertEquals("deg $deg x", 123f, back[0], 1e-4f)
      assertEquals("deg $deg y", 45f, back[1], 1e-4f)
    }
  }

  @Test
  fun ninetyDegreesMatchesClockwiseDisplayRotation() {
    // Sensor top-left corner ends up at the top-right of a portrait (480 × 640) bitmap.
    val r = ImageRotation.toRotated(0f, 0f, 640, 480, 90)
    assertEquals(480f, r[0], 1e-4f)
    assertEquals(0f, r[1], 1e-4f)
    // Sensor top-right ends at bottom-right.
    val r2 = ImageRotation.toRotated(640f, 0f, 640, 480, 90)
    assertEquals(480f, r2[0], 1e-4f)
    assertEquals(640f, r2[1], 1e-4f)
  }
}
