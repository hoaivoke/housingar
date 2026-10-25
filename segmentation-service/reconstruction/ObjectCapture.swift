import Foundation
import RealityKit
import CoreImage
import CoreVideo
import CoreMotion
import ModelIO
import Darwin

enum Failure: Error { case message(String) }

func writeJSON(_ value: [String: Any], to url: URL? = nil) throws {
    var data = try JSONSerialization.data(withJSONObject: value, options: [.sortedKeys])
    data.append(10)
    if let url = url { try data.write(to: url, options: .atomic) }
    else { FileHandle.standardOutput.write(data) }
}

func matrixValues(_ matrix: simd_float4x4) -> [Float] {
    return (0..<4).flatMap { column in (0..<4).map { row in matrix[column][row] } }
}

func pixelBuffer(_ url: URL, mask: Bool, context: CIContext) throws -> CVPixelBuffer {
    guard let image = CIImage(contentsOf: url, options: [.applyOrientationProperty: false]) else {
        throw Failure.message("Cannot decode \(url.path)")
    }
    var buffer: CVPixelBuffer?
    let format = mask ? kCVPixelFormatType_OneComponent8 : kCVPixelFormatType_32BGRA
    let status = CVPixelBufferCreate(kCFAllocatorDefault, Int(image.extent.width), Int(image.extent.height), format,
                                   [kCVPixelBufferIOSurfacePropertiesKey: [:]] as CFDictionary, &buffer)
    guard status == kCVReturnSuccess, let buffer = buffer else { throw Failure.message("Cannot allocate image buffer") }
    context.render(image, to: buffer, bounds: image.extent, colorSpace: mask ? CGColorSpaceCreateDeviceGray() : CGColorSpaceCreateDeviceRGB())
    return buffer
}

@main
struct CaptureCommand {
    static func main() async {
        do {
            if CommandLine.arguments.contains("--check") {
                try writeJSON(["supported": PhotogrammetrySession.isSupported])
                return
            }
            let args = CommandLine.arguments
            guard args.count == 4 else { throw Failure.message("usage: object-capture MANIFEST MASKS_DIRECTORY OUTPUT_DIRECTORY") }
            guard PhotogrammetrySession.isSupported else { throw Failure.message("PhotogrammetrySession.isSupported is false") }
            let manifestURL = URL(fileURLWithPath: args[1])
            let input = manifestURL.deletingLastPathComponent()
            let masks = URL(fileURLWithPath: args[2])
            let output = URL(fileURLWithPath: args[3])
            try FileManager.default.createDirectory(at: output, withIntermediateDirectories: true)
            let manifest = try JSONSerialization.jsonObject(with: Data(contentsOf: manifestURL)) as! [String: Any]
            let frames = manifest["frames"] as! [[String: Any]]
            let anchorValues = manifest["anchor_to_world"] as! [Double]
            let anchorRotation = simd_float3x3(columns: (SIMD3<Float>(Float(anchorValues[0]), Float(anchorValues[1]), Float(anchorValues[2])),
                                                        SIMD3<Float>(Float(anchorValues[4]), Float(anchorValues[5]), Float(anchorValues[6])),
                                                        SIMD3<Float>(Float(anchorValues[8]), Float(anchorValues[9]), Float(anchorValues[10]))))
            let gravityAnchor = anchorRotation.transpose * SIMD3<Float>(0, -1, 0)
            let context = CIContext(options: [.useSoftwareRenderer: false])
            var samples: [PhotogrammetrySample] = []
            for frame in frames {
                let id = frame["index"] as! Int
                let image = try pixelBuffer(input.appendingPathComponent(frame["image"] as! String), mask: false, context: context)
                var sample = PhotogrammetrySample(id: id, image: image)
                sample.objectMask = try pixelBuffer(masks.appendingPathComponent(String(format: "%04d.png", id)), mask: true, context: context)
                let pose = frame["camera_to_anchor"] as! [Double]
                let rotation = simd_float3x3(columns: (SIMD3<Float>(Float(pose[0]), Float(pose[1]), Float(pose[2])),
                                                      SIMD3<Float>(Float(pose[4]), Float(pose[5]), Float(pose[6])),
                                                      SIMD3<Float>(Float(pose[8]), Float(pose[9]), Float(pose[10]))))
                let gravity = rotation.transpose * gravityAnchor
                sample.gravity = CMAcceleration(x: Double(gravity.x), y: Double(gravity.y), z: Double(gravity.z))
                samples.append(sample)
            }
            var configuration = PhotogrammetrySession.Configuration()
            configuration.sampleOrdering = .sequential
            configuration.isObjectMaskingEnabled = true // Required even when explicit masks are supplied.
            configuration.featureSensitivity = .high
            let session = try PhotogrammetrySession(input: samples, configuration: configuration)
            let modelURL = output.appendingPathComponent("object-capture.usdz")
            let outputs = session.outputs
            var poses: [String: [Float]] = [:]
            var intrinsics: [String: [Float]] = [:]
            var completedModel = false
            try session.process(requests: [.poses, .modelFile(url: modelURL, detail: .medium)])
            for try await event in outputs {
                switch event {
                case .requestProgress(_, let fraction): try writeJSON(["event": "progress", "fraction": fraction])
                case .invalidSample(let id, let reason): try writeJSON(["event": "invalid_sample", "id": id, "reason": reason])
                case .skippedSample(let id): try writeJSON(["event": "skipped_sample", "id": id])
                case .requestError(_, let error): session.cancel(); throw error
                case .requestComplete(_, let result):
                    switch result {
                    case .poses(let reconstructed):
                        for (id, pose) in reconstructed.posesBySample {
                            poses[String(id)] = matrixValues(pose.transform.matrix)
                            if #available(macOS 26.0, *), let k = pose.intrinsics {
                                intrinsics[String(id)] = (0..<3).flatMap { column in (0..<3).map { row in k[column][row] } }
                            }
                        }
                    case .modelFile: completedModel = true
                    default: break
                    }
                case .processingComplete:
                    guard completedModel && !poses.isEmpty else { throw Failure.message("Model or camera poses missing") }
                    let objURL = output.appendingPathComponent("object-capture.obj")
                    guard MDLAsset.canExportFileExtension("obj") else { throw Failure.message("ModelIO OBJ export unavailable") }
                    try MDLAsset(url: modelURL).export(to: objURL)
                    try writeJSON(["status": "done", "poses": poses, "intrinsics": intrinsics, "mesh": objURL.path,
                                   "source_model": modelURL.path, "sample_count": samples.count], to: output.appendingPathComponent("result.json"))
                    try writeJSON(["event": "done"])
                    return
                case .processingCancelled: throw Failure.message("Object Capture cancelled")
                default: break
                }
            }
            throw Failure.message("Object Capture ended before completion")
        } catch {
            try? writeJSON(["event": "error", "error": String(describing: error)])
            exit(1)
        }
    }
}
