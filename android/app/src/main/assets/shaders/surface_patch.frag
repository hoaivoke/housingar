#version 300 es
precision mediump float;
uniform vec4 u_Color; // rgb + alpha
layout(location = 0) out vec4 o_FragColor;

void main() {
  // Premultiplied output; BackgroundRenderer composites with (ONE, ONE_MINUS_SRC_ALPHA).
  o_FragColor = vec4(u_Color.rgb * u_Color.a, u_Color.a);
}
