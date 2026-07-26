"""K1 定位服务 v9 — GPS优先 + WiFi兜底 + MQTT心跳 + 共享串口锁"""
import serial, time, json, hashlib, base64, hmac, urllib.parse, glob, os, fcntl, ssl, stat
from contextlib import contextmanager
import paho.mqtt.client as mqtt

PID = os.environ.get('ONENET_PRODUCT_ID', '').strip()
DN  = os.environ.get('ONENET_DEVICE_NAME', '').strip()
DK  = os.environ.get('ONENET_DEVICE_KEY_B64', '').strip()
CRLF = '\r\n'
CHECK_INTERVAL = 3    # GPS 查询间隔
WIFI_COOLDOWN  = 60   # WiFi 扫描最小间隔（真实秒数）
PORT_RESCAN_INTERVAL = 3
ML307A_AT_GLOB = '/dev/serial/by-id/*ML307A*if02*'
RUNTIME_DIR = '/run/elder-assistant'
ML307A_AT_LOCK = os.path.join(RUNTIME_DIR, 'ml307a_at.lock')
LOCATION_SNAPSHOT = os.path.join(RUNTIME_DIR, 'elder_location_snapshot.json')
BUILD_ID = '20260718-indoor-wifi-r2'
MQTT_HOST = 'mqtts.heclouds.com'
MQTT_PORT = 8883
MQTT_CA_FILE = '/etc/elder-assistant/onenet-mqtt-ca.pem'
MQTT_CERT_SHA256 = 'e08b69e2e3f8d5a67084a557eb2c1486c9a5b6a3fabd2eab6051a03077916930'

def write_location_snapshot(lat, lng, fix, source='GPS'):
    """原子发布最新真实定位，供导航读取；导航不再直接争抢 ML307A AT 口。"""
    data = {
        'version': 1,
        'source': source,
        'lat': float(lat),
        'lng': float(lng),
        'fix': int(fix),
        'captured_at': time.time(),
        'writer': 'lbs-service',
        'build': BUILD_ID,
    }
    tmp = LOCATION_SNAPSHOT + '.tmp.%d' % os.getpid()
    try:
        with open(tmp, 'w', encoding='utf-8') as f:
            json.dump(data, f, ensure_ascii=True, separators=(',', ':'))
            f.flush()
            os.fsync(f.fileno())
        # Group access is required for the private cross-service IPC.
        os.chmod(tmp, 0o660)  # nosec B103
        os.replace(tmp, LOCATION_SNAPSHOT)
    except Exception as e:
        print('[WARN] 定位快照写入失败: %s' % str(e)[:80], flush=True)
        try:
            os.unlink(tmp)
        except OSError:
            pass

def _open_ml307a_at_lock():
    flags = os.O_RDWR | getattr(os, 'O_CLOEXEC', 0) | getattr(os, 'O_NOFOLLOW', 0)
    lock_fd = os.open(ML307A_AT_LOCK, flags)
    metadata = os.fstat(lock_fd)
    if (not stat.S_ISREG(metadata.st_mode) or metadata.st_uid != 0 or
            metadata.st_mode & 0o007):
        os.close(lock_fd)
        raise PermissionError('unsafe ML307A lock ownership or mode')
    return lock_fd

@contextmanager
def ml307a_at_lock():
    """短信与定位只串行使用AT口；MQTT线程不停止，OneNET连接保持。"""
    lock_fd = _open_ml307a_at_lock()
    try:
        try:
            # Group access is required for the private cross-service IPC.
            os.fchmod(lock_fd, 0o660)  # nosec B103
        except PermissionError:
            pass
        fcntl.flock(lock_fd, fcntl.LOCK_EX)
        yield
    finally:
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_UN)
        finally:
            os.close(lock_fd)

def find_port():
    # 只探测ML307A的AT接口。遍历所有ttyUSB会打开TTS串口和ML307A的
    # 其他接口，并可能在模组重枚举期间导致再次掉线。
    with ml307a_at_lock():
        for dev in sorted(glob.glob(ML307A_AT_GLOB)):
            try:
                s = serial.Serial(dev, 115200, timeout=0.5, write_timeout=1)
                s.write(b'AT\r\n')
                time.sleep(0.3)
                r = s.read(200).decode(errors='replace')
                s.close()
                if 'OK' in r: return dev
            except: pass
    return None

def init_gps(port):
    def _gps_init(s):
        s.write(('AT+MGNSS=1' + CRLF).encode())
        time.sleep(1)
        r = s.read(300).decode(errors='replace')
        return ('OK' in r or '952' in r)
    return safe_serial(port, _gps_init, 'gps_init')

def make_token():
    if not PID or not DN or not DK:
        raise RuntimeError('OneNET configuration is incomplete; check /etc/elder-assistant/elder.env')
    et = str(int(time.time()) + 86400)
    res = 'products/' + PID + '/devices/' + DN
    k = base64.b64decode(DK)
    src = et + '\nsha1\n' + res + '\n2018-10-31'
    sig = base64.b64encode(hmac.new(k, src.encode(), hashlib.sha1).digest()).decode()
    return 'version=2018-10-31&res=products%2F'+PID+'%2Fdevices%2F'+DN+'&et='+et+'&method=sha1&sign='+urllib.parse.quote(sig,safe='')

TOKEN = make_token()
LAST_TOKEN_TIME = time.time()

def refresh_token():
    global TOKEN, LAST_TOKEN_TIME
    if time.time() - LAST_TOKEN_TIME > 3600:
        TOKEN = make_token()
        LAST_TOKEN_TIME = time.time()

def nmea2deg(s):
    if not s or len(s) < 4: return None
    try:
        d = s[-1]; num = float(s[:-1])
        deg = int(num/100); minutes = num - deg*100
        r = deg + minutes/60.0
        return -r if d in ('S','W') else r
    except: return None

def is_global_unicast_bssid(mac):
    """OneNET WiFi LBS cannot resolve randomized/local or multicast BSSIDs."""
    try:
        parts = mac.split(':')
        return (len(parts) == 6 and all(len(p) == 2 for p in parts)
                and not (int(parts[0], 16) & 0x03))
    except (TypeError, ValueError):
        return False

def safe_serial(port, fn, desc='op'):
    """每次独立开关串口，异常自动恢复"""
    s = None
    try:
        with ml307a_at_lock():
            s = serial.Serial(port, 115200, timeout=2, write_timeout=2)
            try:
                return fn(s)
            finally:
                s.close()
                s = None
    except Exception as e:
        print(' !%s' % desc, end='', flush=True)
        return None
    finally:
        if s:
            try: s.close()
            except: pass

def gps_query(port):
    def _do(s):
        s.write(('AT+MGNSSLOC' + CRLF).encode())
        time.sleep(1.5)
        r = s.read(500).decode(errors='replace')
        for line in r.split('\n'):
            if '+MGNSSLOC:' in line:
                p = line.split(':')[1].strip().split(',')
                if len(p) >= 6:
                    fix = int(p[5]) if p[5].strip().isdigit() else 0
                    if fix >= 2:
                        lat = nmea2deg(p[1]); lng = nmea2deg(p[2])
                        if lat and lng: return (lat, lng, fix)
                    return (None, None, fix)
        return (None, None, 0)
    return safe_serial(port, _do, 'gps') or (None, None, 0)

def wifi_scan(port):
    def _do(s):
        # 清理 NMEA
        s.write(('AT+MGNSSCFG="nmea/mask",0' + CRLF).encode())
        time.sleep(0.3); s.read(500)
        # 配置 + 启动
        s.write(('AT+MWIFISCANCFG="max",8' + CRLF).encode())
        time.sleep(0.3); s.read(200)
        s.write(('AT+MWIFISCANSTART=3' + CRLF).encode())
        time.sleep(1); s.read(200)
        time.sleep(6)  # 等扫描完成
        s.write(('AT+MWIFISCANQUERY' + CRLF).encode())
        time.sleep(2)
        r = s.read(2000).decode(errors='replace')

        macs = []
        for line in r.split('\n'):
            line = line.strip()
            if line and line[0].isdigit() and ',"' in line:
                p = [x.strip() for x in line.split(',')]
                if len(p) >= 5 and len(p[2]) > 10:
                    mac = p[2].strip('"')
                    try: rssi = int(p[4])
                    except: rssi = -99
                    mac = (mac[0:2]+':'+mac[2:4]+':'+mac[4:6]+':'
                           +mac[6:8]+':'+mac[8:10]+':'+mac[10:12]).upper()
                    if is_global_unicast_bssid(mac):
                        macs.append(mac+','+str(rssi))
        return ('|'.join(macs[:10]), len(macs))
    result = safe_serial(port, _do, 'wifi') or ('', 0)
    # WiFi扫描关了NMEA → 立即重新打开GPS确保下次能定位
    def _re_enable_gps(s):
        s.write(('AT+MGNSSCFG="nmea/mask",1' + CRLF).encode())
        time.sleep(0.3)
        s.write(('AT+MGNSS=1' + CRLF).encode())
        time.sleep(0.5)
        s.read(200)
    safe_serial(port, _re_enable_gps, 'gps_re')
    return result

# ====== 主程序 ======
print('[SERVICE] lbs v9 启动（ML307A共享串口锁） build=%s' % BUILD_ID, flush=True)

PORT = find_port()
if PORT:
    ok = init_gps(PORT)
    print('[SERVICE] AT=%s GPS=%s' % (PORT, 'OK' if ok else 'FAIL'), flush=True)
else:
    print('[WARN] 无ML307A，心跳模式；将每%ds自动重扫' % PORT_RESCAN_INTERVAL,
          flush=True)

def verify_mqtt_peer_certificate(tls_socket):
    """Require the exact DER certificate audited for the OneNET MQTT endpoint."""
    certificate = tls_socket.getpeercert(binary_form=True)
    if not certificate:
        raise ssl.SSLError('OneNET MQTT peer did not provide a certificate')
    fingerprint = hashlib.sha256(certificate).hexdigest()
    if not hmac.compare_digest(fingerprint, MQTT_CERT_SHA256):
        raise ssl.SSLError('OneNET MQTT peer certificate fingerprint mismatch')


# MQTT
client = mqtt.Client(client_id=DN, protocol=mqtt.MQTTv311)
client.username_pw_set(PID, TOKEN)
if not os.path.isfile(MQTT_CA_FILE):
    raise RuntimeError('OneNET pinned MQTT certificate is missing')
mqtt_tls = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
mqtt_tls.minimum_version = ssl.TLSVersion.TLSv1_2
# OneNET 8883 uses a self-signed leaf whose CN is not the DNS hostname. Loading
# only that leaf pins the server identity even though conventional hostname
# matching cannot be used.
mqtt_tls.check_hostname = False
mqtt_tls.verify_mode = ssl.CERT_REQUIRED
mqtt_tls.load_verify_locations(cafile=MQTT_CA_FILE)
client.tls_set_context(mqtt_tls)
client.connect(MQTT_HOST, MQTT_PORT, keepalive=120)
peer_socket = client.socket()
if peer_socket is None:
    raise ssl.SSLError('OneNET MQTT TLS socket is unavailable after connect')
try:
    verify_mqtt_peer_certificate(peer_socket)
except Exception:
    client.disconnect()
    raise
client.loop_start()
time.sleep(2)
print('[SERVICE] MQTT已连接  OneNET在线', flush=True)
print('=' * 45, flush=True)

count = 0
was_gps = None
last_wifi_at = 0            # 首次启动立即扫描
last_port_scan = 0

try:
    while True:
        count += 1
        refresh_token()
        topic = '$sys/' + PID + '/' + DN + '/thing/property/post'

        lat = lng = None; fix = 0; label = ''
        n_aps = 0

        # 短信发送会独占串口，ML307A也可能短暂重新枚举。by-id链接恢复后
        # 自动接回定位，不要求人工重启服务。
        if PORT and not os.path.exists(PORT):
            print('[WARN] ML307A AT端口离线，等待重枚举', flush=True)
            PORT = None
        if not PORT and time.time() - last_port_scan >= PORT_RESCAN_INTERVAL:
            last_port_scan = time.time()
            recovered_port = find_port()
            if recovered_port:
                PORT = recovered_port
                ok = init_gps(PORT)
                last_wifi_at = 0
                print('[SERVICE] ML307A恢复 AT=%s GPS=%s' %
                      (PORT, 'OK' if ok else 'FAIL'), flush=True)

        if PORT:
            # 先查 GPS（快，~2s）
            lat, lng, fix = gps_query(PORT)

        if lat and lng and fix >= 2:
            # === GPS 模式 ===
            write_location_snapshot(lat, lng, fix, 'GPS')
            payload = json.dumps({
                'id': str(count), 'version': '1.0',
                'params': {
                    'GPS_LAT': {'value': lat}, 'GPS_LNG': {'value': lng},
                    'LOC_TYPE': {'value': 'GPS'}
                }
            })
            client.publish(topic, payload, qos=1)
            label = 'GPS  %.5f,%.5f  fix=%d' % (lat, lng, fix)

        elif PORT and time.time() - last_wifi_at >= WIFI_COOLDOWN:
            # === WiFi 兜底 ===
            macs_str, n_aps = wifi_scan(PORT)
            if n_aps:
                last_wifi_at = time.time()
            else:
                # 模组刚恢复时第一次扫描偶尔为空，9秒后重试，不能把空定位
                # 当成一次成功扫描并进入约3分钟的正常冷却。
                last_wifi_at = time.time() - WIFI_COOLDOWN + 9
            payload = json.dumps({
                'id': str(count), 'version': '1.0',
                'params': {
                    '$OneNET_LBS_WIFI': {'value': {'macs': macs_str if macs_str else ''}},
                    'LOC_TYPE': {'value': 'WiFi'}
                }
            })
            client.publish(topic, payload, qos=1)
            label = 'WiFi %d APs' % n_aps
        else:
            # 心跳
            payload = json.dumps({
                'id': str(count), 'version': '1.0',
                'params': {'LOC_TYPE': {'value': 'Heartbeat'}}
            })
            client.publish(topic, payload, qos=1)
            cooldown = int(max(0, WIFI_COOLDOWN - (time.time() - last_wifi_at)))
            label = '心跳 (WiFi冷却 %ds)' % cooldown

        is_gps = bool(lat and lng and fix >= 2)
        if is_gps != was_gps:
            prev = {True: 'GPS', False: 'WiFi', None: '启动'}[was_gps]
            curr = 'GPS' if is_gps else 'WiFi'
            print('\n  *** 切换: %s → %s ***' % (prev, curr), flush=True)

        print('[%d] %-40s' % (count, label), flush=True)
        was_gps = is_gps

        if is_gps:
            time.sleep(CHECK_INTERVAL)
        else:
            time.sleep(CHECK_INTERVAL)

except KeyboardInterrupt:
    print('\n[SERVICE] 已停止', flush=True)
finally:
    client.loop_stop()
    client.disconnect()
