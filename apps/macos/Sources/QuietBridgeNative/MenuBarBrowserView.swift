import AppKit
import UniformTypeIdentifiers

enum MenuBarExtensionSetup {
    static func directory(home: URL = FileManager.default.homeDirectoryForCurrentUser,
                          near executable: URL? = Bundle.main.executableURL) -> URL? {
        let installed = QuietBridgeIdentity.userRuntime(home: home).appendingPathComponent("extension/extension-stage")
        if FileManager.default.isReadableFile(atPath: installed.appendingPathComponent("manifest.json").path) { return installed }
        guard var parent = executable?.deletingLastPathComponent() else { return nil }
        for _ in 0..<8 {
            let source = parent.appendingPathComponent("extensions/chromium")
            if FileManager.default.isReadableFile(atPath: source.appendingPathComponent("manifest.json").path) { return source }
            let next = parent.deletingLastPathComponent()
            if next == parent { break }
            parent = next
        }
        return nil
    }
}

@MainActor final class MenuBarBrowserView: MenuBarCard {
    private let heading = MenuBarPanelView.label("任务与窗口", size: 17, weight: .semibold)
    private let status = MenuBarPanelView.label("正在连接", size: 11, color: MenuBarTheme.secondary)
    private let tabs = NSSegmentedControl(labels: ["概览", "活动", "下载", "截图批注"], trackingMode: .selectOne, target: nil, action: nil)
    private let sessions = NSPopUpButton(frame: .zero, pullsDown: false)
    private var sessionChoices: [MenuBarBrowserStore.Session] = []
    private var selectedChannel: String?
    private var selectedSession: MenuBarBrowserStore.Session? {
        sessionChoices.first(where: { $0.channel == selectedChannel })
    }
    private var stopSubmitted = false
    let setupButton = NSButton(title: "安装扩展…", target: nil, action: nil)
    private let backButton = NSButton(title: "‹", target: nil, action: nil)
    private let setupDescription = MenuBarPanelView.label("按下方步骤在 Chrome 中完成安装与授权。", size: 11, color: MenuBarTheme.secondary)
    private(set) var showingSetup = false
    private let scroll = NSScrollView()
    private let document = MenuBarCard()
    private let message = MenuBarPanelView.label("", size: 11, color: MenuBarTheme.warning)
    private var peer: [String: Any] = [:]
    private var state: [String: Any] = [:]
    private var lastSignature = ""
    private var taskId: String? { (state["taskSummary"] as? [String: Any])?["taskId"] as? String }
    private var reviewCanvas: BrowserReviewCanvas?
    private var note: NSTextField?
    var back: (() -> Void)?

    override init(frame: NSRect) {
        super.init(frame: frame)
        fill = MenuBarTheme.surface; radius = 0
        heading.frame = NSRect(x: 24, y: 22, width: 270, height: 25); addSubview(heading)
        backButton.target = self; backButton.action = #selector(goBack)
        backButton.frame = NSRect(x: 350, y: 18, width: 28, height: 28)
        backButton.isBordered = false; backButton.font = .systemFont(ofSize: 20)
        backButton.toolTip = "返回"; backButton.setAccessibilityLabel("返回主面板"); addSubview(backButton)
        status.frame = NSRect(x: 24, y: 52, width: 230, height: 18); addSubview(status)
        setupButton.frame = NSRect(x: 270, y: 47, width: 106, height: 26)
        setupButton.bezelStyle = .rounded; setupButton.controlSize = .small
        setupButton.font = .systemFont(ofSize: 11)
        setupButton.target = self; setupButton.action = #selector(toggleSetup)
        setupButton.setAccessibilityHelp("在控制面板内查看 Chrome 浏览器扩展的安装步骤")
        addSubview(setupButton)
        setupDescription.frame = NSRect(x: 24, y: 82, width: 352, height: 34)
        setupDescription.maximumNumberOfLines = 2; setupDescription.lineBreakMode = .byWordWrapping
        setupDescription.isHidden = true; addSubview(setupDescription)
        sessions.frame = NSRect(x: 24, y: 80, width: 352, height: 28)
        sessions.target = self; sessions.action = #selector(changeSession)
        sessions.setAccessibilityLabel("选择窗口占用会话")
        sessions.toolTip = "只改变控制面板显示；不改变 Agent 或预览的任务归属"
        addSubview(sessions)
        tabs.frame = NSRect(x: 24, y: 116, width: 352, height: 28)
        tabs.selectedSegment = 0; tabs.target = self; tabs.action = #selector(changeTab); addSubview(tabs)
        message.frame = NSRect(x: 24, y: 150, width: 352, height: 31); message.maximumNumberOfLines = 2
        message.lineBreakMode = .byWordWrapping; addSubview(message)
        scroll.frame = NSRect(x: 0, y: 184, width: 400, height: max(1, frame.height - 184))
        scroll.autoresizingMask = [.height]; scroll.drawsBackground = false; scroll.hasVerticalScroller = true
        document.fill = .clear; document.radius = 0
        scroll.documentView = document; addSubview(scroll)
        render()
    }
    required init?(coder: NSCoder) { nil }

    @objc private func goBack() { back?() }
    @objc private func changeTab() { lastSignature = ""; refresh() }
    @objc private func changeSession() {
        selectSession(sessions.selectedItem?.representedObject as? String)
        refresh()
    }
    func selectSession(_ channel: String?) {
        selectedChannel = channel
        stopSubmitted = false
        state = [:]; lastSignature = ""
    }
    @objc private func toggleSetup() {
        showingSetup.toggle()
        heading.stringValue = showingSetup ? "浏览器扩展" : "任务与窗口"
        setupButton.title = showingSetup ? "返回任务" : "安装扩展…"
        setupButton.frame = showingSetup ? NSRect(x: 288, y: 20, width: 88, height: 28)
                                        : NSRect(x: 270, y: 47, width: 106, height: 26)
        status.frame.size.width = showingSetup ? 352 : 230
        backButton.isHidden = showingSetup
        tabs.isHidden = showingSetup
        sessions.isHidden = showingSetup
        setupDescription.isHidden = !showingSetup
        message.stringValue = ""
        render()
    }

    func refresh(snapshot supplied: MenuBarBrowserStore.Snapshot? = nil) {
        do {
            let snapshot = try supplied ?? MenuBarBrowserStore.snapshot()
            sessionChoices = snapshot.sessions
            if !sessionChoices.contains(where: { $0.channel == selectedChannel }) { selectedChannel = nil }
            sessions.removeAllItems()
            sessions.addItem(withTitle: "选择会话（\(sessionChoices.count) 项）…")
            for choice in sessionChoices {
                sessions.addItem(withTitle: choice.label)
                sessions.lastItem?.representedObject = choice.channel
            }
            sessions.selectItem(at: sessionChoices.firstIndex(where: { $0.channel == selectedChannel }).map { $0 + 1 } ?? 0)
            sessions.isEnabled = !sessionChoices.isEmpty
            let next = selectedSession?.peer ?? snapshot.rootPeer
            let now = Date().timeIntervalSince1970
            let heartbeatLimit: TimeInterval = next["enabled"] as? Bool == true ? 15 : 75
            let connected = (next["lastSeen"] as? TimeInterval).map { now - $0 < heartbeatLimit } ?? false
            peer = next
            state = connected && selectedChannel != nil ? (next["panelState"] as? [String: Any] ?? [:]) : [:]
            if let selected = selectedSession {
                if (state["taskSummary"] as? [String: Any])?["taskId"] as? String != selected.taskId { state = [:] }
                var summary = state["taskSummary"] as? [String: Any] ?? [:]
                summary["taskId"] = selected.taskId
                summary["canStop"] = selected.canStop
                summary["windowId"] = selected.windowId
                summary["hostSessionId"] = selected.hostSessionId
                summary["stopping"] = selected.stopping
                state["taskSummary"] = summary
            }
            status.stringValue = !connected ? "扩展未连接" : next["permission"] as? Bool != true ? "等待站点授权" : next["enabled"] as? Bool != true ? "观察已停用" : next["panelState"] == nil ? "等待任务状态，请确认原生宿主已更新" : "扩展已连接"
            if selectedSession?.stopping == true {
                message.stringValue = "已撤销本轮控制，正在停止并释放窗口…"
            } else if stopSubmitted {
                message.stringValue = "停止请求已提交，请确认对应共享已关闭。"
            } else if let result = next["panelResult"] as? [String: Any], result["status"] as? String == "error" {
                message.stringValue = result["error"] as? String ?? "操作失败"
            } else if next["panelCommand"] is [String: Any] { message.stringValue = "正在处理，请稍候…" }
            else { message.stringValue = "" }
            var content = state
            content.removeValue(forKey: "at")
            let signatureData = try JSONSerialization.data(withJSONObject: [content, tabs.selectedSegment], options: [.sortedKeys])
            let signature = signatureData.base64EncodedString()
            if signature != lastSignature { lastSignature = signature; render() }
        } catch MenuBarBrowserStore.StoreError.busy {
            return
        } catch {
            status.stringValue = "扩展未连接"; message.stringValue = error.localizedDescription
            state = [:]
            if !lastSignature.isEmpty { lastSignature = ""; render() }
        }
    }

    private func addLabel(_ text: String, y: CGFloat, size: CGFloat = 12, color: NSColor = MenuBarTheme.text,
                          height: CGFloat = 20, weight: NSFont.Weight = .regular) {
        let label = MenuBarPanelView.label(text, size: size, weight: weight, color: color)
        label.frame = NSRect(x: 24, y: y, width: 352, height: height)
        label.maximumNumberOfLines = 0; label.lineBreakMode = .byWordWrapping; document.addSubview(label)
    }
    @discardableResult private func button(_ title: String, y: CGFloat, action: Selector, x: CGFloat = 24, width: CGFloat = 150) -> NSButton {
        let button = NSButton(title: title, target: self, action: action)
        button.frame = NSRect(x: x, y: y, width: width, height: 30); button.bezelStyle = .rounded
        document.addSubview(button); return button
    }
    private func render() {
        document.subviews.forEach { $0.removeFromSuperview() }; reviewCanvas = nil; note = nil
        if showingSetup { renderSetup(); return }
        var height: CGFloat = max(1, scroll.bounds.height)
        let summary = state["taskSummary"] as? [String: Any]
        switch tabs.selectedSegment {
        case 0:
            addLabel("当前窗口占用", y: 4, size: 13)
            addLabel(summary?["url"] as? String ?? "暂无任务页面", y: 36, size: 14, height: 42)
            let target = (summary?["windowId"] as? Int).map { "窗口：\($0)" } ?? "等待绑定窗口"
            let host = (summary?["hostSessionId"] as? String).map { "WorkBuddy 会话：\($0)" } ?? "宿主归属待确认"
            addLabel("\(target)\n\(host)", y: 84, size: 11, color: MenuBarTheme.secondary, height: 48)
            addLabel(taskId.map { "任务 ID：\($0)" } ?? "请先选择要查看的会话", y: 138, size: 11, color: MenuBarTheme.secondary, height: 38)
            if summary?["canStop"] as? Bool == true {
                button("停止此会话控制", y: 190, action: #selector(stopTask), width: 180)
                addLabel("停止此会话本轮的界面操作并释放窗口。已发出的操作可能已执行；定时计划需在 WorkBuddy 单独暂停。",
                         y: 230, size: 11, color: MenuBarTheme.secondary, height: 60)
            }
            height = max(height, 304)
        case 1:
            let items = state["activity"] as? [[String: Any]] ?? []
            if items.isEmpty { addLabel("暂无活动记录", y: 12, color: MenuBarTheme.secondary) }
            let names = ["snapshot": "页面快照", "screenshot": "页面截图", "diagnostics": "页面诊断", "click": "点击", "fill": "填写", "select": "选择", "scroll": "滚动", "navigate": "导航", "keypress": "按键", "download": "候选下载"]
            let outcomes = ["completed": "已返回", "not_performed": "未执行", "failed": "失败", "started": "已开始"]
            var y: CGFloat = 8
            for item in items {
                let time = Date(timeIntervalSince1970: (item["at"] as? Double ?? 0) / 1000)
                let operation = names[item["operation"] as? String ?? ""] ?? "操作"
                let outcome = outcomes[item["outcome"] as? String ?? ""] ?? "未知"
                addLabel("\(time.formatted(date: .omitted, time: .shortened))  ·  \(operation)  ·  \(outcome)", y: y)
                addLabel(item["site"] as? String ?? "", y: y + 22, size: 11, color: MenuBarTheme.secondary)
                y += 54
            }
            if !items.isEmpty { button("清除活动记录", y: y + 8, action: #selector(clearActivity)); y += 50 }
            height = max(height, y + 20)
        case 2:
            let items = state["downloads"] as? [[String: Any]] ?? []
            addLabel("候选下载的保存位置和归属请在浏览器中确认。", y: 4, size: 11, color: MenuBarTheme.secondary, height: 34)
            if items.isEmpty { addLabel("暂无候选下载", y: 52, color: MenuBarTheme.secondary) }
            let labels = ["started": "已开始", "inProgress": "下载中", "completed": "已完成", "canceled": "已取消"]
            var y: CGFloat = 52
            for item in items {
                addLabel(item["filename"] as? String ?? "未命名文件", y: y)
                addLabel(labels[item["state"] as? String ?? ""] ?? "状态未知", y: y + 22, size: 11, color: MenuBarTheme.secondary); y += 54
            }
            height = max(height, y + 20)
        default:
            let capture = button("截取任务页面", y: 4, action: #selector(capture))
            capture.isEnabled = summary?["canStop"] as? Bool == true
            guard let review = state["review"] as? [String: Any], let encoded = review["image"] as? String,
                  let data = Data(base64Encoded: encoded), let image = NSImage(data: data) else {
                addLabel(state["reviewOmitted"] as? Bool == true ? "截图暂不可显示：本地结果存储已满。" : "暂无截图", y: 56, color: MenuBarTheme.secondary, height: 36); break
            }
            let canvas = BrowserReviewCanvas(frame: NSRect(x: 24, y: 48, width: 352, height: 220))
            canvas.image = image; canvas.annotations = review["annotations"] as? [[String: Any]] ?? []
            document.addSubview(canvas); reviewCanvas = canvas
            let field = NSTextField(frame: NSRect(x: 24, y: 280, width: 352, height: 26))
            field.placeholderString = "输入批注内容"; document.addSubview(field); note = field
            button("添加批注", y: 316, action: #selector(addAnnotation), width: 110)
            button("导出截图", y: 316, action: #selector(exportReview), x: 144, width: 110)
            var y: CGFloat = 360
            for (index, annotation) in canvas.annotations.enumerated() {
                addLabel("\(index + 1). \(annotation["note"] as? String ?? "")", y: y, height: 40)
                let remove = button("删除", y: y + 40, action: #selector(removeAnnotation(_:)), width: 60)
                remove.tag = index; y += 78
            }
            height = max(height, y + 20)
        }
        document.frame = NSRect(x: 0, y: 0, width: 400, height: height)
    }

    private func renderSetup() {
        let directory = MenuBarExtensionSetup.directory()
        addLabel("1 · 打开 Chrome 扩展管理", y: 4, size: 13, weight: .medium)
        button("打开扩展管理", y: 34, action: #selector(openChromeExtensions))
        addLabel("开启右上角“开发者模式”，再点击“加载已解压的扩展程序”。",
                 y: 76, size: 11, color: MenuBarTheme.secondary, height: 40)
        addLabel("2 · 选择扩展目录", y: 130, size: 13, weight: .medium)
        addLabel("在文件窗口中按 ⇧⌘G，粘贴扩展路径，再选择该文件夹。",
                 y: 160, size: 11, color: MenuBarTheme.secondary, height: 40)
        let pathCard = MenuBarCard(frame: NSRect(x: 24, y: 208, width: 352, height: 76))
        pathCard.fill = MenuBarTheme.window; pathCard.radius = 8
        let path = MenuBarPanelView.label(directory.map { ($0.path as NSString).abbreviatingWithTildeInPath }
            ?? "未找到扩展安装文件，请重新安装 QuietBridge。", size: 11, color: MenuBarTheme.secondary)
        path.frame = NSRect(x: 12, y: 10, width: 328, height: 56)
        path.maximumNumberOfLines = 3; path.lineBreakMode = .byCharWrapping
        pathCard.addSubview(path); document.addSubview(pathCard)
        let copy = button("复制路径", y: 296, action: #selector(copyExtensionPath), width: 106)
        copy.identifier = NSUserInterfaceItemIdentifier("browser_setup_copy_path"); copy.isEnabled = directory != nil
        let reveal = button("显示文件夹", y: 296, action: #selector(revealExtensionDirectory), x: 142, width: 106)
        reveal.identifier = NSUserInterfaceItemIdentifier("browser_setup_reveal_directory"); reveal.isEnabled = directory != nil
        addLabel("3 · 授权并启用", y: 352, size: 13, weight: .medium)
        addLabel("点击 Chrome 工具栏中的扩展图标，选择“授权并启用”，再允许访问所需站点。",
                 y: 382, size: 11, color: MenuBarTheme.secondary, height: 42)
        addLabel("完成后，上方连接状态会自动更新。", y: 434, size: 11, color: MenuBarTheme.secondary)
        document.frame = NSRect(x: 0, y: 0, width: 400, height: max(scroll.bounds.height, 478))
        scroll.contentView.scroll(to: .zero)
    }

    @objc private func openChromeExtensions() {
        let chrome = URL(fileURLWithPath: "/Applications/Google Chrome.app")
        guard FileManager.default.fileExists(atPath: chrome.path), let url = URL(string: "chrome://extensions") else {
            message.stringValue = "请先安装 Google Chrome，再打开扩展管理。"; return
        }
        NSWorkspace.shared.open([url], withApplicationAt: chrome, configuration: NSWorkspace.OpenConfiguration()) { [weak self] _, error in
            guard error != nil else { return }
            Task { @MainActor in self?.message.stringValue = "无法打开扩展管理，请在 Chrome 地址栏输入 chrome://extensions。" }
        }
    }

    @objc private func copyExtensionPath() {
        guard let directory = MenuBarExtensionSetup.directory() else { return }
        NSPasteboard.general.clearContents()
        NSPasteboard.general.setString(directory.path, forType: .string)
        message.stringValue = "扩展路径已复制，可在文件窗口中按 ⇧⌘G 粘贴。"
    }

    @objc private func revealExtensionDirectory() {
        guard let directory = MenuBarExtensionSetup.directory() else { return }
        NSWorkspace.shared.activateFileViewerSelecting([directory])
    }
    private func submit(_ operation: String, arguments: [String: Any] = [:]) {
        guard let taskId, let selectedChannel else { message.stringValue = "请先选择浏览器会话"; return }
        do { try MenuBarBrowserStore.submit(operation, channel: selectedChannel, taskId: taskId, arguments: arguments); message.stringValue = "正在处理，请稍候…" }
        catch { message.stringValue = error.localizedDescription }
    }
    @objc private func stopTask() {
        guard let selectedSession else { message.stringValue = "请先选择要停止的会话"; return }
        do {
            try MenuBarBrowserStore.stop(selectedSession)
            stopSubmitted = true
            refresh()
        } catch { message.stringValue = error.localizedDescription }
    }
    @objc private func clearActivity() { submit("clear_activity") }
    @objc private func capture() { submit("capture_review") }
    @objc private func addAnnotation() {
        guard let box = reviewCanvas?.selection, box.width > 0.005, box.height > 0.005,
              let text = note?.stringValue.trimmingCharacters(in: .whitespacesAndNewlines), !text.isEmpty, text.count <= 200 else {
            message.stringValue = "请先选择截图区域并填写不超过 200 字的批注。"; return
        }
        submit("annotate_review", arguments: ["box": ["x": box.minX, "y": box.minY, "width": box.width, "height": box.height], "note": text])
    }
    @objc private func removeAnnotation(_ sender: NSButton) { submit("remove_review_annotation", arguments: ["index": sender.tag]) }
    @objc private func exportReview() {
        guard let canvas = reviewCanvas, let image = canvas.exportImage(), let tiff = image.tiffRepresentation,
              let bitmap = NSBitmapImageRep(data: tiff), let data = bitmap.representation(using: .png, properties: [:]) else { return }
        let panel = NSSavePanel(); panel.allowedContentTypes = [.png]; panel.nameFieldStringValue = "browser-review.png"
        if panel.runModal() == .OK, let url = panel.url {
            do { try data.write(to: url, options: .atomic) } catch { message.stringValue = error.localizedDescription }
        }
    }
}

@MainActor private final class BrowserReviewCanvas: NSView {
    var image: NSImage? { didSet { needsDisplay = true } }
    var annotations: [[String: Any]] = [] { didSet { needsDisplay = true } }
    var selection: NSRect?
    private var start: NSPoint?
    override var isFlipped: Bool { true }
    private var imageRect: NSRect {
        guard let image, image.size.width > 0, image.size.height > 0 else { return .zero }
        let scale = min(bounds.width / image.size.width, bounds.height / image.size.height)
        let size = NSSize(width: image.size.width * scale, height: image.size.height * scale)
        return NSRect(x: (bounds.width - size.width) / 2, y: (bounds.height - size.height) / 2, width: size.width, height: size.height)
    }
    override func draw(_ dirtyRect: NSRect) {
        MenuBarTheme.window.setFill(); bounds.fill()
        guard let image else { return }
        image.draw(in: imageRect, from: .zero, operation: .sourceOver, fraction: 1, respectFlipped: true, hints: nil)
        NSColor.systemRed.setStroke()
        for (index, annotation) in annotations.enumerated() {
            guard let box = annotation["box"] as? [String: Double], let x = box["x"], let y = box["y"], let w = box["width"], let h = box["height"] else { continue }
            let frame = rect(NSRect(x: x, y: y, width: w, height: h))
            NSBezierPath(rect: frame).stroke()
            ("\(index + 1)" as NSString).draw(at: frame.origin, withAttributes: [.font: NSFont.boldSystemFont(ofSize: 12), .foregroundColor: NSColor.systemRed, .backgroundColor: NSColor.white])
        }
        if let selection { NSBezierPath(rect: rect(selection)).stroke() }
    }
    private func rect(_ box: NSRect) -> NSRect { NSRect(x: imageRect.minX + box.minX * imageRect.width, y: imageRect.minY + box.minY * imageRect.height, width: box.width * imageRect.width, height: box.height * imageRect.height) }
    private func point(_ event: NSEvent) -> NSPoint {
        let p = convert(event.locationInWindow, from: nil), r = imageRect
        return NSPoint(x: max(0, min(1, (p.x - r.minX) / max(1, r.width))), y: max(0, min(1, (p.y - r.minY) / max(1, r.height))))
    }
    override func mouseDown(with event: NSEvent) { guard imageRect.contains(convert(event.locationInWindow, from: nil)) else { return }; start = point(event); selection = nil; needsDisplay = true }
    override func mouseDragged(with event: NSEvent) {
        guard let start else { return }; let end = point(event)
        selection = NSRect(x: min(start.x, end.x), y: min(start.y, end.y), width: abs(end.x - start.x), height: abs(end.y - start.y)); needsDisplay = true
    }
    override func mouseUp(with event: NSEvent) { start = nil }
    func exportImage() -> NSImage? {
        guard let image else { return nil }
        let size = image.size
        let font = NSFont.systemFont(ofSize: max(14, size.width / 70))
        let margin = max(16, size.width / 50)
        let attributes: [NSAttributedString.Key: Any] = [.font: font, .foregroundColor: NSColor.black]
        let notes = annotations.enumerated().map { "\($0.offset + 1). \($0.element["note"] as? String ?? "")" }.joined(separator: "\n")
        let textRect = (notes as NSString).boundingRect(with: NSSize(width: size.width - 2 * margin, height: 100000), options: [.usesLineFragmentOrigin, .usesFontLeading], attributes: attributes)
        let extra = notes.isEmpty ? 0 : ceil(textRect.height) + 2 * margin
        let output = NSImage(size: NSSize(width: size.width, height: size.height + extra))
        output.lockFocus()
        NSColor.white.setFill(); NSRect(origin: .zero, size: output.size).fill()
        image.draw(in: NSRect(x: 0, y: extra, width: size.width, height: size.height))
        NSColor.systemRed.setStroke()
        for (index, annotation) in annotations.enumerated() {
            guard let box = annotation["box"] as? [String: Double], let x = box["x"], let y = box["y"], let w = box["width"], let h = box["height"] else { continue }
            let rect = NSRect(x: x * size.width, y: extra + (1 - y - h) * size.height, width: w * size.width, height: h * size.height)
            let path = NSBezierPath(rect: rect); path.lineWidth = max(2, size.width / 500); path.stroke()
            ("\(index + 1)" as NSString).draw(at: NSPoint(x: rect.minX, y: rect.maxY - font.pointSize), withAttributes: [.font: font, .foregroundColor: NSColor.systemRed, .backgroundColor: NSColor.white])
        }
        if !notes.isEmpty { (notes as NSString).draw(with: NSRect(x: margin, y: margin, width: size.width - 2 * margin, height: textRect.height), options: [.usesLineFragmentOrigin, .usesFontLeading], attributes: attributes) }
        output.unlockFocus()
        return output
    }
}
