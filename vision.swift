import Foundation
import ImageIO
import Vision

struct TextLine: Encodable {
    let text: String
    let confidence: Float
}

struct Reply: Encodable {
    let lines: [TextLine]?
    let error: String?
}

func makeRequest() -> VNRecognizeTextRequest {
    let request = VNRecognizeTextRequest()
    request.recognitionLevel = .accurate
    request.usesLanguageCorrection = false
    if let supported = try? request.supportedRecognitionLanguages() {
        let chosen = ["en-US", "zh-Hans", "zh-Hant", "fr-FR", "de-DE"].filter { supported.contains($0) }
        if !chosen.isEmpty { request.recognitionLanguages = chosen }
    }
    return request
}

func recognize(_ file: URL, request: VNRecognizeTextRequest) throws -> [TextLine] {
    let options = [kCGImageSourceShouldCache: false] as CFDictionary
    let thumbnailOptions = [
        kCGImageSourceCreateThumbnailFromImageAlways: true,
        kCGImageSourceCreateThumbnailWithTransform: true,
        kCGImageSourceThumbnailMaxPixelSize: 2800
    ] as CFDictionary
    guard let source = CGImageSourceCreateWithURL(file as CFURL, options),
          let image = CGImageSourceCreateThumbnailAtIndex(source, 0, thumbnailOptions) else {
        throw CocoaError(.fileReadCorruptFile)
    }
    try VNImageRequestHandler(cgImage: image).perform([request])
    return (request.results ?? []).compactMap { observation in
        guard let text = observation.topCandidates(1).first else { return nil }
        return TextLine(text: text.string, confidence: text.confidence)
    }
}

func emit<Value: Encodable>(_ value: Value) throws {
    var bytes = try JSONEncoder().encode(value)
    bytes.append(10)
    try FileHandle.standardOutput.write(contentsOf: bytes)
}

do {
    guard CommandLine.arguments.count == 2 else { exit(2) }
    let argument = CommandLine.arguments[1]
    let request = makeRequest()
    if argument == "--stdin" {
        while let command = readLine() {
            do {
                let path = try JSONDecoder().decode(String.self, from: Data(command.utf8))
                let lines = try recognize(URL(fileURLWithPath: path), request: request)
                try emit(Reply(lines: lines, error: nil))
            } catch {
                try emit(Reply(lines: nil, error: "Vision could not read the image"))
            }
        }
    } else {
        try emit(recognize(URL(fileURLWithPath: argument), request: request))
    }
} catch {
    FileHandle.standardError.write(Data("Vision could not read the image\n".utf8))
    exit(1)
}
