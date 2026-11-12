"""Fixed, content-free preview diagnostics shared by logging and local analysis."""
import math
import re

COUNTERS = ('callbackFrames', 'completeFrames', 'idleFrames', 'blankFrames', 'suspendedFrames',
            'startedFrames', 'stoppedFrames', 'unknownFrames', 'submittedFrames',
            'backpressureDroppedFrames', 'contentSamples', 'sampledContentChanges', 'sampleFailures',
            'bodySamples', 'bodyContentChanges', 'bodyNearUniformSamples', 'bodySampleFailures')
AGES = ('lastCallbackAgeMs', 'lastSubmissionAgeMs', 'lastSampledContentChangeAgeMs',
        'lastBodyContentChangeAgeMs', 'lastSourceSampleAgeMs')
COLOR_IDENTIFIERS = {'ITU_R_709_2', 'ITU_R_601_4', 'ITU_R_2020', 'SMPTE_C', 'P3_D65', 'DCI_P3', 'EBU_3213',
                     'sRGB', 'Linear', 'SMPTE_ST_2084_PQ', 'ITU_R_2100_HLG', 'SMPTE_240M_1995', 'other', 'unspecified'}
LIFECYCLE_EVENTS = frozenset('preview.capture' + suffix for suffix in (
    'Preparing', 'StartRequested', 'StartCompleted', 'StartFailed', 'StopRequested', 'StopCompleted', 'StopFailed', 'DelegateStopped'))


def number(value):
    return (type(value) is int and value >= 0) or (type(value) is float and math.isfinite(value) and value >= 0)


def source_frame(value):
    if not isinstance(value, dict):
        return {}
    result = {}
    if value.get('pixelFormat') in ('BGRA', '420v', '420f'):
        result['pixelFormat'] = value['pixelFormat']
    for key in ('pixelFormatCode', 'width', 'height', 'bytesPerRow', 'planeCount', 'chromaBytesPerRow',
                'bodyX', 'bodyY', 'bodyWidth', 'bodyHeight'):
        if type(value.get(key)) is int and 0 <= value[key] <= 2**32 - 1:
            result[key] = value[key]
    for key in ('bodyChannelSpread', 'bodyChromaSpread', 'bodyAlphaMin', 'bodyAlphaMax'):
        if type(value.get(key)) is int and 0 <= value[key] <= 255:
            result[key] = value[key]
    for key in ('colorPrimaries', 'transferFunction', 'yCbCrMatrix'):
        if isinstance(value.get(key), str) and value[key] in COLOR_IDENTIFIERS:
            result[key] = value[key]
    if type(value.get('bodyNearUniform')) is bool:
        result['bodyNearUniform'] = value['bodyNearUniform']
    return result


def frame_progress(value):
    if not isinstance(value, dict):
        return {}
    result = {key: value[key] for key in COUNTERS + AGES if key in value and
              (number(value[key]) or key in AGES and value[key] is None)}
    source = source_frame(value.get('sourceFrame'))
    if source:
        result['sourceFrame'] = source
    return result


def capture_lifecycle(value):
    if not isinstance(value, dict):
        return {}
    result = {}
    generation = value.get('streamGeneration')
    if isinstance(generation, str) and re.fullmatch(r'[A-Za-z0-9_-]{1,64}', generation):
        result['streamGeneration'] = generation
    window = value.get('trackingWindowId')
    if type(window) is int and 0 < window <= 2**32 - 1:
        result['trackingWindowId'] = window
    for key, allowed in (
        ('source', {'window', 'application', 'desktop'}),
        ('startState', {'preparing', 'requested', 'completed', 'failed'}),
        ('stopState', {'not_requested', 'requested', 'completed', 'failed'}),
        ('stopReason', {'reconfigure', 'release_or_target_change', 'superseded_start', 'start_failed'}),
        ('platformErrorDomain', {'com.apple.ScreenCaptureKit.SCStreamErrorDomain', 'SCStreamErrorDomain',
                                 'NSOSStatusErrorDomain', 'NSCocoaErrorDomain', 'AVFoundationErrorDomain', 'other'})):
        if isinstance(value.get(key), str) and value[key] in allowed:
            result[key] = value[key]
    for key in ('startElapsedMs', 'stopElapsedMs'):
        if number(value.get(key)):
            result[key] = value[key]
    if type(value.get('platformErrorCode')) is int:
        result['platformErrorCode'] = value['platformErrorCode']
    if type(value.get('stopConfirmed')) is bool:
        result['stopConfirmed'] = value['stopConfirmed'] and result.get('stopState') == 'completed'
    return result
