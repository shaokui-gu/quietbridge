// Wallpaper-only capture: never capture the desktop composite or application windows.
import AppKit
import AVFoundation
import ScreenCaptureKit

@MainActor
final class PrivacyWallpaper {
    static let shared = PrivacyWallpaper()
    private(set) var directory: URL?
    private var preparing = false

    func useSnapshots(from directory: URL) { self.directory = directory }

    func clear() {
        if let directory { try? FileManager.default.removeItem(at: directory) }
        directory = nil
    }

    func prepare() {
        guard !preparing, NativePlatform.sessionLocked() == false,
              CGPreflightScreenCaptureAccess() else { return }
        preparing = true
        if directory == nil {
            let path = FileManager.default.temporaryDirectory
                .appendingPathComponent("workbuddy-wallpaper-" + UUID().uuidString, isDirectory: true)
            do {
                try FileManager.default.createDirectory(at: path, withIntermediateDirectories: false,
                                                       attributes: [.posixPermissions: 0o700])
                directory = path
            } catch { preparing = false; return }
        }
        Task { @MainActor in
            defer { preparing = false }
            do {
                let content = try await SCShareableContent.excludingDesktopWindows(false, onScreenWindowsOnly: true)
                for display in content.displays {
                    // These are system-owned background surfaces, below Finder's
                    // desktop icons. Do not fall back to a full-display screenshot.
                    guard let window = content.windows.first(where: {
                        let owner = $0.owningApplication?.bundleIdentifier ?? ""
                        return ["com.apple.dock", "com.apple.wallpaper.agent"].contains(owner)
                            && $0.windowLayer < Int(CGWindowLevelForKey(.desktopIconWindow))
                            && ($0.title ?? "").hasPrefix("Wallpaper-")
                            && $0.frame == display.frame
                    }) else { continue }
                    let filter = SCContentFilter(desktopIndependentWindow: window)
                    let configuration = SCStreamConfiguration()
                    configuration.width = min(display.width, 3840)
                    configuration.height = max(1, Int(CGFloat(configuration.width) * display.frame.height / display.frame.width))
                    configuration.showsCursor = false
                    configuration.ignoreShadowsSingleWindow = true
                    configuration.ignoreGlobalClipSingleWindow = true
                    let image = try await SCScreenshotManager.captureImage(contentFilter: filter, configuration: configuration)
                    guard let directory else { continue }
                    let png = try WindowCapture.encode(image)
                    try png.bytes.write(to: directory.appendingPathComponent("\(display.displayID).png"), options: .atomic)
                }
            } catch { Trace.log("wallpaper snapshot unavailable; retain fallback") }
        }
    }

    func snapshot(for screen: NSScreen) -> NSImage? {
        guard let id = screen.deviceDescription[NSDeviceDescriptionKey("NSScreenNumber")] as? NSNumber,
              let directory else { return nil }
        return NSImage(contentsOf: directory.appendingPathComponent("\(id.uint32Value).png"))
    }

    func fileURL(for screen: NSScreen) -> URL? {
        let store = FileManager.default.homeDirectoryForCurrentUser
            .appendingPathComponent("Library/Application Support/com.apple.wallpaper/Store/Index.plist")
        guard let data = try? Data(contentsOf: store),
              let index = try? PropertyListSerialization.propertyList(from: data, format: nil) as? [String: Any] else {
            return NSWorkspace.shared.desktopImageURL(for: screen)
        }
        let displayID = (screen.deviceDescription[NSDeviceDescriptionKey("NSScreenNumber")] as? NSNumber)?.uint32Value
        let uuid = displayID.flatMap { CGDisplayCreateUUIDFromDisplayID($0)?.takeRetainedValue() }
            .map { CFUUIDCreateString(nil, $0) as String }
        let displays = index["Displays"] as? [String: Any]
        let selection = (uuid.flatMap { displays?[$0] as? [String: Any] })
            ?? index["AllSpacesAndDisplays"] as? [String: Any]
        guard let desktop = selection?["Desktop"] as? [String: Any],
              let content = desktop["Content"] as? [String: Any],
              let choices = content["Choices"] as? [[String: Any]] else { return nil }
        for choice in choices {
            for value in strings(in: choice["Files"] as Any) {
                if let url = URL(string: value), url.isFileURL,
                   FileManager.default.isReadableFile(atPath: url.path) { return url }
            }
            // Only use a downloaded movie whose ID occurs in the active choice.
            // Never choose an arbitrary cached movie or initiate a download.
            if let provider = choice["Provider"] as? String, provider.contains("aerial") {
                for id in strings(in: choice["Configuration"] as Any) where UUID(uuidString: id) != nil {
                    for base in [FileManager.default.homeDirectoryForCurrentUser.appendingPathComponent("Library/Application Support/com.apple.wallpaper/aerials/videos"),
                                 URL(fileURLWithPath: "/Library/Application Support/com.apple.idleassetsd/Customer/4KSDR240FPS")] {
                        let movie = base.appendingPathComponent(id + ".mov")
                        if FileManager.default.isReadableFile(atPath: movie.path) { return movie }
                    }
                }
            }
            if (choice["Provider"] as? String)?.contains("image") == true {
                return NSWorkspace.shared.desktopImageURL(for: screen)
            }
        }
        // Procedural providers can report DefaultDesktop.heic through NSWorkspace.
        // The wallpaper-only snapshot is more faithful than that unrelated image.
        return nil
    }

    private func strings(in value: Any) -> [String] {
        if let text = value as? String { return [text] }
        if let data = value as? Data,
           let decoded = try? PropertyListSerialization.propertyList(from: data, format: nil) {
            return strings(in: decoded)
        }
        if let values = value as? [Any] { return values.flatMap { strings(in: $0) } }
        if let values = value as? [String: Any] { return values.values.flatMap { strings(in: $0) } }
        return []
    }
}

/// A player is deliberately non-interactive; the parent shield retains all input.
@MainActor
final class PrivacyWallpaperVideo {
    let layer = AVPlayerLayer()
    private let player = AVQueuePlayer()
    private var looper: AVPlayerLooper?

    init(url: URL) {
        player.isMuted = true
        layer.player = player
        layer.videoGravity = .resizeAspectFill
        looper = AVPlayerLooper(player: player, templateItem: AVPlayerItem(url: url))
    }

    func setVisible(_ visible: Bool) {
        if visible { player.play() } else { player.pause() }
    }
}
