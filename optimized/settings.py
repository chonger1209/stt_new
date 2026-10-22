"""Read the existing config format and preserve comments and custom values on save."""
import logging
import os
import tempfile
from pathlib import Path
from qwen_config import FIELDS

DEFAULTS = {
    'color_caps_on': '#fa6666', 'color_caps_off': '#4CAF50', 'color_titlebar': '#2c3e50',
    'window_width': 300, 'window_height': 240, 'window_x': -1, 'window_y': -1,
    'always_on_top': True, 'software_list': ['CAXA', 'CAD', 'SOLIDWORKS'],
    'grid_second_per_block': 30, 'screen_time_refresh_frequency': 10,
    'timer_duration_minutes': 25,
    'qwen_idle_timeout_minutes': 120,
}
DEFAULTS.update({key: value for key, (value, _) in FIELDS.items()})
POSITIVE = {'window_width', 'window_height', 'grid_second_per_block',
            'screen_time_refresh_frequency', 'timer_duration_minutes', 'qwen_idle_timeout_minutes'}
POSITIVE.add('qwen_startup_timeout_seconds')

CONFIG_MARKER = '# 配置说明版本: 2'
HEADER = '''# Caps Lock 检测器配置文件
# 配置说明版本: 2
#
# 使用说明：
# 1. 本文件使用 UTF-8 编码；以 # 开头的整行是说明，不是设置。
# 2. 修改并保存后，点击程序标题栏的 ⟳ 或右键菜单“刷新”生效。
# 3. true 表示开启/显示，false 表示关闭/隐藏。
# 4. 颜色、窗口、计时等全局设置必须放在第一个 [进程名称] 之前。
# 5. 程序退出时只更新 window_x / window_y，不重置其他设置。
# 6. 行内分号 ; 后面作为注释，名称等配置值中不要包含分号。
'''

GROUPS = {
    '颜色设置': ['color_caps_on', 'color_caps_off', 'color_titlebar'],
    '窗口设置': ['window_width', 'window_height', 'window_x', 'window_y', 'always_on_top'],
    '自动大小写切换': ['software_list'],
    '时间流与统计刷新': ['grid_second_per_block', 'screen_time_refresh_frequency'],
    '番茄倒计时': ['timer_duration_minutes'],
    '千问休眠设置': ['qwen_idle_timeout_minutes'],
    '千问连接与启动（路径参数下次启动服务生效）': list(FIELDS),
}
HELP = {
    'qwen_idle_timeout_minutes': ('连续闲置后卸载模型的时间，单位分钟，正整数；默认 120（2小时）。\n'
                                  '# 卸载模型释放其内存和显存占用，保留接收服务，本地或局域网请求可自动唤醒。\n'
                                  '# 执行或排队任务时不休眠；最后一个任务结束后重新计时，状态查询不延时。\n'
                                  '# 提前1分钟提醒，点击重置可重新计算完整时长；修改后点击“刷新”生效。\n'
                                  '# 无效值使用120分钟并记录原因；STT退出后不再执行闲置管理。'),
    'color_caps_on': 'Caps Lock 开启时的背景色，默认 #fa6666（红色）；可填 #RRGGBB 颜色值。',
    'color_caps_off': 'Caps Lock 关闭时的背景色，默认 #4CAF50（绿色）。',
    'color_titlebar': '标题栏及标题栏按钮背景色，默认 #2c3e50。',
    'window_width': '窗口宽度，单位像素，正整数；默认 300。',
    'window_height': '统计面板收起时的窗口高度，单位像素，正整数；默认 240。',
    'window_x': '窗口横坐标，单位像素；默认 -1。横/纵坐标任一为 -1 时居中；退出时自动保存位置。',
    'window_y': '窗口纵坐标，单位像素；默认 -1。多显示器坐标可以超过主屏幕宽度，也可以为负数。',
    'always_on_top': '是否始终置顶：true=置顶，false=普通窗口；默认 true。',
    'software_list': ('自动开启大写锁定的软件关键词，用英文逗号分隔，忽略大小写。\n'
                      '# 匹配应用显示名称或窗口标题；切换到不匹配的软件时自动关闭大写锁定。\n'
                      '# 默认 CAXA,CAD,SOLIDWORKS；留空表示切换到任何软件时都关闭大写锁定，并非停用自动切换。'),
    'grid_second_per_block': ('时间流每个色块代表的秒数，也是采样/定时刷新间隔；正整数，默认 30。\n'
                              '# 不同应用的短片段共同累计，满设定秒数才生成一格，不足部分继续累计。\n'
                              '# 色块颜色取该格累计结束时最后使用的应用；切换应用不会额外产生格子。'),
    'screen_time_refresh_frequency': ('统计面板刷新频率，单位“次/10秒”，正整数，默认 10。\n'
                                      '# 10=每秒一次，2=每5秒一次，1=每10秒一次；当前最快每秒一次，超过10不会更快。\n'
                                      '# 只影响统计面板刷新，不改变每200毫秒检测前台窗口的频率。'),
    'timer_duration_minutes': ('番茄倒计时时长，单位分钟，正整数；默认 25。\n'
                               '# 修改后点击“刷新”：尚未开始时立即更新；正在运行或暂停时保留剩余时间，重置后用新时长。'),
}
HELP.update({key: help_text for key, (_, help_text) in FIELDS.items()})


def parse_bool(value):
    normalized = value.lower()
    if normalized not in {'true', '1', 'yes', 'on', 'false', '0', 'no', 'off'}:
        raise ValueError('布尔值必须为 true/false')
    return normalized in {'true', '1', 'yes', 'on'}

PROCESS_HELP = '''# 进程配置（可选，不是白名单）
# 没有配置的程序也会正常记录，默认在统计和时间流中都显示；通常显示进程文件名。
# 只有需要改显示名称或隐藏某个程序时，才需要添加/修改对应条目。
# 格式：[进程名称.exe]，例如 [chrome.exe]；进程名匹配忽略大小写。
# display_name: 在界面中显示的名称；省略时使用本节的进程名称。
# show_in_screen_time: 是否在当天使用时长统计中显示，true/false；省略默认 true。
# show_in_time_stream: 是否在彩色时间流中显示，true/false；省略默认 true。
# false 只隐藏对应视图，不停止记录，也不会删除已有历史数据。
# 不同程序建议使用不同的 display_name；相同名称会合并到同一个统计项目。
# LockApp.exe 是锁屏程序：首次生成配置时，默认在统计中隐藏，在时间流中保留。
# 以下是常用程序的默认规则；可按需要修改、增加或删除整个条目。
'''

DEFAULT_PROCESSES = [
    ('explorer.exe', 'Windows Explorer', True),
    ('chrome.exe', 'Google Chrome', True),
    ('wechat.exe', '微信', True),
    ('code.exe', 'Visual Studio Code', True),
    ('QQ.exe', 'QQ', True),
    ('CAXA.exe', 'CAXA', True),
    ('edge.exe', 'Microsoft Edge', True),
    ('msedge.exe', 'Microsoft Edge', True),
    ('LockApp.exe', 'Lock Screen', False),
    ('Taskmgr.exe', '任务管理器', True),
]


def _default_process_text():
    return '\n\n'.join(
        f'[{name}]\ndisplay_name = {display}\n'
        f'show_in_screen_time = {encode(show)}\nshow_in_time_stream = true'
        for name, display, show in DEFAULT_PROCESSES)


def complete_config_text(text=''):
    """Annotate legacy files, retaining existing values, sections and custom comments."""
    output = [HEADER.rstrip(), '']
    seen_keys, seen_groups = set(), set()
    section_started = False
    key_groups = {key: group for group, keys in GROUPS.items() for key in keys}
    old_labels = {'# Caps Lock 检测器配置文件', '# 颜色设置', '# 窗口设置', '# 其他设置',
                  '# 进程配置', '# 格式：[进程名称]', '# 格式：[进程名称.exe]'}

    def add_field(key, line):
        group = key_groups[key]
        if group not in seen_groups:
            output.extend(['', '# ' + group])
            seen_groups.add(group)
        output.extend([f'# {key}: {HELP[key]}', line])
        seen_keys.add(key)

    def finish_globals():
        for key, value in DEFAULTS.items():
            if key not in seen_keys:
                add_field(key, f'{key} = {encode(value)}')
        output.extend(['', PROCESS_HELP.rstrip(), ''])

    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith('[') and stripped.endswith(']') and not section_started:
            finish_globals()
            section_started = True
        if not section_started:
            if stripped in old_labels or any(
                stripped.startswith(f'# {key}:')
                for key in [*DEFAULTS, 'display_name', 'show_in_screen_time', 'show_in_time_stream']
            ):
                continue
            key = stripped.split('=', 1)[0].strip()
            if '=' in stripped and not stripped.startswith('#') and key in DEFAULTS:
                add_field(key, line)
                continue
        output.append(line)
    if not section_started:
        finish_globals()
        output.append(_default_process_text())
    # Keep separation readable without accumulating blank lines from the old file.
    result = '\n'.join(output)
    while '\n\n\n' in result:
        result = result.replace('\n\n\n', '\n\n')
    return result.rstrip() + '\n'


def _atomic_write(path, text):
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', newline='',
                                         dir=path.parent, prefix=path.name + '.', suffix='.tmp',
                                         delete=False) as handle:
            temporary = Path(handle.name)
            handle.write(text)
        os.replace(temporary, path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def ensure_documented_config(path):
    if not path.exists():
        _atomic_write(path, complete_config_text())
        return
    original = path.read_bytes()
    text = original.decode('utf-8-sig')
    if CONFIG_MARKER in text.splitlines():
        # Add the new global before process sections without rewriting user text.
        prefix = []
        for line in text.splitlines():
            if line.strip().startswith('['):
                break
            prefix.append(line)
        keys = {line.split('=', 1)[0].strip() for line in prefix if '=' in line and not line.lstrip().startswith('#')}
        missing = [key for key in ['qwen_idle_timeout_minutes', *FIELDS] if key not in keys]
        if missing:
            lines = text.splitlines(keepends=True)
            index = next((i for i, line in enumerate(lines) if line.lstrip().startswith('[')), len(lines))
            addition = '\n# 千问管理设置（新增字段；保留原有配置）\n' + '\n\n'.join(
                f'# {key}: {HELP[key]}\n{key} = {encode(DEFAULTS[key])}' for key in missing) + '\n\n'
            lines.insert(index, addition)
            _atomic_write(path, ''.join(lines))
        return
    backup = path.with_name(path.name + '.before_config_upgrade.bak')
    try:
        with backup.open('xb') as handle:
            handle.write(original)
    except FileExistsError:
        pass
    _atomic_write(path, complete_config_text(text))


def read_settings(path, logger=None):
    config = {**DEFAULTS, 'process_config': {}}
    section = None
    ensure_documented_config(path)
    for line in path.read_text(encoding='utf-8-sig').splitlines():
        line = line.strip()
        if not line or line.startswith('#'):
            continue
        if line.startswith('[') and line.endswith(']'):
            section = line[1:-1].strip()
            config['process_config'].setdefault(section, {
                'display_name': section, 'show_in_screen_time': True, 'show_in_time_stream': True})
        elif '=' in line:
            key, value = (part.strip() for part in line.split('=', 1))
            value = value.split(';', 1)[0].strip()
            target = config if section is None else config['process_config'][section]
            try:
                if section is not None:
                    if key == 'display_name':
                        target[key] = value
                    elif key in {'show_in_screen_time', 'show_in_time_stream'}:
                        target[key] = parse_bool(value)
                elif key in DEFAULTS:
                    default = DEFAULTS[key]
                    if isinstance(default, bool):
                        parsed = parse_bool(value)
                    elif isinstance(default, int):
                        parsed = int(value)
                        if key in POSITIVE and parsed <= 0:
                            raise ValueError('must be positive')
                    elif isinstance(default, list):
                        parsed = [v.strip() for v in value.split(',') if v.strip()]
                    else:
                        parsed = value
                    target[key] = parsed
            except ValueError as error:
                fallback = DEFAULTS.get(key, True)
                target[key] = fallback
                (logger or logging.getLogger(__name__)).warning(
                    '无效配置 %s=%s（%s），使用默认值 %s', key, value, error, fallback)
    return config


def encode(value):
    if isinstance(value, bool):
        return str(value).lower()
    if isinstance(value, list):
        return ','.join(value)
    return str(value)


def save_position(path, x, y):
    # Only update window coordinates. Keep sizes, process rules, comments and unknown keys.
    lines = path.read_text(encoding='utf-8-sig').splitlines(keepends=True)
    newline = '\r\n' if '\r\n' in path.read_bytes().decode('utf-8-sig') else '\n'
    pending = {'window_x': x, 'window_y': y}
    output = []
    in_section = False
    for line in lines:
        if line.lstrip().startswith('[') and not in_section:
            output.extend(f'{key} = {value}{newline}' for key, value in pending.items())
            pending.clear()
            in_section = True
        key = line.split('=', 1)[0].strip()
        if not in_section and key in {'window_x', 'window_y'} and '=' in line:
            value = x if key == 'window_x' else y
            comment = ';' + line.split(';', 1)[1].rstrip('\r\n') if ';' in line else ''
            output.append(f'{key} = {value}{" " + comment if comment else ""}{newline}')
            pending.pop(key, None)
        else:
            output.append(line.rstrip('\r\n') + newline)
    output.extend(f'{key} = {value}{newline}' for key, value in pending.items())
    _atomic_write(path, ''.join(output))
