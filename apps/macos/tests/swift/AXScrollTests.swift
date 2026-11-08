import Testing
@testable import QuietBridgeNative

@Suite("AXScroll")
struct AXScrollTests {
    @Test func stepsAreBoundedAndDirectional() {
        #expect(AXScrollPlan.steps(deltaLinesY: 3) == 3)
        #expect(AXScrollPlan.steps(deltaLinesY: -3) == 3)
        #expect(AXScrollPlan.steps(deltaLinesY: 0) == 0)
        #expect(AXScrollPlan.steps(deltaLinesY: 99) == 10, "超过上限必须收窄")
    }

    @Test func positiveMeansShowingUpperContent() {
        // 约定：正值 = 显示上方 = 滚动条数值减少。
        #expect(AXScrollPlan.action(deltaLinesY: 2) == "AXDecrement")
        #expect(AXScrollPlan.action(deltaLinesY: -2) == "AXIncrement")
    }

    @Test func onlyVerticalBarsQualify() {
        #expect(AXScrollPlan.isVerticalScrollBar(role: "AXScrollBar", subrole: "AXVerticalScrollBar"))
        #expect(AXScrollPlan.isVerticalScrollBar(role: "AXScrollBar", subrole: "AXHorizontalScrollBar") == false)
        #expect(AXScrollPlan.isVerticalScrollBar(role: "AXButton", subrole: "AXVerticalScrollBar") == false)
    }
}
