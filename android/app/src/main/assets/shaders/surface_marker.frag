#version 300 es
// Translucent fill, solid border, and a centre crosshair used to line the marker up with a
// physical reference mark (for example a taped "X") during the pan-and-return drift test.
precision mediump float;

uniform vec4 u_Color;      // rgb + base fill alpha
uniform float u_Border;    // border width in UV units
uniform float u_Line;      // crosshair half-width in UV units

in vec2 v_Uv;
layout(location = 0) out vec4 o_FragColor;

void main() {
  vec2 d = abs(v_Uv - 0.5);
  float edge = 0.5 - max(d.x, d.y);
  float a = u_Color.a;
  if (edge < u_Border) {
    a = 0.95;
  }
  bool onCross = (d.x < u_Line || d.y < u_Line) && max(d.x, d.y) < 0.18;
  if (onCross) {
    a = 1.0;
  }
  vec3 rgb = onCross ? vec3(1.0) : u_Color.rgb;
  // Premultiplied output; BackgroundRenderer composites with (ONE, ONE_MINUS_SRC_ALPHA).
  o_FragColor = vec4(rgb * a, a);
}
