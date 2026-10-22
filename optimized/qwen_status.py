"""Whitelisted display metrics; never retain prompts, answers or request history."""
import math


PALETTE = {
    'idle': '#64748b', 'reading': '#2563eb', 'generating': '#16a34a',
    'queued': '#ca8a04', 'error': '#dc2626', 'loading': '#ca8a04',
    'busy': '#2563eb', 'off': '#64748b', 'unloaded': '#64748b',
    'checking': '#64748b', 'unconfigured': '#64748b',
}


def number(value):
    return (isinstance(value, (int, float)) and not isinstance(value, bool)
            and math.isfinite(value) and value >= 0)


def metrics_display(payload, loaded=True, in_flight=0):
    if not isinstance(payload, dict) or not isinstance(payload.get('live'), dict):
        raise ValueError('阶段指标不可用')
    live = payload['live']
    state, phase = live.get('state'), live.get('phase')
    slots = live.get('slots', [])
    if not isinstance(slots, list):
        slots = []
    slot_states = [slot.get('state') for slot in slots if isinstance(slot, dict)]
    counts = {key: int(live[key]) if number(live.get(key)) else 0
              for key in ('queued', 'waiting', 'admitting', 'running')}
    active = (number(in_flight) and in_flight > 0) or any(counts.values())
    generating = state == 'generating' or any(s in ('decoding', 'generating') for s in slot_states)
    reading = state == 'reading' or 'reading' in slot_states
    if generating:
        display = 'generating'
    elif not loaded and (active or reading):
        display = 'loading'
    elif reading:
        # Native "reading" also covers some session-management operations.
        display = 'reading' if phase in (None, 'reading the prompt') or 'reading' in slot_states else 'busy'
    elif state == 'idle' and not active:
        display = 'idle'
    elif state == 'unloaded' and not active:
        display = 'unloaded'
    elif (counts['queued'] or counts['waiting']) and not counts['running'] and not counts['admitting']:
        display = 'queued'
    else:
        display = 'busy'
    active = bool(active or generating or reading or display in ('loading', 'queued', 'busy'))
    rate = live.get('tok_s')
    # /metrics tok_s is Strata's aggregate recent-window rate, not slot averages.
    rate = float(rate) if display == 'generating' and number(rate) else None
    details = {'thinking': '思考中', 'answering': '回答中',
               'reading the prompt': '读取上下文', 'tool call complete': '工具调用已生成',
               'saving the session': '保存会话', 'restoring a session': '恢复会话'}
    phase_detail = details.get(phase, '')
    if isinstance(phase, str) and phase.startswith('writing a tool call:'):
        phase_detail = '生成工具调用'
    return {
        'display_phase': display, 'generation_tps': rate,
        'metrics_available': True, 'metrics_active': active,
        'queued_count': counts['queued'], 'waiting_count': counts['waiting'],
        'admitting_count': counts['admitting'], 'running_count': counts['running'],
        'prompt_read': int(live['prompt_read']) if number(live.get('prompt_read')) else None,
        'prompt_total': int(live['prompt_total']) if number(live.get('prompt_total')) else None,
        'phase_detail': phase_detail,
    }


def rate_text(status, operation=None):
    if status.get('state') in ('error', 'checking', 'unconfigured') and not operation:
        return '— T/s'
    if operation or status.get('display_phase') != 'generating':
        if status.get('state') == 'busy' and not status.get('metrics_available'):
            return '— T/s'
        return '0.0 T/s'
    value = status.get('generation_tps')
    if not number(value):
        return '— T/s'
    value = max(0.0, value)
    return (f'{value:.1f}' if value < 1_000_000 else f'{value:.1e}') + ' T/s'


def contrast_ratio(a, b):
    def luminance(rgb):
        channels = [v / 12.92 if v <= .04045 else ((v + .055) / 1.055) ** 2.4 for v in rgb]
        return sum(v * w for v, w in zip(channels, (.2126, .7152, .0722)))
    x, y = sorted((luminance(a), luminance(b)))
    return (y + .05) / (x + .05)


def readable_color(phase, background):
    """Keep semantic hue, choose the smallest black/white blend reaching 4.5:1."""
    source = PALETTE.get(phase, PALETTE['busy'])
    rgb = tuple(int(source[i:i + 2], 16) / 255 for i in (1, 3, 5))
    for step in range(101):
        fraction = step / 100
        for end in (0, 1):
            candidate = tuple(round((v * (1 - fraction) + end * fraction) * 255) / 255 for v in rgb)
            if contrast_ratio(candidate, background) >= 4.5:
                return '#' + ''.join(f'{round(v * 255):02x}' for v in candidate)
    return '#000000' if contrast_ratio((0, 0, 0), background) >= 4.5 else '#ffffff'
