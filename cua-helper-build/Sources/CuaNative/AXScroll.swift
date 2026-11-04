// AX 滚动：后台窗口无法可靠接收滚轮事件时，直接驱动滚动条。
import Foundation

enum AXScrollPlan {
    /// 一次调用最多驱动几步，避免长距离滚动把页面甩飞。
    static func steps(deltaLinesY: Int) -> Int { min(abs(deltaLinesY), 10) }

    /// 与 cua_scroll 一致：正值表示显示上方内容，对应减少滚动条数值。
    static func action(deltaLinesY: Int) -> String {
        deltaLinesY > 0 ? "AXDecrement" : "AXIncrement"
    }

    static func isVerticalScrollBar(role: String?, subrole: String?) -> Bool {
        role == "AXScrollBar" && (subrole == "AXVerticalScrollBar" || subrole == "AXScrollBar")
    }
}
