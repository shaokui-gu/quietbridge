// WindowServer queries adapted from DSH's MIT-licensed Cua Driver integration.
import AppKit
import ApplicationServices
import CoreFoundation
import Darwin
import Foundation

/// A window's Space list and the active Space on its associated display.
struct WindowSpaceMembership: Sendable {
    let spaceIDs: [UInt64]?
    let currentSpaceID: UInt64?

    var onCurrentSpace: Bool? {
        guard let spaceIDs, !spaceIDs.isEmpty, let currentSpaceID, currentSpaceID != 0 else { return nil }
        return spaceIDs.contains(currentSpaceID)
    }
}

/// 仅在目标归属完整已知时绘制，不用当前桌面或 on-screen 标记猜测。
enum CursorSpaceRoute: Equatable {
    case hidden
    case target([UInt64])

    static func decide(windowID: UInt32?, membership: WindowSpaceMembership?) -> Self {
        guard let windowID, windowID != 0, let membership,
              membership.onCurrentSpace != nil, let ids = membership.spaceIDs,
              !ids.isEmpty, ids.allSatisfy({ $0 > 0 }) else { return .hidden }
        return .target(Array(Set(ids)).sorted())
    }

    static func matches(_ actual: [UInt64]?, target: [UInt64]) -> Bool {
        guard let actual, !target.isEmpty, target.allSatisfy({ $0 > 0 }) else { return false }
        return Set(actual) == Set(target)
    }
}

/// 查询不缓存；写入入口只接受本进程持有的隐藏 NSPanel。
final class WindowSpaces {
    private typealias ConnectionID = @convention(c) () -> UInt32
    private typealias CopySpaces = @convention(c) (UInt32, Int32, CFArray) -> UnsafeMutableRawPointer?
    private typealias CopyDisplay = @convention(c) (UInt32, UInt32) -> UnsafeMutableRawPointer?
    private typealias DisplaySpace = @convention(c) (UInt32, CFString) -> UInt64
    // asmagill/hs._asm.undocumented.spaces 的 CGSSpace.h（SHA 59519b82680567068630028eeddaf2907b412edf）
    // 及 internal.m：void CGS{Add,Remove}Windows{To,From}Spaces(CGSConnectionID, CFArrayRef, CFArrayRef)。
    // CGSConnection.h 定义连接为 32 位 int；与既有 UInt32 连接查询 ABI 同宽，无返回状态可检查。
    private typealias ChangeSpaces = @convention(c) (UInt32, CFArray, CFArray) -> Void
    private typealias FrontProcess = @convention(c) (UnsafeMutablePointer<ProcessSerialNumber>) -> OSStatus
    private typealias ProcessPID = @convention(c) (UnsafePointer<ProcessSerialNumber>, UnsafeMutablePointer<pid_t>) -> OSStatus

    private let library: UnsafeMutableRawPointer?
    private let connectionID: ConnectionID?
    private let copySpaces: CopySpaces?
    private let copyDisplay: CopyDisplay?
    private let displaySpace: DisplaySpace?
    private let addWindows: ChangeSpaces?
    private let removeWindows: ChangeSpaces?
    private let frontProcess: FrontProcess?
    private let processPID: ProcessPID?

    init() {
        let library = dlopen(
            "/System/Library/PrivateFrameworks/SkyLight.framework/SkyLight",
            RTLD_LAZY | RTLD_LOCAL
        )
        self.library = library
        self.connectionID = Self.symbol(["CGSMainConnectionID"], library: library).map {
            unsafeBitCast($0, to: ConnectionID.self)
        }
        self.copySpaces = Self.symbol(["SLSCopySpacesForWindows", "CGSCopySpacesForWindows"], library: library).map {
            unsafeBitCast($0, to: CopySpaces.self)
        }
        self.copyDisplay = Self.symbol(["SLSCopyManagedDisplayForWindow", "CGSCopyManagedDisplayForWindow"], library: library).map {
            unsafeBitCast($0, to: CopyDisplay.self)
        }
        self.displaySpace = Self.symbol(["SLSManagedDisplayGetCurrentSpace", "CGSManagedDisplayGetCurrentSpace"], library: library).map {
            unsafeBitCast($0, to: DisplaySpace.self)
        }
        // 写接口仅从显式打开的 SkyLight 句柄解析，不回退到任意全局符号。
        self.addWindows = library.flatMap { dlsym($0, "CGSAddWindowsToSpaces") }.map {
            unsafeBitCast($0, to: ChangeSpaces.self)
        }
        self.removeWindows = library.flatMap { dlsym($0, "CGSRemoveWindowsFromSpaces") }.map {
            unsafeBitCast($0, to: ChangeSpaces.self)
        }
        self.frontProcess = Self.symbol(["_SLPSGetFrontProcess"], library: library).map {
            unsafeBitCast($0, to: FrontProcess.self)
        }
        // ApplicationServices/HIServices/Processes.h defines this public read-only conversion.
        self.processPID = Self.symbol(["GetProcessPID"], library: library).map {
            unsafeBitCast($0, to: ProcessPID.self)
        }
    }

    deinit {
        if let library { dlclose(library) }
    }

    /// Return one entry per requested ID; missing SPI, malformed results and empty lists remain unknown.
    func memberships(windowIDs: [UInt32]) -> [UInt32: WindowSpaceMembership] {
        withExtendedLifetime(self) {
            var results: [UInt32: WindowSpaceMembership] = [:]
            guard !windowIDs.isEmpty else { return results }
            let connection = connectionID?()
            for windowID in windowIDs where results[windowID] == nil {
                guard let connection else {
                    results[windowID] = WindowSpaceMembership(spaceIDs: nil, currentSpaceID: nil)
                    continue
                }
                let spaces = spaces(for: windowID, connection: connection)
                let current = spaces == nil ? nil : currentSpace(for: windowID, connection: connection)
                results[windowID] = WindowSpaceMembership(spaceIDs: spaces, currentSpaceID: current)
            }
            return results
        }
    }

    /// 只迁移调用方实际持有的自有浮层；目标应用窗口号永远不进入写入数组。
    @MainActor func assignOverlay(_ panel: NSPanel, to destination: [UInt64]) -> Bool {
        guard NSApp.windows.contains(where: { $0 === panel }), panel.alphaValue == 0,
              let overlayID = UInt32(exactly: panel.windowNumber), overlayID != 0,
              !destination.isEmpty, destination.allSatisfy({ $0 > 0 }),
              let connection = connectionID?(), connection != 0,
              let original = spaces(for: overlayID, connection: connection) else { return false }
        if CursorSpaceRoute.matches(original, target: destination) { return true }
        guard let addWindows, let removeWindows else { return false }
        let windows = [NSNumber(value: overlayID)] as CFArray
        let missing = Set(destination).subtracting(original).sorted()
        if !missing.isEmpty {
            addWindows(connection, windows, missing.map { NSNumber(value: $0) } as CFArray)
        }
        guard let added = spaces(for: overlayID, connection: connection),
              Set(destination).isSubset(of: Set(added)) else { return false }
        let extra = Set(added).subtracting(destination).sorted()
        if !extra.isEmpty {
            removeWindows(connection, windows, extra.map { NSNumber(value: $0) } as CFArray)
        }
        // 接口返回 void：必须回读完整集合，禁止将发出调用当作迁移成功。
        return CursorSpaceRoute.matches(spaces(for: overlayID, connection: connection), target: destination)
    }

    /// Read WindowServer's current front PID directly, without NSWorkspace's activation cache.
    func frontPID() -> Int32? {
        withExtendedLifetime(self) {
            guard let frontProcess, let processPID else { return nil }
            var serial = ProcessSerialNumber(highLongOfPSN: 0, lowLongOfPSN: 0)
            var pid: pid_t = 0
            guard frontProcess(&serial) == 0, processPID(&serial, &pid) == 0, pid > 0 else { return nil }
            return pid
        }
    }

    private func spaces(for windowID: UInt32, connection: UInt32) -> [UInt64]? {
        guard let copySpaces else { return nil }
        // This SPI returns the union for all supplied windows, so each query contains exactly one ID.
        let windows = [NSNumber(value: windowID)] as CFArray
        guard let raw = copySpaces(connection, 0x7, windows) else { return nil }
        // Copy returns +1. Swift owns that retain through this local reference.
        let values = Unmanaged<CFArray>.fromOpaque(raw).takeRetainedValue()
        guard CFGetTypeID(values) == CFArrayGetTypeID() else { return nil }
        var result: [UInt64] = []
        for index in 0..<CFArrayGetCount(values) {
            guard let pointer = CFArrayGetValueAtIndex(values, index) else { return nil }
            let value = Unmanaged<CFTypeRef>.fromOpaque(pointer).takeUnretainedValue()
            guard CFGetTypeID(value) == CFNumberGetTypeID() else { return nil }
            let number = Unmanaged<CFNumber>.fromOpaque(pointer).takeUnretainedValue()
            var spaceID: Int64 = 0
            guard CFNumberGetValue(number, .sInt64Type, &spaceID), spaceID >= 0 else { return nil }
            if spaceID != 0 { result.append(UInt64(spaceID)) }
        }
        return result.isEmpty ? nil : result
    }

    private func currentSpace(for windowID: UInt32, connection: UInt32) -> UInt64? {
        guard let copyDisplay, let displaySpace,
              let raw = copyDisplay(connection, windowID) else { return nil }
        let display = Unmanaged<CFString>.fromOpaque(raw).takeRetainedValue()
        guard CFGetTypeID(display) == CFStringGetTypeID() else { return nil }
        let current = displaySpace(connection, display)
        return current == 0 ? nil : current
    }

    private static func symbol(_ names: [String], library: UnsafeMutableRawPointer?) -> UnsafeMutableRawPointer? {
        for name in names {
            if let library, let address = dlsym(library, name) { return address }
            if let address = dlsym(UnsafeMutableRawPointer(bitPattern: -2), name) { return address }
        }
        return nil
    }
}
