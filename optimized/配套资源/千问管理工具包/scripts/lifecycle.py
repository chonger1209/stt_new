"""Qwen-drafted lifecycle policy; GPT-reviewed, exact-identity Strata shutdown."""
import json
import os
from pathlib import Path
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import psutil
import manager

def decide_unload(loaded, in_flight, live_codex_count, last_request_at, all_exited_at,
                  now, idle_seconds=1800, exit_grace_seconds=20):
    # Adapted from Qwen's revised draft; idle is independent of whether Codex stays open.
    if not loaded:
        return 'not_loaded'
    if in_flight > 0:
        return 'keep'
    if all_exited_at is not None and live_codex_count == 0 and now - all_exited_at >= exit_grace_seconds:
        return 'exit_unload'
    if last_request_at is not None and now - last_request_at >= idle_seconds:
        return 'idle_unload'
    return 'keep'

def identity(pid):
    p = psutil.Process(pid)
    return {'pid': p.pid, 'created': p.create_time()}

def alive(record):
    try:
        return abs(psutil.Process(record['pid']).create_time() - record['created']) < .001
    except psutil.AccessDenied:
        return True  # Unknown is not evidence of exit.
    except (psutil.NoSuchProcess, KeyError, ValueError, TypeError):
        return False

def discover_codex():
    """Observe Codex executable identities; do not confuse ordinary ChatGPT or one closed tab with exit."""
    found = []
    for p in psutil.process_iter(['pid', 'name']):
        if (p.info['name'] or '').lower() not in ('codex.exe', 'chatgpt.exe'):
            continue
        try:
            exe = p.exe().replace('/', '\\').lower()
            if '\\openai.codex_' in exe or '\\openai\\codex\\' in exe:
                found.append(identity(p.pid))
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
    return found

def service_activity(c):
    r = manager.request_json(c['base_url'] + '/status', c, timeout=4)
    if r.get('service') != 'strata' or r.get('model') != c['model']:
        raise ValueError('服务身份不匹配，不管理其资源')
    a = r.get('activity', {})
    # Fail closed if busy/idle metadata is absent; do not serialize response fragments.
    if not isinstance(a.get('in_flight'), (int, float)):
        raise ValueError('缺少活动状态，不自动卸载')
    return {'loaded': r.get('loaded') is True, 'in_flight': a['in_flight'],
            'last_request_at': a.get('last_request_at') or r.get('started')}

def server_matches(process, c):
    """Match configured server script and config, including venv Python wrapper parents."""
    def normalized(value):
        return str(Path(value).resolve()).casefold()
    args = process.cmdline()
    expected = c['launch']['args']
    script = next((a for a in expected if a.lower().endswith('.py')), None)
    scripts = [script] if script else []
    if '--strata-server' in expected:
        scripts.append(expected[expected.index('--strata-server') + 1])
    if not scripts or not any(normalized(a) == normalized(candidate) for a in args for candidate in scripts):
        return False
    for flag in ('--config', '--engine'):
        if flag in expected:
            if flag not in args:
                return False
            e, a = expected[expected.index(flag)+1], args[args.index(flag)+1]
            same = normalized(e) == normalized(a) if flag == '--config' else e == a
            if not same:
                return False
    return True

def identify_service(c):
    port = urllib.parse.urlsplit(c['base_url']).port or 80
    ids = {conn.pid for conn in psutil.net_connections(kind='tcp')
           if conn.status == psutil.CONN_LISTEN and conn.laddr.port == port
           and conn.laddr.ip in ('127.0.0.1', '0.0.0.0', '::1', '::') and conn.pid}
    if len(ids) != 1:
        raise ValueError('无法唯一确认模型服务监听进程，未结束任何进程')
    listener = psutil.Process(ids.pop())
    if not server_matches(listener, c):
        raise ValueError('监听进程与配置的服务器脚本/配置不匹配，未结束进程')
    root = listener
    while True:
        parent = root.parent()
        if parent is None or not server_matches(parent, c):
            break
        root = parent
    # Only this verified server's existing process tree, never all processes of a given name.
    return {'listener': identity(listener.pid), 'root': identity(root.pid),
            'listener_addresses': [conn.laddr.ip for conn in listener.net_connections(kind='tcp')
                                   if conn.status == psutil.CONN_LISTEN and conn.laddr.port == port],
            'tree': [identity(p.pid) for p in root.children(recursive=True)] + [identity(root.pid)]}

def event(action, **fields):
    with manager.lock('power-events', timeout=2):
        path = manager.ROOT/'runtime'/'lifecycle-events.jsonl'
        with path.open('a', encoding='utf-8') as f:
            f.write(json.dumps({'at':manager.now(),'action':action,**fields},ensure_ascii=False)+'\n')

def stop_verified_service(c, service):
    """Called only with request/startup locks held, after successful native unload."""
    if not alive(service['listener']) or not alive(service['root']):
        return False
    listener = psutil.Process(service['listener']['pid'])
    if not server_matches(listener, c):
        return False
    after = service_activity(c)
    # A new load/generation by an external client wins; do not stop its work.
    if after['in_flight'] or after['loaded']:
        return False
    targets = []
    for record in service['tree']:
        if alive(record):
            try:
                p = psutil.Process(record['pid'])
                if abs(p.create_time()-record['created']) >= .001:
                    return False
                targets.append(p)
                p.terminate()
            except psutil.NoSuchProcess:
                pass  # It already exited between the identity check and termination/wait.
    _, pending = psutil.wait_procs(targets, timeout=5)
    # Windows terminate already ends a process; no broad taskkill or process-name fallback.
    if pending or manager.port_busy(c):
        return False
    manager.atomic_json(manager.ROOT/'runtime'/'last-service-stop.json', {
        'at':manager.now(), 'root_pid':service['root']['pid'], 'model':c['model'], 'complete':True})
    return True

def unload_if_due(c, host_count, exited_at, now=None):
    """Recheck under the same startup lock used for load. Strata itself rejects busy unload atomically."""
    policy = c.get('lifecycle', {})
    with manager.lock('model-request', timeout=.2), manager.lock('startup', timeout=.2):
        full_stop = policy.get('shutdown_mode') == 'service'
        if full_stop and not manager.port_busy(c):
            return 'service_off'
        activity = service_activity(c)
        decision = dict(activity, loaded=activity['loaded'] or full_stop)
        action = decide_unload(**decision, live_codex_count=host_count, all_exited_at=exited_at,
                               now=time.time() if now is None else now,
                               idle_seconds=float(policy.get('idle_unload_sec', 1800)),
                               exit_grace_seconds=float(policy.get('exit_grace_sec', 20)))
        if action == 'exit_unload' and not policy.get('unload_on_codex_exit', True):
            action = 'keep'
        if action in ('idle_unload', 'exit_unload'):
            service = identify_service(c) if full_stop else None
            try:
                result = manager.request_json(c['base_url'] + '/unload', c, {'model': c['model']}, timeout=75)
            except urllib.error.HTTPError as e:
                if e.code == 409:
                    return 'busy'
                raise
            if result.get('status') not in ('unloaded', 'not loaded'):
                return 'not_confirmed'
            if full_stop:
                if not stop_verified_service(c, service):
                    event('service_stop_not_confirmed',reason=action)
                    return 'stop_not_confirmed'
                action = action.replace('_unload','_stop')
                event(action,root_pid=service['root']['pid'],model=c['model'])
        return action

def ensure_watchdog():
    if not manager.config().get('lifecycle', {}).get('enabled', False):
        return False
    root = manager.ROOT / 'runtime'
    root.mkdir(exist_ok=True)
    with manager.lock('watchdog-spawn', timeout=2):
        record = root / 'watchdog.json'
        try:
            previous = manager.read_json(record)
            if alive(previous):
                return True
        except (OSError, ValueError, KeyError):
            pass
        flags = (subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP) if os.name == 'nt' else 0
        with (root / 'watchdog.log').open('ab') as log:
            p = subprocess.Popen([sys.executable, '-X', 'utf8', '-B', str(Path(__file__).resolve()), 'watch'],
                                 stdin=subprocess.DEVNULL, stdout=log, stderr=log, cwd=str(manager.ROOT), creationflags=flags)
        manager.atomic_json(record, dict(identity(p.pid), started_at=manager.now()))
    return True

def watch():
    try:
        with manager.lock('watchdog-instance', timeout=.2):
            known = {}
            observed = False
            exited_at = None
            while True:
                c = manager.config()
                policy = c.get('lifecycle', {})
                if not policy.get('enabled', False):
                    return
                for item in discover_codex():
                    known[(item['pid'], item['created'])] = item
                    observed = True
                live = [item for item in known.values() if alive(item)]
                current = time.time()
                exited_at = None if live else (exited_at or current) if observed else None
                action = 'keep'
                try:
                    action = unload_if_due(c, len(live), exited_at)
                except (OSError, ValueError, KeyError, TimeoutError, psutil.Error) as e:
                    action = 'unavailable:' + type(e).__name__
                # No prompts, answers, status bodies or executable paths are written here.
                manager.atomic_json(manager.ROOT/'runtime'/'power-status.json', {
                    'at': manager.now(), 'action': action, 'live_codex_processes': len(live),
                    'observed_codex': observed, 'idle_unload_sec': policy.get('idle_unload_sec',1800)})
                if observed and not live and exited_at is not None and current-exited_at >= policy.get('exit_grace_sec',20):
                    if action in ('exit_unload', 'not_loaded', 'exit_stop', 'service_off'):
                        return
                time.sleep(max(1, min(float(policy.get('poll_sec', 10)), 30)))
    except TimeoutError:
        return  # Another watchdog owns the instance lock.

if __name__ == '__main__':
    if len(sys.argv) > 1 and sys.argv[1] == 'watch':
        watch()
    else:
        print(json.dumps({'watchdog_started_or_running': ensure_watchdog()},ensure_ascii=False))
