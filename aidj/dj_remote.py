"""Bounded parallel CPU preparation on an owner's Mac, over existing SSH access.

The server keeps account credentials and leases work. This process transfers one
verified original per slot and returns only numeric analysis, never transcripts.
"""
import argparse
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
import os
from pathlib import Path
import re
import shlex
import signal
import shutil
import subprocess
import sys
import tempfile
import threading
import time

MAX_JSON=1024*1024
MAX_AUDIO=40*1024**2
RSS_LIMIT=1024**2*1024
PRINT_LOCK=threading.Lock()


class RemoteError(RuntimeError):
    def __init__(self,kind):
        self.kind=kind if isinstance(kind,str) and re.fullmatch('[A-Z_]{2,64}',kind) else 'REMOTE_FAILED'
        super().__init__(self.kind)


def emit(message):
    with PRINT_LOCK:print(message,flush=True)


def ram_bytes():
    try:
        if sys.platform=='darwin':return int(subprocess.check_output(['sysctl','-n','hw.memsize'],text=True,stderr=subprocess.DEVNULL))
        return os.sysconf('SC_PHYS_PAGES')*os.sysconf('SC_PAGE_SIZE')
    except (OSError,ValueError,subprocess.SubprocessError):return 4*1024**3


def worker_count(requested,cpus=None,memory=None):
    cores=cpus if cpus is not None else os.cpu_count() or 2
    ram=memory if memory is not None else ram_bytes()
    # Keep room for macOS and the user's normal applications. One worker has a
    # hard process-tree watchdog at 1 GiB, plus controller/interpreter overhead.
    limit=max(1,min(8,max(1,cores-2),int(max(0,ram-2*1024**3)/(1.5*1024**3))))
    return min(requested if requested is not None else 4,limit)


def ssh_command(server,project,control=None):
    if not re.fullmatch(r'[A-Za-z0-9_][A-Za-z0-9_.@:\-]*',server):raise ValueError('Некорректный SSH-адрес.')
    if not project.startswith('/') or '\0' in project or '\n' in project:raise ValueError('Нужен абсолютный путь проекта на сервере.')
    argv=['ssh','-T','-o','BatchMode=yes','-o','ConnectTimeout=15','-o','ServerAliveInterval=20','-o','ServerAliveCountMax=3']
    if control:argv+=['-o','ControlMaster=auto','-o','ControlPersist=600','-o','ControlPath='+str(control/'connection')]
    return argv+['--',server], 'cd -- '+shlex.quote(project)+' && exec .venv/bin/python -m aidj.dj_batch'


class Transport:
    def __init__(self,server,project,control=None):self.argv,self.remote=ssh_command(server,project,control)
    def rpc(self,body):
        raw=json.dumps(body,separators=(',',':')).encode()
        if len(raw)>MAX_JSON:raise RemoteError('REQUEST_TOO_LARGE')
        try:result=subprocess.run([*self.argv,self.remote+' rpc'],input=raw,capture_output=True,timeout=90)
        except (OSError,subprocess.TimeoutExpired):raise RemoteError('SSH_FAILED') from None
        if len(result.stdout)>MAX_JSON:raise RemoteError('RESPONSE_TOO_LARGE')
        try:value=json.loads(result.stdout)
        except (ValueError,TypeError):raise RemoteError('SSH_FAILED') from None
        if not isinstance(value,dict) or value.get('ok') is not True:
            raise RemoteError(value.get('error') if isinstance(value,dict) else 'REMOTE_FAILED')
        if result.returncode:raise RemoteError('SSH_FAILED')
        return value
    def audio(self,identity,asset,file,stopped):
        if not valid_asset(asset) or identity.get('md5')!=asset.get('md5',identity.get('md5')):raise RemoteError('INVALID_JOB')
        flags=' audio --run '+shlex.quote(identity['runId'])+' --md5 '+shlex.quote(identity['md5'])+' --token '+shlex.quote(identity['token'])
        try:process=subprocess.Popen([*self.argv,self.remote+flags],stdout=subprocess.PIPE,stderr=subprocess.DEVNULL,start_new_session=True)
        except OSError:raise RemoteError('SSH_FAILED') from None
        guard_done=threading.Event();timed_out=threading.Event()
        def guard():
            deadline=time.monotonic()+300
            while not guard_done.wait(.5):
                if stopped.is_set() or time.monotonic()>deadline:
                    timed_out.set();kill(process);return
        thread=threading.Thread(target=guard,daemon=True);thread.start()
        digest=hashlib.md5();size=0
        try:
            with file.open('wb') as out:
                while True:
                    block=process.stdout.read(65536)
                    if not block:break
                    size+=len(block)
                    if size>asset['bytes'] or size>MAX_AUDIO:raise RemoteError('DOWNLOAD_TOO_LARGE')
                    digest.update(block);out.write(block)
            if process.wait(timeout=10) or timed_out.is_set():raise RemoteError('DOWNLOAD_FAILED')
            if size!=asset['bytes'] or digest.hexdigest()!=identity['md5']:raise RemoteError('DOWNLOAD_MISMATCH')
        except BaseException:
            kill(process);file.unlink(missing_ok=True);raise
        finally:
            guard_done.set();thread.join(timeout=1)
            if process.stdout:process.stdout.close()
    def close(self):
        # This controller owns its private socket; do not touch another SSH session.
        try:subprocess.run([*self.argv[:-2],'-O','exit',*self.argv[-2:]],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,timeout=5)
        except (OSError,subprocess.TimeoutExpired):pass


def valid_asset(asset):
    return (isinstance(asset,dict) and type(asset.get('bytes')) is int and 0<asset['bytes']<=MAX_AUDIO
            and isinstance(asset.get('duration'),(int,float)) and 30<=asset['duration']<=600
            and isinstance(asset.get('format'),str) and re.fullmatch('[a-z0-9]{1,8}',asset['format']) is not None)


def kill(process):
    if process.poll() is None:
        try:os.killpg(process.pid,signal.SIGKILL)
        except ProcessLookupError:pass
    try:process.wait(timeout=10)
    except subprocess.TimeoutExpired:pass


def process_rss(pid):
    """RSS of this analysis and its descendants; no command lines or env read."""
    try:
        rows=subprocess.check_output(['ps','-axo','pid=,ppid=,rss='],text=True,stderr=subprocess.DEVNULL,timeout=3).splitlines()
        values=[]
        for row in rows:
            parts=row.split()
            if len(parts)==3:values.append(tuple(map(int,parts)))
        own={pid};changed=True
        while changed:
            children={p for p,parent,rss in values if parent in own};changed=bool(children-own);own|=children
        return sum(rss for p,parent,rss in values if p in own)*1024
    except (OSError,ValueError,subprocess.SubprocessError):raise RemoteError('RESOURCE_MONITOR_FAILED') from None


def analyse_command(file,output,checkpoint,cached,threads,ffmpeg):
    command=[sys.executable,'-m','aidj.dj_worker','analyse','--file',str(file),'--output',str(output),
             '--ffmpeg',ffmpeg,'--full','--threads',str(threads),'--checkpoint',str(checkpoint)]
    if cached:command+=['--cached',str(cached)]
    return command


def run_analysis(command,env,stopped,lease_lost,timeout=900,memory_limit=RSS_LIMIT):
    try:process=subprocess.Popen(command,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,env=env,start_new_session=True)
    except OSError:raise RemoteError('ANALYSIS_FAILED') from None
    began=time.monotonic();monitored=0
    try:
        while process.poll() is None:
            if stopped.wait(.5):raise RemoteError('INTERRUPTED')
            if lease_lost.is_set():raise RemoteError('LEASE_LOST')
            if time.monotonic()-began>timeout:raise RemoteError('TIMEOUT')
            if time.monotonic()-monitored>=2:
                if process_rss(process.pid)>memory_limit:raise RemoteError('RESOURCE_LIMIT')
                monitored=time.monotonic()
        if process.returncode in (-9,137):raise RemoteError('RESOURCE_LIMIT')
        if process.returncode:raise RemoteError('ANALYSIS_FAILED')
    except BaseException:
        kill(process);raise


def json_file(file):
    if not file.is_file() or not 0<file.stat().st_size<=MAX_JSON:raise RemoteError('RESULT_INVALID')
    try:
        value=json.loads(file.read_text())
        if not isinstance(value,dict):raise ValueError()
        return value
    except (ValueError,OSError):raise RemoteError('RESULT_INVALID') from None


def checkpoint_for(server,local):
    if local and local.get('version')==3 and len(local.get('probes',[]))>=len((server or {}).get('probes',[])):
        return local
    return server


def status_line(stats):
    return f"Готово {stats.get('done',0)}/{stats.get('total',0)} · в работе {stats.get('running',0)} · ошибок {stats.get('failed',0)}"


def job_identity(run,job):
    values={'runId':run,'md5':job.get('md5'),'token':job.get('token')}
    if not all(isinstance(value,str) and re.fullmatch('[a-f0-9]{'+str(length)+'}',value) for value,length in zip(values.values(),(24,32,32))):raise RemoteError('INVALID_JOB')
    return values


def heartbeat(transport,identity,done,lost):
    failures=0
    while not done.wait(30):
        try:transport.rpc({'op':'heartbeat',**identity});failures=0
        except RemoteError:
            failures+=1
            if failures>=3:lost.set();return


def complete_job(transport,identity,result,stopped):
    for attempt in range(3):
        try:return transport.rpc({'op':'finish',**identity,'result':result})
        except RemoteError as exc:
            if exc.kind not in ('SSH_FAILED','REMOTE_FAILED') or attempt==2:raise
            if stopped.wait(2):raise RemoteError('INTERRUPTED') from None


def work_job(transport,run,job,state,threads,ffmpeg,stopped):
    identity=job_identity(run,job);asset=job.get('asset')
    if not valid_asset(asset):raise RemoteError('INVALID_JOB')
    checkpoint=state/'checkpoints'/(identity['md5']+'.json');done=threading.Event();lost=threading.Event()
    keeper=threading.Thread(target=heartbeat,args=(transport,identity,done,lost),daemon=True);keeper.start()
    uploaded=False
    try:
        with tempfile.TemporaryDirectory(prefix='song-',dir=state/'work') as scratch:
            folder=Path(scratch);file=folder/('source.'+asset['format']);output=folder/'result.json'
            transport.audio(identity,asset,file,stopped)
            cached=job.get('cached');local=None
            if checkpoint.is_file():
                try:local=json_file(checkpoint)
                except RemoteError:checkpoint.unlink(missing_ok=True)
            cached=checkpoint_for(cached,local)
            if cached:
                seed=folder/'cached.json';seed.write_text(json.dumps(cached))
            else:seed=None
            env=dict(os.environ);env['AIDJ_STATE_DIR']=str(state)
            env.update(OPENBLAS_NUM_THREADS='1',MKL_NUM_THREADS='1',OMP_NUM_THREADS=str(threads),NUMBA_NUM_THREADS=str(threads))
            command=analyse_command(file,output,checkpoint,seed,threads,ffmpeg)
            if re.search('[А-Яа-яЁё]',str(asset.get('artist',''))+' '+str(asset.get('title',''))):command+=['--language','ru']
            run_analysis(command,env,stopped,lost)
            result=json_file(output)
            if result.get('evidence',{}).get('fullComplete') is not True:raise RemoteError('RESULT_INVALID')
            reply=complete_job(transport,identity,result,stopped);uploaded=True;checkpoint.unlink(missing_ok=True)
            emit(status_line(reply.get('stats',{}))+f" · DJ {reply.get('regular',0)}, ремикс {reply.get('remix',0)} фрагм.")
    except (OSError,ValueError,RemoteError) as original:
        exc=original if isinstance(original,RemoteError) else RemoteError('LOCAL_IO_FAILED')
        try:
            if stopped.is_set() or exc.kind in ('INTERRUPTED','LEASE_LOST','SSH_FAILED','REMOTE_FAILED'):
                transport.rpc({'op':'release',**identity})
            else:
                error=exc.kind if exc.kind in ('RESOURCE_LIMIT','TIMEOUT','RESULT_INVALID') else 'DOWNLOAD_FAILED' if exc.kind.startswith('DOWNLOAD') else 'ANALYSIS_FAILED'
                transport.rpc({'op':'fail',**identity,'error':error})
        except RemoteError:pass
        if not stopped.is_set():emit('Не обработана одна песня: '+exc.kind+'. Результат предыдущих песен сохранён.')
        if exc.kind in ('RESOURCE_LIMIT','RESOURCE_MONITOR_FAILED'):
            # Never repeat an OOM/memory-budget failure with a bigger limit.
            stopped.set();raise exc
    finally:
        done.set();keeper.join(timeout=1)
        if not uploaded and stopped.is_set():
            try:transport.rpc({'op':'release',**identity})
            except RemoteError:pass


class ClaimBudget:
    def __init__(self,limit=None):self.remaining=limit;self.lock=threading.Lock()
    def claim(self,transport,run):
        with self.lock:
            if self.remaining==0:return None
            reply=transport.rpc({'op':'claim','runId':run})
            if reply.get('job') is not None and self.remaining is not None:self.remaining-=1
            return reply


def worker(transport,run,state,threads,ffmpeg,stopped,budget=None):
    failures=0;budget=budget or ClaimBudget()
    while not stopped.is_set():
        try:
            reply=budget.claim(transport,run)
            if reply is None:return
            failures=0;job=reply.get('job');stats=reply.get('stats',{})
            if job is None:
                if not stats.get('pending',0) and not stats.get('running',0):return
                stopped.wait(3);continue
            work_job(transport,run,job,state,threads,ffmpeg,stopped)
        except RemoteError as exc:
            if exc.kind in ('RESOURCE_LIMIT','RESOURCE_MONITOR_FAILED'):raise
            failures+=1
            if failures>=3:emit('Нет связи с сервером: '+exc.kind+'. Повторный запуск продолжит подготовку.');stopped.set();return
            stopped.wait(3)


def prepare_model(state):
    # Once before parallel workers: cache locks also protect another invocation.
    from faster_whisper.utils import download_model
    download_model('base',cache_dir=str(state/'dj-models'))


def main():
    parser=argparse.ArgumentParser(description='Полная параллельная подготовка DJ на Mac с возвратом результатов по SSH.')
    parser.add_argument('--server',default='ilya-grid-vm');parser.add_argument('--remote-project',default='/home/ilya-grid/aidj_codex_music')
    parser.add_argument('--workers',type=int);parser.add_argument('--threads',type=int,default=1)
    parser.add_argument('--limit',type=int,help='Пробный запуск: обработать не больше указанного числа песен.')
    parser.add_argument('--playlist',help='По умолчанию вся библиотека; можно указать имя одного плейлиста.')
    parser.add_argument('--state',type=Path,default=Path.home()/'.local/share/aidj-codex-music/dj-mac')
    args=parser.parse_args()
    if args.workers is not None and not 1<=args.workers<=8:parser.error('--workers: от 1 до 8')
    if not 1<=args.threads<=4:parser.error('--threads: от 1 до 4')
    if args.limit is not None and not 1<=args.limit<=10000:parser.error('--limit: от 1 до 10000')
    state=args.state.expanduser().resolve();state.mkdir(parents=True,exist_ok=True,mode=0o700)
    for name in ('work','checkpoints'):(state/name).mkdir(exist_ok=True,mode=0o700)
    if shutil.disk_usage(state).free<768*1024**2:raise RemoteError('LOW_DISK')
    count=worker_count(args.workers);count=min(count,max(1,((os.cpu_count() or 2)-1)//args.threads))
    stopped=threading.Event()
    def stop(signum,frame):
        if not stopped.is_set():emit('Останавливаю текущие задания. Сохранённые фрагменты останутся на сервере.')
        stopped.set()
    signal.signal(signal.SIGINT,stop);signal.signal(signal.SIGTERM,stop)
    with tempfile.TemporaryDirectory(prefix='aidj-ssh-',dir='/tmp') as sockets:
        transport=Transport(args.server,args.remote_project,Path(sockets))
        try:
            started=transport.rpc({'op':'start','playlist':args.playlist});run=started['runId']
            emit(status_line(started.get('stats',{})))
            stats=started.get('stats',{})
            if not stats.get('pending',0) and not stats.get('running',0):return
            emit(f'Подготовка модели и запуск: {count} процессов, по {args.threads} CPU-потоку. Ctrl+C — остановить; повторный запуск продолжит.')
            prepare_model(state)
            import imageio_ffmpeg
            ffmpeg=imageio_ffmpeg.get_ffmpeg_exe()
            budget=ClaimBudget(args.limit)
            with ThreadPoolExecutor(max_workers=count) as pool:
                futures=[pool.submit(worker,transport,run,state,args.threads,ffmpeg,stopped,budget) for _ in range(count)]
                for future in futures:future.result()
            if not stopped.is_set():
                final=transport.rpc({'op':'status','runId':run}).get('stats',{})
                emit(f"Из обработанных {final.get('done',0)} песен: DJ — {final.get('regularReady',0)}, ремикс — {final.get('remixReady',0)}. Без DJ — {final.get('withoutRegular',0)}, без ремикса — {final.get('withoutRemix',0)}.")
                emit('Пробный запуск завершён. Убери --limit для всей библиотеки.' if args.limit is not None else 'Обработка доступных заданий завершена. Фрагменты появятся в приложении после обновления библиотеки.')
        finally:transport.close()


if __name__=='__main__':
    try:main()
    except RemoteError as exc:emit('Подготовка остановлена: '+exc.kind+'. Проверь SSH-доступ и повтори команду.');raise SystemExit(1)
    except Exception:emit('Подготовка остановлена: проверь Python, свободное место и доступ к серверу.');raise SystemExit(1)
