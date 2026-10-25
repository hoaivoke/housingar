#version 300 es
// Selected-surface patch, defined in its anchor's local coordinates.
uniform mat4 u_ModelViewProjection;
layout(location = 0) in vec3 a_Position;

void main() {
  gl_Position = u_ModelViewProjection * vec4(a_Position, 1.0);
}
