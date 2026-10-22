"""Local-only service and switch manager. Standard library; no shell execution."""
from __future__ import annotations
import argparse
import contextlib
import datetime
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

if hasattr(sys.stdout, 'reconfigure'):
    sys.stdout.reconfigure(encoding='utf-8')
if hasattr(sys.stderr, 'reconfigure'):
    sys.stderr.reconfigure(encoding='utf-8')

ROOT = Path(os.environ.get('QWEN_COLLAB_ROOT', Path(__file__).resolve().parents[1])).resolve()

class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise urllib.error.HTTPError(req.full_url, code, 'Local redirect refused', headers, fp)

OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())

def now():
    return datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=8))).isoformat(timespec='seconds')

def read_json(path):
    return json.loads(path.read_text(encoding='utf-8-sig'))

def atomic_json(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + '.' + str(os.getpid()) + '.tmp')
    try:
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)

def state():
    try:
        s = read_json(ROOT / 'state.json')
        if not isinstance(s.get('enabled'), bool) or not isinstance(s.get('auto_start'), bool):
            raise ValueError('invalid switch')
        return s
    except (OSError, ValueError, TypeError, AttributeError):
        return {'enabled': False, 'auto_start': False, 'state_error': '开关文件缺失或损坏，按关闭处理'}

@contextlib.contextmanager
def lock(name, timeout=10):
    folder = ROOT / 'runtime'
    folder.mkdir(exist_ok=True)
    f = (folder / (name + '.lock')).open('a+b')
    f.seek(0, 2)
    if not f.tell():
        f.write(b'0'); f.flush()
    deadline = time.monotonic() + timeout
    while True:
        try:
            f.seek(0)
            if os.name == 'nt':
                import msvcrt
                msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(f.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            break
        except OSError:
            if time.monotonic() >= deadline:
                f.close(); raise TimeoutError('其他会话正在处理本地服务，请稍后再试')
            time.sleep(.15)
    try:
        yield
    finally:
        f.seek(0)
        if os.name == 'nt':
            import msvcrt
            msvcrt.locking(f.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl
            fcntl.flock(f.fileno(), fcntl.LOCK_UN)
        f.close()

def set_state(enabled=None, auto_start=None):
    with lock('state'):
        s = state()
        s.pop('state_error', None)
        if enabled is not None:
            s['enabled'] = enabled
        if auto_start is not None:
            s['auto_start'] = auto_start
        # Revision invalidates in-flight outputs across off/on cycles.
        s.update(updated_at=now(), revision=time.time_ns())
        atomic_json(ROOT / 'state.json', s)
    return state()

def config():
    c = read_json(ROOT / 'service.json')
    u = urllib.parse.urlsplit(c['base_url'])
    if u.scheme != 'http' or u.hostname not in ('127.0.0.1', 'localhost', '::1') or u.username or u.password or u.query or u.fragment:
        raise ValueError('本工具包只允许本机回环HTTP服务，不能自动转发到外部地址')
    c['base_url'] = c['base_url'].rstrip('/')
    return c

def api_key(c):
    """One credential source for STT, local clients and the service process."""
    if c.get('api_key_required') is False:
        return ''
    if c.get('api_key_file'):
        key = (ROOT / c['api_key_file']).read_text(encoding='utf-8').strip()
        if not key or any(ch.isspace() for ch in key):
            raise ValueError('API密钥文件为空或格式无效，拒绝无认证启动')
        return key
    return os.environ.get(c.get('api_key_env', 'QWEN_LOCAL_API_KEY'), '')

def service_environment(c):
    environment = os.environ.copy()
    if c.get('api_key_required') is False:
        environment.pop('STRATA_API_KEY', None)
        return environment
    key = api_key(c)
    if key:
        environment['STRATA_API_KEY'] = key
    return environment

def request_json(url, c, body=None, timeout=3):
    headers = {'Content-Type': 'application/json'}
    key = api_key(c)
    if key:
        headers['Authorization'] = 'Bearer ' + key
    req = urllib.request.Request(url, data=None if body is None else json.dumps(body).encode('utf-8'), headers=headers)
    with OPENER.open(req, timeout=timeout) as r:
        return json.load(r)

def probe(c):
    u = urllib.parse.urlsplit(c['base_url'])
    origin = urllib.parse.urlunsplit((u.scheme, u.netloc, '', '', ''))
    try:
        models = request_json(c['base_url'] + '/models', c)
        rows = models.get('data', [])
        matched = next((m for m in rows if m.get('id') == c['model']), None)
        if not matched:
            return {'ready': False, 'reachable': True, 'error': '服务模型与配置不匹配，不自动启动第二份服务'}
        health = {}
        try:
            health = request_json(origin + '/health', c)
        except (OSError, ValueError):
            pass
        model_state = matched.get('status', {})
        if isinstance(model_state, dict):
            model_state = model_state.get('value')
        loaded = health.get('loaded', model_state not in ('unloaded', 'loading'))
        return {'ready': loaded is True, 'reachable': True, 'model': matched['id'],
                'context_length': health.get('max_context', matched.get('meta', {}).get('n_ctx')),
                'loaded': loaded, 'service': health.get('service')}
    except urllib.error.HTTPError as e:
        return {'ready': False, 'reachable': True, 'error': '本地服务HTTP ' + str(e.code)}
    except (OSError, ValueError, TypeError, AttributeError):
        return {'ready': False, 'reachable': False, 'error': '本地模型服务未就绪'}

def port_busy(c):
    u = urllib.parse.urlsplit(c['base_url'])
    try:
        with socket.create_connection((u.hostname, u.port or 80), timeout=1):
            return True
    except OSError:
        return False

def status():
    s = state()
    try:
        p = probe(config())
    except (OSError, ValueError, KeyError) as e:
        p = {'ready': False, 'error': '服务配置无效：' + type(e).__name__}
    try:
        policy = config().get('lifecycle', {})
    except (OSError, ValueError, KeyError):
        policy = {}
    watching = False
    if policy.get('enabled'):
        try:
            import lifecycle
            watching = lifecycle.alive(read_json(ROOT/'runtime'/'watchdog.json'))
        except (ImportError, OSError, ValueError, KeyError):
            pass
    return {'enabled': s['enabled'], 'auto_start': s['auto_start'],
            'state_error': s.get('state_error'), 'power_policy': policy,
            'power_watchdog_running': watching, **p}

def ensure(one_shot=False):
    s = state()
    if not s['enabled'] and not one_shot:
        return {'ok': False, 'enabled': False, 'error': '千问协作已关闭；由GPT独立完成'}
    try:
        c = config()
        p = probe(c)
        if p['ready']:
            return {'ok': True, 'reused': True, **p}
        if not s['auto_start']:
            return {'ok': False, **p, 'error': '自动启动已关闭，且匹配模型尚未就绪'}
        wait = min(max(float(c.get('startup_timeout_sec', 180)), 1), 300)
        with lock('startup', timeout=wait + 5):
            # A waiting session must not launch after another session disables collaboration.
            if not state()['enabled'] and not one_shot:
                return {'ok': False, 'error': '协作已关闭，取消启动'}
            if not state()['auto_start']:
                p = probe(c)
                return {'ok': p['ready'], **p}
            p = probe(c)
            if p['ready']:
                return {'ok': True, 'reused': True, **p}
            # An idle-unloaded Strata keeps its HTTP service. Explicitly load before generation.
            if p.get('reachable') and p.get('loaded') is False and p.get('service') == 'strata':
                request_json(c['base_url'] + '/load', c, {'model': c['model']}, timeout=wait)
                p = probe(c)
                if p['ready']:
                    return {'ok': True, 'reused': True, 'preheated': True, **p}
            occupied = port_busy(c)
            if p.get('reachable') and p.get('error'):
                return {'ok': False, **p}
            proc = None
            if not occupied:
                launch = c['launch']
                command = Path(launch['command'])
                if not command.is_file() or not Path(launch['cwd']).is_dir():
                    return {'ok': False, 'error': '启动程序或工作目录不存在，请修改service.json'}
                logdir = ROOT / 'runtime'
                logdir.mkdir(exist_ok=True)
                flags = (subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP) if os.name == 'nt' else 0
                with (logdir / 'service.log').open('ab') as log:
                    proc = subprocess.Popen([str(command), *launch['args']], cwd=launch['cwd'], stdin=subprocess.DEVNULL,
                                            stdout=log, stderr=log, creationflags=flags,
                                            env=service_environment(c))
                atomic_json(logdir / 'last-launch.json', {'pid': proc.pid, 'at': now(), 'model': c['model']})
            deadline = time.monotonic() + wait
            load_requested = False
            while time.monotonic() < deadline:
                if not state()['enabled'] and not one_shot:
                    return {'ok': False, 'error': '协作已关闭，停止等待；不终止用户服务'}
                p = probe(c)
                if p['ready']:
                    return {'ok': True, 'reused': occupied, **p}
                if (not load_requested and p.get('reachable') and not p.get('error')
                        and p.get('service') == 'strata' and p.get('loaded') is False):
                    load_requested = True
                    request_json(c['base_url'] + '/load', c, {'model': c['model']}, timeout=wait)
                if proc is not None and proc.poll() is not None and not port_busy(c):
                    return {'ok': False, 'error': '模型启动程序退出，请查看runtime/service.log'}
                time.sleep(.7)
            return {'ok': False, 'error': '模型启动等待超时，GPT可继续；后台服务如仍在加载不强行关闭'}
    except (OSError, ValueError, KeyError, TimeoutError) as e:
        return {'ok': False, 'error': '本地服务管理失败：' + str(e)}

def prewarm():
    try:
        import lifecycle
        lifecycle.ensure_watchdog()
    except (ImportError, OSError, ValueError, TimeoutError):
        pass
    if not (state()['enabled'] and state()['auto_start']):
        return False
    folder = ROOT / 'runtime'
    folder.mkdir(exist_ok=True)
    flags = (subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP) if os.name == 'nt' else 0
    with (folder / 'prewarm.log').open('ab') as log:
        subprocess.Popen([sys.executable, str(Path(__file__).resolve()), 'ensure'], stdin=subprocess.DEVNULL,
                         stdout=log, stderr=log, creationflags=flags, cwd=str(ROOT))
    return True

def prompt_check():
    """Fast status probe for the WorkBuddy UserPromptSubmit hook; never blocks on startup."""
    s = state()
    try:
        c = config()
        p = probe(c)
    except (OSError, ValueError, KeyError) as e:
        return {'ready': False, 'launched': False, 'error': '服务配置无效：' + type(e).__name__}
    launched = False
    if not p['ready'] and s['enabled'] and s['auto_start']:
        marker = ROOT / 'runtime' / 'launch-requested.json'
        recent = False
        try:
            if time.time() - float(read_json(marker).get('requested_at', 0)) < 120:
                recent = True
        except (OSError, ValueError, TypeError):
            pass
        if not recent:
            marker.parent.mkdir(exist_ok=True)
            atomic_json(marker, {'requested_at': time.time()})
            flags = (subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP) if os.name == 'nt' else 0
            with (marker.parent / 'prewarm.log').open('ab') as log:
                subprocess.Popen([sys.executable, str(Path(__file__).resolve()), 'ensure'], stdin=subprocess.DEVNULL,
                                 stdout=log, stderr=log, creationflags=flags, cwd=str(ROOT))
            launched = True
    try:  # heartbeat: proves whether the hook actually fired, independent of what the model sees
        logdir = ROOT / 'runtime'
        logdir.mkdir(exist_ok=True)
        with (logdir / 'prompt-check.log').open('a', encoding='utf-8') as f:
            f.write(f"{now()} ready={p.get('ready')} reachable={p.get('reachable')} launched={launched}\n")
    except OSError:
        pass
    return {'ready': p.get('ready', False), 'launched': launched, **p}

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('action', choices=['status', 'ensure', 'prewarm', 'enable', 'disable', 'auto-on', 'auto-off', 'session-start', 'prompt-check'])
    a = parser.parse_args().action
    if a in ('enable', 'disable', 'auto-on', 'auto-off'):
        s = set_state(enabled=(a == 'enable') if a in ('enable', 'disable') else None,
                      auto_start=(a == 'auto-on') if a in ('auto-on', 'auto-off') else None)
        result = {'ok': True, **s}
    elif a == 'ensure':
        result = ensure()
    elif a == 'prompt-check':
        r = prompt_check()
        if not r['ready']:
            message = '千问状态：' + ('已发起后台启动，尚未就绪。' if r.get('launched') else '未就绪（自动启动已关闭，不拉起）。')
        else:
            message = '千问状态：在线。'
        result = {'systemMessage': message, 'hookSpecificOutput': {'hookEventName': 'UserPromptSubmit', 'additionalContext': message}}
    elif a in ('prewarm', 'session-start'):
        started = prewarm()
        result = status()
        result['prewarm_requested'] = started
    else:
        result = status()
    if a == 'session-start':
        s = state()
        if not s['enabled']:
            message = 'GPT：千问协作已关闭，本次由GPT独立完成。'
        elif result.get('ready'):
            message = 'GPT：千问已就绪，协作已开启；优先委托适合的分析工作。'
        else:
            message = 'GPT：千问协作已开启，当前服务未就绪。' + ('已请求后台启动。' if started else '自动启动已关闭。')
        context = message + ' 请读取全局 qwen-collaboration 技能，按实际主要来源署名；不得把未就绪说成已就绪。'
        result = {'systemMessage': message, 'hookSpecificOutput': {'hookEventName': 'SessionStart', 'additionalContext': context}}
    print(json.dumps(result, ensure_ascii=False))

if __name__ == '__main__':
    main()
