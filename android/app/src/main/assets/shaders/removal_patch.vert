#version 300 es
// Background patch for 3D removal, in the selection anchor's frame.
uniform mat4 u_ModelViewProjection;
uniform mat4 u_ModelView;
layout(location = 0) in vec3 a_Position;
layout(location = 1) in vec2 a_TexCoord;
out vec2 v_TexCoord;
out float v_ViewDepthM;

void main() {
  v_TexCoord = a_TexCoord;
  v_ViewDepthM = -(u_ModelView * vec4(a_Position, 1.0)).z;
  gl_Position = u_ModelViewProjection * vec4(a_Position, 1.0);
}
