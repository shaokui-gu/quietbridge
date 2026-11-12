// Sparse statistics of the incoming source buffer. No pixels or hashes are exported.
import CoreVideo
import Foundation

struct PreviewPixelSample {
    let wholeFingerprint: UInt64
    let bodyFingerprint: UInt64
    let nearUniform: Bool
    let metadata: [String: Any]

    static func inspect(_ buffer: CVPixelBuffer) -> PreviewPixelSample? {
        guard CVPixelBufferLockBaseAddress(buffer, .readOnly) == kCVReturnSuccess else { return nil }
        defer { CVPixelBufferUnlockBaseAddress(buffer, .readOnly) }
        let format = CVPixelBufferGetPixelFormatType(buffer)
        let width = CVPixelBufferGetWidth(buffer), height = CVPixelBufferGetHeight(buffer)
        guard width > 0, height > 0 else { return nil }
        let planar = CVPixelBufferIsPlanar(buffer)
        let stride = planar ? CVPixelBufferGetBytesPerRowOfPlane(buffer, 0) : CVPixelBufferGetBytesPerRow(buffer)
        let base = planar ? CVPixelBufferGetBaseAddressOfPlane(buffer, 0) : CVPixelBufferGetBaseAddress(buffer)
        let bgra = format == kCVPixelFormatType_32BGRA
        let nv12 = format == kCVPixelFormatType_420YpCbCr8BiPlanarVideoRange || format == kCVPixelFormatType_420YpCbCr8BiPlanarFullRange
        let channels = bgra ? 4 : 1
        guard (bgra && !planar) || (nv12 && CVPixelBufferGetPlaneCount(buffer) == 2),
              let base, stride >= width * channels else { return nil }
        if nv12 {
            guard CVPixelBufferGetWidthOfPlane(buffer, 0) == width,
                  CVPixelBufferGetHeightOfPlane(buffer, 0) == height else { return nil }
        }
        // A fixed central probe excludes typical browser chrome. It is not a DOM-derived rectangle.
        let x0 = width / 8, y0 = height / 4
        let x1 = max(x0 + 1, width * 7 / 8), y1 = max(y0 + 1, height * 7 / 8)
        func scan(_ pointer: UnsafeMutableRawPointer, stride: Int, channels: Int,
                  x: Int, y: Int, width: Int, height: Int) -> (UInt64, [Int], [Int]) {
            var hash = (UInt64(width) &* 1099511628211) ^ UInt64(height)
            var low = [Int](repeating: 255, count: channels), high = [Int](repeating: 0, count: channels)
            for row in 0..<min(36, height) {
                let sy = y + row * height / min(36, height)
                for column in 0..<min(64, width) {
                    let sx = x + column * width / min(64, width)
                    for channel in 0..<channels {
                        let value = pointer.load(fromByteOffset: sy * stride + sx * channels + channel, as: UInt8.self)
                        hash = (hash ^ UInt64(value)) &* 1099511628211
                        low[channel] = min(low[channel], Int(value)); high[channel] = max(high[channel], Int(value))
                    }
                }
            }
            return (hash, low, high)
        }
        let whole = scan(base, stride: stride, channels: channels, x: 0, y: 0, width: width, height: height)
        let body = scan(base, stride: stride, channels: channels, x: x0, y: y0, width: x1 - x0, height: y1 - y0)
        var bodyHash = body.0
        var spread = (0..<(bgra ? 3 : 1)).map { body.2[$0] - body.1[$0] }.max() ?? 0
        var metadata: [String: Any] = ["pixelFormat": bgra ? "BGRA" : (format == kCVPixelFormatType_420YpCbCr8BiPlanarVideoRange ? "420v" : "420f"),
            "pixelFormatCode": Int(format), "width": width, "height": height, "bytesPerRow": stride,
            "planeCount": CVPixelBufferGetPlaneCount(buffer),
            "bodyX": x0, "bodyY": y0, "bodyWidth": x1 - x0, "bodyHeight": y1 - y0,
            "bodyChannelSpread": spread]
        if bgra {
            metadata["bodyAlphaMin"] = body.1[3]; metadata["bodyAlphaMax"] = body.2[3]
        } else {
            let cw = CVPixelBufferGetWidthOfPlane(buffer, 1), ch = CVPixelBufferGetHeightOfPlane(buffer, 1)
            let cs = CVPixelBufferGetBytesPerRowOfPlane(buffer, 1)
            guard let cb = CVPixelBufferGetBaseAddressOfPlane(buffer, 1), cw > 0, ch > 0, cs >= cw * 2 else { return nil }
            let cx = min(cw - 1, x0 * cw / width), cy = min(ch - 1, y0 * ch / height)
            let cx1 = min(cw, max(cx + 1, x1 * cw / width)), cy1 = min(ch, max(cy + 1, y1 * ch / height))
            let chroma = scan(cb, stride: cs, channels: 2, x: cx, y: cy, width: cx1 - cx, height: cy1 - cy)
            bodyHash ^= chroma.0 &* 1099511628211
            let chromaSpread = max(chroma.2[0] - chroma.1[0], chroma.2[1] - chroma.1[1])
            metadata["chromaBytesPerRow"] = cs; metadata["bodyChromaSpread"] = chromaSpread
            spread = max(spread, chromaSpread)
        }
        let uniform = spread <= 2
        metadata["bodyNearUniform"] = uniform
        // Fixed color-space identifiers only, never arbitrary attachment strings.
        let allowed = ["ITU_R_709_2", "ITU_R_601_4", "ITU_R_2020", "SMPTE_C", "P3_D65", "DCI_P3", "EBU_3213",
                       "sRGB", "Linear", "SMPTE_ST_2084_PQ", "ITU_R_2100_HLG", "SMPTE_240M_1995"]
        for (name, key) in [("colorPrimaries", kCVImageBufferColorPrimariesKey),
                            ("transferFunction", kCVImageBufferTransferFunctionKey),
                            ("yCbCrMatrix", kCVImageBufferYCbCrMatrixKey)] {
            if let value = CVBufferCopyAttachment(buffer, key, nil) {
                let text = value as? String
                metadata[name] = text.map { allowed.contains($0) ? $0 : "other" } ?? "other"
            } else { metadata[name] = "unspecified" }
        }
        return PreviewPixelSample(wholeFingerprint: whole.0, bodyFingerprint: bodyHash, nearUniform: uniform, metadata: metadata)
    }
}
