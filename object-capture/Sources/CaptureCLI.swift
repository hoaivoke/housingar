import Foundation
import RealityKit
import ImageIO
import Darwin

enum CaptureError: Error, CustomStringConvertible {
    case invalid(String)
    var description: String {
        switch self { case .invalid(let message): return message }
    }
}

struct Options {
    var command = "help"
    var input: URL?
    var output: URL?
    var detail = "reduced"
    var sequential = false
    var objectMasking = false
    var timeout: Double = 600
    var measurements: [String: Double] = [:]

    static func parse(_ arguments: [String]) throws -> Options {
        var options = Options()
        guard let command = arguments.first else { return options }
        options.command = command
        guard ["help", "--help", "check", "validate", "reconstruct"].contains(command) else {
            throw CaptureError.invalid("Unknown command: \(command)")
        }
        var index = 1
        var seen = Set<String>()
        while index < arguments.count {
            let key = arguments[index]
            guard seen.insert(key).inserted else { throw CaptureError.invalid("Repeated option: \(key)") }
            if key == "--sequential" { options.sequential = true; index += 1; continue }
            if key == "--object-masking" { options.objectMasking = true; index += 1; continue }
            guard index + 1 < arguments.count else { throw CaptureError.invalid("Missing value for \(key)") }
            let value = arguments[index + 1]
            switch key {
            case "--input": options.input = URL(fileURLWithPath: value).standardizedFileURL
            case "--output": options.output = URL(fileURLWithPath: value).standardizedFileURL
            case "--detail":
                guard ["preview", "reduced", "medium", "full"].contains(value) else {
                    throw CaptureError.invalid("detail must be preview, reduced, medium or full")
                }
                options.detail = value
            case "--width-m", "--height-m", "--depth-m", "--timeout-seconds":
                guard let number = Double(value), number.isFinite, number > 0 else {
                    throw CaptureError.invalid("\(key) must be a positive finite number")
                }
                if key == "--timeout-seconds" {
                    guard number <= 86400 else { throw CaptureError.invalid("timeout must be at most 86400 seconds") }
                    options.timeout = number
                }
                else { options.measurements[String(key.dropFirst(2).dropLast(2))] = number }
            default: throw CaptureError.invalid("Unknown option: \(key)")
            }
            index += 2
        }
        if !options.measurements.isEmpty && options.measurements.count != 3 {
            throw CaptureError.invalid("Supply width, height and depth together, in meters")
        }
        if ["validate", "reconstruct"].contains(command) && options.input == nil {
            throw CaptureError.invalid("--input must point to a photo directory")
        }
        if command == "reconstruct" && options.output == nil {
            throw CaptureError.invalid("--output must specify a new .usdz file")
        }
        return options
    }

    var requestDetail: PhotogrammetrySession.Request.Detail {
        switch detail {
        case "preview": return .preview
        case "medium": return .medium
        case "full": return .full
        default: return .reduced
        }
    }
}

func jsonData(_ value: [String: Any]) throws -> Data {
    var data = try JSONSerialization.data(withJSONObject: value, options: [.sortedKeys])
    data.append(0x0a)
    return data
}

final class Events {
    private let file: FileHandle?
    init(url: URL? = nil) throws {
        if let url = url {
            guard FileManager.default.createFile(atPath: url.path, contents: nil) else {
                throw CaptureError.invalid("Cannot create events file: \(url.path)")
            }
            file = try FileHandle(forWritingTo: url)
        } else { file = nil }
    }
    deinit { try? file?.close() }
    func emit(_ event: String, _ fields: [String: Any] = [:]) throws {
        var payload = fields
        payload["event"] = event
        payload["timestamp"] = ISO8601DateFormatter().string(from: Date())
        let data = try jsonData(payload)
        FileHandle.standardOutput.write(data)
        try file?.write(contentsOf: data)
    }
}

func inspectPhotos(_ input: URL) throws -> [[String: Any]] {
    var isDirectory: ObjCBool = false
    guard FileManager.default.fileExists(atPath: input.path, isDirectory: &isDirectory), isDirectory.boolValue else {
        throw CaptureError.invalid("Input must be an existing photo directory")
    }
    let formats: Set<String> = ["jpg", "jpeg", "png", "heic", "heif", "tif", "tiff"]
    let urls = try FileManager.default.contentsOfDirectory(at: input, includingPropertiesForKeys: [.isRegularFileKey], options: .skipsHiddenFiles)
        .filter { formats.contains($0.pathExtension.lowercased()) }
        .sorted { $0.lastPathComponent < $1.lastPathComponent }
    guard urls.count >= 10 else {
        throw CaptureError.invalid("At least 10 overlapping photos are required; found \(urls.count). Aim for 30–100 views.")
    }
    var photos: [[String: Any]] = []
    for url in urls {
        guard try url.resourceValues(forKeys: [.isRegularFileKey]).isRegularFile == true,
              let source = CGImageSourceCreateWithURL(url as CFURL, nil),
              let properties = CGImageSourceCopyPropertiesAtIndex(source, 0, nil) as? [CFString: Any],
              let width = properties[kCGImagePropertyPixelWidth] as? Int,
              let height = properties[kCGImagePropertyPixelHeight] as? Int, width > 0, height > 0 else {
            throw CaptureError.invalid("Cannot read photo: \(url.lastPathComponent)")
        }
        photos.append(["file": url.lastPathComponent, "width": width, "height": height])
    }
    return photos
}

func canonicalDestination(_ url: URL) -> URL {
    // Resolve existing ancestors even when the final output directory is new.
    var existing = url
    var suffix: [String] = []
    while !FileManager.default.fileExists(atPath: existing.path) && existing.path != "/" {
        suffix.insert(existing.lastPathComponent, at: 0)
        existing.deleteLastPathComponent()
    }
    var result = existing.resolvingSymlinksInPath().standardizedFileURL
    for component in suffix { result.appendPathComponent(component) }
    return result
}

final class CancellationReason {
    private let lock = NSLock()
    private var value: String?
    func set(_ reason: String) { lock.lock(); if value == nil { value = reason }; lock.unlock() }
    func get() -> String? { lock.lock(); defer { lock.unlock() }; return value }
}

func reconstruct(_ options: Options, photos: [[String: Any]]) async throws {
    let input = options.input!
    let output = options.output!
    guard output.pathExtension.lowercased() == "usdz" else { throw CaptureError.invalid("Output must have the .usdz extension") }
    let base = output.deletingPathExtension()
    let manifest = base.appendingPathExtension("capture.json")
    let eventURL = base.appendingPathExtension("events.jsonl")
    for target in [output, manifest, eventURL] {
        guard !FileManager.default.fileExists(atPath: target.path) else {
            throw CaptureError.invalid("Refusing to overwrite existing file: \(target.path)")
        }
    }
    let parent = output.deletingLastPathComponent()
    let inputComponents = canonicalDestination(input).pathComponents
    let parentComponents = canonicalDestination(parent).pathComponents
    guard !parentComponents.starts(with: inputComponents) else {
        throw CaptureError.invalid("Write outputs outside the source photo directory")
    }
    guard PhotogrammetrySession.isSupported else { throw CaptureError.invalid("Object Capture is unsupported on this Mac") }
    try FileManager.default.createDirectory(at: parent, withIntermediateDirectories: true)
    let events = try Events(url: eventURL)
    let started = Date()
    var record: [String: Any] = [
        "schema_version": 1, "status": "processing", "representation": "captured_mesh",
        "generator": "RealityKit PhotogrammetrySession", "input_directory": input.path,
        "model_file": output.lastPathComponent, "format": "usdz", "detail": options.detail,
        "photo_count": photos.count, "photos": photos,
        "measured_dimensions_m": options.measurements,
        "scale_status": "unverified", "pivot_status": "unverified", "orientation_status": "unverified",
        "glb_conversion_status": "not_performed", "created_at": ISO8601DateFormatter().string(from: started)
    ]
    func save() throws { try jsonData(record).write(to: manifest, options: .atomic) }
    try save()
    try events.emit("started", ["photo_count": photos.count, "output": output.path, "detail": options.detail])
    var configuration = PhotogrammetrySession.Configuration()
    configuration.sampleOrdering = options.sequential ? .sequential : .unordered
    configuration.featureSensitivity = .normal
    configuration.isObjectMaskingEnabled = options.objectMasking
    do {
        let session = try PhotogrammetrySession(input: input, configuration: configuration)
        let cancellation = CancellationReason()
        let timer = DispatchWorkItem { cancellation.set("timeout"); session.cancel() }
        DispatchQueue.global().asyncAfter(deadline: .now() + options.timeout, execute: timer)
        signal(SIGINT, SIG_IGN)
        signal(SIGTERM, SIG_IGN)
        let sources = [SIGINT, SIGTERM].map { number -> DispatchSourceSignal in
            let source = DispatchSource.makeSignalSource(signal: number, queue: .global())
            source.setEventHandler { cancellation.set("interrupted"); session.cancel() }
            source.resume()
            return source
        }
        defer { timer.cancel(); sources.forEach { $0.cancel() }; signal(SIGINT, SIG_DFL); signal(SIGTERM, SIG_DFL) }
        var modelComplete = false
        var invalidSamples = 0
        var skippedSamples = 0
        var lastPercent = -1
        let outputs = session.outputs
        try session.process(requests: [.bounds, .modelFile(url: output, detail: options.requestDetail)])
        for try await event in outputs {
            switch event {
            case .inputComplete: try events.emit("input_complete")
            case .requestProgress(let request, let fraction):
                if case .modelFile = request {
                    let percent = Int(fraction * 100)
                    if percent != lastPercent {
                        lastPercent = percent
                        try events.emit("progress", ["fraction_complete": fraction])
                    }
                }
            case .requestComplete(_, let result):
                switch result {
                case .modelFile: modelComplete = true
                case .bounds(let bounds):
                    record["reconstruction_bounds"] = [
                        "min": [bounds.min.x, bounds.min.y, bounds.min.z],
                        "max": [bounds.max.x, bounds.max.y, bounds.max.z],
                        "units": "scale_unverified"
                    ]
                default: break
                }
            case .requestError(_, let error): session.cancel(); throw error
            case .invalidSample(let id, let reason):
                invalidSamples += 1
                try events.emit("invalid_sample", ["id": id, "reason": reason])
            case .skippedSample(let id): skippedSamples += 1; try events.emit("skipped_sample", ["id": id])
            case .automaticDownsampling: try events.emit("automatic_downsampling")
            case .processingCancelled: throw CaptureError.invalid("Reconstruction cancelled: \(cancellation.get() ?? "cancelled")")
            case .processingComplete:
                guard modelComplete, let size = try FileManager.default.attributesOfItem(atPath: output.path)[.size] as? NSNumber, size.int64Value > 0 else {
                    throw CaptureError.invalid("Processing finished without a nonempty model file")
                }
                record["status"] = "model_generated"
                record["elapsed_seconds"] = Date().timeIntervalSince(started)
                record["invalid_sample_count"] = invalidSamples
                record["skipped_sample_count"] = skippedSamples
                record["model_bytes"] = size.int64Value
                try save()
                try events.emit("complete", ["output": output.path, "manifest": manifest.path, "elapsed_seconds": Date().timeIntervalSince(started)])
                return
            default: break
            }
        }
        throw CaptureError.invalid("Reconstruction ended before processingComplete")
    } catch {
        record["status"] = "failed"
        record["error"] = String(describing: error)
        record["elapsed_seconds"] = Date().timeIntervalSince(started)
        try? save()
        try? events.emit("failed", ["error": String(describing: error)])
        throw error
    }
}

@main
struct CaptureCLI {
    static func main() async {
        do {
            let options = try Options.parse(Array(CommandLine.arguments.dropFirst()))
            switch options.command {
            case "help", "--help":
                print("""
                Housing AR Mac object reconstruction (macOS 13+)
                  housing-object-capture check
                  housing-object-capture validate --input PHOTO_FOLDER
                  housing-object-capture reconstruct --input PHOTO_FOLDER --output NEW_MODEL.usdz
                    [--detail preview|reduced|medium|full] [--sequential] [--object-masking]
                    [--width-m W --height-m H --depth-m D] [--timeout-seconds 600]
                Measurements are recorded; the mesh is not automatically rescaled or converted to GLB.
                """)
            case "check":
                try Events().emit("support", ["supported": PhotogrammetrySession.isSupported, "os": ProcessInfo.processInfo.operatingSystemVersionString])
                if !PhotogrammetrySession.isSupported { exit(1) }
            case "validate", "reconstruct":
                let photos = try inspectPhotos(options.input!)
                if options.command == "validate" {
                    try Events().emit("validated", ["photo_count": photos.count, "photos": photos,
                        "warnings": photos.count < 30 ? ["Fewer than 30 views; coverage and overlap still need inspection"] : [],
                        "reconstruction_attempted": false])
                } else { try await reconstruct(options, photos: photos) }
            default: break
            }
        } catch {
            let message: [String: Any] = ["event": "error", "error": String(describing: error)]
            if let data = try? jsonData(message) { FileHandle.standardError.write(data) }
            exit(1)
        }
    }
}
