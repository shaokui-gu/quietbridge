"""Agent-facing recovery policy. Metadata is guidance, not host enforcement."""
GUIDANCE = ('异常恢复：遇到 recovery.status=user_action_required，先向用户说明原因和具体操作，'
            '暂停依赖该能力的步骤，等待用户完成后只读复查权限、连接、原目标和共享窗口。'
            '禁止自动开启开关/授权、反复重启、跳过失败步骤或换其他工具绕过；'
            '副作用未知时不重复输入。连接器整个不可用时也应通知用户恢复原连接器。')

USER_ACTIONS = {
    'APP_NOT_RUNNING': ('open_app', '请打开“Workbuddy CUA”，确认菜单栏图标出现，完成后告诉我继续。'),
    'CAPABILITY_DISABLED': ('enable_capability', '请在 Workbuddy CUA 控制面板开启本任务所需的能力；如出现授权向导，请按说明完成后告诉我继续。'),
    'PERMISSION_DENIED': ('grant_permission', '请打开 Workbuddy CUA 的权限向导，按说明授权所需的辅助功能或屏幕录制权限，完成后告诉我继续。'),
    'SCREEN_RECORDING_DENIED': ('grant_permission', '请通过 Workbuddy CUA 权限向导开启屏幕录制权限，完成后告诉我继续。'),
    'SESSION_LOCKED': ('unlock_session', '请解锁电脑，完成后告诉我继续。'),
    'HELPER_MISSING': ('repair_installation', 'Workbuddy CUA 原生组件缺失，请重新安装应用，完成后告诉我继续。'),
    'HELPER_BROKEN': ('restart_connector', '原生组件连接已断开。请先确认 Workbuddy CUA 正在运行；若仍无法连接，请在 Workbuddy 中重启 workbuddy-cua 连接器，完成后告诉我继续。'),
    'HELPER_TIMEOUT': ('check_connection', '原生组件响应超时。请检查 Workbuddy CUA 是否正常运行；若持续无响应，请重启该连接器，完成后告诉我继续。'),
    'BROWSER_HOST_UNAVAILABLE': ('connect_extension', '请确认浏览器中的 Workbuddy CUA 扩展已启用，并在应用控制面板检查扩展连接，恢复后告诉我继续。'),
    'BROWSER_PERMISSION_MISSING': ('authorize_extension', '请在 Workbuddy CUA 控制面板开启浏览器能力，并按扩展说明完成授权，完成后告诉我继续。'),
    'BROWSER_DISABLED': ('enable_browser', '请在 Workbuddy CUA 控制面板开启浏览器能力，完成后告诉我继续。'),
    'EXTENSION_UPGRADE_REQUIRED': ('update_extension', '请在浏览器扩展管理页重新加载或更新 Workbuddy CUA 扩展，完成后告诉我继续。'),
    'READ_ONLY': ('change_read_only', '当前连接仅允许观察。请确认是否要解除只读限制；完成配置并重新连接后告诉我继续。'),
    'APP_NOT_ALLOWED': ('approve_target', '当前目标不在允许列表中。请确认并配置该应用的访问范围，完成后告诉我继续。'),
    'FOREGROUND_REQUIRED': ('resolve_background_limit', '此操作无法在保持用户焦点的条件下完成，任务已暂停，请确认可用的后台操作方式。'),
    'FOCUS_STATE_UNKNOWN': ('check_focus_state', '无法核实用户焦点，已暂停输入。请检查目标与共享窗口状态，完成后告诉我继续。'),
    'UNSAFE_WINDOW_DELIVERY': ('resolve_background_limit', '无法确认输入只送往目标窗口，已暂停。请检查目标是否支持后台操作，完成后告诉我继续。'),
}
WAITING_CODES = {'host_unavailable': 'BROWSER_HOST_UNAVAILABLE',
                 'permission_missing': 'BROWSER_PERMISSION_MISSING',
                 'disabled': 'BROWSER_DISABLED',
                 'extension_upgrade_required': 'EXTENSION_UPGRADE_REQUIRED'}
REFRESH_CODES = {'STALE_SNAPSHOT', 'STALE_APP', 'REQUIRES_REVALIDATION', 'CONNECTOR_UPDATED',
                 'BROWSER_TARGET_CHANGED', 'TASK_NOT_BOUND'}
WAIT_CODES = {'CAPTURE_BUSY', 'SHARE_NOT_READY', 'PREVIEW_NOT_READY', 'SHARE_REQUIRED',
              'CANDIDATE_REFRESH_TIMEOUT', 'BACKGROUND_WAITING'}


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
                    message='本任务已取消或中断，共享和保护已停止。报告中断原因并等待用户继续；先核实既有结果，继续时创建并显式绑定新任务，不能复活旧任务或重放输入。')
    if code == 'TASK_IN_USE':
        return dict(common, status='user_action_required', action='resolve_task_ownership', reason=code,
                    notifyUser=True, waitForUser=True,
                    message='原任务仍由其他活动连接持有。请用户确认该任务是否仍在执行；不得自动结束、改绑或接管。')
    if code == 'TARGET_MISMATCH':
        return dict(common, status='revalidate_required', action='inspect_task_and_original_target', reason=code,
                    message='当前目标与任务绑定不一致，不代表旧任务已失效。只读检查任务状态和原目标；确认需要新任务后才显式 begin，不能自动结束或接管活动任务。')
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
