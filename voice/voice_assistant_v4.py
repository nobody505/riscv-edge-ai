#!/usr/bin/env python3
"""K1 语音助手 v4 — 唤醒词"小空小空" + TTS 全链路 + GPS 位置驱动导航"""
import sys, os, re, time, subprocess, gc, threading, serial, ctypes, queue, itertools, signal, json

# ═══ 抑制 ALSA lib 噪音（满屏 Unknown PCM 警告只是探测，不影响功能）═══
_asound = ctypes.cdll.LoadLibrary('libasound.so.2')
_CERR = ctypes.CFUNCTYPE(None, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p)
_asound.snd_lib_error_set_handler(_CERR(lambda *a: None))

# ============ ONNX 单线程 patch ============
import onnxruntime as _ort
_original_init = _ort.InferenceSession.__init__
def _patched_init(self, *args, **kwargs):
    if 'sess_options' not in kwargs or kwargs['sess_options'] is None:
        kwargs['sess_options'] = _ort.SessionOptions()
    kwargs['sess_options'].intra_op_num_threads = 1
    kwargs['sess_options'].inter_op_num_threads = 1
    _original_init(self, *args, **kwargs)
_ort.InferenceSession.__init__ = _patched_init

# ============ 路径 & 常量 ============
DEMO_DIR = "/home/space/_tts_demo/examples/NLP"
sys.path.insert(0, DEMO_DIR)
sys.path.insert(0, "/home/space")
from spacemit_audio import RecAudioVad

AUDIO_CARD = 0                 # USB 音箱
AUDIO_FRAME_STALL_TIMEOUT = 2.0
AUDIO_STREAM_REOPEN_ATTEMPTS = 3
AUDIO_STREAM_REOPEN_DELAY = 0.2
AUDIO_WATCHDOG_BUILD = "20260719-audio-stream-reopen-r3"


class AudioCaptureStalled(RuntimeError):
    pass


class _AudioReadWatchdogStream:
    """Avoid entering PortAudio's unbounded blocking read when USB frames stop."""

    def __init__(self, stream, stall_timeout=AUDIO_FRAME_STALL_TIMEOUT):
        self._stream = stream
        self._stall_timeout = float(stall_timeout)

    def read(self, num_frames, *args, **kwargs):
        deadline = time.monotonic() + self._stall_timeout
        while True:
            try:
                available = self._stream.get_read_available()
            except Exception as exc:
                raise AudioCaptureStalled(
                    "USB capture availability check failed: %s" % exc) from exc
            if available >= num_frames:
                return self._stream.read(num_frames, *args, **kwargs)
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise AudioCaptureStalled(
                    "USB capture delivered no frames for %.1fs" %
                    self._stall_timeout)
            time.sleep(min(0.02, remaining))

    def start_stream(self):
        try:
            return self._stream.start_stream()
        except Exception as exc:
            raise AudioCaptureStalled(
                "USB capture stream could not start: %s" % exc) from exc

    def __getattr__(self, name):
        return getattr(self._stream, name)


def _reopen_audio_capture_stream(rec, device_index):
    """Reopen only PortAudio's input stream, preserving RecAudioVad state."""
    old_stream = rec.stream
    try:
        old_stream.stop_stream()
    except Exception:
        pass
    try:
        old_stream.close()
    except Exception:
        pass

    last_error = None
    for attempt in range(1, AUDIO_STREAM_REOPEN_ATTEMPTS + 1):
        try:
            raw_stream = rec.pa.open(
                format=rec.FORMAT,
                channels=rec.CHANNELS,
                rate=rec.RATE,
                input=True,
                frames_per_buffer=rec.FRAME_SIZE,
                input_device_index=device_index,
            )
            rec.stream = _AudioReadWatchdogStream(raw_stream)
            with rec._vad_lock:
                rec.prob_avg = 0.0
                rec.hist.clear()
                rec.vad.reset()
            while True:
                try:
                    rec._frame_q.get_nowait()
                except queue.Empty:
                    break
            rec.frame_is_append = False
            print("[AUDIO] capture stream reopened on attempt %d" % attempt,
                  flush=True)
            return
        except Exception as exc:
            last_error = exc
            if attempt < AUDIO_STREAM_REOPEN_ATTEMPTS:
                time.sleep(AUDIO_STREAM_REOPEN_DELAY)

    raise AudioCaptureStalled(
        "USB capture stream reopen failed after %d attempts: %s" %
        (AUDIO_STREAM_REOPEN_ATTEMPTS, last_error))


def _record_audio(rec):
    """Preserve a capture-stall signal if RecAudioVad cleanup masks it."""
    try:
        return rec.record_audio()
    except AudioCaptureStalled:
        raise
    except OSError as exc:
        message = str(exc).lower()
        if (getattr(exc, 'errno', None) == -9988 or
                'stream closed' in message or
                'stream not open' in message):
            raise AudioCaptureStalled(
                "USB capture stream became unusable: %s" % exc) from exc
        raise
import glob as _glob
TTS_STABLE_DEVICE = '/dev/serial/by-id/usb-1a86_USB_Serial-if00-port0'

def _find_ch340():
    if os.path.exists(TTS_STABLE_DEVICE):
        return TTS_STABLE_DEVICE
    for dev in sorted(_glob.glob('/dev/ttyUSB*')):
        try:
            info = subprocess.check_output(
                ['/usr/bin/udevadm', 'info', '--query=property', '--name', dev],
                timeout=3,
            ).decode(errors='replace')
            properties = dict(
                line.split('=', 1) for line in info.splitlines() if '=' in line
            )
            if (properties.get('ID_VENDOR_ID', '').lower() == '1a86' and
                    properties.get('ID_MODEL_ID', '').lower() == '7523'):
                return dev
        except (OSError, subprocess.SubprocessError, ValueError):
            pass
    return None

TTY_TTS = _find_ch340() or TTS_STABLE_DEVICE  # TW-TTS auto-detect serial port

# ASRPRO V2.0通过K1 UART0的RX脚（40pin Pin 10）发送一行"WAKE\r\n"。
# 空闲态只等待这个硬件事件，不再让K1的SenseVoice模型参与唤醒推理。
ASRPRO_WAKE_TTY = '/dev/ttyS0'
ASRPRO_WAKE_BAUD = 115200
ASRPRO_WAKE_TOKEN = 'WAKE'
ASRPRO_UART_RETRY_DELAY = 1.0
ASRPRO_COMMAND_QUEUE_SIZE = 16
ASRPRO_COMMAND_GRACE = 0.20
ASRPRO_COMMAND_TEXT = {
    'TRAVEL_ON': '打开出行模式',
    'TRAVEL_OFF': '关闭出行模式',
    'LIGHT_ON': '打开灯带',
    'LIGHT_OFF': '关闭灯带',
    'FALL_ACK': '关闭警报',
}
ASRPRO_WAKE_BUILD = '20260724-asrpro-fixed-command-events-r2'

# ============ 唤醒词 "小空小空" ============
WAKE_PINYIN = [
    ["xiao", "xi", "xia", "xie", "xian", "xiang", "xiao", "xiu", "xue", "xiong"],
    ["kong", "keng", "kang", "kun", "kuo", "gong", "hong", "heng", "hang", "hun", "huo", "geng"],
    ["xiao", "xi", "xia", "xie", "xian", "xiang", "xiao", "xiu", "xue", "xiong"],
    ["kong", "keng", "kang", "kun", "kuo", "gong", "hong", "heng", "hang", "hun", "huo", "geng"],
]

WAKE_VARIANTS = [
    "小空小空", "小空时空", "小空是空",
    "晓空小空", "效空小空", "校空小空", "笑空小空",
    "小孔小空", "小控小空", "小空小孔", "小空小控",
    "小空时空", "晓空时空", "校空时空", "小空空",
    "小空晓空", "晓空晓空", "萧空小空", "肖空小空",
    "小空消空", "笑空时空", "小空小空小空",
    "孔小空", "控小空",
    "小红小红", "小工小工", "小公小公", "小轰小轰",
    "小空小红", "小红小空", "小工小红",
]

CMD_TIMEOUT = 40        # 唤醒后 40 秒内无有效指令 → 休眠
NAV_WAIT_TIMEOUT = 40  # 打开导航后 40 秒内无目的地 → 休眠

# "导航" 的同音/近音字，命令中必须包含其中之一才能触发导航
NAV_KEYWORDS = [
    "导航", "导行", "到航", "道行",
    "导杭", "到杭", "导肮",
]
NAV_OPEN_PHRASES = [
    '打开导航', '开启导航', '开始导航', '我要导航',
    '开导航', '开导行', '开到航',
]
NAV_CLOSE_PHRASES = ['关闭导航', '停止导航', '结束导航', '关掉导航']
TRAVEL_CLOSE_PHRASES = [
    '关闭行路模式', '关闭出行模式', '关闭行路', '关掉行路',
    '停止行路模式', '关闭所有', '全关', '关掉所有', '停止所有',
]
NAV_DEST_TRAILING_MARKERS = [
    '你听', '你能', '你明白', '你知道', '听懂了吗', '听懂吗',
    '怎么可能', '可以吗', '行不行', '好不好', '然后', '接着',
    '我说', '我们再', '再帮我', '先帮我',
]

# 摔倒警报由独立 fall_detect_adxl345.py 进程拥有。语音助手只通过带警报ID的
# 受控运行目录内的握手文件只提交请求，不直接控制灯带或ML307A串口。
RUNTIME_DIR = '/run/elder-assistant'
FALL_ALERT_ACTIVE_FILE = os.path.join(RUNTIME_DIR, 'fall_alert_active')
FALL_ALERT_ACK_FILE = os.path.join(RUNTIME_DIR, 'fall_alert_ack_request')
FALL_INITIAL_SMS_DONE_FILE = os.path.join(RUNTIME_DIR, 'fall_initial_sms_done')
FALL_ALERT_RESOLVING_FILE = os.path.join(RUNTIME_DIR, 'fall_alert_resolving')
USER_SMS_REQUEST_FILE = os.path.join(RUNTIME_DIR, 'elder_user_sms_request.json')
USER_SMS_PROCESSING_FILE = os.path.join(RUNTIME_DIR, 'elder_user_sms_processing.json')
USER_SMS_RESULT_FILE = os.path.join(RUNTIME_DIR, 'elder_user_sms_result.json')
USER_SMS_PHONE = os.environ.get('ELDER_SMS_PHONE', '').strip()
USER_SMS_CONTACT_NAME = os.environ.get('ELDER_SMS_CONTACT_NAME', '紧急联系人').strip()
INCOMING_MESSAGE_REQUEST_FILE = os.path.join(RUNTIME_DIR, 'elder_incoming_message_request.json')
INCOMING_MESSAGE_PROCESSING_FILE = os.path.join(RUNTIME_DIR, 'elder_incoming_message_processing.json')
INCOMING_MESSAGE_RESULT_FILE = os.path.join(RUNTIME_DIR, 'elder_incoming_message_result.json')
INCOMING_MESSAGE_MAX_CHARS = 50
INCOMING_MESSAGE_MAX_AGE = 600
INCOMING_MESSAGE_CONTROL_TAG_RE = re.compile(
    r'\[(?:v|s|m|r|t)\d{1,3}\]', re.IGNORECASE)
SMS_DIALOG_TIMEOUT = 60
SMS_SEND_TIMEOUT = 90
FALL_ALARM_CONFLICT_WORDS = [
    '导航', '导行', '到航', '道行', '导杭', '到杭', '导肮',
    '行路', '出行', '行动模式', '检测', '监控', '所有', '全关',
    '车辆', '左侧', '右侧', '灯带', '警示灯', '灯光',
]
FALL_ALARM_EXACT_VARIANTS = [
    '关闭警报', '关闭报警', '关闭摔倒警报', '关闭摔倒报警',
    '关掉警报', '关掉报警', '关掉摔倒警报', '关掉摔倒报警',
    '停止警报', '停止报警', '停止摔倒警报', '停止摔倒报警',
    '解除警报', '解除报警', '解除摔倒警报', '解除摔倒报警',
    '取消警报', '取消报警', '取消摔倒警报', '取消摔倒报警',
    '关警报', '关报警',
]
FALL_ALARM_PINYIN_TARGETS = [
    ('guan', 'bi', 'jing', 'bao'),
    ('guan', 'bi', 'bao', 'jing'),
    ('guan', 'diao', 'jing', 'bao'),
    ('guan', 'diao', 'bao', 'jing'),
    ('ting', 'zhi', 'jing', 'bao'),
    ('ting', 'zhi', 'bao', 'jing'),
    ('jie', 'chu', 'jing', 'bao'),
    ('jie', 'chu', 'bao', 'jing'),
    ('qu', 'xiao', 'jing', 'bao'),
    ('qu', 'xiao', 'bao', 'jing'),
    ('guan', 'jing', 'bao'),
    ('guan', 'bao', 'jing'),
    ('guan', 'bi', 'shuai', 'dao', 'jing', 'bao'),
    ('guan', 'bi', 'shuai', 'dao', 'bao', 'jing'),
]

# 绿色标记独立控制绿色常亮；行路标记只控制2米内蓝灯距离提醒。
# 黄色聆听反馈由radar_led统一执行；蓝/绿/摔倒红色会短暂让权并自动恢复。
RADAR_LED_BIN = '/usr/local/libexec/elder-assistant/radar_led'
HWCTL_BIN = '/usr/local/sbin/elder-hwctl'
SUDO_BIN = '/usr/bin/sudo'
RADAR_GREEN_ENABLED_FILE = os.path.join(RUNTIME_DIR, 'radar_green_enabled')
RADAR_TRAVEL_ENABLED_FILE = os.path.join(RUNTIME_DIR, 'radar_travel_enabled')
RADAR_LISTENING_YELLOW_REQUEST_FILE = os.path.join(RUNTIME_DIR, 'radar_listening_yellow_request')
# Names must match the root-owned POSIX SHM objects created by hook_simple.so.
CAMERA_SHM_PIPE0 = '/dev/shm/pipe0_frame'  # nosec B108
CAMERA_SHM_PIPE1 = '/dev/shm/pipe1_frame'  # nosec B108
CAMERA_SHM_FILES = (CAMERA_SHM_PIPE0, CAMERA_SHM_PIPE1)
RADAR_MANAGED_EXTERNALLY = os.environ.get('RADAR_MANAGED_EXTERNALLY', '').strip() == '1'
RADAR_LIGHT_CONFLICT_WORDS = [
    '导航', '导行', '到航', '道行', '导杭', '到杭', '导肮',
    '行路', '出行', '行动模式', '检测', '监控', '所有', '全关',
    '车辆', '左侧', '右侧', '警报', '报警', '摔倒',
]
RADAR_LIGHT_OPEN_VARIANTS = [
    '打开灯带', '开启灯带', '启动灯带',
    '打开警示灯', '开启警示灯', '启动警示灯',
    '打开灯光', '开启灯光',
]
RADAR_LIGHT_CLOSE_VARIANTS = [
    '关闭灯带', '关掉灯带', '停止灯带',
    '关闭警示灯', '关掉警示灯', '停止警示灯',
    '关闭灯光', '关掉灯光',
]
RADAR_LIGHT_OPEN_PINYIN = [
    ('da', 'kai', 'deng', 'dai'),
    ('kai', 'qi', 'deng', 'dai'),
    ('qi', 'dong', 'deng', 'dai'),
    ('da', 'kai', 'jing', 'shi', 'deng'),
    ('kai', 'qi', 'jing', 'shi', 'deng'),
]
RADAR_LIGHT_CLOSE_PINYIN = [
    ('guan', 'bi', 'deng', 'dai'),
    ('guan', 'diao', 'deng', 'dai'),
    ('ting', 'zhi', 'deng', 'dai'),
    ('guan', 'bi', 'jing', 'shi', 'deng'),
    ('guan', 'diao', 'jing', 'shi', 'deng'),
]

# ============ 全局状态 ============
IDLE, WOKEN, NAV_WAITING, SMS_CONTENT_WAITING, SMS_CONFIRM_WAITING = range(5)
SMS_STATES = (SMS_CONTENT_WAITING, SMS_CONFIRM_WAITING)
BUILD_ID = "20260724-asrpro-fixed-command-events-r2"
FRAME_PROGRESS_TIMEOUT = 3.0
CAMERA_TRANSITION_ASR_GRACE = 1.0

jdk_process = None
camera_process = None
_yolo_generation = 0
_yolo_stop_requested = True
_yolo_state_lock = threading.Lock()
_yolo_starting = threading.Event()
_yolo_ready = threading.Event()
_yolo_manager_thread = None
_asr_in_progress = threading.Event()
_asrpro_wake_event = threading.Event()
_asrpro_stop_event = threading.Event()
_asrpro_command_session = threading.Event()
_asrpro_command_queue = queue.Queue(maxsize=ASRPRO_COMMAND_QUEUE_SIZE)
nav_active = False
radar_process = None
radar_started_by_assistant = False

# ============ TW-TTS 语音合成 ============
_tts_lock = threading.Lock()
_tts_playing = threading.Event()
_tts_state_lock = threading.Lock()
_tts_generation = 0
_tts_guard_until = 0.0
TTS_TAIL_GUARD = 0.5
TTS_VOLUME_TAG = "[v1]"
TTS_SERIAL_RETRY_DELAYS = (0.0, 0.15, 0.35, 0.75, 1.5)

TTS_PRIORITY_SAFETY = 0
TTS_PRIORITY_ALERT = 2
TTS_PRIORITY_WAKE = 5
TTS_PRIORITY_ALERT_AFTER_WAKE = 6
TTS_PRIORITY_NORMAL = 10
TTS_PRIORITY_INCOMING_MESSAGE = 12
_tts_queue = queue.PriorityQueue()
_tts_sequence = itertools.count()
_tts_enqueue_lock = threading.Lock()
_wake_ack_pending = threading.Event()
_tts_worker_lock = threading.Lock()
_tts_worker_started = False
_active_tts_lock = threading.Lock()
_active_tts_request = None

def _tts_state_snapshot():
    """返回 (播报代次, 当前是否应抑制麦克风/ASR)。"""
    with _tts_state_lock:
        busy = _tts_playing.is_set() or time.monotonic() < _tts_guard_until
        return _tts_generation, busy

def _tts_begin():
    global _tts_generation
    with _tts_state_lock:
        _tts_generation += 1
        _tts_playing.set()

def _tts_end():
    global _tts_guard_until
    with _tts_state_lock:
        _tts_playing.clear()
        _tts_guard_until = time.monotonic() + TTS_TAIL_GUARD

def _open_tts_serial():
    """Resolve the CH340 on every request so USB re-enumeration can recover."""
    global TTY_TTS
    last_error = None
    attempts = len(TTS_SERIAL_RETRY_DELAYS)
    for attempt, delay in enumerate(TTS_SERIAL_RETRY_DELAYS, 1):
        if delay:
            time.sleep(delay)
        port = _find_ch340()
        if port is None:
            last_error = FileNotFoundError('CH340 TTS serial device is unavailable')
        else:
            try:
                ser = serial.Serial(port, 9600, timeout=0.2)
                if port != TTY_TTS:
                    print('[TTS] serial rebound %s -> %s' % (TTY_TTS, port), flush=True)
                TTY_TTS = port
                return ser
            except Exception as exc:
                last_error = exc
        if attempt < attempts:
            print('[TTS] serial open retry %d/%d: %s' %
                  (attempt, attempts, str(last_error)[:100]), flush=True)
    raise last_error

def _play_tts_request(request):
    """播放一个调度请求；导航被车辆告警抢占时返回 True。"""
    txt = TTS_VOLUME_TAG + request['text'].strip()
    role = request['role']
    cancel = request['cancel']
    preempted = False
    with _tts_lock:
        if role == 'navigation' and cancel.is_set():
            return True
        _tts_begin()
        ser = None
        started = time.monotonic()
        acked = False
        finished = False
        try:
            ser = _open_tts_serial()
            time.sleep(0.05)
            gb = txt.encode('gb2312')
            total = len(gb) + 2
            frame = bytes([0xFD, (total >> 8) & 0xFF, total & 0xFF, 0x01, 0x00]) + gb
            ser.write(frame)
            # 阶段1: 等 ACK ('A'=0x41) 确认模块收到帧
            deadline = time.time() + max(2.0, len(txt) * 0.4)
            while time.time() < deadline:
                if role == 'navigation' and cancel.is_set():
                    preempted = True
                    break
                b = ser.read(1)
                if b == b'A':
                    acked = True
                    break
                elif b == b'E':
                    print('[TTS] Error from module', flush=True)
                    return
            # 阶段2: 轮询 'O'（空闲）确认模块播完，最长等 30 秒
            if acked and not preempted:
                deadline = time.time() + max(2.0, len(txt) * 0.5)
                while time.time() < deadline:
                    if role == 'navigation' and cancel.is_set():
                        preempted = True
                        break
                    b = ser.read(1)
                    if b == b'O':
                        finished = True
                        break
        except Exception as e:
            print('[TTS] Error: %s' % str(e)[:80], flush=True)
        finally:
            if ser:
                try: ser.close()
                except: pass
            _tts_end()
            elapsed = time.monotonic() - started
            if preempted:
                print('[TTSQ] navigation interrupted after %.2fs' % elapsed, flush=True)
            elif not acked:
                print('[TTS] WARN no ACK after %.2fs' % elapsed, flush=True)
            elif not finished:
                print('[TTS] WARN no idle response after %.2fs' % elapsed, flush=True)
            request['delivered'] = finished
    return preempted

def _tts_priority_for_role(role):
    if role == 'safety':
        return TTS_PRIORITY_SAFETY
    if role == 'wake':
        return TTS_PRIORITY_WAKE
    if role == 'alert':
        # 已排队的唤醒确认必须先完整播完；后来车辆告警不能持续插队。
        if _wake_ack_pending.is_set():
            return TTS_PRIORITY_ALERT_AFTER_WAKE
        return TTS_PRIORITY_ALERT
    if role == 'incoming_message':
        return TTS_PRIORITY_INCOMING_MESSAGE
    return TTS_PRIORITY_NORMAL

def _tts_worker():
    global _active_tts_request
    while True:
        _, _, request = _tts_queue.get()
        try:
            with _active_tts_lock:
                _active_tts_request = request
            if request['role'] == 'wake':
                print('[TTSQ] mandatory wake acknowledgement started', flush=True)
            preempted = _play_tts_request(request)
            if preempted and request['role'] == 'navigation':
                request['cancel'].clear()
                request['resume_count'] += 1
                print('[TTSQ] navigation requeued after alert (resume=%d)' %
                      request['resume_count'], flush=True)
                _tts_queue.put((TTS_PRIORITY_NORMAL, next(_tts_sequence), request))
            else:
                if request['role'] == 'wake':
                    if request.get('delivered'):
                        print('[TTSQ] mandatory wake acknowledgement completed', flush=True)
                    else:
                        print('[TTSQ] ERROR wake acknowledgement was not confirmed by TTS', flush=True)
                request['done'].set()
        except Exception as e:
            print('[TTSQ] worker error: %s' % str(e)[:100], flush=True)
            request['done'].set()
        finally:
            if request['role'] == 'wake':
                with _tts_enqueue_lock:
                    _wake_ack_pending.clear()
            with _active_tts_lock:
                if _active_tts_request is request:
                    _active_tts_request = None
            _tts_queue.task_done()

def _ensure_tts_worker():
    global _tts_worker_started
    with _tts_worker_lock:
        if _tts_worker_started:
            return
        threading.Thread(target=_tts_worker, name='priority-tts', daemon=True).start()
        _tts_worker_started = True

def _request_navigation_preempt():
    with _active_tts_lock:
        request = _active_tts_request
        if request is not None and request['role'] == 'navigation':
            request['cancel'].set()
            return True
    return False

def tts_say(text, role='normal'):
    """同步提交 TTS。

    摔倒警报确认保持最高优先级；车辆告警可抢占导航；唤醒确认不抢占
    正在播放的内容，但一旦排队，后续车辆告警必须让它先完整播完。
    """
    if not text or not text.strip():
        return
    _ensure_tts_worker()
    request = {
        'text': text.strip(),
        'role': role,
        'done': threading.Event(),
        'cancel': threading.Event(),
        'resume_count': 0,
        'delivered': False,
    }
    with _tts_enqueue_lock:
        if role == 'wake':
            _wake_ack_pending.set()
        priority = _tts_priority_for_role(role)
        _tts_queue.put((priority, next(_tts_sequence), request))
    if role == 'wake':
        print('[TTSQ] mandatory wake acknowledgement queued (priority=%d)' % priority, flush=True)
    if role in ('alert', 'safety') and _request_navigation_preempt():
        label = 'vehicle alert' if role == 'alert' else 'fall alarm acknowledgement'
        print('[TTSQ] %s requested navigation preemption' % label, flush=True)
    request['done'].wait()
    return bool(request.get('delivered'))

# ============ 车辆告警调度 ============
# 检测日志线程绝不直接等待 TTS。这里只保留一条最新待播告警，旧告警不补播。
ALERT_MAX_QUEUE_AGE = 1.5
_alert_queue = queue.Queue(maxsize=1)
_alert_queue_lock = threading.Lock()
_alert_worker_lock = threading.Lock()
_alert_worker_started = False

def _clear_pending_alerts():
    with _alert_queue_lock:
        while True:
            try:
                _alert_queue.get_nowait()
                _alert_queue.task_done()
            except queue.Empty:
                break

def _alert_worker():
    while True:
        side, text, created_at = _alert_queue.get()
        try:
            age = time.monotonic() - created_at
            if age > ALERT_MAX_QUEUE_AGE:
                print('[ALERTQ] drop stale %s age=%.2fs' % (side, age), flush=True)
                continue
            print('[ALERTQ] speak %s delay=%.3fs' % (side, age), flush=True)
            tts_say(text, role='alert')
        finally:
            _alert_queue.task_done()

def _ensure_alert_worker():
    global _alert_worker_started
    with _alert_worker_lock:
        if _alert_worker_started:
            return
        threading.Thread(target=_alert_worker, name='vehicle-alert-tts', daemon=True).start()
        _alert_worker_started = True

def _enqueue_vehicle_alert(side):
    item = (side, '左侧有车辆靠近' if side == 'left' else '右侧有车辆靠近', time.monotonic())
    with _alert_queue_lock:
        try:
            _alert_queue.put_nowait(item)
        except queue.Full:
            try:
                replaced = _alert_queue.get_nowait()
                _alert_queue.task_done()
                print('[ALERTQ] replace pending %s -> %s' % (replaced[0], side), flush=True)
            except queue.Empty:
                pass
            try:
                _alert_queue.put_nowait(item)
            except queue.Full:
                print('[ALERTQ] drop race %s' % side, flush=True)
                return False
    return True

# ============ ASR 工具 ============

def _asrpro_wake_listener():
    """持续监听ASRPRO；串口异常时自动关闭并重试。"""
    while not _asrpro_stop_event.is_set():
        ser = None
        try:
            ser = serial.Serial(
                ASRPRO_WAKE_TTY,
                ASRPRO_WAKE_BAUD,
                timeout=0.5,
                bytesize=serial.EIGHTBITS,
                parity=serial.PARITY_NONE,
                stopbits=serial.STOPBITS_ONE,
            )
            ser.reset_input_buffer()
            print('[ASRPRO] UART ready device=%s baud=%d build=%s' %
                  (ASRPRO_WAKE_TTY, ASRPRO_WAKE_BAUD, ASRPRO_WAKE_BUILD),
                  flush=True)
            while not _asrpro_stop_event.is_set():
                raw_line = ser.readline()
                if not raw_line:
                    continue
                line = raw_line.decode('ascii', 'replace').strip().upper()
                if not line:
                    continue
                print('[ASRPRO] RX %s' % line[:80], flush=True)
                if line == ASRPRO_WAKE_TOKEN:
                    _asrpro_wake_event.set()
                elif line in ASRPRO_COMMAND_TEXT:
                    if not _asrpro_command_session.is_set():
                        print('[ASRPRO] Ignore fixed command outside wake session: %s' %
                              line, flush=True)
                        continue
                    _, tts_busy = _tts_state_snapshot()
                    if tts_busy:
                        print('[ASRPRO] Ignore fixed command overlapping TTS: %s' %
                              line, flush=True)
                        continue
                    try:
                        _asrpro_command_queue.put_nowait(line)
                    except queue.Full:
                        try:
                            dropped = _asrpro_command_queue.get_nowait()
                        except queue.Empty:
                            dropped = 'unknown'
                        print('[ASRPRO] Command queue full; replace %s -> %s' %
                              (dropped, line), flush=True)
                        try:
                            _asrpro_command_queue.put_nowait(line)
                        except queue.Full:
                            print('[ASRPRO] Command queue race; drop %s' % line,
                                  flush=True)
        except Exception as exc:
            print('[ASRPRO] UART unavailable: %s; retrying in %.1fs' %
                  (str(exc)[:160], ASRPRO_UART_RETRY_DELAY), flush=True)
            _asrpro_stop_event.wait(ASRPRO_UART_RETRY_DELAY)
        finally:
            if ser is not None:
                try:
                    ser.close()
                except Exception:
                    pass


def _start_asrpro_wake_listener():
    _asrpro_stop_event.clear()
    thread = threading.Thread(
        target=_asrpro_wake_listener,
        name='asrpro-uart-wake',
        daemon=True,
    )
    thread.start()
    return thread


def _clear_asrpro_commands(reason):
    count = 0
    while True:
        try:
            _asrpro_command_queue.get_nowait()
            count += 1
        except queue.Empty:
            break
    if count:
        print('[ASRPRO] Cleared %d stale fixed command(s): %s' %
              (count, reason), flush=True)
    return count


def _take_asrpro_command(timeout=0.0):
    try:
        if timeout > 0:
            return _asrpro_command_queue.get(timeout=timeout)
        return _asrpro_command_queue.get_nowait()
    except queue.Empty:
        return None


def _pause_audio_capture_while_idle(rec):
    """空闲态停止USB录音读取并清除旧VAD状态，模型本身保持常驻。"""
    try:
        if rec.stream.is_active():
            while rec.stream.get_read_available() >= rec.FRAME_SIZE:
                rec.stream.read(rec.FRAME_SIZE, exception_on_overflow=False)
            rec.stream.stop_stream()
    except Exception as exc:
        print('[AUDIO] idle capture pause warning: %s' % str(exc)[:120],
              flush=True)
    try:
        rec._stop_vad_thread.set()
        with rec._vad_lock:
            rec.prob_avg = 0.0
            rec.hist.clear()
            rec.vad.reset()
        while True:
            try:
                rec._frame_q.get_nowait()
            except queue.Empty:
                break
        rec.frame_is_append = False
    except Exception as exc:
        print('[AUDIO] idle VAD reset warning: %s' % str(exc)[:120],
              flush=True)

def cleanup_asr():
    gc.collect()

def load_asr():
    from spacemit_asr import ASRModel
    return ASRModel()

def clean_asr(text):
    if text is None: return ""
    return re.sub(r"<\|[^|]*\|>", "", text).strip()

def is_wake_word(text):
    """在ASR文本中查找唤醒词——扫描所有4字滑动窗口+拼音匹配"""
    # 1. 文本变体匹配：在原始文本(raw)和清洗后文本中都做
    raw_cleaned = re.sub(r"<\|[^|]*\|>", "", text).strip()
    for v in WAKE_VARIANTS:
        if v in raw_cleaned:
            return True
        if v in text:  # raw text too
            return True

    # 2. 拼音模糊匹配：扫描所有4字窗口
    try:
        from pypinyin import pinyin, Style
        chars = re.findall(r"[一-鿿]", raw_cleaned)
        if len(chars) >= 4:
            # 扫描每一个4字窗口
            for start in range(len(chars) - 3):
                window_chars = chars[start:start+4]
                window_py = pinyin("".join(window_chars), style=Style.TONE3)
                matches = 0
                for i, (syl, tgts) in enumerate(zip(window_py, WAKE_PINYIN)):
                    s = syl[0].rstrip('0123456789')
                    for t in tgts:
                        if s == t or s.startswith(t[:2]):
                            matches += 1
                            break
                if matches >= 3:
                    return True
    except: pass
    return False

def _normalize_command_text(text):
    return re.sub(r'[^0-9A-Za-z一-鿿]', '', text or '').lower()

def _pinyin_syllable_close(actual, expected):
    if actual == expected:
        return True
    # ASR常见声近字通常拼音完全相同；只额外容忍 jin/jing、guan/guang 这类尾音差异。
    return len(actual) >= 2 and len(expected) >= 2 and actual[:2] == expected[:2]

def _matches_pinyin_target(syllables, target):
    if len(syllables) < len(target):
        return False
    for start in range(len(syllables) - len(target) + 1):
        window = syllables[start:start + len(target)]
        mismatches = sum(
            0 if _pinyin_syllable_close(actual, expected) else 1
            for actual, expected in zip(window, target)
        )
        # 动作词必须可靠；其余部分最多容忍一个ASR音节偏差。
        action_len = 1 if len(target) == 3 else 2
        action_ok = all(
            _pinyin_syllable_close(window[i], target[i])
            for i in range(action_len)
        )
        if action_ok and mismatches <= 1:
            return True
    return False

def is_close_fall_alarm_command(text):
    """识别“关闭警报”及声近字，同时显式排除导航/行路/车辆域命令。"""
    normalized = _normalize_command_text(text)
    if not normalized or any(w in normalized for w in ['不要', '别', '不用', '无需']):
        return False
    if any(w in normalized for w in FALL_ALARM_CONFLICT_WORDS):
        return False
    if any(v in normalized for v in FALL_ALARM_EXACT_VARIANTS):
        return True
    try:
        from pypinyin import pinyin, Style
        chars = ''.join(re.findall(r'[一-鿿]', normalized))
        syllables = [item[0].lower() for item in pinyin(chars, style=Style.NORMAL)]
        return any(_matches_pinyin_target(syllables, target)
                   for target in FALL_ALARM_PINYIN_TARGETS)
    except Exception:
        return False

def _read_active_fall_alert_id():
    try:
        values = {}
        with open(FALL_ALERT_ACTIVE_FILE, 'r', encoding='ascii', errors='replace') as f:
            for line in f.read(512).splitlines():
                if '=' in line:
                    key, value = line.split('=', 1)
                    values[key.strip()] = value.strip()
        alert_id = values.get('alert_id', '')
        pid = int(values.get('pid', '0'))
        if not alert_id or pid <= 0 or not os.path.isdir('/proc/%d' % pid):
            return None
        return alert_id
    except (OSError, ValueError):
        return None

def request_close_fall_alarm():
    """向当前警报写入带ID的原子确认请求；不触碰导航、YOLO或短信串口。"""
    alert_id = _read_active_fall_alert_id()
    if not alert_id:
        return 'inactive'
    tmp = '%s.%d.tmp' % (FALL_ALERT_ACK_FILE, os.getpid())
    try:
        with open(tmp, 'w', encoding='ascii') as f:
            f.write(alert_id + '\n')
            f.flush()
            os.fsync(f.fileno())
        # Group access is required for the private cross-service IPC.
        os.chmod(tmp, 0o660)  # nosec B103
        os.replace(tmp, FALL_ALERT_ACK_FILE)
        print('[FALL-ALARM] acknowledgement requested id=%s' % alert_id, flush=True)
        return 'requested'
    except OSError as e:
        print('[FALL-ALARM] request failed: %s' % str(e)[:100], flush=True)
        try: os.unlink(tmp)
        except OSError: pass
        return 'error'

# ============ 老人语音普通短信 ============
def is_sms_start_command(text):
    normalized = _normalize_command_text(text)
    if '短信' not in normalized:
        return False
    return any(word in normalized for word in
               ['发送', '发短信', '发一条', '我要发', '帮我发', '给'])

def sanitize_sms_content(text):
    content = (text or '').strip()
    content = re.sub(r'^(?:短信)?内容(?:是|为)?[，,:：]?', '', content).strip()
    return content

def is_sms_cancel_command(text):
    normalized = _normalize_command_text(text)
    return normalized in {
        '取消', '取消发送', '取消短信', '取消发送短信',
        '不发了', '不要发送', '不要发送短信', '别发了', '算了',
    }

def is_sms_confirm_command(text):
    normalized = _normalize_command_text(text)
    return normalized in {
        '确认', '确认发送', '发送', '是的', '对', '可以', '确定', '确定发送',
    }

def _content_for_speech(content):
    return content.encode('gb2312', errors='replace').decode('gb2312')

def _fall_blocks_user_sms():
    """首条摔倒短信完成前及警报解除期间封闭普通短信入口。"""
    if os.path.exists(FALL_ALERT_RESOLVING_FILE):
        return True
    return (os.path.exists(FALL_ALERT_ACTIVE_FILE) and
            not os.path.exists(FALL_INITIAL_SMS_DONE_FILE))

def _read_user_sms_result(request_id):
    try:
        with open(USER_SMS_RESULT_FILE, 'r', encoding='utf-8') as f:
            result = json.load(f)
        if str(result.get('request_id', '')) == request_id:
            return str(result.get('status', 'failed'))
    except (FileNotFoundError, OSError, ValueError, TypeError):
        pass
    return None

def submit_user_sms(text, timeout=SMS_SEND_TIMEOUT):
    """向固定联系人原子提交短信；实际AT/PDU发送只由摔倒服务执行。"""
    if _fall_blocks_user_sms():
        return 'blocked_by_fall'
    if (os.path.exists(USER_SMS_REQUEST_FILE) or
            os.path.exists(USER_SMS_PROCESSING_FILE)):
        return 'busy'

    request_id = '%d-%d' % (int(time.time() * 1000), os.getpid())
    request = {
        'version': 1,
        'request_id': request_id,
        'phone': USER_SMS_PHONE,
        'text': text,
        'created_unix': time.time(),
        'expires_unix': time.time() + timeout + 30,
    }
    tmp = '%s.%d.tmp' % (USER_SMS_REQUEST_FILE, os.getpid())
    try:
        with open(tmp, 'w', encoding='utf-8') as f:
            json.dump(request, f, ensure_ascii=False, separators=(',', ':'))
            f.write('\n')
            f.flush()
            os.fsync(f.fileno())
        os.chmod(tmp, 0o600)
        os.replace(tmp, USER_SMS_REQUEST_FILE)
        print('[USER-SMS] Request submitted id=%s contact=%s phone=*******%s chars=%d' %
              (request_id, USER_SMS_CONTACT_NAME, USER_SMS_PHONE[-4:], len(text)),
              flush=True)
    except OSError as e:
        print('[USER-SMS] Submit failed: %s' % str(e)[:100], flush=True)
        try: os.unlink(tmp)
        except OSError: pass
        return 'failed'

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        status = _read_user_sms_result(request_id)
        if status:
            print('[USER-SMS] Result id=%s status=%s' %
                  (request_id, status), flush=True)
            return status
        time.sleep(0.20)

    # 尚未被工作者领取时可安全取消，避免超时后意外补发。
    try:
        with open(USER_SMS_REQUEST_FILE, 'r', encoding='utf-8') as f:
            pending = json.load(f)
        if str(pending.get('request_id', '')) == request_id:
            os.unlink(USER_SMS_REQUEST_FILE)
    except (FileNotFoundError, OSError, ValueError, TypeError):
        pass
    print('[USER-SMS] Timed out id=%s' % request_id, flush=True)
    return 'timeout'

# ============ 手机来信播报 IPC ============
def _sanitize_incoming_message_text(value):
    text = str(value or '').replace('\x00', '')
    text = INCOMING_MESSAGE_CONTROL_TAG_RE.sub('', text)
    text = ''.join(' ' if char in '\r\n\t' else char for char in text)
    text = ''.join(char for char in text
                   if ord(char) >= 0x20 and ord(char) != 0x7f)
    text = re.sub(r'\s+', ' ', text).strip()
    if not text or len(text) > INCOMING_MESSAGE_MAX_CHARS:
        return None
    try:
        text.encode('gb2312')
    except UnicodeEncodeError:
        return None
    return text

def _validate_incoming_message_request(request):
    request_id = str(request.get('request_id', '')).strip()
    fingerprint = str(request.get('message_fingerprint', '')).strip().lower()
    sender = re.sub(r'\D', '', str(request.get('sender_normalized', '')))
    sender_name = str(request.get('sender_name', '')).strip()
    try:
        sim_index = int(request.get('sim_index', -1))
        created_at = float(request.get('created_at', 0) or 0)
        expires_at = float(request.get('expires_at', 0) or 0)
    except (TypeError, ValueError):
        return None, 'invalid'
    raw_text = str(request.get('text', ''))
    text = _sanitize_incoming_message_text(raw_text)
    if (not request_id or not re.fullmatch(r'[0-9a-f]{64}', fingerprint) or
            sim_index < 0 or sender != USER_SMS_PHONE or
            sender_name != USER_SMS_CONTACT_NAME or text is None or
            text != raw_text):
        return None, 'invalid'
    now = time.time()
    if (created_at <= 0 or created_at > now + 60 or
            expires_at <= now or now - created_at > INCOMING_MESSAGE_MAX_AGE):
        return (request_id, fingerprint, sim_index, text), 'expired'
    return (request_id, fingerprint, sim_index, text), None

def _write_incoming_message_result(request_id, fingerprint, sim_index, status):
    result = {
        'version': 1,
        'request_id': request_id,
        'message_fingerprint': fingerprint,
        'sim_index': sim_index,
        'status': status,
        'completed_at': time.time(),
    }
    tmp = '%s.%d.%d.tmp' % (
        INCOMING_MESSAGE_RESULT_FILE, os.getpid(), threading.get_ident())
    try:
        with open(tmp, 'w', encoding='utf-8') as f:
            json.dump(result, f, ensure_ascii=False, separators=(',', ':'))
            f.write('\n')
            f.flush()
            os.fsync(f.fileno())
        os.chmod(tmp, 0o600)
        os.replace(tmp, INCOMING_MESSAGE_RESULT_FILE)
    finally:
        try: os.unlink(tmp)
        except FileNotFoundError: pass

def _recover_incoming_message_processing():
    if not os.path.exists(INCOMING_MESSAGE_PROCESSING_FILE):
        return
    if os.path.exists(INCOMING_MESSAGE_RESULT_FILE):
        try: os.unlink(INCOMING_MESSAGE_PROCESSING_FILE)
        except FileNotFoundError: pass
        return
    if not os.path.exists(INCOMING_MESSAGE_REQUEST_FILE):
        try:
            os.replace(INCOMING_MESSAGE_PROCESSING_FILE,
                       INCOMING_MESSAGE_REQUEST_FILE)
            print('[INCOMING-SMS] Requeued interrupted request', flush=True)
            return
        except OSError as e:
            print('[INCOMING-SMS] Cannot recover processing request: %s' %
                  str(e)[:100], flush=True)

def _incoming_message_worker():
    _recover_incoming_message_processing()
    print('[INCOMING-SMS] TTS request worker ready priority=%d' %
          TTS_PRIORITY_INCOMING_MESSAGE, flush=True)
    while True:
        if os.path.exists(INCOMING_MESSAGE_RESULT_FILE):
            time.sleep(0.20)
            continue
        try:
            os.replace(INCOMING_MESSAGE_REQUEST_FILE,
                       INCOMING_MESSAGE_PROCESSING_FILE)
        except FileNotFoundError:
            time.sleep(0.20)
            continue
        except OSError as e:
            print('[INCOMING-SMS] Cannot claim request: %s' % str(e)[:100],
                  flush=True)
            time.sleep(0.50)
            continue

        request_id = ''
        fingerprint = ''
        sim_index = -1
        try:
            with open(INCOMING_MESSAGE_PROCESSING_FILE, 'r',
                      encoding='utf-8') as f:
                request = json.load(f)
            validated, error = _validate_incoming_message_request(request)
            if validated is None:
                print('[INCOMING-SMS] Rejected malformed request', flush=True)
                continue
            request_id, fingerprint, sim_index, text = validated
            if error:
                status = error
            else:
                print('[INCOMING-SMS] Queuing message id=%s index=%d chars=%d' %
                      (request_id, sim_index, len(text)), flush=True)
                delivered = tts_say(
                    '收到' + USER_SMS_CONTACT_NAME + '的消息，' + text,
                    role='incoming_message')
                status = 'played' if delivered else 'failed'
            _write_incoming_message_result(
                request_id, fingerprint, sim_index, status)
            print('[INCOMING-SMS] Result id=%s status=%s' %
                  (request_id, status), flush=True)
        except Exception as e:
            print('[INCOMING-SMS] Worker error id=%s: %s' %
                  (request_id or '?', str(e)[:120]), flush=True)
            if request_id and fingerprint and sim_index >= 0:
                try:
                    _write_incoming_message_result(
                        request_id, fingerprint, sim_index, 'failed')
                except OSError:
                    pass
        finally:
            try: os.unlink(INCOMING_MESSAGE_PROCESSING_FILE)
            except FileNotFoundError: pass
            except OSError as e:
                print('[INCOMING-SMS] Cannot clear processing request: %s' %
                      str(e)[:100], flush=True)

def classify_radar_light_command(text):
    """返回'open'/'close'/None；灯带命令与警报、导航和出行域显式隔离。"""
    normalized = _normalize_command_text(text)
    if not normalized or any(w in normalized for w in ['不要', '别', '不用', '无需']):
        return None
    if any(w in normalized for w in RADAR_LIGHT_CONFLICT_WORDS):
        return None
    if any(v in normalized for v in RADAR_LIGHT_OPEN_VARIANTS):
        return 'open'
    if any(v in normalized for v in RADAR_LIGHT_CLOSE_VARIANTS):
        return 'close'
    try:
        from pypinyin import pinyin, Style
        chars = ''.join(re.findall(r'[一-鿿]', normalized))
        syllables = [item[0].lower() for item in pinyin(chars, style=Style.NORMAL)]
        if any(_matches_pinyin_target(syllables, target)
               for target in RADAR_LIGHT_OPEN_PINYIN):
            return 'open'
        if any(_matches_pinyin_target(syllables, target)
               for target in RADAR_LIGHT_CLOSE_PINYIN):
            return 'close'
    except Exception:
        pass
    return None

def set_radar_green_enabled(enabled):
    """只切换绿色常亮模式，不停止雷达，不直接写灯带硬件。"""
    if enabled:
        tmp = '%s.%d.tmp' % (RADAR_GREEN_ENABLED_FILE, os.getpid())
        try:
            with open(tmp, 'w', encoding='ascii') as f:
                f.write('enabled=1\npid=%d\n' % os.getpid())
                f.flush()
                os.fsync(f.fileno())
            # Group access is required for the private cross-service IPC.
            os.chmod(tmp, 0o660)  # nosec B103
            os.replace(tmp, RADAR_GREEN_ENABLED_FILE)
        finally:
            if os.path.exists(tmp):
                try: os.unlink(tmp)
                except OSError: pass
    else:
        try: os.unlink(RADAR_GREEN_ENABLED_FILE)
        except FileNotFoundError: pass
        except OSError as e:
            print('[RADAR] Cannot disable green mode: %s' % str(e)[:100], flush=True)
            return False
    print('[RADAR] Green mode %s' % ('ENABLED' if enabled else 'DISABLED'), flush=True)
    return True

def set_radar_travel_enabled(enabled):
    """行路模式开启时允许雷达在2米内蓝闪；绿色模式保持独立。"""
    if enabled:
        tmp = '%s.%d.tmp' % (RADAR_TRAVEL_ENABLED_FILE, os.getpid())
        try:
            with open(tmp, 'w', encoding='ascii') as f:
                f.write('enabled=1\npid=%d\n' % os.getpid())
                f.flush()
                os.fsync(f.fileno())
            # Group access is required for the private cross-service IPC.
            os.chmod(tmp, 0o660)  # nosec B103
            os.replace(tmp, RADAR_TRAVEL_ENABLED_FILE)
        finally:
            if os.path.exists(tmp):
                try: os.unlink(tmp)
                except OSError: pass
    else:
        try: os.unlink(RADAR_TRAVEL_ENABLED_FILE)
        except FileNotFoundError: pass
        except OSError as e:
            print('[RADAR] Cannot disable travel mode: %s' % str(e)[:100], flush=True)
            return False
    print('[RADAR] Travel blue mode %s' %
          ('ENABLED' if enabled else 'DISABLED'), flush=True)
    return True

def request_listening_yellow_flash():
    """提交一次黄灯双闪请求；灯带硬件始终由radar_led统一写入。"""
    tmp = '%s.%d.tmp' % (RADAR_LISTENING_YELLOW_REQUEST_FILE, os.getpid())
    try:
        with open(tmp, 'w', encoding='ascii') as f:
            f.write('version=1\npid=%d\nrequested_unix=%.3f\n' %
                    (os.getpid(), time.time()))
            f.flush()
            os.fsync(f.fileno())
        # Group access is required for the private cross-service IPC.
        os.chmod(tmp, 0o660)  # nosec B103
        os.replace(tmp, RADAR_LISTENING_YELLOW_REQUEST_FILE)
        print('[RADAR] Listening yellow double-flash requested', flush=True)
        return True
    except OSError as e:
        print('[RADAR] Cannot request listening yellow flash: %s' % str(e)[:100], flush=True)
        return False
    finally:
        if os.path.exists(tmp):
            try: os.unlink(tmp)
            except OSError: pass

def is_radar_led_running():
    result = subprocess.run(['/usr/bin/pgrep', '-x', 'radar_led'],
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return result.returncode == 0

def start_radar_led():
    """语音助手运行期间确保雷达联动在线。"""
    global radar_process, radar_started_by_assistant
    if RADAR_MANAGED_EXTERNALLY:
        radar_started_by_assistant = False
        if is_radar_led_running():
            print('[RADAR] radar_led managed by systemd', flush=True)
            return True
        print('[RADAR] systemd-managed radar_led is not running', flush=True)
        return False
    if is_radar_led_running():
        print('[RADAR] radar_led already running', flush=True)
        radar_started_by_assistant = False
        return True
    try:
        radar_process = subprocess.Popen(
            [SUDO_BIN, '-n', HWCTL_BIN, 'radar-start'],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            preexec_fn=os.setpgrp)
        radar_started_by_assistant = True
        time.sleep(0.5)
        if is_radar_led_running():
            print('[RADAR] radar_led started | default far mode=OFF', flush=True)
            return True
        print('[RADAR] radar_led failed to start', flush=True)
    except Exception as e:
        print('[RADAR] start error: %s' % str(e)[:100], flush=True)
    radar_process = None
    radar_started_by_assistant = False
    return False

def stop_radar_led():
    """只停止由本次语音助手启动的雷达进程；不碰用户手动启动的实例。"""
    global radar_process, radar_started_by_assistant
    if RADAR_MANAGED_EXTERNALLY:
        return
    if not radar_started_by_assistant:
        return
    subprocess.run(
        [SUDO_BIN, '-n', HWCTL_BIN, 'radar-stop'],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    if radar_process is not None:
        try: radar_process.wait(timeout=3)
        except Exception: pass
    radar_process = None
    radar_started_by_assistant = False
    print('[RADAR] radar_led stopped', flush=True)

# ============ YOLO 检测控制 ============

def is_yolo_running():
    return _yolo_manager_thread is not None and _yolo_manager_thread.is_alive()

def _begin_yolo_generation():
    global _yolo_generation, _yolo_stop_requested
    with _yolo_state_lock:
        _yolo_generation += 1
        _yolo_stop_requested = False
        return _yolo_generation

def _request_yolo_stop():
    global _yolo_generation, _yolo_stop_requested
    with _yolo_state_lock:
        _yolo_generation += 1
        _yolo_stop_requested = True
    _yolo_starting.clear()
    _yolo_ready.clear()

def _yolo_generation_is_active(generation):
    with _yolo_state_lock:
        return not _yolo_stop_requested and generation == _yolo_generation

def _process_running(pattern, exact=False):
    args = ['/usr/bin/pgrep', '-x' if exact else '-f', pattern]
    return subprocess.run(
        args, stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL).returncode == 0

def _cleanup_yolo_processes():
    """Stop userspace inference, then let cam-test release the driver cleanly.

    SIGKILL while cam-test owns the K1 camera pipeline can wedge the board. The
    documented Ctrl+C/SIGINT path powers both sensors down and detaches the media
    topology, so a stuck camera is left for a later retry instead of force-killed.
    """
    consumer_pattern = '/home/space/jdk_cam/workspace/consumer_final_new_gray'
    subprocess.run(
        [SUDO_BIN, '-n', HWCTL_BIN, 'consumer-stop'],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    consumer_deadline = time.monotonic() + 3.0
    while (_process_running(consumer_pattern) and
           time.monotonic() < consumer_deadline):
        time.sleep(0.1)
    if _process_running(consumer_pattern):
        subprocess.run(
            [SUDO_BIN, '-n', HWCTL_BIN, 'consumer-kill'],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    camera_stopped = True
    if _process_running('cam-test', exact=True):
        print('[DET] Requesting graceful cam-test stop (SIGINT)...', flush=True)
        subprocess.run(
            [SUDO_BIN, '-n', HWCTL_BIN, 'camera-stop'],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        camera_deadline = time.monotonic() + 20.0
        while (_process_running('cam-test', exact=True) and
               time.monotonic() < camera_deadline):
            time.sleep(0.2)
        camera_stopped = not _process_running('cam-test', exact=True)
        if camera_stopped:
            print('[DET] cam-test stopped cleanly', flush=True)
        else:
            print('[DET] WARN cam-test did not stop; refusing SIGKILL and restart',
                  flush=True)

    subprocess.run(
        [SUDO_BIN, '-n', HWCTL_BIN, 'tcm-perms'],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    if camera_stopped:
        for frame_path in CAMERA_SHM_FILES:
            try:
                os.unlink(frame_path)
            except FileNotFoundError:
                pass
            except OSError as error:
                print('[DET] WARN cannot remove %s: %s' %
                      (frame_path, str(error)[:80]), flush=True)
    return camera_stopped

def _launch_camera_pipeline():
    return subprocess.Popen(
        [SUDO_BIN, '-n', HWCTL_BIN, 'camera-start'],
        stdout=None, stderr=None, preexec_fn=os.setpgrp)

def _launch_yolo_consumer():
    return subprocess.Popen(
        ['/usr/bin/stdbuf', '-oL', '-eL',
         '/home/space/jdk_cam/workspace/consumer_final_new_gray',
         '/home/space/jdk_cam/best_6out.xq.onnx'],
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        text=True, bufsize=1)

def _read_consumer_lines(process, output_queue):
    try:
        for line in process.stdout:
            output_queue.put(line.rstrip())
    except Exception as e:
        output_queue.put('[DET-READER-ERROR] ' + str(e)[:100])
    finally:
        output_queue.put(None)

def _wait_yolo_restart(generation, restart_count):
    delay = min(2.0 * (2 ** min(max(restart_count - 1, 0), 4)), 30.0)
    print('[DET] Retry backoff %.0fs' % delay, flush=True)
    deadline = time.monotonic() + delay
    while (_yolo_generation_is_active(generation) and
           time.monotonic() < deadline):
        time.sleep(min(0.2, deadline - time.monotonic()))

def _run_yolo_manager(generation):
    global jdk_process, camera_process
    last_alert_time = {'left': 0, 'right': 0}
    restart_count = 0

    while _yolo_generation_is_active(generation):
        _yolo_starting.set()
        _yolo_ready.clear()
        _clear_pending_alerts()
        if not _cleanup_yolo_processes():
            restart_count += 1
            _wait_yolo_restart(generation, restart_count)
            continue
        if not _yolo_generation_is_active(generation):
            break
        time.sleep(1)

        print('[DET] Starting cam-test (dual pipeline)...', flush=True)
        try:
            camera = _launch_camera_pipeline()
        except Exception as e:
            print('[DET] Camera launch failed: %s' % str(e)[:100], flush=True)
            restart_count += 1
            _wait_yolo_restart(generation, restart_count)
            continue
        camera_process = camera
        print('[DET] cam-test launcher PID=%d, waiting for SHM...' % camera.pid,
              flush=True)

        shm_ready = False
        for second in range(20):
            if not _yolo_generation_is_active(generation):
                break
            if camera.poll() is not None:
                print('[DET] cam-test exited before SHM (rc=%d)' % camera.returncode,
                      flush=True)
                break
            if all(os.path.exists(path) for path in CAMERA_SHM_FILES):
                shm_ready = True
                print('[DET] SHM ready after %ds' % (second + 1), flush=True)
                break
            time.sleep(1)
        if not _yolo_generation_is_active(generation):
            break
        if not shm_ready:
            print('[DET] Pipeline unhealthy: SHM unavailable; restarting', flush=True)
            _cleanup_yolo_processes()
            restart_count += 1
            _wait_yolo_restart(generation, restart_count)
            continue

        time.sleep(2)
        print('[DET] Starting consumer_final...', flush=True)
        try:
            consumer = _launch_yolo_consumer()
        except Exception as e:
            print('[DET] Consumer launch failed: %s' % str(e)[:100], flush=True)
            _cleanup_yolo_processes()
            restart_count += 1
            _wait_yolo_restart(generation, restart_count)
            continue
        jdk_process = consumer
        output_queue = queue.Queue()
        threading.Thread(target=_read_consumer_lines,
                         args=(consumer, output_queue), daemon=True).start()
        print('[DET] Pipeline launched | camera=%d consumer=%d | waiting first frame' %
              (camera.pid, consumer.pid), flush=True)

        frame_seen = False
        last_frame_progress = None
        first_frame_deadline = time.monotonic() + 15.0
        restart_reason = None
        while _yolo_generation_is_active(generation):
            if camera.poll() is not None:
                restart_reason = 'cam-test exited rc=%d' % camera.returncode
                break
            if consumer.poll() is not None:
                restart_reason = 'consumer exited rc=%d' % consumer.returncode
                break

            try:
                line = output_queue.get(timeout=0.10)
            except queue.Empty:
                line = ''
            if line is None:
                restart_reason = 'consumer output closed'
                break
            if line:
                is_detection_frame = re.match(r'^\[F\d+\]', line) is not None
                is_hidden_frame = re.match(r'^\[FRAME\] \d+$', line) is not None
                if not is_hidden_frame:
                    print(line, flush=True)
                if is_detection_frame or is_hidden_frame:
                    last_frame_progress = time.monotonic()
                    if not frame_seen:
                        frame_seen = True
                        _yolo_starting.clear()
                        _yolo_ready.set()
                        print('[DET] Dual-camera YOLO active after first frame | '
                              'camera=%d consumer=%d restart=%d' %
                              (camera.pid, consumer.pid, restart_count), flush=True)
                if is_detection_frame:
                    if '[ALERT]' in line:
                        now = time.monotonic()
                        if ('左侧' in line and
                                now - last_alert_time['left'] >= 5 and
                                _enqueue_vehicle_alert('left')):
                            last_alert_time['left'] = now
                        elif ('右侧' in line and
                              now - last_alert_time['right'] >= 5 and
                              _enqueue_vehicle_alert('right')):
                            last_alert_time['right'] = now
                elif '[ALERT]' in line:
                    now = time.monotonic()
                    if ('左侧' in line and
                            now - last_alert_time['left'] >= 5 and
                            _enqueue_vehicle_alert('left')):
                        last_alert_time['left'] = now
                    elif ('右侧' in line and
                          now - last_alert_time['right'] >= 5 and
                          _enqueue_vehicle_alert('right')):
                        last_alert_time['right'] = now

            # ASR模型加载/推理会短时占用大量CPU。此时消费者可能暂时没有
            # 帧日志，但并不代表摄像头故障；给流水线留出ASR结束后的恢复窗口。
            if _asr_in_progress.is_set():
                if frame_seen:
                    last_frame_progress = time.monotonic()
                else:
                    first_frame_deadline = time.monotonic() + 15.0

            if not frame_seen and time.monotonic() >= first_frame_deadline:
                restart_reason = 'no frame output within 15s'
                break
            if (frame_seen and last_frame_progress is not None and
                    time.monotonic() - last_frame_progress >= FRAME_PROGRESS_TIMEOUT):
                restart_reason = 'no frame progress for %.1fs' % FRAME_PROGRESS_TIMEOUT
                break

        if not _yolo_generation_is_active(generation):
            break
        _yolo_starting.set()
        _yolo_ready.clear()
        restart_count += 1
        print('[DET] Pipeline unhealthy: %s; restarting full pipeline #%d' %
              (restart_reason or 'unknown', restart_count), flush=True)
        _cleanup_yolo_processes()
        jdk_process = None
        camera_process = None
        _wait_yolo_restart(generation, restart_count)

    _yolo_starting.clear()
    _yolo_ready.clear()
    if jdk_process is not None and jdk_process.poll() is not None:
        jdk_process = None
    if camera_process is not None and camera_process.poll() is not None:
        camera_process = None
    print('[DET] Pipeline manager stopped', flush=True)

def start_yolo():
    global _yolo_manager_thread
    set_radar_travel_enabled(True)
    if is_yolo_running():
        print("[DET] Already running, skip", flush=True)
        return

    generation = _begin_yolo_generation()
    _yolo_starting.set()
    _yolo_ready.clear()
    print("[DET] === Starting dual-camera YOLO ===", flush=True)
    _ensure_alert_worker()
    _clear_pending_alerts()
    _yolo_manager_thread = threading.Thread(
        target=_run_yolo_manager, args=(generation,),
        name='dual-camera-manager', daemon=True)
    _yolo_manager_thread.start()
    print('[DET] Pipeline manager started; active waits for first frame', flush=True)

def stop_yolo():
    global jdk_process, camera_process, _yolo_manager_thread
    set_radar_travel_enabled(False)
    _request_yolo_stop()
    _clear_pending_alerts()
    process = jdk_process
    if process is not None and process.poll() is None:
        print("[DET] Stopping consumer_final PID=%d..." % process.pid, flush=True)
        process.terminate()
        try: process.wait(timeout=5)
        except subprocess.TimeoutExpired: process.kill()
    camera_stopped = _cleanup_yolo_processes()
    manager = _yolo_manager_thread
    if manager is not None and manager.is_alive() and manager is not threading.current_thread():
        manager.join(timeout=3)
    jdk_process = None
    camera_process = None
    _yolo_manager_thread = None
    print("\n[DET] Stopped (cameras + YOLO%s)" %
          ('' if camera_stopped else '; camera cleanup pending'), flush=True)

# ============ 导航控制 ============
_nav_engine_loaded = False

def _load_nav_engine():
    global _nav_engine_loaded
    if _nav_engine_loaded: return True
    try:
        import nav_engine
        nav_engine.set_speak_fn(lambda text: tts_say(text, role='navigation'))
        nav_engine.load_tts()
        # 路程摘要完整播报后才启动摄像头检测，避免打断第一句导航语。
        def _on_route_summary_finished():
            print("[NAV] Route summary finished; starting travel detection", flush=True)
            start_yolo()
        nav_engine.set_on_nav_started(_on_route_summary_finished)
        def _on_arrived():
            global nav_active
            nav_active = False
            print("[NAV] Arrived", flush=True)
        nav_engine.set_on_nav_end(_on_arrived)
        def _on_fail():
            global nav_active
            nav_active = False
            print("[NAV] Failed", flush=True)
        nav_engine.set_on_nav_fail(_on_fail)
        _nav_engine_loaded = True
        return True
    except ImportError as e:
        print("[NAV] Engine not available: %s" % e, flush=True)
        tts_say("导航引擎不可用")
        return False

def start_voice_navigation(dest_text):
    global nav_active
    if not _load_nav_engine(): return False
    from nav_engine import geocode, start_navigation as nav_start, stop_navigation as nav_stop

    raw_dest_text = dest_text
    dest_text = sanitize_nav_destination(dest_text)
    if len(dest_text) < 2:
        print('[NAV] Destination rejected after sanitizing: %s' % raw_dest_text, flush=True)
        tts_say('请只说目的地名称')
        return False
    if dest_text != raw_dest_text:
        print('[NAV] Destination sanitized before geocoding: %s -> %s' %
              (raw_dest_text, dest_text), flush=True)

    print("[NAV] Geocoding: %s" % dest_text, flush=True)
    dest = geocode(dest_text)
    if not dest:
        print("[NAV] Geocode failed", flush=True)
        tts_say("找不到" + dest_text)
        return False

    dest_lat, dest_lng = dest
    print("[NAV] Destination: %s (%.5f, %.5f)" % (dest_text, dest_lat, dest_lng), flush=True)

    # 摄像头检测等导航开场播报完后再启动（由 on_nav_started 回调触发）
    if is_nav_running():
        nav_stop()

    print("\n" + "="*50, flush=True)
    nav_start(dest_text, dest_lat, dest_lng)
    nav_active = True
    print("[NAV] Started to %s" % dest_text, flush=True)
    return True

def is_nav_running():
    try:
        import nav_engine
        return nav_engine.nav_active
    except: return False

def stop_navigation_only():
    if not is_nav_running(): return
    try:
        import nav_engine
        nav_engine.stop_navigation()
    except: pass
    global nav_active
    nav_active = False
    print("[NAV] Stopped (detection stays on)", flush=True)

# ============ 命令处理 ============

def has_nav_keyword(text):
    """命令中必须包含'导航'或其近音字"""
    for kw in NAV_KEYWORDS:
        if kw in text:
            return True
    return False

def sanitize_nav_destination(value):
    """去掉地点后同一段录音中紧跟的闲聊，避免原始ASR尾巴进入导航TTS。"""
    dest = (value or '').strip()
    dest = re.split(r'[，。！？、；;]', dest, maxsplit=1)[0]
    dest = re.sub(r'\s+', '', dest)
    cut_at = len(dest)
    for marker in NAV_DEST_TRAILING_MARKERS:
        index = dest.find(marker)
        if index >= 2:
            cut_at = min(cut_at, index)
    dest = dest[:cut_at]
    # 不删除单字语气词；例如“酒吧”的“吧”是地点本身。
    dest = re.sub(r'(?:行吗|好吗)$', '', dest)
    return dest.strip()

def extract_dest(text):
    """从含'导航'的命令中提取目的地"""
    for pat in [
        r'(?:导航|导行|到航|道行|导杭|到杭|导肮)[到岛道大](.+)',
    ]:
        m = re.search(pat, text)
        if m:
            raw_dest = m.group(1).strip()
            dest = sanitize_nav_destination(raw_dest)
            if len(dest) >= 2:
                if dest != raw_dest:
                    print('[NAV] Destination sanitized: %s -> %s' %
                          (raw_dest, dest), flush=True)
                return dest
    return None

def handle_command(text):
    text_lower = text.lower()

    # 否定的灯带/警报命令必须在通用“关闭”兜底前截获，避免误关导航或YOLO。
    normalized = _normalize_command_text(text)
    if (any(w in normalized for w in ['不要', '别', '不用', '无需']) and
            any(w in normalized for w in ['灯带', '警示灯', '灯光', '警报', '报警'])):
        tts_say("已取消")
        return True

    # ═══ 关闭摔倒警报：独立安全域，不改变导航/出行/YOLO状态 ═══
    if is_close_fall_alarm_command(text):
        result = request_close_fall_alarm()
        if result == 'requested':
            tts_say("已关闭摔倒警报", role='safety')
        elif result == 'inactive':
            tts_say("当前没有摔倒警报")
        else:
            tts_say("关闭摔倒警报失败，请再试一次", role='safety')
        return True

    # ═══ 绿色灯带模式：与行路模式控制的2米内蓝闪相互独立 ═══
    radar_light_command = classify_radar_light_command(text)
    if radar_light_command == 'open':
        if set_radar_green_enabled(True):
            tts_say("已打开灯带")
        else:
            tts_say("打开灯带失败，请再试一次")
        return True
    if radar_light_command == 'close':
        if set_radar_green_enabled(False):
            tts_say("已关闭灯带")
        else:
            tts_say("关闭灯带失败，请再试一次")
        return True

    # ═══ 导航到xxx（必须有"导航"关键字）→ YOLO + 导航 ═══
    if has_nav_keyword(text):
        dest = extract_dest(text)
        if dest:
            print("[CMD] Nav to: %s" % dest, flush=True)
            tts_say("已打开导航")
            time.sleep(0.3)
            if start_voice_navigation(dest):
                return True
            else:
                return False

    # ═══ 打开导航（无目的地）→ 问去哪里 ═══
    if any(w in text for w in NAV_OPEN_PHRASES):
        print("[CMD] Open nav (no dest)", flush=True)
        tts_say("已打开导航")
        time.sleep(0.3)
        tts_say("请问要导航到什么地方")
        return 'nav_wait'

    # ═══ 关闭所有 ═══
    if any(w in text_lower for w in TRAVEL_CLOSE_PHRASES):
        stop_navigation_only()
        stop_yolo()
        tts_say("已关闭出行模式")
        return True

    # ═══ 关闭导航（保留检测）═══
    if any(w in text_lower for w in NAV_CLOSE_PHRASES):
        if is_nav_running():
            stop_navigation_only()
        tts_say("已关闭导航")
        return True

    # ═══ 打开行路模式 / 出行模式（只开检测，不开导航）═══
    if any(w in text_lower for w in ['行路模式', '出行模式', '行动模式', '打开检测', '启动检测',
                                       '开始检测', '开启检测', '打开监控', '开启监控']):
        start_yolo()
        tts_say("已打开出行模式")
        return True

    # ═══ 关闭 / 停止（兜底）═══
    if any(w in text_lower for w in ['关闭', '停止', '结束', '退出', '关掉']):
        stop_navigation_only()
        stop_yolo()
        tts_say("已关闭出行模式")
        return True

    return False

# ============ 主循环 ============

def main():
    # systemd停止服务时走与Ctrl+C相同的清理路径；雷达若由独立服务托管则不停止它。
    def _shutdown_from_signal(signum, frame):
        raise KeyboardInterrupt
    signal.signal(signal.SIGTERM, _shutdown_from_signal)

    # 杀 PipeWire 释放声卡
    for pw in ["pipewire", "wireplumber", "pipewire-pulse"]:
        subprocess.run(
            [SUDO_BIN, "-n", HWCTL_BIN, "audio-stop", pw],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    time.sleep(0.5)

    # USB 声卡是 card 2，pyaudio 设备索引是 1（设备 0 是板载 ES8326）
    device_index = 0
    print("[INIT] Audio dev=%d (USB)  card=%d" % (device_index, AUDIO_CARD), flush=True)

    rec = RecAudioVad(
        sld=1, max_time=5, channels=1, rate=48000,
        device_index=device_index, trig_on=0.30, trig_off=0.15
    )
    rec.stream = _AudioReadWatchdogStream(rec.stream)
    print("[AUDIO] read watchdog build=%s timeout=%.1fs" %
          (AUDIO_WATCHDOG_BUILD, AUDIO_FRAME_STALL_TIMEOUT), flush=True)
    print("[TTS] hardware volume=%s" % TTS_VOLUME_TAG, flush=True)

    # RecAudioVad initializes the USB mixer, so enforce output volume afterwards.
    subprocess.run(["/usr/bin/amixer", "-c", str(AUDIO_CARD), "sset", "Speaker", "100%"],
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    # 每次语音助手启动都重置绿色和行路标记，避免异常退出遗留模式。
    set_radar_green_enabled(False)
    set_radar_travel_enabled(False)
    # 雷达服务持续测距，但只有行路标记存在时才允许2米内蓝闪。
    start_radar_led()
    threading.Thread(
        target=_incoming_message_worker,
        name='incoming-sms-tts', daemon=True).start()

    # 在服务启动阶段加载一次SenseVoice。ASRModel本身是进程内单例；此引用在
    # 整个主循环存活期间保留，因此空闲态不推理也不会卸载模型或ONNX会话。
    preload_started = time.monotonic()
    print('[ASR] Preloading resident command model...', flush=True)
    resident_asr = load_asr()
    print('[ASR] Resident command model ready in %.2fs' %
          (time.monotonic() - preload_started), flush=True)

    asrpro_thread = _start_asrpro_wake_listener()

    state = IDLE
    woken_time = 0
    sms_text = None
    idle_uart_armed = False

    print("[INIT] Voice assistant v4 ready | build=%s" % BUILD_ID, flush=True)
    print("[INIT] Wake: ASRPRO UART only  |  Command ASR: resident", flush=True)
    print("-" * 50, flush=True)

    while True:
        try:
            labels = {
                IDLE: "IDLE", WOKEN: "WOKEN", NAV_WAITING: "NAV_WAIT",
                SMS_CONTENT_WAITING: "SMS_CONTENT",
                SMS_CONFIRM_WAITING: "SMS_CONFIRM",
            }

            # 空闲态完全由ASRPRO唤醒。USB录音对象和SenseVoice模型均已初始化，
            # 但这里既不录音，也不调用generate()。进入空闲态时清掉在上一轮
            # WOKEN期间收到的重复WAKE，之后到达的新事件才是有效唤醒。
            if state == IDLE:
                if not idle_uart_armed:
                    _asrpro_command_session.clear()
                    _clear_asrpro_commands('entering IDLE')
                    _asrpro_wake_event.clear()
                    _pause_audio_capture_while_idle(rec)
                    idle_uart_armed = True
                    print('\n[IDLE] Waiting for ASRPRO WAKE on %s...' %
                          ASRPRO_WAKE_TTY, flush=True)
                _asrpro_wake_event.wait()
                _asrpro_wake_event.clear()
                idle_uart_armed = False
                print('[WAKE] +++ ASRPRO WAKE +++', flush=True)
                _asrpro_command_session.set()
                tts_say('我在', role='wake')
                state = WOKEN
                woken_time = time.time()
                continue

            idle_uart_armed = False
            print("\n[%s] Listening..." % labels[state], flush=True)

            # ═══ 第一步：超时检查（VAD 前，倒计时到了直接休眠，不录音不推理）═══
            if state in (WOKEN, NAV_WAITING) + SMS_STATES:
                if state in SMS_STATES:
                    limit = SMS_DIALOG_TIMEOUT
                else:
                    limit = CMD_TIMEOUT if state == WOKEN else NAV_WAIT_TIMEOUT
                if time.time() - woken_time > limit:
                    if state in SMS_STATES:
                        print("[%s] Timeout -> SMS CANCEL" % labels[state], flush=True)
                        tts_say("短信发送已取消")
                        sms_text = None
                    else:
                        print("[%s] Timeout -> SLEEP" % labels[state], flush=True)
                        tts_say("我进入休眠状态，请喊小空小空唤醒我")
                    state = IDLE
                    continue

            # 助手自己的播报不能再次触发 VAD/ASR。若播报正在进行，先不录音；
            # 若播报在录音过程中开始，即使已经结束，也用代次变化识别并丢弃该段。
            tts_gen_before, tts_busy = _tts_state_snapshot()
            if tts_busy:
                while tts_busy:
                    time.sleep(0.05)
                    _, tts_busy = _tts_state_snapshot()
                continue

            asrpro_command = _take_asrpro_command()
            if asrpro_command is not None:
                print('[ASRPRO] Execute fixed command before K1 capture: %s -> %s' %
                      (asrpro_command, ASRPRO_COMMAND_TEXT[asrpro_command]),
                      flush=True)
                handle_command(ASRPRO_COMMAND_TEXT[asrpro_command])
                if state in SMS_STATES:
                    sms_text = None
                state = IDLE
                continue

            audio = _record_audio(rec)

            tts_gen_after, tts_busy = _tts_state_snapshot()
            if tts_busy or tts_gen_after != tts_gen_before:
                print('[ASR] Discard audio overlapping assistant TTS', flush=True)
                _clear_asrpro_commands('audio overlapped assistant TTS')
                continue

            # ASRPRO通常会在K1的VAD结束前发出固定命令。给串口一个很短的
            # 收尾窗口；若收到事件则丢弃同一段USB录音，完全跳过SenseVoice推理。
            asrpro_command = _take_asrpro_command(
                ASRPRO_COMMAND_GRACE if audio is not None and len(audio) > 0 else 0.0)
            if asrpro_command is not None:
                print('[ASRPRO] Execute fixed command; skip K1 command ASR: %s -> %s' %
                      (asrpro_command, ASRPRO_COMMAND_TEXT[asrpro_command]),
                      flush=True)
                handle_command(ASRPRO_COMMAND_TEXT[asrpro_command])
                if state in SMS_STATES:
                    sms_text = None
                state = IDLE
                continue

            if audio is None or len(audio) == 0:
                continue

            # 双摄启动/重启时保留已经录到的命令。先给摄像头一个很短的稳定
            # 窗口；若硬件仍在恢复，也必须继续ASR，不能让用户的指令无声丢失。
            if _yolo_starting.is_set():
                print('[ASR] Camera transition active; preserving recorded command',
                      flush=True)
                transition_deadline = time.monotonic() + CAMERA_TRANSITION_ASR_GRACE
                while (_yolo_starting.is_set() and
                       time.monotonic() < transition_deadline):
                    time.sleep(0.05)
                    current_tts_gen, current_tts_busy = _tts_state_snapshot()
                    if current_tts_busy or current_tts_gen != tts_gen_before:
                        print('[ASR] Discard audio overlapping assistant TTS', flush=True)
                        audio = None
                        break
                if audio is None:
                    continue
                if _yolo_starting.is_set():
                    print('[ASR] Camera still transitioning; processing preserved command',
                          flush=True)
                else:
                    print('[ASR] Camera transition settled; processing preserved command',
                          flush=True)

            # ═══ 第二步：VAD 后再次检查（录制可能耗时近 max_time=5s，可能刚好超时）═══
            if state in (WOKEN, NAV_WAITING) + SMS_STATES:
                if state in SMS_STATES:
                    limit = SMS_DIALOG_TIMEOUT
                else:
                    limit = CMD_TIMEOUT if state == WOKEN else NAV_WAIT_TIMEOUT
                if time.time() - woken_time > limit:
                    if state in SMS_STATES:
                        print("[%s] Timeout -> SMS CANCEL (after VAD)" %
                              labels[state], flush=True)
                        tts_say("短信发送已取消")
                        sms_text = None
                    else:
                        print("[%s] Timeout -> SLEEP (after VAD)" % labels[state], flush=True)
                        tts_say("我进入休眠状态，请喊小空小空唤醒我")
                    state = IDLE
                    continue

            _asr_in_progress.set()
            try:
                raw = resident_asr.generate(audio)
            finally:
                _asr_in_progress.clear()

            if raw is None or raw.strip() == "": continue
            text = clean_asr(raw)
            print("[ASR] >>> %s" % text, flush=True)

            if state == IDLE:
                # 防御性门禁：正常流程在IDLE分支已经阻止录音和推理。
                print('[ASRPRO] Defensive drop: command ASR returned while IDLE',
                      flush=True)
                continue

            elif state == WOKEN:
                # 唤醒词刷新的逻辑去掉——唤醒后再说小空小空不重置计时器
                if is_sms_start_command(text):
                    if _fall_blocks_user_sms():
                        tts_say("摔倒短信正在发送或警报正在解除，请稍后再试")
                        state = IDLE
                        continue
                    sms_text = None
                    print('[USER-SMS] Fixed recipient contact=%s phone=*******%s' %
                          (USER_SMS_CONTACT_NAME, USER_SMS_PHONE[-4:]), flush=True)
                    tts_say("请说要发送给" + USER_SMS_CONTACT_NAME + "的短信内容")
                    state = SMS_CONTENT_WAITING
                    woken_time = time.time()
                    continue
                result = handle_command(text)
                if result == 'nav_wait':
                    state = NAV_WAITING
                    woken_time = time.time()
                elif result is True:
                    state = IDLE
                else:
                    tts_say("请再说一遍")
                    # 无效指令不重置计时器！10s 倒计时继续走

            elif state in SMS_STATES:
                # 摔倒警报命令始终优先，进入后取消未完成的普通短信对话。
                if is_close_fall_alarm_command(text):
                    handle_command(text)
                    sms_text = None
                    state = IDLE
                    continue
                if is_sms_cancel_command(text):
                    print('[USER-SMS] Dialog cancelled by user', flush=True)
                    tts_say("短信发送已取消")
                    sms_text = None
                    state = IDLE
                    continue

                if state == SMS_CONTENT_WAITING:
                    content = sanitize_sms_content(text)
                    try:
                        content_bytes = content.encode('utf-16-be')
                    except UnicodeEncodeError:
                        content_bytes = b''
                    if not content:
                        tts_say("短信内容不能为空，请重新说短信内容")
                        woken_time = time.time()
                        continue
                    if len(content_bytes) > 140:
                        tts_say("短信内容过长，请控制在七十个汉字以内")
                        woken_time = time.time()
                        continue
                    sms_text = content
                    print('[USER-SMS] Content captured chars=%d' % len(content),
                          flush=True)
                    tts_say(
                        "将向" + USER_SMS_CONTACT_NAME +
                        "发送短信，内容是，" + _content_for_speech(sms_text) +
                        "。请说确认发送或取消发送")
                    state = SMS_CONFIRM_WAITING
                    woken_time = time.time()
                    continue

                if state == SMS_CONFIRM_WAITING:
                    if not is_sms_confirm_command(text):
                        tts_say("请说确认发送或取消发送")
                        woken_time = time.time()
                        continue
                    if _fall_blocks_user_sms():
                        tts_say("摔倒短信正在发送或警报正在解除，普通短信未发送")
                        status = 'blocked_by_fall'
                    else:
                        tts_say("正在发送短信")
                        status = submit_user_sms(sms_text)
                        if status == 'sent':
                            tts_say("短信发送成功")
                        elif status == 'busy':
                            tts_say("还有一条短信正在处理，请稍后再试")
                        elif status == 'blocked_by_fall':
                            tts_say("摔倒短信正在发送或警报正在解除，普通短信未发送")
                        elif status == 'timeout':
                            tts_say("短信发送超时，请稍后再试")
                        else:
                            tts_say("短信发送失败，请稍后再试")
                    print('[USER-SMS] Dialog completed status=%s' % status, flush=True)
                    sms_text = None
                    state = IDLE
                    continue

            elif state == NAV_WAITING:
                # 等待目的地时，关闭命令仍是控制命令，不能当成地名去地理编码。
                if (any(w in text for w in NAV_CLOSE_PHRASES) or
                        any(w in text for w in TRAVEL_CLOSE_PHRASES)):
                    handle_command(text)
                    state = IDLE
                    continue
                # 警报/灯带命令不能被误当成导航目的地。
                if (is_close_fall_alarm_command(text) or
                        classify_radar_light_command(text) is not None):
                    handle_command(text)
                    state = IDLE
                    continue
                if has_nav_keyword(text):
                    dest = extract_dest(text)
                else:
                    dest = None
                if not dest:
                    raw_dest = text
                    dest = sanitize_nav_destination(raw_dest)
                    if dest != raw_dest:
                        print('[NAV_WAIT] Destination sanitized: %s -> %s' %
                              (raw_dest, dest), flush=True)
                    if len(dest) < 2:
                        dest = None
                if dest and len(dest) >= 2:
                    print("[NAV_WAIT] Destination: %s" % dest, flush=True)
                    ok = start_voice_navigation(dest)
                    if ok:
                        state = IDLE
                    else:
                        tts_say("请再说一遍")
                        woken_time = time.time()
                elif any(w in text for w in ['取消', '算了', '不要', '不用']):
                    print("[NAV_WAIT] Cancelled", flush=True)
                    tts_say("已取消")
                    state = IDLE
                else:
                    tts_say("请再说一遍")
                    woken_time = time.time()

        except KeyboardInterrupt:
            print("\n[EXIT] Shutting down...", flush=True)
            _asrpro_stop_event.set()
            _asrpro_wake_event.set()
            _asrpro_command_session.clear()
            if asrpro_thread.is_alive():
                asrpro_thread.join(timeout=1.0)
            stop_navigation_only()
            stop_yolo()
            stop_radar_led()
            if hasattr(rec, 'cleanup'): rec.cleanup()
            break
        except AudioCaptureStalled as e:
            print("[AUDIO] %s; reopening the capture stream" % e, flush=True)
            try:
                _reopen_audio_capture_stream(rec, device_index)
            except AudioCaptureStalled as recovery_error:
                print("[AUDIO] %s; retrying on the next listening cycle" %
                      recovery_error, flush=True)
            time.sleep(AUDIO_STREAM_REOPEN_DELAY)
        except Exception as e:
            print("[ERR] %s" % e, flush=True)
            import traceback; traceback.print_exc()
            time.sleep(1)

if __name__ == "__main__":
    main()
