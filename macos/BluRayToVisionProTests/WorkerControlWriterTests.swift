import Darwin
import Foundation
import XCTest
@testable import BluRayToVisionPro

final class WorkerControlWriterTests: XCTestCase {
    func testWritesOneAtomicJSONLCommandWithExactIdentity() async throws {
        let pipe = Pipe()
        let writer = try WorkerControlWriter(handle: pipe.fileHandleForWriting)
        defer { writer.close() }
        let command = makeCommand()
        try await writer.send(command)
        writer.close()
        let data = try XCTUnwrap(pipe.fileHandleForReading.read(upToCount: 512))
        XCTAssertEqual(data.last, 0x0A)
        let object = try XCTUnwrap(JSONSerialization.jsonObject(with: data) as? [String: Any])
        XCTAssertEqual(object["job_id"] as? String, command.jobID.uuidString)
        XCTAssertEqual(object["tool_run_id"] as? String, command.toolRunID.uuidString)
        XCTAssertEqual(object["stall_episode_id"] as? String, command.stallEpisodeID.uuidString)
        XCTAssertEqual(object["command_id"] as? String, command.commandID.uuidString)
        XCTAssertEqual(object["protocol_version"] as? Int, 13)
        XCTAssertEqual(object["type"] as? String, "job.keep_waiting")
        XCTAssertEqual(object.count, 6)
    }

    func testViewerPositionIsOneSmallFrameWithTheSharedShape() async throws {
        let pipe = Pipe()
        let writer = try WorkerControlWriter(handle: pipe.fileHandleForWriting)
        defer { writer.close() }
        let fixture = try JSONSerialization.jsonObject(with: sharedFixtureData("native_worker_viewer_position")) as? NSDictionary
        let jobID = try XCTUnwrap(UUID(uuidString: try XCTUnwrap(fixture?["job_id"] as? String)))
        let position = try XCTUnwrap(fixture?["position_microseconds"] as? Int64)
        try await writer.send(WorkerViewerPositionCommand(jobID: jobID, positionMicroseconds: position))
        // The largest position still fits one atomic control frame.
        try await writer.send(WorkerViewerPositionCommand(jobID: jobID, positionMicroseconds: .max))
        writer.close()

        let data = try pipe.fileHandleForReading.readToEnd() ?? Data()
        let frames = data.split(separator: 0x0A)
        XCTAssertEqual(frames.count, 2)
        XCTAssertTrue(frames.allSatisfy { $0.count < 512 })
        var first = try XCTUnwrap(JSONSerialization.jsonObject(with: Data(frames[0])) as? [String: Any])
        first["job_id"] = (first["job_id"] as? String)?.lowercased()
        XCTAssertEqual(first as NSDictionary, fixture)
        let largest = try XCTUnwrap(JSONSerialization.jsonObject(with: Data(frames[1])) as? [String: Any])
        XCTAssertEqual(largest["position_microseconds"] as? Int64, WorkerViewerPositionCommand.maximumPositionMicroseconds)
    }

    private func sharedFixtureData(_ family: String) throws -> Data {
        let url = URL(fileURLWithPath: #filePath)
            .deletingLastPathComponent()
            .deletingLastPathComponent()
            .deletingLastPathComponent()
            .appendingPathComponent("tests/fixtures/\(family)_v\(WorkerJobSpec.protocolVersion).json")
        return try XCTUnwrap(FileManager.default.contents(atPath: url.path))
    }

    func testClosedChildInputThrowsWithoutSIGPIPE() async throws {
        let pipe = Pipe()
        let writer = try WorkerControlWriter(handle: pipe.fileHandleForWriting)
        defer { writer.close() }
        try pipe.fileHandleForReading.close()
        do {
            try await writer.send(makeCommand())
            XCTFail("A closed child must report a write failure")
        } catch {
            XCTAssertFalse(error.localizedDescription.isEmpty)
        }
    }

    func testFullPipeFailsPromptlyWithoutPartialCommandOrBlockingShutdown() async throws {
        let pipe = Pipe()
        let writer = try WorkerControlWriter(handle: pipe.fileHandleForWriting)
        let bytes = [UInt8](repeating: 0x20, count: 512)
        var written = 0
        while true {
            let count = bytes.withUnsafeBytes {
                Darwin.write(pipe.fileHandleForWriting.fileDescriptor, $0.baseAddress, $0.count)
            }
            if count < 0 {
                XCTAssertEqual(errno, EAGAIN)
                break
            }
            written += count
            XCTAssertLessThan(written, 4 * 1_024 * 1_024)
        }
        let started = Date()
        do {
            try await writer.send(makeCommand())
            XCTFail("A full pipe must report failure without waiting for a reader")
        } catch {
            XCTAssertLessThan(Date().timeIntervalSince(started), 2)
        }
        writer.close()
        let drained = try pipe.fileHandleForReading.readToEnd() ?? Data()
        XCTAssertEqual(drained.count, written)
        XCTAssertTrue(drained.allSatisfy { $0 == 0x20 })
    }

    func testClosedWriterRefusesNewCommands() async throws {
        let pipe = Pipe()
        let writer = try WorkerControlWriter(handle: pipe.fileHandleForWriting)
        writer.close()
        do {
            try await writer.send(makeCommand())
            XCTFail("A finished attempt must not accept another command")
        } catch {}
        let data = try pipe.fileHandleForReading.readToEnd() ?? Data()
        XCTAssertTrue(data.isEmpty)
    }

    private func makeCommand() -> WorkerWaitCommand {
        WorkerWaitCommand(commandID: UUID(), jobID: UUID(), toolRunID: UUID(), stallEpisodeID: UUID())
    }
}
