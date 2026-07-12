import Foundation

enum JSONLFramingError: Error, LocalizedError, Equatable {
    case lineTooLarge
    case incompleteLine

    var errorDescription: String? {
        switch self {
        case .lineTooLarge:
            return "The worker sent an event larger than the protocol limit."
        case .incompleteLine:
            return "The worker stopped in the middle of an event."
        }
    }
}

struct JSONLFramer {
    static let maximumLineBytes = 1024 * 1024

    private var buffer = Data()

    mutating func append(_ data: Data) throws -> [Data] {
        buffer.append(data)
        var lines: [Data] = []

        while let newlineIndex = buffer.firstIndex(of: 0x0A) {
            var line = Data(buffer[..<newlineIndex])
            buffer.removeSubrange(buffer.startIndex...newlineIndex)
            if line.last == 0x0D {
                line.removeLast()
            }
            guard line.count <= Self.maximumLineBytes else {
                throw JSONLFramingError.lineTooLarge
            }
            lines.append(line)
        }

        guard buffer.count <= Self.maximumLineBytes else {
            throw JSONLFramingError.lineTooLarge
        }
        return lines
    }

    mutating func finish() throws {
        guard buffer.isEmpty else {
            throw JSONLFramingError.incompleteLine
        }
    }
}
