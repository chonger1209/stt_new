"""Portable, documented STT inputs for the existing shared toolkit config."""
import copy
import json
from pathlib import Path
from urllib.parse import urlsplit

FIELDS = {
    'qwen_enabled': (True, '是否启用千问管理；true/false。false 不查询、不启动、不自动休眠，普通统计照常。'),
    'qwen_prepare_service_on_start': (True, 'STT 启动时是否准备接收服务；true/false。不主动加载模型。'),
    'qwen_toolkit_path': ('', '完整管理工具包目录，内含 scripts/manager.py 和 lifecycle.py；留空自动查找。示例 E:/AI/qwen-management。'),
    'qwen_base_url': ('', '本机服务地址，例 http://127.0.0.1:8765/v1；留空沿用工具包。不能填写远程 IP。'),
    'qwen_model_id': ('', '服务公布的模型 ID，不一定等于模型文件夹名称；留空沿用工具包。'),
    'qwen_python_path': ('', '模型环境的 python.exe 完整路径；例 E:/AI/Strata/.venv/Scripts/python.exe。留空沿用工具包。'),
    'qwen_working_directory': ('', '模型服务工作目录，例 E:/AI/Strata；留空沿用工具包。'),
    'qwen_launch_args': ('', '启动参数，填 JSON 字符串数组，建议路径用 /；例 ["serve/server.py", "--port", "8765"]。\n# 必须复制已经验证可用的参数；模型路径通常也在其中。留空沿用，[] 表示明确清空参数。'),
    'qwen_startup_timeout_seconds': (900, '整个启动操作的总等待预算，单位秒，正整数；默认900。'),
}


def toolkit_scripts(root=''):
    candidates = [Path(root)] if root else [Path.home()/'.agents/skills/qwen-collaboration', Path.home()/'.codex/skills/qwen-collaboration']
    for candidate in candidates:
        scripts = candidate.resolve() / 'scripts'
        if all((scripts/name).is_file() for name in ('manager.py', 'lifecycle.py')):
            return scripts
    raise FileNotFoundError('qwen_toolkit_path：未找到完整管理工具包（scripts/manager.py、lifecycle.py）')


def read_global_values(text):
    values = {}
    for line in text.splitlines():
        line = line.strip()
        if line.startswith('['):
            break
        if '=' in line and not line.startswith('#'):
            key, value = line.split('=', 1)
            values[key.strip()] = value.split(';', 1)[0].strip()
    return values


def migrate_existing(path, scripts=None):
    """Fill only absent fields from the actual service.json; never rewrite it."""
    from settings import _atomic_write, encode
    if not path.exists():
        return []
    original = path.read_text(encoding='utf-8-sig')
    values = read_global_values(original)
    try:
        scripts = Path(scripts) if scripts else toolkit_scripts(values.get('qwen_toolkit_path', ''))
        c = json.loads((scripts.parent/'service.json').read_text(encoding='utf-8-sig'))
    except FileNotFoundError:
        return []
    launch = c.get('launch', {})
    imports = {'qwen_toolkit_path': str(scripts.parent), 'qwen_base_url': c.get('base_url', ''),
               'qwen_model_id': c.get('model', ''), 'qwen_python_path': launch.get('command', ''),
               'qwen_working_directory': launch.get('cwd', ''),
               'qwen_launch_args': json.dumps(launch.get('args', []), ensure_ascii=False),
               'qwen_startup_timeout_seconds': c.get('startup_timeout_sec', 900)}
    addition = []
    for key, value in imports.items():
        if key not in values:
            addition += [f'# {key}: {FIELDS[key][1]}', f'{key} = {encode(value)}', '']
    if addition:
        backup = path.with_name(path.name+'.before_qwen_migration.bak')
        if not backup.exists():
            backup.write_bytes(path.read_bytes())
        lines = original.splitlines(keepends=True)
        index = next((i for i,l in enumerate(lines) if l.lstrip().startswith('[')), len(lines))
        lines.insert(index, '\n# 从当前千问服务迁入的配置；未覆盖已有字段。\n'+'\n'.join(addition)+'\n')
        _atomic_write(path, ''.join(lines))
    return [key for key in imports if key not in values]


def effective_config(settings, existing):
    """Validate before any shared config write; preserve adapter/network options."""
    config = copy.deepcopy(existing)
    launch = config.setdefault('launch', {})
    for key, target in [('qwen_base_url','base_url'), ('qwen_model_id','model')]:
        if settings.get(key):
            config[target] = settings[key].strip()
    for key, target in [('qwen_python_path','command'), ('qwen_working_directory','cwd')]:
        if settings.get(key):
            launch[target] = settings[key].strip()
    if settings.get('qwen_launch_args', '').strip():
        try:
            args = json.loads(settings['qwen_launch_args'])
            if not isinstance(args, list) or not all(isinstance(a,str) for a in args):
                raise ValueError()
        except (ValueError, TypeError):
            raise ValueError('qwen_launch_args：请填写 JSON 字符串数组，路径建议用 /') from None
        launch['args'] = args
    url = urlsplit(config.get('base_url', ''))
    try:
        port = url.port
    except ValueError:
        raise ValueError('qwen_base_url：端口无效') from None
    if (url.scheme != 'http' or url.hostname not in ('127.0.0.1','localhost','::1')
            or url.username or url.password or url.query or url.fragment or url.path.rstrip('/') != '/v1'
            or port == 0):
        raise ValueError('qwen_base_url：必须是本机 HTTP 地址并以 /v1 结尾，例如 http://127.0.0.1:8765/v1')
    if not isinstance(config.get('model'),str) or not config['model'].strip():
        raise ValueError('qwen_model_id：缺少服务公布的模型 ID')
    if not Path(launch.get('command','')).is_file():
        raise ValueError('qwen_python_path：启动解释器不存在')
    if not Path(launch.get('cwd','')).is_dir():
        raise ValueError('qwen_working_directory：工作目录不存在')
    if not isinstance(launch.get('args'),list) or not all(isinstance(a,str) for a in launch['args']):
        raise ValueError('qwen_launch_args：缺少启动参数数组')
    wait = settings.get('qwen_startup_timeout_seconds',900)
    if isinstance(wait,bool) or not isinstance(wait,int) or wait <= 0:
        raise ValueError('qwen_startup_timeout_seconds：必须是正整数')
    config['startup_timeout_sec'] = wait
    config['base_url'] = config['base_url'].rstrip('/')
    lifecycle = config.setdefault('lifecycle', {})
    lifecycle.update(owner='stt', enabled=False, shutdown_mode='unload')
    # Existing auth-free and subnet/adapter policies are preserved, not invented.
    return config
