"""Agent-facing recovery policy. Metadata is guidance, not host enforcement."""
GUIDANCE = ('异常恢复：遇到 recovery.status=user_action_required，先向用户说明原因和具体操作，'
            '暂停依赖该能力的步骤，等待用户完成后只读复查权限、连接、原目标和共享窗口。'
            '禁止自动开启开关/授权、反复重启、跳过失败步骤或换其他工具绕过；'
            '副作用未知时不重复输入。连接器整个不可用时也应通知用户恢复原连接器。')

USER_ACTIONS = {
    'APP_NOT_RUNNING': ('open_app', '请打开“QuietBridge”，确认菜单栏图标出现，完成后告诉我继续。'),
    'CAPABILITY_DISABLED': ('enable_capability', '请在 QuietBridge 控制面板开启本任务所需的能力；如出现授权向导，请按说明完成后告诉我继续。'),
    'PERMISSION_DENIED': ('grant_permission', '请打开 QuietBridge 的权限向导，按说明授权所需的辅助功能或屏幕录制权限，完成后告诉我继续。'),
    'SCREEN_RECORDING_DENIED': ('grant_permission', '请通过 QuietBridge 权限向导开启屏幕录制权限，完成后告诉我继续。'),
    'SESSION_LOCKED': ('unlock_session', '请解锁电脑，完成后告诉我继续。'),
    'MANUAL_AUTHENTICATION_REQUIRED': ('finish_manual_unlock', '请用户完成系统解锁；这表示等待手动认证，不表示任务取消。保留原任务和目标绑定，解锁后重新观察原目标、核实共享窗口，再继续未执行步骤；不要重放结果未知的动作、更换通道或为此结束重建任务。'),
    'HELPER_MISSING': ('repair_installation', 'QuietBridge 原生组件缺失，请重新安装应用，完成后告诉我继续。'),
    'HELPER_BROKEN': ('restart_connector', '原生组件连接已断开。请先确认 QuietBridge 正在运行；若仍无法连接，请在宿主客户端中重启 QuietBridge 连接器，完成后告诉我继续。'),
    'HELPER_TIMEOUT': ('check_connection', '原生组件响应超时。请检查 QuietBridge 是否正常运行；若持续无响应，请重启该连接器，完成后告诉我继续。'),
    'BROWSER_HOST_UNAVAILABLE': ('connect_extension', '请确认浏览器中的 QuietBridge 扩展已启用，并在应用控制面板检查扩展连接，恢复后告诉我继续。'),
    'BROWSER_REQUEST_REJECTED': ('check_browser_protocol', '原生端已响应但拒绝了请求，不代表进程退出。请检查 QuietBridge 应用和扩展版本、权限及原任务归属；不要反复重连或重放输入。'),
    'BROWSER_STORAGE_UNAVAILABLE': ('check_browser_storage', '原生端暂时无法访问任务存储。请检查 QuietBridge 应用及本地文件访问状态，恢复后先只读核实原目标。'),
    'INVALID_BROWSER_RESULT': ('check_browser_result', '扩展回执未通过校验，已作为失败记录，未采信其内容。请核对应用和扩展版本，保留原目标；若涉及输入，先观察已有结果，不能重放。'),
    'BROWSER_PERMISSION_MISSING': ('authorize_extension', '请在 QuietBridge 控制面板开启浏览器能力，并按扩展说明完成授权，完成后告诉我继续。'),
    'BROWSER_DISABLED': ('enable_browser', '请在 QuietBridge 控制面板开启浏览器能力，完成后告诉我继续。'),
    'POPUP_GUARD_LEGACY': ('refresh_legacy_guard_document', '旧版弹窗守卫残留在原文档中。请通知用户刷新该专用标签一次，以替换旧页面钩子；未执行本次输入，不自动刷新或导航，不停用扩展、不换工具。刷新后核实新文档及原目标，再继续新操作。'),
    'POPUP_GUARD_CONFLICT': ('inspect_popup_guard_lease', '页面弹窗守卫租约未确认，本次输入未执行。保留原任务和标签，只读核实任务与文档；通知用户确认归属，不强制接管、换工具或重放输入。'),
    'BROWSER_DEBUGGER_RESTRICTED': ('resolve_extension_conflict', 'Chrome 拒绝该目标的调试访问。请用户检查原页面中的其他扩展注入；保留原任务和标签，不自动停用扩展、更换目标或重放输入。独立受管浏览器不是即用恢复路径：需用户确认新阶段、加载扩展并重新登录，不能复制原 profile 或声称继承原登录态。恢复后先只读观察已有结果。'),
    'BACKGROUND_COMMAND_TIMEOUT': ('inspect_background_timeout', '页面输入未按时回执，结果未知，不能重放。先用原标签的 snapshot/diagnostics 核实已发生的变化并通知用户；不通过改绑、激活窗口、切换输入通道或反复重连继续点击。'),
    'TASK_SCOPE_REQUIRED': ('confirm_task_scope', '当前操作超出已绑定阶段的范围。保留原任务和结果，先通知用户确认恢复方式；不能自动结束或改绑。新独立浏览器须在未绑定阶段 prepare/start，再核实返回的真实 PID/窗口并 bind；用户需单独加载扩展及登录。'),
    'EXTENSION_UPGRADE_REQUIRED': ('update_extension', '请在浏览器扩展管理页重新加载或更新 QuietBridge 扩展，完成后告诉我继续。'),
    'READ_ONLY': ('change_read_only', '当前连接仅允许观察。请确认是否要解除只读限制；完成配置并重新连接后告诉我继续。'),
    'APP_NOT_ALLOWED': ('approve_target', '当前目标不在允许列表中。请确认并配置该应用的访问范围，完成后告诉我继续。'),
    'FOREGROUND_REQUIRED': ('resolve_background_limit', '此操作无法在保持用户焦点的条件下完成，任务已暂停，请确认可用的后台操作方式。'),
    'FOCUS_STATE_UNKNOWN': ('check_focus_state', '无法核实用户焦点，已暂停输入。请检查目标与共享窗口状态，完成后告诉我继续。'),
    'UNSAFE_WINDOW_DELIVERY': ('resolve_background_limit', '无法确认输入只送往目标窗口，已暂停。请检查目标是否支持后台操作，完成后告诉我继续。'),
    'SESSION_ROUTING_REQUIRED': ('resolve_session_routing', '新阶段尚未核实属于当前保护会话。保留原会话，等待宿主 begin 回执后只读复查；不同会话需要独立连接或宿主提供可靠调用上下文。若仍阻塞，请通知用户；不能结束原会话、猜测归属或换工具绕过。'),
    'SESSION_ROUTING_INVALID': ('refresh_host_context', '宿主调用凭据失效或已经使用。请确认 QuietBridge hooks 已更新；不能猜测会话身份、重用凭据或换工具绕过。'),
    'SESSION_CAPACITY_FULL': ('wait_for_session_capacity', '当前已达到三会话上限。请用户结束自己的一项后再开始；不能抢占或结束其他会话。'),
    'TARGET_IN_USE': ('select_independent_target', '该窗口或原生应用已被另一会话使用。请用户提供独立目标或等待其完成；不能接管、更换通道或强行并行输入。'),
}
WAITING_CODES = {'host_unavailable': 'BROWSER_HOST_UNAVAILABLE',
                 'browser_channel_starting': 'BROWSER_CHANNEL_STARTING',
                 'observer_not_ready': 'BROWSER_OBSERVER_NOT_READY',
                 'permission_missing': 'BROWSER_PERMISSION_MISSING',
                 'disabled': 'BROWSER_DISABLED',
                 'extension_upgrade_required': 'EXTENSION_UPGRADE_REQUIRED'}
REFRESH_CODES = {'STALE_SNAPSHOT', 'STALE_APP', 'REQUIRES_REVALIDATION', 'CONNECTOR_UPDATED',
                 'BROWSER_TARGET_CHANGED', 'STALE_TARGET', 'TASK_NOT_BOUND', 'SESSION_WORKER_EXITED', 'NATIVE_WORKER_EXITED'}
WAIT_CODES = {'CAPTURE_BUSY', 'SHARE_NOT_READY', 'PREVIEW_NOT_READY', 'SHARE_REQUIRED',
              'BROWSER_CHANNEL_STARTING',
              'BROWSER_OBSERVER_NOT_READY', 'BROWSER_LIFECYCLE_TIMEOUT', 'BROWSER_LIFECYCLE_FAILED',
              'CANDIDATE_REFRESH_TIMEOUT', 'BACKGROUND_WAITING', 'BROWSER_OBSERVATION_TIMEOUT', 'SESSION_BUSY'}


def policy(code, execution, payload=None):
    """Return a blocker even when a successful status read reports one."""
    payload = payload if isinstance(payload, dict) else {}
    reason = (payload.get('executionPolicy') or {}).get('waitingReason')
    if not code:
        code = WAITING_CODES.get(reason)
        permissions = payload.get('permissions') or {}
        if not code and isinstance(permissions, dict) and any(permissions.get(key) is False for key in ('accessibility', 'screenRecording')):
            code = 'PERMISSION_DENIED'
        if not code and payload.get('nativeConnection') == 'host_unavailable':
            code = 'BROWSER_HOST_UNAVAILABLE'
        if not code and payload.get('globalPermission') is False:
            code = 'BROWSER_PERMISSION_MISSING'
    unknown = execution in ('unknown', 'queued')
    common = {'allowAlternativeTools': False, 'replaySideEffects': False,
              'resumeAfter': 'revalidate_permissions_connection_target_and_share',
              'executionUncertain': unknown}
    if code in USER_ACTIONS:
        action, message = USER_ACTIONS[code]
        return dict(common, status='user_action_required', action=action, reason=code,
                    message=message, notifyUser=True, waitForUser=True,
                    sideEffectGuidance='若先前操作可能已执行，恢复后先查询既有结果或观察原目标，不能重放输入。')
    if code in ('TASK_CANCELLED', 'TASK_INTERRUPTED'):
        return dict(common, status='task_interrupted', action='report_interruption_and_wait', reason=code,
                    notifyUser=True, waitForUser=True,
                    message='本界面任务已取消或中断，不能继续旧输入；会话保护的保留状态应以 sessionProtection 为准。报告中断原因并等待用户继续；先核实既有结果，继续时创建并显式绑定新任务，不能复活旧任务或重放输入。')
    if code == 'TASK_IN_USE':
        return dict(common, status='user_action_required', action='resolve_task_ownership', reason=code,
                    notifyUser=True, waitForUser=True,
                    message='原任务仍由其他活动连接持有。请用户确认该任务是否仍在执行；不得自动结束、改绑或接管。')
    if code == 'TARGET_MISMATCH':
        return dict(common, status='revalidate_required', action='inspect_task_and_original_target', reason=code,
                    message='当前目标与任务绑定不一致，不代表旧任务已失效。只读检查任务状态和原目标；确认需要新任务后才显式 begin，不能自动结束或接管活动任务。')
    if code == 'TASK_ROUTE_MISMATCH':
        return dict(common, status='revalidate_required', action='inspect_task_route', reason=code,
                    message='本阶段的操作对象与请求通道不一致，未派发。先 task status 核实 routing 和原目标；'
                            '只有实际进入另一种界面时才结束自己的阶段并创建新阶段。扩展故障、权限拒绝或结果未知不能触发原生兜底，不能重放输入。')
    if code in ('STALE_DOCUMENT', 'STALE_TARGET'):
        return dict(common, status='revalidate_required', action='inspect_bound_document', reason=code,
                    maxAutomaticChecks=2, onExhausted='notify_user_and_wait',
                    message='命令属于旧文档或失效目标，未迁移到新文档。保留原任务和标签，先只读检查当前绑定与文档状态；新文档就绪后重新观察。不要每次导航都 candidates+assign，不猜测其他目标；结果未知的输入不得重放。')
    if code == 'STALE_APP':
        return dict(common, status='revalidate_required', action='reobserve_original_target', reason=code,
                    maxAutomaticChecks=2, onExhausted='notify_user_and_wait',
                    message='应用句柄或进程路径身份未通过核验，不等于已确认应用升级或退出。最多两次只读重取原应用；仍失败时通知用户并等待，不自动关闭或重启应用、不忽略校验、不切换工具或输入通道。恢复后重新确认原目标与共享，不能重放结果未知的输入。')
    if code in REFRESH_CODES:
        return dict(common, status='revalidate_required', action='reobserve_original_target', reason=code,
                    message='只读重新核实原目标、连接和共享窗口，再显式确认绑定；禁止猜测目标或重复先前输入。')
    if code in WAIT_CODES or reason in ('document_pending', 'target_paused'):
        return dict(common, status='wait_required', action='wait_then_check_status', reason=code or reason,
                    maxAutomaticChecks=2, onExhausted='notify_user_and_wait',
                    message='保留原任务，最多进行两次只读状态检查；仍未恢复时通知用户并等待，禁止更换工具或输入通道。')
    if unknown:
        return dict(common, status='outcome_unknown', action='query_existing_result',
                    message='查询既有命令或只读观察原目标；未知结果不能当作失败而重复执行。')
    if code:
        return dict(common, status='blocked', action='report_blocker', reason=code,
                    notifyUser=True, message='报告错误与当前阻塞；不能跳过此步骤或换工具绕过。')
    return None
