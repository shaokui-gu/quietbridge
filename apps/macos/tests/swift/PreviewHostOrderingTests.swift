import AppKit
import Testing
@testable import QuietBridgeNative

private let orderingHost = PreviewHostWindow(windowID: 10, pid: 42,
    bounds: CGRect(x: 100, y: 100, width: 1000, height: 700), isOnScreen: true)

private func window(_ id: UInt32, pid: Int32 = 99, level: Int = 0) -> PreviewHostOrdering.Window {
    .init(id: id, pid: id == 10 ? 42 : pid, level: level)
}

private func plan(_ ids: [UInt32], _ windows: [PreviewHostOrdering.Window]) -> [PreviewHostOrdering.Placement]? {
    PreviewHostOrdering.plan(host: orderingHost, panelIDs: ids, panelPID: 99, windows: windows)
}

@Test func previewOrderingLeavesAnAdjacentPreviewBehindTheForegroundApplication() {
    // The active app is allowed above both; a floating-level menu is also untouched.
    #expect(plan([20], [window(70, level: 3), window(30), window(20), window(10), window(40)]) == [])
}

@Test func previewOrderingRepairsAnInterveningApplicationWithoutRaisingTheHost() {
    let result = plan([20], [window(20), window(30), window(10), window(40)])
    #expect(result == [.init(windowID: 20, relativeTo: 10)])
    #expect(result?.allSatisfy { $0.windowID != 10 && $0.relativeTo != 0 } == true)
    // Host brought forward by the user: restore its preview, not the foreign window behind it.
    #expect(plan([20], [window(10), window(30), window(20)]) == [.init(windowID: 20, relativeTo: 10)])
}

@Test func previewOrderingKeepsTheWholeSessionDeckContiguousWithTheSelectedCardInFront() {
    let placements: [PreviewHostOrdering.Placement] = [
        .init(windowID: 22, relativeTo: 10), .init(windowID: 21, relativeTo: 22),
        .init(windowID: 20, relativeTo: 21)]
    #expect(plan([22, 21, 20], [window(30), window(20), window(21), window(40), window(22), window(10)]) == placements)
    #expect(plan([22, 21, 20], [window(30), window(20), window(21), window(22), window(10)]) == [])
    #expect(plan([20, 22, 21], [window(30), window(20), window(21), window(22), window(10)]) != [])
}

@Test func previewOrderingReattachesMissingCardsAndDoesNotAffectAnotherHostsDeck() {
    #expect(plan([20], [window(50), window(11, pid: 43), window(10)]) == [.init(windowID: 20, relativeTo: 10)])
    #expect(plan([20], [window(50), window(11, pid: 43), window(20), window(10)]) == [])
}

@Test func previewOrderingRefusesMissingOrReusedHostIDsAndMismatchedWindowLevels() {
    #expect(plan([20], [window(20), window(30)]) == nil)
    #expect(plan([20], [window(20), .init(id: 10, pid: 43, level: 0)]) == nil)
    #expect(plan([20], [window(20), window(10, level: 3)]) == nil)
    #expect(plan([20], [window(20, pid: 43), window(10)]) == nil)
    #expect(plan([20], [window(20, level: 3), window(10)]) == nil)
    for ids: [UInt32] in [[], [0], [10], [20, 20]] {
        #expect(plan(ids, [window(20), window(10)]) == nil)
    }
}

@Test @MainActor func previewOrderingDoesNotDisplayHiddenPanelsOrOperateOnFloatingPanels() {
    _ = NSApplication.shared
    let panel = NSPanel(contentRect: CGRect(x: 0, y: 0, width: 1, height: 1),
                        styleMask: [.borderless, .nonactivatingPanel], backing: .buffered, defer: false)
    panel.level = .normal
    #expect(!PreviewHostOrdering.restore(panels: [panel], host: orderingHost))
    #expect(!panel.isVisible)
    panel.level = .floating
    #expect(!PreviewHostOrdering.restore(panels: [panel], host: orderingHost))
    #expect(!panel.isVisible)
}
