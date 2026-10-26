import Darwin
import Foundation
import os

/// Temporary diagnostic for #1130: what a Location's folder looks like through the URL the
/// Files picker handed over and through the URL its saved bookmark reopens. Remove once the
/// SMB listing problem is understood.
enum LocationAccessProbe {
    static let logger = Logger(subsystem: "com.shinycomputers.bd-to-avp.player", category: "locations")

    /// Reads `url` three ways and returns one plain report line per fact.
    static func report(label: String, url: URL, startedAccess: Bool?, isStale: Bool?) -> String {
        var lines = ["\(label)", "path: \(url.path)"]
        lines.append("startAccessing: \(startedAccess.map { String($0) } ?? "n/a")")
        lines.append("isStale: \(isStale.map { String($0) } ?? "n/a")")
        do {
            let entries = try FileManager.default.contentsOfDirectory(at: url, includingPropertiesForKeys: nil)
            lines.append("FileManager: \(entries.count) [\(sample(entries.map(\.lastPathComponent)))]")
        } catch {
            lines.append("FileManager error: \(describe(error))")
        }
        do {
            let entries = try LocationScanner.coordinatedContents(of: url)
            lines.append("coordinated: \(entries.count)")
        } catch {
            lines.append("coordinated error: \(describe(error))")
        }
        lines.append("readdir: \(posixListing(url.path))")
        let text = lines.joined(separator: "\n")
        logger.log("\(text, privacy: .public)")
        return text
    }

    /// Resolves the saved bookmark itself so `isStale` and the scope result are visible.
    static func reportBookmark(_ data: Data) -> String {
        var isStale = false
        let url: URL
        do {
            url = try URL(resolvingBookmarkData: data, options: [], relativeTo: nil, bookmarkDataIsStale: &isStale)
        } catch {
            let text = "bookmark\nresolve error: \(describe(error))"
            logger.log("\(text, privacy: .public)")
            return text
        }
        let started = url.startAccessingSecurityScopedResource()
        defer { if started { url.stopAccessingSecurityScopedResource() } }
        return report(label: "bookmark", url: url, startedAccess: started, isStale: isStale)
    }

    private static func posixListing(_ path: String) -> String {
        errno = 0
        guard let directory = opendir(path) else { return "opendir errno \(errno) (\(String(cString: strerror(errno))))" }
        defer { closedir(directory) }
        var names: [String] = []
        while true {
            errno = 0
            guard let entry = readdir(directory) else { break }
            let name = withUnsafePointer(to: entry.pointee.d_name) {
                $0.withMemoryRebound(to: CChar.self, capacity: Int(MAXPATHLEN)) { String(cString: $0) }
            }
            if name != "." && name != ".." { names.append(name) }
        }
        let endErrno = errno
        return "\(names.count) [\(sample(names))]" + (endErrno == 0 ? "" : " errno \(endErrno)")
    }

    private static func sample(_ names: [String]) -> String {
        names.sorted().prefix(5).joined(separator: ", ")
    }

    private static func describe(_ error: Error) -> String {
        let error = error as NSError
        return "\(error.domain) \(error.code) \(error.localizedDescription)"
    }
}
