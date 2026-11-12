import CoreVideo
import Foundation
import Testing
@testable import QuietBridgeNative

@Suite struct PreviewPixelSampleTests {
    private func buffer(_ format: OSType = kCVPixelFormatType_32BGRA) throws -> CVPixelBuffer {
        var optional: CVPixelBuffer?
        #expect(CVPixelBufferCreate(kCFAllocatorDefault, 128, 80, format, nil, &optional) == kCVReturnSuccess)
        return try #require(optional)
    }

    private func paintBGRA(_ buffer: CVPixelBuffer, rows: Range<Int> = 0..<80,
                           color: UInt8, alpha: UInt8 = 255) throws {
        #expect(CVPixelBufferLockBaseAddress(buffer, []) == kCVReturnSuccess)
        defer { CVPixelBufferUnlockBaseAddress(buffer, []) }
        let base = try #require(CVPixelBufferGetBaseAddress(buffer))
        let stride = CVPixelBufferGetBytesPerRow(buffer)
        for y in rows {
            for x in 0..<128 {
                for channel in 0..<3 { base.storeBytes(of: color, toByteOffset: y * stride + x * 4 + channel, as: UInt8.self) }
                base.storeBytes(of: alpha, toByteOffset: y * stride + x * 4 + 3, as: UInt8.self)
            }
        }
    }

    @Test func toolbarChangesCannotCountAsBodyChanges() throws {
        let pixels = try buffer(), progress = PreviewFrameProgress()
        try paintBGRA(pixels, color: 30)
        progress.sample(pixels, now: 10)
        try paintBGRA(pixels, rows: 0..<20, color: 90)
        progress.sample(pixels, now: 11)
        #expect(progress.snapshot(now: 11)["sampledContentChanges"] as? Int == 1)
        #expect(progress.snapshot(now: 11)["bodyContentChanges"] as? Int == 0)
        try paintBGRA(pixels, rows: 35..<45, color: 230)
        progress.sample(pixels, now: 12)
        let state = progress.snapshot(now: 12)
        #expect(state["bodyContentChanges"] as? Int == 1)
        #expect(state["bodyNearUniformSamples"] as? Int == 2)
        let source = try #require(state["sourceFrame"] as? [String: Any])
        #expect(source["bodyNearUniform"] as? Bool == false)
        #expect(source["bodyChannelSpread"] as? Int == 200)
        #expect(source["bodyY"] as? Int == 20)
        #expect(source["bodyAlphaMin"] as? Int == 255)
        #expect(state["lastBodyContentChangeAgeMs"] as? Double == 0)
    }

    @Test(arguments: [kCVPixelFormatType_420YpCbCr8BiPlanarFullRange, kCVPixelFormatType_420YpCbCr8BiPlanarVideoRange])
    func equalLumaButDifferentChromaStillChangesBody(_ format: OSType) throws {
        let pixels = try buffer(format), progress = PreviewFrameProgress()
        func paint(_ chroma: Int32) throws {
            #expect(CVPixelBufferLockBaseAddress(pixels, []) == kCVReturnSuccess)
            defer { CVPixelBufferUnlockBaseAddress(pixels, []) }
            for plane in 0..<2 {
                let base = try #require(CVPixelBufferGetBaseAddressOfPlane(pixels, plane))
                memset(base, plane == 0 ? 100 : chroma,
                       CVPixelBufferGetBytesPerRowOfPlane(pixels, plane) * CVPixelBufferGetHeightOfPlane(pixels, plane))
            }
        }
        try paint(128); progress.sample(pixels, now: 10)
        try paint(200); progress.sample(pixels, now: 11)
        let state = progress.snapshot(now: 11)
        #expect(state["sampledContentChanges"] as? Int == 0, "whole-window legacy metric samples NV12 luma only")
        #expect(state["bodyContentChanges"] as? Int == 1, "body probe must also inspect chroma")
        let source = try #require(state["sourceFrame"] as? [String: Any])
        #expect(source["planeCount"] as? Int == 2)
        #expect(source["bodyNearUniform"] as? Bool == true)
        #expect(source["bodyChromaSpread"] as? Int == 0)
        #expect(source["chromaBytesPerRow"] as? Int == CVPixelBufferGetBytesPerRowOfPlane(pixels, 1))
    }

    @Test func transparentBodyAndUnknownColorMetadataAreVisibleWithoutSavingContent() throws {
        let pixels = try buffer(), progress = PreviewFrameProgress()
        try paintBGRA(pixels, color: 90, alpha: 0)
        CVBufferSetAttachment(pixels, kCVImageBufferColorPrimariesKey, "private-content" as CFString, .shouldPropagate)
        progress.sample(pixels, now: 10)
        let state = progress.snapshot(now: 10), source = try #require(state["sourceFrame"] as? [String: Any])
        #expect(source["bodyAlphaMax"] as? Int == 0)
        #expect(source["colorPrimaries"] as? String == "other")
        let json = String(decoding: try JSONSerialization.data(withJSONObject: state), as: UTF8.self)
        #expect(!json.contains("private-content"))
        #expect(!json.contains("Fingerprint"))
        #expect(!json.contains("fingerprint"))
    }

    @Test func failedInspectionClearsOldMetadataInsteadOfClaimingItDescribesNewFrame() throws {
        let pixels = try buffer(), unknown = try buffer(kCVPixelFormatType_OneComponent8)
        let progress = PreviewFrameProgress()
        try paintBGRA(pixels, color: 20); progress.sample(pixels, now: 10)
        #expect(progress.snapshot(now: 10)["sourceFrame"] != nil)
        progress.sample(unknown, now: 11)
        let state = progress.snapshot(now: 11)
        #expect(state["sourceFrame"] == nil)
        #expect(state["sampleFailures"] as? Int == 1)
        #expect(state["bodySampleFailures"] as? Int == 1)
    }
}
