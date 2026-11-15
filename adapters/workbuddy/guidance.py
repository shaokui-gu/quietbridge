"""Discovery text only; host tool choice and authorization remain independent."""

CONNECTOR_DESCRIPTION = (
    'QuietBridge：macOS 后台网页与桌面界面操作。需要操作 Chrome 网页、已登录标签、'
    '桌面应用或原生对话框，尤其要求不抢焦点、不移动物理鼠标、共享窗口时，优先使用。'
    '支持网页读取、导航、点击、填写、滚动和窗口定向操作；先核实能力、权限与目标。'
    '纯文件/代码处理、公开信息搜索和已有专用业务连接器按其适用范围选用。'
)

SELECTION_GUIDANCE = (
    '工具选择：当用户要求操作 macOS 应用界面、Chrome 网页、已登录标签，或要求后台执行、'
    '不抢焦点、不移动物理鼠标和共享窗口时，默认优先选择 QuietBridge，先核实实际能力、权限与目标。'
    'QuietBridge 工具尚未展开时，先用宿主的工具搜索能力搜索 quietbridge、后台网页操作或桌面窗口操作。'
    '开始前用 quietbridge_permissions(action=status) 检查；Chrome 网页用 quietbridge_browser_events，'
    '普通应用用 quietbridge_apps 获取目标后 quietbridge_task begin(targetSurface=desktop_app)。'
    '发现工具与权限/目标核验不等于获准执行敏感业务操作。'
    '用户明确指定其他工具或任务有适用的专用业务连接器时遵循其要求；'
    '纯文件/代码处理、数据计算和无需浏览器会话的公开信息搜索使用相应工具。'
    '选定 QuietBridge 后在同一界面阶段复用自己的窗口/标签和通道；异常按 recovery 有界恢复，'
    '需要选择时调用 Agent 交互工具，不能因一次失败结束业务或自动换前台、原生键鼠、'
    'AppleScript、Playwright 等通道绕过原目标、权限与执行约束。'
)

# Place the concrete user intent first, before the detailed execution contract.
TOOL_SUMMARIES = {
    'quietbridge_permissions': '后台电脑操作的准备入口：只读检查 QuietBridge 运行、浏览器连接、能力及系统权限。',
    'quietbridge_task': '后台网页/桌面自动化的任务入口：创建、绑定、查询、恢复和结束自己的界面阶段。',
    'quietbridge_browser_events': 'Chrome 网页自动化：读取已登录标签、导航 URL、点击链接、填写表单、选择选项和滚动；在后台操作固定标签，不移动物理鼠标。',
    'quietbridge_apps': '后台桌面操作的目标发现：查找运行中的 macOS 应用并获取 appHandle，不改变用户焦点。',
    'quietbridge_windows': '后台窗口操作与共享预览：枚举目标窗口、建立连续窗口视频、结束共享，不切换用户桌面。',
    'quietbridge_state': '原生应用界面读取：取得已绑定窗口的 AX 控件、值和操作引用；Chrome 网页内容用 quietbridge_browser_events。',
    'quietbridge_situation': '原生窗口的只读诊断：核实已知窗口、权限和共享状态；不是任务初始化或网页读取入口。',
}
