import AudioToolbox
import CoreMedia
import Foundation

enum CoreAudioChannelLayout {
    static func label(for speaker: AudioSpeaker) -> AudioChannelLabel {
        switch speaker {
        case .left: kAudioChannelLabel_Left
        case .right: kAudioChannelLabel_Right
        case .center: kAudioChannelLabel_Center
        case .lfe: kAudioChannelLabel_LFEScreen
        case .leftSurround: kAudioChannelLabel_LeftSurround
        case .rightSurround: kAudioChannelLabel_RightSurround
        case .rearLeft: kAudioChannelLabel_RearSurroundLeft
        case .rearRight: kAudioChannelLabel_RearSurroundRight
        case .rearCenter: kAudioChannelLabel_CenterSurround
        case .leftCenter: kAudioChannelLabel_LeftCenter
        case .rightCenter: kAudioChannelLabel_RightCenter
        case .leftWide: kAudioChannelLabel_LeftWide
        case .rightWide: kAudioChannelLabel_RightWide
        case .topCenter: kAudioChannelLabel_TopCenterSurround
        case .leftTopFront: kAudioChannelLabel_VerticalHeightLeft
        case .centerTopFront: kAudioChannelLabel_VerticalHeightCenter
        case .rightTopFront: kAudioChannelLabel_VerticalHeightRight
        case .leftTopRear: kAudioChannelLabel_TopBackLeft
        case .rightTopRear: kAudioChannelLabel_TopBackRight
        case .centerTopRear: kAudioChannelLabel_TopBackCenter
        case .lfe2: kAudioChannelLabel_LFE2
        case .mono: kAudioChannelLabel_Mono
        }
    }

    static func speaker(for label: AudioChannelLabel) -> AudioSpeaker? {
        AudioSpeaker.allCases.first { self.label(for: $0) == label }
    }

    /// Speakers of a CoreAudio layout, expanding a layout tag to its channel descriptions.
    static func speakers(_ layout: UnsafePointer<AudioChannelLayout>, size: Int) -> [AudioSpeaker] {
        if layout.pointee.mChannelLayoutTag == kAudioChannelLayoutTag_UseChannelDescriptions {
            let count = Int(layout.pointee.mNumberChannelDescriptions)
            let offset = MemoryLayout<AudioChannelLayout>.offset(of: \.mChannelDescriptions) ?? 12
            guard offset + count * MemoryLayout<AudioChannelDescription>.stride <= size else {
                return []
            }
            let descriptions = UnsafeRawPointer(layout).advanced(by: offset)
                .assumingMemoryBound(to: AudioChannelDescription.self)
            return (0..<count).compactMap { speaker(for: descriptions[$0].mChannelLabel) }
        }
        var tag = layout.pointee.mChannelLayoutTag
        let tagSize = UInt32(MemoryLayout<AudioChannelLayoutTag>.size)
        var expandedSize: UInt32 = 0
        guard AudioFormatGetPropertyInfo(kAudioFormatProperty_ChannelLayoutForTag, tagSize, &tag, &expandedSize) == noErr,
              expandedSize > 0 else {
            return []
        }
        let expanded = UnsafeMutableRawPointer.allocate(byteCount: Int(expandedSize), alignment: 8)
        defer { expanded.deallocate() }
        guard AudioFormatGetProperty(kAudioFormatProperty_ChannelLayoutForTag, tagSize, &tag, &expandedSize, expanded) == noErr else {
            return []
        }
        let expandedLayout = expanded.assumingMemoryBound(to: AudioChannelLayout.self)
        guard expandedLayout.pointee.mChannelLayoutTag == kAudioChannelLayoutTag_UseChannelDescriptions else {
            return []
        }
        return speakers(expandedLayout, size: Int(expandedSize))
    }

    /// A channel-description layout for `speakers`, as bytes for CMAudioFormatDescriptionCreate.
    static func layoutData(for speakers: [AudioSpeaker]) -> Data {
        let offset = MemoryLayout<AudioChannelLayout>.offset(of: \.mChannelDescriptions) ?? 12
        let size = offset + max(speakers.count, 1) * MemoryLayout<AudioChannelDescription>.stride
        var data = Data(count: size)
        data.withUnsafeMutableBytes { raw in
            let layout = raw.baseAddress!.assumingMemoryBound(to: AudioChannelLayout.self)
            layout.pointee.mChannelLayoutTag = kAudioChannelLayoutTag_UseChannelDescriptions
            layout.pointee.mChannelBitmap = []
            layout.pointee.mNumberChannelDescriptions = UInt32(speakers.count)
            let descriptions = raw.baseAddress!.advanced(by: offset).assumingMemoryBound(to: AudioChannelDescription.self)
            for (index, speaker) in speakers.enumerated() {
                descriptions[index] = AudioChannelDescription(
                    mChannelLabel: label(for: speaker),
                    mChannelFlags: [],
                    mCoordinates: (0, 0, 0)
                )
            }
        }
        return data
    }
}

/// Builds CMSampleBuffers of interleaved Float32 PCM for AVSampleBufferAudioRenderer, reusing
/// the format description while the format stays the same.
final class PCMSampleBufferFactory {
    private var cachedFormat: (sampleRate: Int, speakers: [AudioSpeaker], description: CMAudioFormatDescription)?

    func sampleBuffer(for chunk: DecodedAudioChunk) throws -> CMSampleBuffer {
        let format = try formatDescription(sampleRate: chunk.sampleRate, speakers: chunk.speakers)
        let byteCount = chunk.samples.count * MemoryLayout<Float>.size
        var blockBuffer: CMBlockBuffer?
        var status = CMBlockBufferCreateWithMemoryBlock(
            allocator: kCFAllocatorDefault,
            memoryBlock: nil,
            blockLength: byteCount,
            blockAllocator: kCFAllocatorDefault,
            customBlockSource: nil,
            offsetToData: 0,
            dataLength: byteCount,
            flags: kCMBlockBufferAssureMemoryNowFlag,
            blockBufferOut: &blockBuffer
        )
        guard status == noErr, let blockBuffer else {
            throw PCMSampleBufferError.coreMedia(status)
        }
        status = chunk.samples.withUnsafeBytes { raw in
            CMBlockBufferReplaceDataBytes(with: raw.baseAddress!, blockBuffer: blockBuffer, offsetIntoDestination: 0, dataLength: byteCount)
        }
        guard status == noErr else {
            throw PCMSampleBufferError.coreMedia(status)
        }
        var sampleBuffer: CMSampleBuffer?
        status = CMAudioSampleBufferCreateReadyWithPacketDescriptions(
            allocator: kCFAllocatorDefault,
            dataBuffer: blockBuffer,
            formatDescription: format,
            sampleCount: chunk.frameCount,
            presentationTimeStamp: CMTime(value: chunk.startTicks, timescale: CMTimeScale(BluRayPresentationTimeline.ticksPerSecond)),
            packetDescriptions: nil,
            sampleBufferOut: &sampleBuffer
        )
        guard status == noErr, let sampleBuffer else {
            throw PCMSampleBufferError.coreMedia(status)
        }
        return sampleBuffer
    }

    private func formatDescription(sampleRate: Int, speakers: [AudioSpeaker]) throws -> CMAudioFormatDescription {
        if let cachedFormat, cachedFormat.sampleRate == sampleRate, cachedFormat.speakers == speakers {
            return cachedFormat.description
        }
        let channels = UInt32(speakers.count)
        var description = AudioStreamBasicDescription(
            mSampleRate: Float64(sampleRate),
            mFormatID: kAudioFormatLinearPCM,
            mFormatFlags: kAudioFormatFlagIsFloat | kAudioFormatFlagIsPacked,
            mBytesPerPacket: 4 * channels,
            mFramesPerPacket: 1,
            mBytesPerFrame: 4 * channels,
            mChannelsPerFrame: channels,
            mBitsPerChannel: 32,
            mReserved: 0
        )
        let layout = CoreAudioChannelLayout.layoutData(for: speakers)
        var format: CMAudioFormatDescription?
        let status = layout.withUnsafeBytes { raw in
            CMAudioFormatDescriptionCreate(
                allocator: kCFAllocatorDefault,
                asbd: &description,
                layoutSize: raw.count,
                layout: raw.baseAddress!.assumingMemoryBound(to: AudioChannelLayout.self),
                magicCookieSize: 0,
                magicCookie: nil,
                extensions: nil,
                formatDescriptionOut: &format
            )
        }
        guard status == noErr, let format else {
            throw PCMSampleBufferError.coreMedia(status)
        }
        cachedFormat = (sampleRate, speakers, format)
        return format
    }
}

enum PCMSampleBufferError: Error, Equatable {
    case coreMedia(OSStatus)
}
