"""Agent-facing recovery policy. Metadata is guidance, not host enforcement."""
GUIDANCE = ('异常恢复：可恢复错误不是任务结束。先按 recovery 用原工具核实和恢复，继续用户已授权的剩余工作，'
            '不因旧快照、候选过期、通道启动或本任务专用窗口被误关就直接结束。'
            '遇到 recovery.status=user_action_required，先向用户说明原因和具体操作，'
            '暂停依赖该能力的步骤，等待用户完成后只读复查权限、连接、原目标和共享窗口。'
            '需要用户决策或自动恢复耗尽时，优先使用宿主可用的 AskUserQuestion 等交互工具弹出选择，'
            '提供“处理后继续/保持暂停/结束任务”，不能自行代选或用最终回复把等待决策当作业务结束；'
            '无交互工具时在对话中询问并保留进度。等待期间停止新输入并释放自己的共享，'
            '按具体恢复要求保留绑定或结束失效阶段，用户选择结束才取消业务；'
            '窗口关闭一次不代表取消，连续多次关闭只触发暂停决策，不推断用户意图。'
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
    'BROWSER_DEBUGGER_RESTRICTED': ('resolve_extension_conflict', 'Chrome 原文表示目标含其他扩展页面，不能据此认定另一调试客户端占用。'
        '核对 debuggerFailure 的原文和 foreignFrameExtensionIds；JSON 查看器等无 debugger 权限的扩展也可能插入自己的 iframe。'
        '请用户在原专用标签手动打开普通 HTML 测试页后复查；若仍出现相同扩展 frame，由用户调整对应扩展的站点访问。'
        '仅有 debugger 权限不能证明某扩展占用，重载 QuietBridge 也不保证移除其他扩展的 frame。'
        '不自动停用扩展、更换目标、绕过弹窗保护或重放输入。独立受管浏览器仍需用户确认新阶段、加载扩展并登录，不作为即用恢复路径。'),
    'BROWSER_DEBUGGER_IN_USE': ('resolve_debugger_owner', 'Chrome 明确报告已有调试客户端附着；请用户核对该标签的 DevTools 或其他控制客户端。'
        '保留原任务和标签，不强制 detach、不结束其他会话、不仅凭扩展 debugger 权限猜测占用者，也不重放输入。'),
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
    'TARGET_IN_USE': ('select_independent_target', '该窗口或原生应用已被另一会话使用。按 targetConflict 的窗口、任务和宿主会话 ID 向用户说明占用者；'
                      '用户可在 QuietBridge“任务与窗口”中选中对应会话，点击“停止此会话控制”，等待释放后再核实目标。'
                      '停止 HTTP 测试服务或其他会话不会释放此租约；定时计划需在 WorkBuddy 单独暂停。'
                      '也可提供独立目标或等待其完成，Agent 不能自行结束其他会话、换通道或强行并行输入。'),
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
              'BROWSER_CHANNEL_STARTING', 'BROWSER_MAPPING_REQUIRED', 'BROWSER_MAPPING_PENDING',
              'BROWSER_OBSERVER_NOT_READY', 'BROWSER_LIFECYCLE_TIMEOUT', 'BROWSER_LIFECYCLE_FAILED',
              'CANDIDATE_REFRESH_TIMEOUT', 'BACKGROUND_WAITING', 'BROWSER_OBSERVATION_TIMEOUT', 'SESSION_BUSY'}


def _policy(code, execution, payload=None):
    """Return a blocker even when a successful status read reports one."""
    payload = payload if isinstance(payload, dict) else {}
    reason = (payload.get('executionPolicy') or {}).get('waitingReason')
    if not code:
        if payload.get('state') == 'suspended' and (payload.get('resumption') or {}).get('automatic'):
            code = 'TASK_SUSPENDED'
        if payload.get('state') == 'invalidated':
            code = 'TARGET_LOST'
        if payload.get('state') == 'interrupted':
            code = 'TASK_INTERRUPTED'
        code = code or WAITING_CODES.get(reason)
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
              'executionUncertain': unknown, 'taskMustEnd': False}
    if code in USER_ACTIONS:
        action, message = USER_ACTIONS[code]
        return dict(common, status='user_action_required', action=action, reason=code,
                    message=message, notifyUser=True, waitForUser=True,
                    sideEffectGuidance='若先前操作可能已执行，恢复后先查询既有结果或观察原目标，不能重放输入。')
    if code == 'TASK_SUSPENDED':
        return dict(common, status='revalidate_required', action='restore_original_share', reason=code,
                    waitForUser=False, notifyUser=False, maxAutomaticChecks=2, onExhausted='notify_user_and_wait',
                    message='原任务因等待输入暂时释放共享。立即按 resumption.nextSteps 对原窗口发起 share，'
                            '插件会重新核验并恢复连接；随后重新观察，不等用户提醒、不重放旧输入。')
    if code == 'TASK_RESUMED':
        return dict(common, status='revalidate_required', action='observe_resumed_target', reason=code,
                    waitForUser=False, notifyUser=False, maxAutomaticChecks=2, onExhausted='notify_user_and_wait',
                    message='等待导致的空闲暂停已自动恢复原目标。立即按 resumption.nextSteps 复核窗口映射、共享和新快照，'
                            '继续用户已授权的剩余工作，不等待用户提醒重连；本次输入未执行，旧快照和未知结果的动作不得重放。')
    if code == 'TASK_CANCELLED' or (code == 'TASK_INTERRUPTED' and
            (payload.get('interruption') or {}).get('explicitStop') is True):
        return dict(common, status='task_stopped', action='respect_explicit_stop', reason=code,
                    taskMustEnd=True, notifyUser=True, waitForUser=False,
                    message='已收到明确取消或宿主停止信号，释放自己的资源并停止本轮工作；'
                            '不弹框要求继续、不自动重建任务，不重放未知输入。保留已取得的业务结果。')
    if code == 'TASK_INTERRUPTED':
        return dict(common, status='task_interrupted', action='report_interruption_and_wait', reason=code,
                    notifyUser=True, waitForUser=True,
                    message='本界面阶段已中断，尚无明确业务取消证据，不能继续旧输入。'
                            '会话保护状态以 sessionProtection 为准；保留业务进度，说明中断原因并让用户选择。'
                            '选择继续后先核实既有结果，再创建并显式绑定新阶段，不能复活旧任务或重放输入。')
    if code == 'TASK_IN_USE':
        return dict(common, status='user_action_required', action='resolve_task_ownership', reason=code,
                    notifyUser=True, waitForUser=True,
                    message='原任务仍由其他活动连接持有。请用户确认该任务是否仍在执行；不得自动结束、改绑或接管。')
    if code == 'TARGET_MISMATCH':
        return dict(common, status='revalidate_required', action='inspect_task_and_original_target', reason=code,
                    waitForUser=False, notifyUser=False, maxAutomaticChecks=2, onExhausted='notify_user_and_wait',
                    message='当前目标与任务绑定不一致，不代表旧任务已失效。只读检查任务状态和原目标；确认需要新任务后才显式 begin，不能自动结束或接管活动任务。')
    if code in ('TARGET_LOST', 'WINDOW_UNAVAILABLE', 'TASK_INVALIDATED'):
        evidence = payload.get('targetRecovery') or {}
        if evidence.get('status') == 'decision_required':
            return dict(common, status='user_action_required', action='choose_window_recovery', reason=code,
                        waitForUser=True, notifyUser=True,
                        message='窗口在两次自动恢复后再次关闭，已暂停自动建窗，未将其视为取消。'
                                '请通过 Agent 弹框让用户选择“重建后继续 / 保持暂停 / 结束任务”；等待期间清理自己的共享和输入资源，保留进度。')
        if evidence.get('status') == 'recreate_allowed':
            return dict(common, status='revalidate_required', action='recreate_closed_task_window', reason=code,
                        waitForUser=False, notifyUser=False, maxAutomaticRecoveries=2,
                        onExhausted='notify_user_and_wait',
                        message='已核实本任务创建的专用 Chrome 窗口关闭，原进程仍在，宿主未停止。'
                                '若用户未明确要求停止且原目标仍需窗口，自动 end 自己的失效阶段 → begin(targetSurface=web_content,recoveryOf=旧taskId) → '
                                '用 create_target(url=原任务已授权的普通网页入口) 建立替代专用窗口，'
                                '再根据真实候选和系统窗口证据 bind/assign/verify_mapping/share/snapshot，继续未执行工作。'
                                '一次恢复过程最多建一个替代窗口；创建结果未知时查询原请求，不再建窗。'
                                '不恢复已丢失草稿、不重新提交表单/支付/发送；先核实旧命令及现存业务结果，不能重放未知输入。'
                                '恢复链允许两次自动替换，再次关闭或恢复失败时暂停并让用户弹框选择，不能无限 end/begin。')
        return dict(common, status='revalidate_required', action='inspect_lost_target', reason=code,
                    waitForUser=False, notifyUser=False, maxAutomaticChecks=2,
                    onExhausted='notify_user_and_wait',
                    message='先 task status 核实原目标和 targetRecovery，再只读枚举原应用窗口。'
                            '查不到窗口不等于用户取消任务，也不允许选择另一个现存窗口。'
                            '仅 targetRecovery.status=recreate_allowed 时按计划自动重建本任务专用窗口；'
                            '未知归属、原生应用窗口、进程身份变化或无法确认关闭时保留结果并说明具体缺失，不猜测恢复。')
    if code == 'STALE_SNAPSHOT':
        return dict(common, status='revalidate_required', action='refresh_snapshot', reason=code,
                    waitForUser=False, notifyUser=False, maxAutomaticChecks=2,
                    onExhausted='notify_user_and_wait',
                    message='快照引用已过期，自动对同一目标获取新 snapshot/state 并重新定位元素；'
                            '先核实上一输入是否已完成，再继续尚未执行的步骤，无需让用户重开窗口或重新授权。')
    if code == 'BROWSER_TARGET_CHANGED':
        return dict(common, status='revalidate_required', action='refresh_original_candidate', reason=code,
                    waitForUser=False, notifyUser=False, maxAutomaticChecks=2,
                    onExhausted='notify_user_and_wait',
                    message='自动刷新 candidates，核实仍为原 browserWindowId/tabId 且新地址属于用户任务，'
                            '再 assign 原标签；不因候选过期结束任务、不新建窗口或选择其他标签。')
    if code == 'BACKGROUND_COMMAND_TIMEOUT':
        return dict(common, status='outcome_unknown', action='query_then_observe_original_target', reason=code,
                    waitForUser=False, notifyUser=False, maxAutomaticChecks=2,
                    onExhausted='notify_user_and_wait',
                    message='页面输入回执超时，先查询原 commandId，再用原标签 snapshot/diagnostics 核实结果。'
                            '能确认结果时继续剩余工作；仍无法确认再通知用户。未知输入不重放，不改绑或换原生输入。')
    if code == 'RECOVERY_DECISION_REQUIRED':
        return dict(common, status='user_action_required', action='choose_window_recovery', reason=code,
                    waitForUser=True, notifyUser=True,
                    message='自动窗口恢复已达到上限，业务已暂停。让用户选择重建后继续、保持暂停或结束，不认定为用户取消。')
    if code == 'TASK_ROUTE_MISMATCH':
        return dict(common, status='revalidate_required', action='inspect_task_route', reason=code,
                    waitForUser=False, notifyUser=False, maxAutomaticChecks=2, onExhausted='notify_user_and_wait',
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
    if code == 'TASK_NOT_BOUND':
        return dict(common, status='revalidate_required', action='inspect_task_and_original_target', reason=code,
                    waitForUser=False, notifyUser=False, maxAutomaticChecks=2, onExhausted='notify_user_and_wait',
                    message='先 task status 查看 state 和 targetRecovery；窗口丢失按原任务恢复计划处理，'
                            '待绑定阶段继续核实自己的目标。不能仅因尚未绑定就结束业务，也不能改绑其他窗口。')
    if code in REFRESH_CODES:
        return dict(common, status='revalidate_required', action='reobserve_original_target', reason=code,
                    waitForUser=False, notifyUser=False, maxAutomaticChecks=2, onExhausted='notify_user_and_wait',
                    message='只读重新核实原目标、连接和共享窗口，再显式确认绑定；禁止猜测目标或重复先前输入。')
    if code in WAIT_CODES or reason in ('document_pending', 'target_paused'):
        return dict(common, status='wait_required', action='wait_then_check_status', reason=code or reason,
                    waitForUser=False, notifyUser=False,
                    maxAutomaticChecks=2, onExhausted='notify_user_and_wait',
                    message='保留原任务，最多进行两次只读状态检查；仍未恢复时通知用户并等待，禁止更换工具或输入通道。')
    if unknown:
        result = dict(common, status='outcome_unknown', action='query_existing_result',
                    waitForUser=False, notifyUser=False,
                    message='查询既有命令或只读观察原目标；未知结果不能当作失败而重复执行。')
        if execution != 'queued':
            result.update(maxAutomaticChecks=2, onExhausted='notify_user_and_wait')
        return result
    if code:
        return dict(common, status='blocked', action='report_blocker', reason=code,
                    notifyUser=True, waitForUser=True,
                    message='暂停依赖步骤，说明错误及已尝试的恢复，让用户通过 Agent 选择处理后继续、保持暂停或结束；不能自行宣布业务结束或换工具绕过。')
    return None


def policy(code, execution, payload=None):
    """Use the host's decision dialog consistently; never silently choose."""
    result = _policy(code, execution, payload)
    if not result:
        return result
    if result.get('taskMustEnd'):
        return result
    if result.get('waitForUser') or result.get('onExhausted') == 'notify_user_and_wait':
        window = result['action'] in ('choose_window_recovery', 'recreate_closed_task_window')
        result['userDecision'] = {
            'presentation': 'agent_dialog', 'preferredTool': 'AskUserQuestion',
            'fallback': 'ask_in_chat_and_wait', 'autoSelect': False,
            'when': 'now' if result.get('waitForUser') else 'after_automatic_recovery_exhausted',
            'context': result['message'],
            'prompt': '窗口恢复已暂停，请选择下一步。' if window else '当前步骤需要处理，请选择如何继续。',
            'options': [
                {'id': 'continue', 'label': '重建后继续' if window else '处理后继续'},
                {'id': 'pause', 'label': '保持暂停'},
                {'id': 'end', 'label': '结束任务'}],
            'whileWaiting': 'stop_new_input_and_release_own_preview_preserve_business_progress',
            'phaseCleanup': 'follow_specific_recovery_keep_binding_unless_invalidated',
            'resume': 'revalidate_target_share_and_observe_before_remaining_work',
            'endOnlyOn': 'explicit_user_choice_or_host_stop'}
        if result.get('onExhausted'):
            result['onExhausted'] = 'ask_user_choice'
    return result
