import Foundation
import zlib

enum DiagnosticZipArchiveError: Error, LocalizedError {
    case invalidEntryName(String)
    case entryTooLarge(String)
    case archiveTooLarge
    case compressionFailed(Int32)

    var errorDescription: String? {
        switch self {
        case let .invalidEntryName(name):
            return "The support archive contains an invalid entry name: \(name)"
        case let .entryTooLarge(name):
            return "The support archive entry is too large: \(name)"
        case .archiveTooLarge:
            return "The support archive is too large."
        case let .compressionFailed(status):
            return "The support archive could not be compressed (zlib status \(status))."
        }
    }
}

enum DiagnosticZipArchive {
    struct Entry {
        let name: String
        let data: Data
    }

    private struct CentralDirectoryEntry {
        let nameData: Data
        let crc32: UInt32
        let compressedSize: UInt32
        let uncompressedSize: UInt32
        let localHeaderOffset: UInt32
        let modificationTime: UInt16
        let modificationDate: UInt16
    }

    static func data(entries: [Entry], modificationDate: Date) throws -> Data {
        guard entries.count <= Int(UInt16.max) else {
            throw DiagnosticZipArchiveError.archiveTooLarge
        }
        let timestamp = dosTimestamp(for: modificationDate)
        var archive = Data()
        var centralEntries: [CentralDirectoryEntry] = []

        for entry in entries {
            guard !entry.name.isEmpty,
                  !entry.name.hasPrefix("/"),
                  !entry.name.contains(".."),
                  !entry.name.contains("\\")
            else {
                throw DiagnosticZipArchiveError.invalidEntryName(entry.name)
            }
            let nameData = Data(entry.name.utf8)
            guard nameData.count <= Int(UInt16.max),
                  entry.data.count <= Int(UInt32.max),
                  archive.count <= Int(UInt32.max)
            else {
                throw DiagnosticZipArchiveError.entryTooLarge(entry.name)
            }

            let compressedData = try deflated(entry.data)
            guard compressedData.count <= Int(UInt32.max) else {
                throw DiagnosticZipArchiveError.entryTooLarge(entry.name)
            }
            let checksum = checksum(entry.data)
            let localHeaderOffset = UInt32(archive.count)

            archive.appendLittleEndian(UInt32(0x0403_4B50))
            archive.appendLittleEndian(UInt16(20))
            archive.appendLittleEndian(UInt16(0x0800))
            archive.appendLittleEndian(UInt16(8))
            archive.appendLittleEndian(timestamp.time)
            archive.appendLittleEndian(timestamp.date)
            archive.appendLittleEndian(checksum)
            archive.appendLittleEndian(UInt32(compressedData.count))
            archive.appendLittleEndian(UInt32(entry.data.count))
            archive.appendLittleEndian(UInt16(nameData.count))
            archive.appendLittleEndian(UInt16(0))
            archive.append(nameData)
            archive.append(compressedData)

            centralEntries.append(
                CentralDirectoryEntry(
                    nameData: nameData,
                    crc32: checksum,
                    compressedSize: UInt32(compressedData.count),
                    uncompressedSize: UInt32(entry.data.count),
                    localHeaderOffset: localHeaderOffset,
                    modificationTime: timestamp.time,
                    modificationDate: timestamp.date
                )
            )
        }

        guard archive.count <= Int(UInt32.max) else {
            throw DiagnosticZipArchiveError.archiveTooLarge
        }
        let centralDirectoryOffset = UInt32(archive.count)
        for entry in centralEntries {
            archive.appendLittleEndian(UInt32(0x0201_4B50))
            archive.appendLittleEndian(UInt16(0x0314))
            archive.appendLittleEndian(UInt16(20))
            archive.appendLittleEndian(UInt16(0x0800))
            archive.appendLittleEndian(UInt16(8))
            archive.appendLittleEndian(entry.modificationTime)
            archive.appendLittleEndian(entry.modificationDate)
            archive.appendLittleEndian(entry.crc32)
            archive.appendLittleEndian(entry.compressedSize)
            archive.appendLittleEndian(entry.uncompressedSize)
            archive.appendLittleEndian(UInt16(entry.nameData.count))
            archive.appendLittleEndian(UInt16(0))
            archive.appendLittleEndian(UInt16(0))
            archive.appendLittleEndian(UInt16(0))
            archive.appendLittleEndian(UInt16(0))
            archive.appendLittleEndian(UInt32(0))
            archive.appendLittleEndian(entry.localHeaderOffset)
            archive.append(entry.nameData)
        }

        guard archive.count <= Int(UInt32.max) else {
            throw DiagnosticZipArchiveError.archiveTooLarge
        }
        let centralDirectorySize = UInt32(archive.count) - centralDirectoryOffset
        let entryCount = UInt16(centralEntries.count)
        archive.appendLittleEndian(UInt32(0x0605_4B50))
        archive.appendLittleEndian(UInt16(0))
        archive.appendLittleEndian(UInt16(0))
        archive.appendLittleEndian(entryCount)
        archive.appendLittleEndian(entryCount)
        archive.appendLittleEndian(centralDirectorySize)
        archive.appendLittleEndian(centralDirectoryOffset)
        archive.appendLittleEndian(UInt16(0))
        return archive
    }

    private static func checksum(_ data: Data) -> UInt32 {
        data.withUnsafeBytes { buffer in
            let bytes = buffer.bindMemory(to: Bytef.self)
            return UInt32(crc32(0, bytes.baseAddress, uInt(bytes.count)))
        }
    }

    private static func deflated(_ data: Data) throws -> Data {
        var stream = z_stream()
        let initializationStatus = deflateInit2_(
            &stream,
            Z_DEFAULT_COMPRESSION,
            Z_DEFLATED,
            -MAX_WBITS,
            8,
            Z_DEFAULT_STRATEGY,
            ZLIB_VERSION,
            Int32(MemoryLayout<z_stream>.size)
        )
        guard initializationStatus == Z_OK else {
            throw DiagnosticZipArchiveError.compressionFailed(initializationStatus)
        }
        defer { deflateEnd(&stream) }

        let outputCapacity = max(64, Int(deflateBound(&stream, uLong(data.count))))
        var output = Data(count: outputCapacity)
        let status = data.withUnsafeBytes { inputBuffer in
            output.withUnsafeMutableBytes { outputBuffer in
                let inputBytes = inputBuffer.bindMemory(to: Bytef.self)
                let outputBytes = outputBuffer.bindMemory(to: Bytef.self)
                stream.next_in = UnsafeMutablePointer(mutating: inputBytes.baseAddress)
                stream.avail_in = uInt(inputBytes.count)
                stream.next_out = outputBytes.baseAddress
                stream.avail_out = uInt(outputBytes.count)
                return deflate(&stream, Z_FINISH)
            }
        }
        guard status == Z_STREAM_END else {
            throw DiagnosticZipArchiveError.compressionFailed(status)
        }
        output.removeSubrange(Int(stream.total_out)..<output.count)
        return output
    }

    private static func dosTimestamp(for date: Date) -> (time: UInt16, date: UInt16) {
        var calendar = Calendar(identifier: .gregorian)
        calendar.timeZone = TimeZone(secondsFromGMT: 0)!
        let components = calendar.dateComponents([.year, .month, .day, .hour, .minute, .second], from: date)
        let year = min(2107, max(1980, components.year ?? 1980))
        let month = min(12, max(1, components.month ?? 1))
        let day = min(31, max(1, components.day ?? 1))
        let hour = min(23, max(0, components.hour ?? 0))
        let minute = min(59, max(0, components.minute ?? 0))
        let second = min(59, max(0, components.second ?? 0))
        let dosTime = UInt16((hour << 11) | (minute << 5) | (second / 2))
        let dosDate = UInt16(((year - 1980) << 9) | (month << 5) | day)
        return (dosTime, dosDate)
    }
}

private extension Data {
    mutating func appendLittleEndian<T: FixedWidthInteger>(_ value: T) {
        var littleEndianValue = value.littleEndian
        Swift.withUnsafeBytes(of: &littleEndianValue) { buffer in
            append(contentsOf: buffer)
        }
    }
}
