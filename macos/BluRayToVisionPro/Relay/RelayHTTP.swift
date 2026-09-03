import Foundation

enum RelayHTTPParseError: Error, Equatable, Sendable {
    case incomplete
    case malformedRequest
    case unsupportedTransferEncoding
    case headersTooLarge
    case bodyTooLarge
}

struct RelayHTTPParsingLimits: Sendable, Equatable {
    static let `default` = RelayHTTPParsingLimits()

    let maximumHeaderBytes: Int
    let maximumBodyBytes: Int
    let maximumHeaderCount: Int

    init(
        maximumHeaderBytes: Int = 16 * 1_024,
        maximumBodyBytes: Int = 1 * 1_024 * 1_024,
        maximumHeaderCount: Int = 32
    ) {
        self.maximumHeaderBytes = min(max(maximumHeaderBytes, 1_024), 64 * 1_024)
        self.maximumBodyBytes = min(max(maximumBodyBytes, 0), 16 * 1_024 * 1_024)
        self.maximumHeaderCount = min(max(maximumHeaderCount, 1), 128)
    }

    var maximumRequestBytes: Int {
        maximumHeaderBytes + maximumBodyBytes
    }
}

struct RelayHTTPRequest: Sendable, Equatable {
    let method: String
    let requestTarget: String
    let headers: [String: String]
    let body: Data

    func header(named name: String) -> String? {
        headers[name.lowercased()]
    }
}

struct RelayHTTPResponse: Sendable, Equatable {
    let statusCode: Int
    let headers: [String: String]
    let body: Data

    init(statusCode: Int, headers: [String: String] = [:], body: Data = Data()) {
        self.statusCode = statusCode
        self.headers = headers
        self.body = body
    }

    static func json<T: Encodable>(_ value: T, statusCode: Int = 200) -> RelayHTTPResponse {
        let encoder = JSONEncoder()
        encoder.outputFormatting = [.sortedKeys]
        let body = (try? encoder.encode(value)) ?? Data("{}".utf8)
        return RelayHTTPResponse(
            statusCode: statusCode,
            headers: ["content-type": "application/json; charset=utf-8"],
            body: body
        )
    }

    static func text(_ value: String, statusCode: Int = 200, contentType: String = "text/plain; charset=utf-8") -> RelayHTTPResponse {
        RelayHTTPResponse(statusCode: statusCode, headers: ["content-type": contentType], body: Data(value.utf8))
    }

    static func empty(statusCode: Int) -> RelayHTTPResponse {
        RelayHTTPResponse(statusCode: statusCode)
    }

    func serialized() -> Data {
        let reason = Self.reasonPhrase(for: statusCode)
        var renderedHeaders = headers
        renderedHeaders["content-length"] = String(body.count)
        renderedHeaders["connection"] = "close"
        renderedHeaders["cache-control"] = "no-store"

        var output = Data("HTTP/1.1 \(statusCode) \(reason)\r\n".utf8)
        for (name, value) in renderedHeaders.sorted(by: { $0.key < $1.key }) {
            output.append(Data("\(name): \(value)\r\n".utf8))
        }
        output.append(Data("\r\n".utf8))
        output.append(body)
        return output
    }

    private static func reasonPhrase(for statusCode: Int) -> String {
        switch statusCode {
        case 200: "OK"
        case 201: "Created"
        case 204: "No Content"
        case 400: "Bad Request"
        case 401: "Unauthorized"
        case 403: "Forbidden"
        case 404: "Not Found"
        case 405: "Method Not Allowed"
        case 409: "Conflict"
        case 410: "Gone"
        case 413: "Payload Too Large"
        case 431: "Request Header Fields Too Large"
        case 500: "Internal Server Error"
        case 503: "Service Unavailable"
        default: "Error"
        }
    }
}

enum RelayHTTPParser {
    static func parse(_ data: Data, limits: RelayHTTPParsingLimits) throws -> RelayHTTPRequest {
        guard data.count <= limits.maximumRequestBytes else {
            throw RelayHTTPParseError.bodyTooLarge
        }
        guard let headerRange = data.range(of: Data("\r\n\r\n".utf8)) else {
            if data.count > limits.maximumHeaderBytes {
                throw RelayHTTPParseError.headersTooLarge
            }
            throw RelayHTTPParseError.incomplete
        }
        guard headerRange.upperBound <= limits.maximumHeaderBytes else {
            throw RelayHTTPParseError.headersTooLarge
        }

        let headerData = data[..<headerRange.lowerBound]
        guard let headerText = String(data: headerData, encoding: .ascii) else {
            throw RelayHTTPParseError.malformedRequest
        }
        let lines = headerText.components(separatedBy: "\r\n")
        guard let requestLine = lines.first else {
            throw RelayHTTPParseError.malformedRequest
        }
        let requestLineParts = requestLine.split(separator: " ", omittingEmptySubsequences: false)
        guard requestLineParts.count == 3,
              !requestLineParts[0].isEmpty,
              !requestLineParts[1].isEmpty,
              requestLineParts[2] == "HTTP/1.1"
        else {
            throw RelayHTTPParseError.malformedRequest
        }

        var headers: [String: String] = [:]
        guard lines.count - 1 <= limits.maximumHeaderCount else {
            throw RelayHTTPParseError.headersTooLarge
        }
        for line in lines.dropFirst() {
            guard let separator = line.firstIndex(of: ":") else {
                throw RelayHTTPParseError.malformedRequest
            }
            let rawName = String(line[..<separator])
            let valueStart = line.index(after: separator)
            let rawValue = String(line[valueStart...]).trimmingCharacters(in: .whitespaces)
            let name = rawName.lowercased()
            guard isValidHeaderName(name), isValidHeaderValue(rawValue), headers[name] == nil else {
                throw RelayHTTPParseError.malformedRequest
            }
            headers[name] = rawValue
        }
        guard headers["transfer-encoding"] == nil else {
            throw RelayHTTPParseError.unsupportedTransferEncoding
        }

        let bodyLength: Int
        if let contentLength = headers["content-length"] {
            guard contentLength.allSatisfy(\.isNumber),
                  let parsedLength = Int(contentLength),
                  parsedLength <= limits.maximumBodyBytes
            else {
                throw RelayHTTPParseError.bodyTooLarge
            }
            bodyLength = parsedLength
        } else {
            bodyLength = 0
        }
        let bodyStart = headerRange.upperBound
        let availableBodyLength = data.count - bodyStart
        guard availableBodyLength >= bodyLength else {
            throw RelayHTTPParseError.incomplete
        }
        guard availableBodyLength == bodyLength else {
            throw RelayHTTPParseError.malformedRequest
        }

        return RelayHTTPRequest(
            method: String(requestLineParts[0]),
            requestTarget: String(requestLineParts[1]),
            headers: headers,
            body: Data(data[bodyStart ..< bodyStart + bodyLength])
        )
    }

    private static func isValidHeaderName(_ value: String) -> Bool {
        !value.isEmpty && value.utf8.allSatisfy { byte in
            (byte >= 65 && byte <= 90)
                || (byte >= 97 && byte <= 122)
                || (byte >= 48 && byte <= 57)
                || "!#$%&'*+-.^_`|~".utf8.contains(byte)
        }
    }

    private static func isValidHeaderValue(_ value: String) -> Bool {
        value.utf8.allSatisfy { $0 == 9 || ($0 >= 32 && $0 <= 126) }
    }
}
