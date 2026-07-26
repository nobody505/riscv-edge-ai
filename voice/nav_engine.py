"""K1 导航引擎 v5 — TW-TTS 硬件语音 + GPS 位置驱动 + 腾讯地图"""
import os, time, json, re, sys, threading, math, socket
import urllib.request, urllib.parse, urllib.error, ssl, base64, hmac, hashlib

# ============ 配置 ============
TENCENT_KEY = os.environ.get('TENCENT_MAP_KEY', '').strip()
CRLF = '\r\n'
BUILD_ID = '20260724-dynamic-location-safe-route-r6'

GPS_POLL_INTERVAL = 3        # GPS 查询间隔(秒)
STEP_THRESHOLD    = 30       # 距当前步终点 < N 米 = 进入下一步
ARRIVAL_THRESHOLD = 30       # GPS 距最终目的地 < N 米 = 真正到达
STATUS_INTERVAL   = 50       # 进度播报最小间隔(秒)
STATIONARY_RADIUS = 8        # 未离开这个半径时，视为位置基本不动
STATIONARY_REPEAT_INTERVAL = 50  # 原地时重复缓存的当前步骤指令，不请求地图API
CRITICAL_STEPS    = [1, 2]   # 前几步即使距离短也播报（刚出发）
LOCATION_SNAPSHOT = '/run/elder-assistant/elder_location_snapshot.json'
LOCATION_MAX_AGE  = 20       # GPS 快照超过20秒即视为失效
WIFI_LOCATION_MAX_AGE = 300  # OneNET WiFi定位超过5分钟即拒绝
API_RESPONSE_MAX_BYTES = 2 * 1024 * 1024
ALLOWED_API_HOSTS = frozenset({'iot-api.heclouds.com', 'apis.map.qq.com'})


def _validate_api_url(url):
    """Return a parsed URL only for an approved HTTPS origin."""
    parsed = urllib.parse.urlsplit(url)
    if (parsed.scheme != 'https' or parsed.hostname not in ALLOWED_API_HOSTS or
            parsed.username is not None or parsed.password is not None or
            parsed.port not in (None, 443)):
        raise ValueError('refusing non-allowlisted API URL')
    return parsed


def _load_https_json(request, timeout):
    """Load bounded JSON only from the two configured HTTPS API hosts."""
    url = request.full_url if isinstance(request, urllib.request.Request) else str(request)
    _validate_api_url(url)
    # Scheme, credentials, port and host are validated immediately above.
    with urllib.request.urlopen(request, timeout=timeout) as response:  # nosec B310
        _validate_api_url(response.geturl())
        payload = response.read(API_RESPONSE_MAX_BYTES + 1)
    if len(payload) > API_RESPONSE_MAX_BYTES:
        raise ValueError('API response exceeds size limit')
    return json.loads(payload.decode('utf-8'))

# ============ 数学工具 ============

def haversine(lat1, lng1, lat2, lng2):
    """球面距离 (米)"""
    R = 6371000
    dlat = math.radians(lat2 - lat1)
    dlng = math.radians(lng2 - lng1)
    a = math.sin(dlat/2)**2 + math.cos(math.radians(lat1)) * math.cos(math.radians(lat2)) * math.sin(dlng/2)**2
    return R * 2 * math.atan2(math.sqrt(a), math.sqrt(1-a))

def fmt_walk_time(minutes):
    m = int(minutes)
    if m >= 60:
        h = m // 60
        r = m % 60
        return '%d小时%d分钟'%(h,r) if r else '%d小时'%h
    return '%d分钟'%m

def decode_polyline(pl):
    """
    解码腾讯路线 API 的差分 polyline。
    格式: [绝对lat, 绝对lng, Δlat, Δlng, Δlat, Δlng, ...]
    Δ 单位: 1/1,000,000 度
    返回: [(lat, lng), ...] 解码后的所有坐标点
    """
    if not pl or len(pl) < 2:
        return []
    points = [(pl[0], pl[1])]
    lat, lng = pl[0], pl[1]
    i = 2
    while i + 1 < len(pl):
        lat += pl[i]     / 1_000_000
        lng += pl[i + 1] / 1_000_000
        points.append((lat, lng))
        i += 2
    return points

# ============ TTS 语音合成（由语音助手注入，避免多线程抢串口）============

_speak = None  # 由外部调用 set_speak_fn() 注入

def set_speak_fn(fn):
    global _speak; _speak = fn

def load_tts():
    print('[TTS] TW-TTS hardware ready (zero load time!)', flush=True)

# ============ 回调 ============

nav_active = False
_nav_generation = 0
_nav_state_lock = threading.Lock()
_on_ended = None
_on_failed = None
_on_started = None

def set_on_nav_end(cb):
    global _on_ended; _on_ended = cb
def set_on_nav_fail(cb):
    global _on_failed; _on_failed = cb
def set_on_nav_started(cb):
    global _on_started; _on_started = cb

def _nav_generation_is_active(generation):
    with _nav_state_lock:
        return nav_active and generation == _nav_generation

def _finish_navigation(generation):
    """结束当前代次；旧线程和已取消线程无权改变新导航状态。"""
    global nav_active
    with _nav_state_lock:
        if generation != _nav_generation or not nav_active:
            return False
        nav_active = False
        return True

def _sleep_while_active(generation, seconds):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if not _nav_generation_is_active(generation):
            return False
        time.sleep(min(0.1, deadline - time.monotonic()))
    return _nav_generation_is_active(generation)

def start_navigation(dn, dlat, dlng):
    global nav_active, _nav_generation
    with _nav_state_lock:
        _nav_generation += 1
        generation = _nav_generation
        nav_active = True
    threading.Thread(
        target=_nav_loop,
        args=(dn, dlat, dlng, generation),
        daemon=True).start()
def stop_navigation():
    global nav_active, _nav_generation
    with _nav_state_lock:
        _nav_generation += 1
        nav_active = False

# ============ GPS ============

def nmea2deg(s):
    if not s or len(s) < 4: return None
    try:
        d = s[-1]; num = float(s[:-1])
        return (int(num/100) + (num - int(num/100)*100)/60.0) * (-1 if d in ('S','W') else 1)
    except: return None

def get_gps():
    """读取 lbs-service 原子快照；导航不再直接打开任何 ML307A 串口。"""
    try:
        with open(LOCATION_SNAPSHOT, 'r', encoding='utf-8') as f:
            data = json.load(f)
        if data.get('version') != 1 or data.get('source') != 'GPS':
            return None
        lat = float(data['lat']); lng = float(data['lng'])
        fix = int(data.get('fix', 0)); captured_at = float(data['captured_at'])
        age = time.time() - captured_at
        if fix < 2 or not (-90 <= lat <= 90 and -180 <= lng <= 180):
            return None
        if age < -5 or age > LOCATION_MAX_AGE:
            return None
        return (lat, lng)
    except (OSError, ValueError, TypeError, KeyError, json.JSONDecodeError):
        pass
    return None

# ============ WiFi 定位兜底 ============

LBS_API_HOST = 'iot-api.heclouds.com'
LBS_PID = os.environ.get('ONENET_PRODUCT_ID', '').strip()
LBS_DN  = os.environ.get('ONENET_DEVICE_NAME', '').strip()
LBS_KEY = os.environ.get('ONENET_PRODUCT_KEY_B64', '').strip()

def get_wifi_location():
    try:
        if not LBS_PID or not LBS_DN or not LBS_KEY:
            return None
        et = str(int(time.time())+3600)
        res = 'products/'+LBS_PID
        org = et+'\nsha1\n'+res+'\n2022-05-01'
        key = base64.b64decode(LBS_KEY)
        sig = base64.b64encode(hmac.new(key,org.encode(),'sha1').digest()).decode()
        token = 'version=2022-05-01&res='+urllib.parse.quote(res,safe='')+'&et='+et+'&method=sha1&sign='+urllib.parse.quote(sig,safe='')
        url = 'https://'+LBS_API_HOST+'/fuse-lbs/latest-wifi-location?product_id='+LBS_PID+'&device_name='+LBS_DN
        req = urllib.request.Request(url)
        req.add_header('Authorization',token)
        d = _load_https_json(req, timeout=8)
        if d.get('code')==0 and d.get('data'):
            item = d['data']
            stamp = str(item.get('at', ''))[:19]
            try:
                located_at = time.mktime(time.strptime(stamp, '%Y-%m-%d %H:%M:%S'))
            except (ValueError, TypeError, OverflowError):
                print('[NAV] Reject WiFi location without valid timestamp', flush=True)
                return None
            age = time.time() - located_at
            if age < -60 or age > WIFI_LOCATION_MAX_AGE:
                print('[NAV] Reject stale WiFi location age=%.0fs' % age, flush=True)
                return None
            return (float(item['lat']),float(item['lon']))
    except: pass
    return None

def get_location():
    """返回最新真实位置：lbs-service GPS 快照优先，OneNET WiFi 兜底。"""
    gps = get_gps()
    if gps:
        return (gps[0], gps[1], 'GPS')
    wifi = get_wifi_location()
    if wifi:
        return (wifi[0], wifi[1], 'WiFi')
    return None

# ============ 地理编码 + 路线 ============

DEFAULT_CITIES = ['北京市','上海市','广州市','深圳市','郑州市','武汉市',
                  '成都市','杭州市','南京市','重庆市','西安市','长沙市']
GEOCODE_RETRY_DELAYS = (0, 1, 2)
GEOCODE_TIMEOUT = 10
_last_geocode_failure = None

class GeocodeRequestDeferred(Exception):
    """网络或地图服务暂不可用；结束本次命令且不误报地点不存在。"""

def _finish_geocode_failure(reason, prompt=None):
    global _last_geocode_failure
    _last_geocode_failure = reason
    if _speak is not None:
        if prompt:
            _speak(prompt)
        raise GeocodeRequestDeferred(reason)
    return None

def geocode(addr):
    global _last_geocode_failure
    _last_geocode_failure = None
    m = re.search(r'([一-鿿]{2,4}(?:市|县|区|省))', addr)
    ci = m.group(1) if m else None
    bnd = [('region('+c+',0)',c) for c in ([ci] if ci else [])+[x for x in DEFAULT_CITIES if x!=ci]]
    network_prompted = False
    for b,lb in bnd:
        params = urllib.parse.urlencode({'keyword':addr.encode('utf-8'),'boundary':b,'page_size':'1','key':TENCENT_KEY})
        for attempt, delay in enumerate(GEOCODE_RETRY_DELAYS, 1):
            if delay:
                time.sleep(delay)
            try:
                req = urllib.request.Request('https://apis.map.qq.com/ws/place/v1/search/?'+params)
                d = _load_https_json(req, timeout=GEOCODE_TIMEOUT)
            except urllib.error.HTTPError as e:
                print('[GEO] service error region=%s http=%s' % (lb, e.code), flush=True)
                return _finish_geocode_failure(
                    'service', '[s0]地图服务暂时不可用，请稍后重试')
            except (urllib.error.URLError, socket.gaierror, socket.timeout,
                    TimeoutError, ConnectionError, ssl.SSLError) as e:
                reason = getattr(e, 'reason', e)
                print('[GEO] network error region=%s attempt=%d/%d: %s' %
                      (lb, attempt, len(GEOCODE_RETRY_DELAYS), str(reason)[:80]), flush=True)
                if not network_prompted and _speak is not None:
                    _speak('[s0]连接网络中')
                    network_prompted = True
                if attempt < len(GEOCODE_RETRY_DELAYS):
                    continue
                return _finish_geocode_failure('network')
            except Exception as e:
                print('[GEO] invalid response region=%s: %s' % (lb, str(e)[:80]), flush=True)
                return _finish_geocode_failure(
                    'service', '[s0]地图服务暂时不可用，请稍后重试')

            if d.get('status') != 0:
                print('[GEO] service status=%s message=%s' %
                      (d.get('status'), str(d.get('message', ''))[:80]), flush=True)
                return _finish_geocode_failure(
                    'service', '[s0]地图服务暂时不可用，请稍后重试')
            if d.get('data'):
                loc = d['data'][0]['location']
                print('[GEO] %s -> %s,%s'%(addr,loc['lat'],loc['lng']),flush=True)
                return (float(loc['lat']),float(loc['lng']))
            break
    _last_geocode_failure = 'not_found'
    return None

def get_route(fla,flo,tla,tlo,mode='walking'):
    params = urllib.parse.urlencode({'from':'%f,%f'%(fla,flo),'to':'%f,%f'%(tla,tlo),'key':TENCENT_KEY})
    try:
        req = urllib.request.Request('https://apis.map.qq.com/ws/direction/v1/walking/?'+params)
        d = _load_https_json(req, timeout=10)
        if d.get('status') == 0 and d.get('result',{}).get('routes'):
            return d['result']['routes'][0]
        print('[ROUTE] Walking API rejected status=%s message=%s' %
              (d.get('status'), str(d.get('message', ''))[:100]), flush=True)
    except Exception as e:
        print('[ROUTE] Walking API failed: %s' % str(e)[:100], flush=True)
    # 腾讯步行接口可能拒绝超长路线。继续提供直达方向兜底，但必须按
    # 腾讯差分 polyline 格式编码终点。旧实现直接放入绝对终点坐标，
    # 解码后终点落在起点约十几米处，导致刚启动就误报到达。
    dist = haversine(fla, flo, tla, tlo)
    walk_min = int(dist / 83.3)
    delta_lat = int(round((tla - fla) * 1_000_000))
    delta_lng = int(round((tlo - flo) * 1_000_000))
    print('[ROUTE] Direct fallback distance=%.0fm time=%dmin' %
          (dist, walk_min), flush=True)
    return {
        'distance': dist,
        'duration': walk_min,
        'steps': [{'instruction': '朝目的地方向行进',
                   'distance': dist,
                   'polyline_idx': [0, 2]}],
        'polyline': [fla, flo, delta_lat, delta_lng],
        '_direct_fallback': True,
    }

# ============ 导航主循环（GPS 位置驱动版）============

def _nav_loop(dest_name, dest_lat, dest_lng, generation):
    print('[NAV] Engine build=%s generation=%d' % (BUILD_ID, generation), flush=True)

    # ── 1. 获取当前位置 ──
    loc = None
    for i in range(8):
        loc = get_location()
        if not _nav_generation_is_active(generation):
            print('[NAV] Cancelled while locating', flush=True)
            return
        if loc: break
        print('[NAV] Waiting for location (%d/8)...' % (i+1), flush=True)
        if not _sleep_while_active(generation, 2):
            print('[NAV] Cancelled while waiting for location', flush=True)
            return
    if not loc:
        print('[NAV] No location', flush=True)
        _speak('[s0]无法获取当前位置')
        if _finish_navigation(generation) and _on_failed:
            _on_failed()
        return

    mlat, mlon, src = loc
    print('[NAV] Start: %.5f,%.5f (%s) -> %s' % (mlat, mlon, src, dest_name), flush=True)

    # ── 2. 路线规划 ──
    route = get_route(mlat, mlon, dest_lat, dest_lng)
    if not _nav_generation_is_active(generation):
        print('[NAV] Cancelled while planning route', flush=True)
        return
    if not route:
        _speak('[s0]无法规划路线')
        if _finish_navigation(generation) and _on_failed:
            _on_failed()
        return

    steps = route.get('steps', [])
    td = route.get('distance', 0)
    tt = route.get('duration', 0)
    print('[NAV] %d steps, %.0fm, %s' % (len(steps), td, fmt_walk_time(tt)), flush=True)

    # ── 3. 解码 polyline → 提取每步终点坐标 ──
    all_points = decode_polyline(route.get('polyline', []))
    step_targets = []  # [(lat, lng), ...] 每步终点
    for st in steps:
        idx_end = st['polyline_idx'][1]
        pt_idx = idx_end // 2  # polyline 平铺索引 → 坐标点索引
        if pt_idx < len(all_points):
            step_targets.append(all_points[pt_idx])
        elif all_points:
            step_targets.append(all_points[-1])  # 兜底：总终点
        else:
            step_targets.append((dest_lat, dest_lng))

    # ── 4. 开场播报 ──
    _speak('[s0]距离%s约%d米，预计需要%s' % (dest_name, int(td), fmt_walk_time(tt)))
    if not _nav_generation_is_active(generation):
        print('[NAV] Cancelled after route summary', flush=True)
        return

    # 开场播报完毕，通知语音助手可以启动摄像头检测了
    if _on_started:
        _on_started()
    if not _nav_generation_is_active(generation):
        print('[NAV] Cancelled while starting travel detection', flush=True)
        return

    # ── 5. 位置驱动逐步播报 ──
    cur_step = 0
    last_announce_time = 0.0
    last_announced_step = -1
    last_remain_dist = td
    gps_stale_count = 0
    stationary_anchor = None
    stationary_anchor_src = None
    stationary_since = None

    arrived = False
    while _nav_generation_is_active(generation) and cur_step < len(steps):
        step = steps[cur_step]
        instr = step.get('instruction', '').strip()
        step_dist = step.get('distance', 0)
        target_lat, target_lng = step_targets[cur_step]

        # 逐步推进和到达判定只信任 lbs-service 的新鲜 GPS 快照。
        # WiFi 定位可作为真实导航起点和低精度位置参考，但精度不足以
        # 支撑 30 米级转向/到达判断。
        new_gps = get_gps()
        if not _nav_generation_is_active(generation):
            print('[NAV] Cancelled while refreshing location', flush=True)
            return
        position_valid = False
        if new_gps:
            mlat, mlon = new_gps
            src = 'GPS'
            position_valid = True
            gps_stale_count = 0
            # 到当前步终点的距离
            dist_to_step = haversine(mlat, mlon, target_lat, target_lng)
            # 到最终目的地的剩余距离
            remain_dist = haversine(mlat, mlon, dest_lat, dest_lng)
        else:
            gps_stale_count += 1
            dist_to_step = 99999
            remain_dist = -1
            # WiFi 定位精度不够做步进判断，只能靠惯性估计
            wifi_loc = get_wifi_location()
            if wifi_loc:
                mlat, mlon = wifi_loc
                src = 'WiFi'
                position_valid = True

        now = time.monotonic()
        if position_valid:
            current_pos = (mlat, mlon)
            if (stationary_anchor is None or stationary_anchor_src != src or
                    haversine(stationary_anchor[0], stationary_anchor[1], mlat, mlon) > STATIONARY_RADIUS):
                stationary_anchor = current_pos
                stationary_anchor_src = src
                stationary_since = now
            stationary_for = now - stationary_since
        else:
            # 没有新位置时不把“定位丢失”误判成原地不动。
            stationary_for = 0.0
        progress = '%d/%d' % (cur_step + 1, len(steps))

        # 判断是否该切下一步。最后一步必须用 GPS 到最终目的地的距离
        # 单独确认，不能只相信路线 step target，也不能使用 WiFi 触发到达。
        if cur_step == len(steps) - 1:
            step_complete = (
                src == 'GPS' and remain_dist >= 0 and
                remain_dist < ARRIVAL_THRESHOLD
            )
        else:
            step_complete = (src == 'GPS' and dist_to_step < STEP_THRESHOLD)
        if step_complete:
            if cur_step == len(steps) - 1:
                arrived = True
                break
            print('[NAV] Step %s done (%.0fm to target), advancing' % (progress, dist_to_step), flush=True)
            last_announce_time = now
            _speak('[s0]' + instr)
            if not _nav_generation_is_active(generation):
                print('[NAV] Cancelled after step announcement', flush=True)
                return
            cur_step += 1
            last_announced_step = cur_step - 1
            stationary_anchor = None
            stationary_since = None
            continue

        # 地图API只在路线规划时调用一次；这里始终复用 steps 中缓存的 instruction。
        new_step = (last_announced_step != cur_step)
        stationary_repeat = (
            stationary_for >= STATIONARY_REPEAT_INTERVAL and
            now - last_announce_time >= STATIONARY_REPEAT_INTERVAL
        )
        status_due = (now - last_announce_time >= STATUS_INTERVAL)

        if new_step or stationary_repeat or status_due:
            # 进度播报：包含剩余总距
            msg_parts = [instr]
            if remain_dist > 0 and abs(remain_dist - last_remain_dist) > 50:
                msg_parts.append('，还剩%d米' % int(remain_dist))
                last_remain_dist = remain_dist
            # 在同步TTS之前记时，确保长句播放耗时不会额外叠加到10秒周期上。
            last_announce_time = now
            last_announced_step = cur_step
            reason = 'new-step' if new_step else ('stationary-repeat' if stationary_repeat else 'status')
            _speak('[s0]' + ''.join(msg_parts))
            if not _nav_generation_is_active(generation):
                print('[NAV] Cancelled after progress announcement', flush=True)
                return
            print('[NAV] Step %s | dist=%.0fm remain=%.0fm | %s | %s' % (
                progress, dist_to_step, remain_dist, reason, instr), flush=True)

        # 如果 GPS 长时间丢失
        if gps_stale_count > 10:
            print('[NAV] GPS lost for %d polls, trying WiFi...' % gps_stale_count, flush=True)
            gps_stale_count = 0

        if not _sleep_while_active(generation, GPS_POLL_INTERVAL):
            print('[NAV] Cancelled between location polls', flush=True)
            return

    # ── 6. 到达 ──
    if not arrived or not _nav_generation_is_active(generation):
        print('[NAV] Stopped without arrival', flush=True)
        return
    print('[NAV] Arrived!', flush=True)
    _speak('[s0]已到达目的地')
    if _finish_navigation(generation) and _on_ended:
        try: _on_ended()
        except: pass

# ============ 直接测试入口 ============
if __name__ == '__main__':
    dest = sys.argv[1] if len(sys.argv) > 1 else '河南工业大学'

    print('=' * 50)
    print('  TW-TTS 导航测试 - %s' % dest)
    print('  模式: GPS 位置驱动 + TW-TTS 硬件语音')
    print('=' * 50, flush=True)

    # 测试 TTS
    print('\n[TEST] TW-TTS 语音测试...', flush=True)
    _speak('[s0]导航测试开始')

    # 获取位置
    print('[TEST] 获取位置...', flush=True)
    loc = None
    for i in range(10):
        loc = get_location()
        if loc: break
        print('  retry %d/10...' % (i+1), flush=True)
        time.sleep(2)
    if not loc:
        print('[FAIL] 无法获取位置！', flush=True)
        _speak('[s0]无法获取当前位置')
        sys.exit(1)

    mlat, mlon, src = loc
    print('[OK] %s (%.5f, %.5f)' % (src, mlat, mlon), flush=True)

    # 搜索目的地
    print('[TEST] 搜索: %s...' % dest, flush=True)
    geo = geocode(dest)
    if not geo:
        if _last_geocode_failure == 'network':
            print('[FAIL] 地理编码网络异常！', flush=True)
            _speak('[s0]连接网络中')
        elif _last_geocode_failure == 'service':
            print('[FAIL] 腾讯地图服务异常！', flush=True)
            _speak('[s0]地图服务暂时不可用，请稍后重试')
        else:
            print('[FAIL] 找不到！', flush=True)
            _speak('[s0]找不到' + dest)
        sys.exit(1)
    dlat, dlng = geo
    print('[OK] %s (%.5f, %.5f)' % (dest, dlat, dlng), flush=True)

    # 路线
    print('[TEST] 规划路线...', flush=True)
    route = get_route(mlat, mlon, dlat, dlng)
    if not route:
        print('[FAIL] 无法规划路线！', flush=True)
        _speak('[s0]无法规划路线')
        sys.exit(1)

    steps = route.get('steps', [])
    td = route.get('distance', 0)
    tt = route.get('duration', 0)

    # 编解码 polyline
    all_points = decode_polyline(route.get('polyline', []))
    print('[OK] %d steps, %.0fm, %s, %d polyline points' % (
        len(steps), td, fmt_walk_time(tt), len(all_points)), flush=True)

    # 提取步终点
    step_targets = []
    for st in steps:
        idx_end = st['polyline_idx'][1]
        pt_idx = idx_end // 2
        step_targets.append(all_points[pt_idx] if pt_idx < len(all_points) else (dlat, dlng))

    # 开场
    _speak('[s0]已启动导航，全程约%d米，步行约%s' % (int(td), fmt_walk_time(tt)))
    time.sleep(1)

    if _on_started: _on_started()

    # 逐步播报（位置驱动）
    cur = 0
    last_announce = 0
    last_remain = td

    print('\n[NAV] 开始导航...\n', flush=True)

    while cur < len(steps):
        step = steps[cur]
        instr = step.get('instruction', '').strip()
        step_dist = step.get('distance', 0)
        target_lat, target_lng = step_targets[cur]

        new_loc = get_location()
        if new_loc:
            mlat, mlon, src = new_loc
            d2step = haversine(mlat, mlon, target_lat, target_lng)
            remain = haversine(mlat, mlon, dlat, dlng)
        else:
            d2step = 99999
            remain = -1

        now = time.time()
        prog = '%d/%d' % (cur + 1, len(steps))

        # 步进判断
        if d2step < STEP_THRESHOLD and cur < len(steps) - 1:
            print('[%s] Arrived at step target (%.0fm), advancing...' % (prog, d2step), flush=True)
            _speak('[s0]' + instr)
            cur += 1
            last_announce = now
            continue

        should = (cur == 0 or step_dist >= 200 or cur == len(steps) - 1
                  or now - last_announce > STATUS_INTERVAL or cur in CRITICAL_STEPS)

        if should and now - last_announce > 3:
            msg = instr
            if remain > 0 and abs(remain - last_remain) > 50:
                msg += '，还剩%d米' % int(remain)
                last_remain = remain
            print('[%s] d=%.0fm rem=%.0fm: %s' % (prog, d2step, remain, instr), flush=True)
            _speak('[s0]' + msg)
            last_announce = now

        time.sleep(GPS_POLL_INTERVAL)

    _speak('[s0]已到达目的地')
    print('\n' + '=' * 50)
    print('  导航测试完成！')
    print('=' * 50)
