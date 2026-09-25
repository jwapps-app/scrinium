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
import os
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

private let env = ProcessInfo.processInfo.environment

// MARK: - Bounded Vision executor
//
// Vision `perform` is synchronous and blocks its thread for the whole
// recognition. If every connection ran it on the shared global pool, a burst
// of concurrent requests (one OCR client fanning out × several documents at
// once) would exhaust libdispatch's threads and wedge the listener — the
// server would stop answering even /health. So all recognition goes through
// a dedicated concurrent queue capped by a semaphore: at most VISION_LIMIT
// run at once, the rest queue, and the listener's own queue stays free to
// keep accepting and reading requests no matter how hard clients push.
//
// The queue itself is bounded too. A request waiting for a slot holds its
// page image, a thread and an open socket, so the semaphore alone only
// limited what was running, not what a flood could make the process retain.
// Past `maxWaiting` requests in line, /ocr answers 503 at once instead.
enum Vision {
    static let limit = max(1, Int(env["OCR_HELPER_MAX_CONCURRENCY"] ?? "4") ?? 4)
    static let maxWaiting = max(1, Int(env["OCR_HELPER_MAX_WAITING"] ?? "") ?? (2 * limit + 4))
    static let queue = DispatchQueue(label: "ocr.vision", attributes: .concurrent)
    static let slots = DispatchSemaphore(value: limit)
    private static let waiting = OSAllocatedUnfairLock(initialState: 0)

    /// Claim a place in line for a recognition slot; false when the line is full.
    static func join() -> Bool {
        waiting.withLock { count in
            guard count < maxWaiting else { return false }
            count += 1
            return true
        }
    }

    static func leave() {
        waiting.withLock { $0 -= 1 }
    }
}

// MARK: - Minimal HTTP/1.1 handling

enum Limits {
    static let headerBytes = 16 * 1024
    // A 300-DPI page image is a few megabytes; nothing legitimate comes
    // anywhere near this, and it is what bounds the per-connection buffer.
    static let bodyBytes = 64 * 1024 * 1024
    // A connection that has not delivered a complete request by then is
    // closed, so a peer that connects and trickles (or sends nothing) cannot
    // hold a buffer and socket indefinitely.
    static let readTimeout = max(1.0, Double(env["OCR_HELPER_READ_TIMEOUT_SECONDS"] ?? "") ?? 30)
}

private struct BadRequest: Error {
    let status: String
    let message: String
}

final class HTTPConnection {
    private let connection: NWConnection
    // Each connection gets its own serial queue for I/O, so connections
    // never block one another; heavy Vision work is handed off to the
    // bounded executor above. Everything below runs on this queue.
    private let queue = DispatchQueue(label: "ocr.conn")
    private var buffer = Data()
    private var headerEnd: Int?
    private var contentLength = 0
    private var method = ""
    private var path = ""
    private var deadline: DispatchWorkItem?

    init(_ connection: NWConnection) {
        self.connection = connection
    }

    func start() {
        connection.start(queue: queue)
        let timeout = DispatchWorkItem { [weak self] in
            self?.respond(status: "408 Request Timeout",
                          json: ["error": "request not completed in time"])
        }
        deadline = timeout
        queue.asyncAfter(deadline: .now() + Limits.readTimeout, execute: timeout)
        receive()
    }

    private func receive() {
        // Strong self: the pending receive callback is what keeps this
        // handler alive; it drops out of existence after cancel().
        connection.receive(minimumIncompleteLength: 1, maximumLength: 1 << 22) {
            data, _, isComplete, error in
            if let data { self.buffer.append(data) }
            if error != nil {
                self.close()
                return
            }
            if self.tryHandle() { return }
            if isComplete {
                self.close()
                return
            }
            self.receive()
        }
    }

    private func close() {
        deadline?.cancel()
        connection.cancel()
    }

    /// Returns true once a full request has been handled — or refused.
    private func tryHandle() -> Bool {
        if headerEnd == nil {
            guard let range = buffer.range(of: Data("\r\n\r\n".utf8)) else {
                if buffer.count > Limits.headerBytes {
                    return reject("431 Request Header Fields Too Large",
                                  "request headers exceed \(Limits.headerBytes) bytes")
                }
                return false
            }
            if range.lowerBound > Limits.headerBytes {
                return reject("431 Request Header Fields Too Large",
                              "request headers exceed \(Limits.headerBytes) bytes")
            }
            do {
                try parseHead(buffer[..<range.lowerBound])
            } catch let bad as BadRequest {
                return reject(bad.status, bad.message)
            } catch {
                return reject("400 Bad Request", "malformed request")
            }
            headerEnd = range.upperBound
        }
        guard let headerEnd else { return false }
        // Both operands are capped above, so this cannot overflow; checking
        // anyway is what keeps that true if someone raises a cap later.
        let (bodyEnd, overflow) = headerEnd.addingReportingOverflow(contentLength)
        if overflow {
            return reject("400 Bad Request", "invalid content-length")
        }
        guard buffer.count >= bodyEnd else { return false }

        deadline?.cancel()
        route(body: buffer.subdata(in: headerEnd..<bodyEnd))
        return true
    }

    private func parseHead(_ head: Data) throws {
        let lines = String(decoding: head, as: UTF8.self)
            .split(separator: "\r\n", omittingEmptySubsequences: false)
        let requestParts = (lines.first ?? "").split(separator: " ")
        method = requestParts.count > 0 ? String(requestParts[0]) : ""
        path = requestParts.count > 1 ? String(requestParts[1]) : ""

        var declared: Int?
        for line in lines.dropFirst() {
            guard let colon = line.firstIndex(of: ":") else { continue }
            let name = line[..<colon].trimmingCharacters(in: .whitespaces).lowercased()
            let value = line[line.index(after: colon)...].trimmingCharacters(in: .whitespaces)
            switch name {
            case "content-length":
                // Digits only. Int("-1") parses happily, and a negative length
                // built a reversed range below that trapped and took the whole
                // helper down — for any request, /health included.
                guard !value.isEmpty, value.allSatisfy({ $0.isASCII && $0.isNumber }) else {
                    throw BadRequest(status: "400 Bad Request", message: "invalid content-length")
                }
                guard let length = Int(value), length <= Limits.bodyBytes else {
                    throw BadRequest(status: "413 Content Too Large",
                                     message: "body exceeds \(Limits.bodyBytes) bytes")
                }
                // Repeats that agree are allowed (RFC 9110); two different
                // lengths on one request is smuggling or confusion, not a body.
                if let declared, declared != length {
                    throw BadRequest(status: "400 Bad Request", message: "conflicting content-length")
                }
                declared = length
            case "transfer-encoding":
                throw BadRequest(status: "400 Bad Request",
                                 message: "transfer-encoding is not supported; send content-length")
            default:
                break
            }
        }
        contentLength = declared ?? 0
    }

    private func reject(_ status: String, _ message: String) -> Bool {
        deadline?.cancel()
        respond(status: status, json: ["error": message])
        return true
    }

    private func route(body: Data) {
        switch (method, path) {
        case ("GET", "/health"):
            respond(status: "200 OK", json: HealthResponse(status: "ok", engine: "apple-vision"))
        case ("POST", "/ocr"):
            guard Vision.join() else {
                respond(status: "503 Service Unavailable",
                        json: ["error": "too many requests waiting for recognition"],
                        extraHeaders: ["Retry-After: 1"])
                return
            }
            // Off the connection's I/O queue and onto the bounded Vision
            // executor, so recognition load can never starve the listener.
            Vision.queue.async {
                Vision.slots.wait()
                Vision.leave()
                defer { Vision.slots.signal() }
                do {
                    self.respond(status: "200 OK", json: try recognize(imageData: body))
                } catch {
                    self.respond(status: "422 Unprocessable Entity",
                                 json: ["error": error.localizedDescription])
                }
            }
        default:
            respond(status: "404 Not Found", json: ["error": "not found"])
        }
    }

    private func respond<T: Encodable>(status: String, json: T, extraHeaders: [String] = []) {
        let body = (try? JSONEncoder().encode(json)) ?? Data("{}".utf8)
        var head = "HTTP/1.1 \(status)\r\n"
        head += "Content-Type: application/json\r\n"
        head += "Content-Length: \(body.count)\r\n"
        for header in extraHeaders {
            head += "\(header)\r\n"
        }
        head += "Connection: close\r\n\r\n"
        var response = Data(head.utf8)
        response.append(body)
        connection.send(content: response, completion: .contentProcessed { _ in
            self.connection.cancel()
        })
    }
}

// MARK: - Server

let port = UInt16(env["OCR_HELPER_PORT"] ?? "9876") ?? 9876

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
        print("vision ocr helper listening on port \(port) "
              + "(\(Vision.limit) recognitions at once, \(Vision.maxWaiting) waiting)")
    }
    if case .failed(let error) = state {
        FileHandle.standardError.write(Data("listener failed: \(error)\n".utf8))
        exit(1)
    }
}

listener.start(queue: .main)
dispatchMain()
