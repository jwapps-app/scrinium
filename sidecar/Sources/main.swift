// Vision OCR sidecar: a small HTTP server bridging the container to macOS's
// Vision framework, which cannot run inside Linux Docker.
//
//   GET  /health -> 200 {"status":"ok"}
//   POST /ocr    -> body is a page image (PNG/JPEG); returns recognized text
//                   blocks with normalized bounding boxes (Vision coordinates:
//                   origin bottom-left, 0-1) and confidences.
//
// Port comes from OCR_HELPER_PORT (default 9876). Run under launchd for
// start-on-login; see the README.

import Foundation
import ImageIO
import Network
import Vision

struct OCRBlock: Codable {
    let text: String
    let confidence: Float
    let bbox: [Double]
}

struct OCRResponse: Codable {
    let width: Int
    let height: Int
    let blocks: [OCRBlock]
}

struct HealthResponse: Codable {
    let status: String
    let engine: String
}

func recognize(imageData: Data) throws -> OCRResponse {
    guard let source = CGImageSourceCreateWithData(imageData as CFData, nil),
          let image = CGImageSourceCreateImageAtIndex(source, 0, nil)
    else {
        throw NSError(
            domain: "ocr", code: 1,
            userInfo: [NSLocalizedDescriptionKey: "could not decode image"])
    }

    let request = VNRecognizeTextRequest()
    request.recognitionLevel = .accurate
    request.usesLanguageCorrection = true
    if let langs = ProcessInfo.processInfo.environment["OCR_HELPER_LANGUAGES"] {
        request.recognitionLanguages = langs.split(separator: ",").map(String.init)
    }

    let handler = VNImageRequestHandler(cgImage: image, options: [:])
    try handler.perform([request])

    let blocks = (request.results ?? []).compactMap { observation -> OCRBlock? in
        guard let candidate = observation.topCandidates(1).first else { return nil }
        let box = observation.boundingBox  // normalized, bottom-left origin
        return OCRBlock(
            text: candidate.string,
            confidence: candidate.confidence,
            bbox: [box.minX, box.minY, box.maxX, box.maxY]
        )
    }
    return OCRResponse(width: image.width, height: image.height, blocks: blocks)
}

// MARK: - Minimal HTTP/1.1 handling

final class HTTPConnection {
    private let connection: NWConnection
    private var buffer = Data()
    private var headerEnd: Int?
    private var contentLength = 0
    private var method = ""
    private var path = ""

    init(_ connection: NWConnection) {
        self.connection = connection
    }

    func start() {
        connection.start(queue: .global(qos: .userInitiated))
        receive()
    }

    private func receive() {
        // Strong self: the pending receive callback is what keeps this
        // handler alive; it drops out of existence after cancel().
        connection.receive(minimumIncompleteLength: 1, maximumLength: 1 << 22) {
            data, _, isComplete, error in
            if let data { self.buffer.append(data) }
            if error != nil {
                self.connection.cancel()
                return
            }
            if self.tryHandle() { return }
            if isComplete {
                self.connection.cancel()
                return
            }
            self.receive()
        }
    }

    /// Returns true once a full request has been handled.
    private func tryHandle() -> Bool {
        if headerEnd == nil {
            guard let range = buffer.range(of: Data("\r\n\r\n".utf8)) else {
                return false
            }
            headerEnd = range.upperBound
            let head = String(decoding: buffer[..<range.lowerBound], as: UTF8.self)
            let lines = head.split(separator: "\r\n", omittingEmptySubsequences: false)
            let requestParts = (lines.first ?? "").split(separator: " ")
            method = requestParts.count > 0 ? String(requestParts[0]) : ""
            path = requestParts.count > 1 ? String(requestParts[1]) : ""
            for line in lines.dropFirst() {
                let pair = line.split(separator: ":", maxSplits: 1)
                if pair.count == 2,
                   pair[0].trimmingCharacters(in: .whitespaces).lowercased()
                       == "content-length" {
                    contentLength = Int(pair[1].trimmingCharacters(in: .whitespaces)) ?? 0
                }
            }
        }
        guard let headerEnd else { return false }
        guard buffer.count - headerEnd >= contentLength else { return false }

        let body = buffer.subdata(in: headerEnd..<(headerEnd + contentLength))
        route(body: body)
        return true
    }

    private func route(body: Data) {
        switch (method, path) {
        case ("GET", "/health"):
            respond(status: "200 OK", json: HealthResponse(status: "ok", engine: "apple-vision"))
        case ("POST", "/ocr"):
            do {
                respond(status: "200 OK", json: try recognize(imageData: body))
            } catch {
                respond(status: "422 Unprocessable Entity",
                        json: ["error": error.localizedDescription])
            }
        default:
            respond(status: "404 Not Found", json: ["error": "not found"])
        }
    }

    private func respond<T: Encodable>(status: String, json: T) {
        let body = (try? JSONEncoder().encode(json)) ?? Data("{}".utf8)
        var head = "HTTP/1.1 \(status)\r\n"
        head += "Content-Type: application/json\r\n"
        head += "Content-Length: \(body.count)\r\n"
        head += "Connection: close\r\n\r\n"
        var response = Data(head.utf8)
        response.append(body)
        connection.send(content: response, completion: .contentProcessed { _ in
            self.connection.cancel()
        })
    }
}

// MARK: - Server

let port = UInt16(ProcessInfo.processInfo.environment["OCR_HELPER_PORT"] ?? "9876") ?? 9876

let listener: NWListener
do {
    listener = try NWListener(using: .tcp, on: NWEndpoint.Port(rawValue: port)!)
} catch {
    FileHandle.standardError.write(Data("failed to bind port \(port): \(error)\n".utf8))
    exit(1)
}

listener.newConnectionHandler = { connection in
    HTTPConnection(connection).start()
}
listener.stateUpdateHandler = { state in
    if case .ready = state {
        print("vision ocr helper listening on port \(port)")
    }
    if case .failed(let error) = state {
        FileHandle.standardError.write(Data("listener failed: \(error)\n".utf8))
        exit(1)
    }
}

listener.start(queue: .main)
dispatchMain()
