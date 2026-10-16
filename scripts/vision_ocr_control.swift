// Native Swift control for #893: recognize the exact grayscale bitmaps that the
// PyObjC path handed to Vision, without PyObjC. A different outcome on the same
// bytes points at the bridge or request setup; the same outcome points at the
// OS/VM backend. This is a diagnostic, not a production OCR path.

import CoreGraphics
import CryptoKit
import Foundation
import Vision

struct Bitmap: Decodable {
    let file: String
    let width: Int
    let height: Int
    let bytesPerRow: Int
    let sha256: String
    let languages: [String]
    let usesLanguageCorrection: Bool
    let pythonSuccess: Bool?
    let pythonError: String?
    let pythonResults: [String]?
}

func emit(_ event: String, _ fields: [String: Any]) {
    var record = fields
    record["event"] = event
    let data = try! JSONSerialization.data(withJSONObject: record, options: [.sortedKeys])
    print(String(decoding: data, as: UTF8.self))
    fflush(stdout)
}

func image(from bytes: Data, _ bitmap: Bitmap) -> CGImage? {
    // Mirrors AppleVisionOcr._cg_image: 8-bit DeviceGray, no alpha.
    guard let provider = CGDataProvider(data: bytes as CFData) else { return nil }
    return CGImage(
        width: bitmap.width,
        height: bitmap.height,
        bitsPerComponent: 8,
        bitsPerPixel: 8,
        bytesPerRow: bitmap.bytesPerRow,
        space: CGColorSpaceCreateDeviceGray(),
        bitmapInfo: CGBitmapInfo(rawValue: CGImageAlphaInfo.none.rawValue),
        provider: provider,
        decode: nil,
        shouldInterpolate: false,
        intent: .defaultIntent
    )
}

guard CommandLine.arguments.count == 2 else {
    FileHandle.standardError.write(Data("usage: vision_ocr_control BITMAP-DIRECTORY\n".utf8))
    exit(2)
}
let directory = URL(fileURLWithPath: CommandLine.arguments[1])
let decoder = JSONDecoder()
decoder.keyDecodingStrategy = .convertFromSnakeCase
let bitmaps = try decoder.decode(
    [Bitmap].self, from: Data(contentsOf: directory.appendingPathComponent("manifest.json"))
)
emit("environment", [
    "macos": ProcessInfo.processInfo.operatingSystemVersionString,
    "bitmaps": bitmaps.count,
])

var recognized = 0
for bitmap in bitmaps {
    let bytes = try Data(contentsOf: directory.appendingPathComponent(bitmap.file))
    let digest = SHA256.hash(data: bytes).map { String(format: "%02x", $0) }.joined()
    guard digest == bitmap.sha256, let cgImage = image(from: bytes, bitmap) else {
        emit("bitmap_invalid", ["file": bitmap.file, "sha256": digest, "expected_sha256": bitmap.sha256])
        exit(1)
    }
    let request = VNRecognizeTextRequest()
    request.recognitionLevel = .accurate
    request.usesLanguageCorrection = bitmap.usesLanguageCorrection
    if !bitmap.languages.isEmpty {
        request.recognitionLanguages = bitmap.languages
    }
    var fields: [String: Any] = [
        "file": bitmap.file,
        "sha256": digest,
        "revision": request.revision,
        "languages": request.recognitionLanguages,
        "python_success": bitmap.pythonSuccess ?? NSNull(),
        "python_error": bitmap.pythonError ?? NSNull(),
        "python_results": bitmap.pythonResults ?? NSNull(),
    ]
    do {
        // A false return without NSError surfaces here as a thrown nil error.
        try VNImageRequestHandler(cgImage: cgImage, options: [:]).perform([request])
        let text = (request.results ?? []).compactMap { $0.topCandidates(1).first?.string }
        fields["success"] = true
        fields["results"] = text
        if !text.isEmpty { recognized += 1 }
    } catch {
        let nsError = error as NSError
        fields["success"] = false
        fields["error"] = String(describing: error)
        fields["error_domain"] = nsError.domain
        fields["error_code"] = nsError.code
    }
    emit("response", fields)
}

let passed = !bitmaps.isEmpty && recognized == bitmaps.count
emit("qualification", ["passed": passed, "bitmaps": bitmaps.count, "recognized": recognized])
exit(passed ? 0 : 1)
