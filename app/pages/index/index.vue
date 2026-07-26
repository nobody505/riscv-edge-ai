<template>
  <view class="page">
    <!-- 自定义渐变标题栏 -->
    <view class="nav-bar" :style="{ paddingTop: statusBarHeight + 'px' }">
      <text class="nav-title">时空守护</text>
    </view>

    <!-- 地图卡片容器 -->
    <view class="map-card">
      <view class="map-frame" :style="{ height: mapHeight + 'px' }">
        <map
          id="mainMap"
          class="map"
          style="width: 100%; height: 100%;"
          :latitude="device.lat"
          :longitude="device.lng"
          :markers="markers"
          :scale="15"
          show-location
        ></map>
      </view>
    </view>

    <!-- 卡片区域: 可滚动 -->
    <scroll-view class="cards-area" scroll-y="true" :style="{ height: scrollHeight + 'px' }">
      <!-- 设备状态 -->
      <view class="card">
        <view class="card-header">
          <image class="card-icon" src="/static/icons/device.svg" mode="aspectFit"></image>
          <text class="card-title">设备状态</text>
        </view>
        <view class="card-row">
          <text class="label">连接状态</text>
          <text class="value online" v-if="device.online">● 在线</text>
          <text class="value offline" v-else>● 离线</text>
        </view>
        <view class="card-row">
          <text class="label">最后更新</text>
          <text class="value" :class="{ 'placeholder': !device.lastUpdate }">{{ device.lastUpdate || '—— 无 ——' }}</text>
        </view>
      </view>

      <!-- WiFi 定位 -->
      <view class="card">
        <view class="card-header">
          <image class="card-icon" src="/static/icons/location.svg" mode="aspectFit"></image>
          <text class="card-title">当前位置</text>
        </view>
        <view class="card-row">
          <text class="label">纬度</text>
          <text class="value" :class="{ 'placeholder': !device.lat }">{{ device.lat ? device.lat.toFixed(5) : '—— 无 ——' }}</text>
        </view>
        <view class="card-row">
          <text class="label">经度</text>
          <text class="value" :class="{ 'placeholder': !device.lng }">{{ device.lng ? device.lng.toFixed(5) : '—— 无 ——' }}</text>
        </view>
        <view class="card-row">
          <text class="label">定位方式</text>
          <text class="value" :class="{ 'placeholder': !device.locType }">{{ device.locType || '—— 无 ——' }}</text>
        </view>
      </view>

      <view style="height: 40rpx;"></view>
    </scroll-view>

    <!-- 点击刷新波纹扩散层 -->
    <view v-if="rippleShow" class="ripple-overlay">
      <view class="ripple-circle"
            :style="{ left: rippleX + 'px', top: rippleY + 'px' }"
            :class="{ 'ripple-expand': rippleAnimating }">
      </view>
    </view>

    <!-- Toast -->
    <view v-if="toastVisible" class="toast">{{ toastText }}</view>

    <!-- 悬浮刷新按钮 -->
    <view class="float-btn"
          :class="{ 'btn-locked': rippleAnimating }"
          :style="{ left: btnX + 'px', top: btnY + 'px' }"
          @touchstart="onTouchStart"
          @touchmove.stop.prevent="onTouchMove"
          @touchend="onTouchEnd">
      <image :class="{ rotating: rotating }" src="/static/icons/refresh.svg" mode="aspectFit" style="width: 44rpx; height: 44rpx;"></image>
    </view>
  </view>
</template>

<script>
import CONFIG from '../../config.js'

// ==================== OneNET API 配置 ====================
const ONENET_PID = CONFIG.onenetProductId
const ONENET_DN = CONFIG.onenetDeviceName
const ONENET_KEY_B64 = CONFIG.onenetProductKeyB64

// 用户提供的坐标已经是腾讯地图 GCJ-02 坐标，直接用于 App 地图和设备标记。
// 运行时不再让 OneNET/GPS/WiFi 响应覆盖这个位置。
const FIXED_DEVICE_LOCATION = Object.freeze({
  lng: CONFIG.fixedDeviceLocation.lng,
  lat: CONFIG.fixedDeviceLocation.lat,
  locType: CONFIG.fixedDeviceLocation.locType,
})

// 设备列表 API 检查在线状态
const ONENET_DEVICE_URL =
  'https://iot-api.heclouds.com/devices' +
  '?product_id=' + encodeURIComponent(ONENET_PID)

// 查询设备物模型属性值（LOC_TYPE, GPS_LAT, GPS_LNG）
const ONENET_PROPS_URL =
  'https://iot-api.heclouds.com/thingmodel/query-device-property' +
  '?product_id=' + encodeURIComponent(ONENET_PID) +
  '&device_name=' + encodeURIComponent(ONENET_DN)

// ==================== crypto-js ====================
import CryptoJS from 'crypto-js'

function generateOneNetToken() {
  const version = '2022-05-01'
  const res = 'products/' + ONENET_PID
  const et = Math.floor(Date.now() / 1000) + 3600
  const signSrc = et + '\nsha1\n' + res + '\n' + version
  const key = CryptoJS.enc.Base64.parse(ONENET_KEY_B64)
  const sig = CryptoJS.HmacSHA1(signSrc, key).toString(CryptoJS.enc.Base64)
  return 'version=' + version
    + '&res=' + encodeURIComponent(res)
    + '&et=' + et
    + '&method=sha1'
    + '&sign=' + encodeURIComponent(sig)
}

function updateMarker(self, lat, lng) {
  self.markers = [{
    id: Date.now(),
    latitude: lat,
    longitude: lng,
    width: 36, height: 36,
    iconPath: '/static/icons/location.svg',
    callout: { content: 'K1', display: 'ALWAYS', fontSize: 12, borderRadius: 8, padding: 6 }
  }]
}

function applyFixedDeviceLocation(self) {
  self.device.lat = FIXED_DEVICE_LOCATION.lat
  self.device.lng = FIXED_DEVICE_LOCATION.lng
  self.device.locType = FIXED_DEVICE_LOCATION.locType
  updateMarker(self, FIXED_DEVICE_LOCATION.lat, FIXED_DEVICE_LOCATION.lng)
}

function propertyListToMap(items) {
  var result = Object.create(null)
  if (!Array.isArray(items)) return result
  for (var i = 0; i < items.length; i++) {
    var item = items[i]
    if (item && item.identifier === 'FALL_ALERT') result.FALL_ALERT = item
  }
  return result
}

function cleanAlertText(value, fallback, maxLength) {
  var text = String(value == null ? '' : value)
    .replace(/[\u0000-\u001f\u007f]/g, ' ')
    .replace(/\s+/g, ' ')
    .trim()
  if (!text) text = fallback
  return text.slice(0, maxLength)
}

function boundedCoordinate(value, min, max) {
  var number = Number(value)
  return Number.isFinite(number) && number >= min && number <= max
    ? number
    : null
}

function formatLocalClock(date) {
  function twoDigits(value) {
    return value < 10 ? '0' + value : '' + value
  }
  return twoDigits(date.getHours()) + ':'
    + twoDigits(date.getMinutes()) + ':'
    + twoDigits(date.getSeconds())
}

function normalizeOnlineStatus(value) {
  if (typeof value === 'string') {
    var normalized = value.toLowerCase()
    return normalized === 'true' || normalized === '1' || normalized === 'online'
  }
  return value === true || value === 1
}

export default {
  data() {
    return {
      device: {
        online: false,
        lat: FIXED_DEVICE_LOCATION.lat,
        lng: FIXED_DEVICE_LOCATION.lng,
        lastUpdate: '',
        locType: FIXED_DEVICE_LOCATION.locType,
      },
      alerts: [],
      markers: [],
      scrollHeight: 400,
      statusBarHeight: 0,
      mapHeight: 200,
      rotating: false,
      btnX: 0,
      btnY: 0,
      dragStartX: 0,
      dragStartY: 0,
      dragBtnX: 0,
      dragBtnY: 0,
      isDragging: false,
      dragged: false,
      rippleShow: false,
      rippleAnimating: false,
      rippleX: 0,
      rippleY: 0,
      toastVisible: false,
      toastText: '',
      deviceStatusRequestPending: false,
      }
  },

  onLoad() {
    const info = uni.getSystemInfoSync()
    this.statusBarHeight = info.statusBarHeight || 20
    const navHeight = this.statusBarHeight + 36
    this.mapHeight = info.windowHeight * 0.26
    this.scrollHeight = info.windowHeight - navHeight - this.mapHeight - 96
    this.btnX = info.windowWidth - 60
    this.btnY = info.windowHeight - 160
    applyFixedDeviceLocation(this)
    this.fetchData()
    this.autoTimer = setInterval(() => { this.fetchData() }, 5000)
  },

  onUnload() {
    if (this.autoTimer) clearInterval(this.autoTimer)
  },

  methods: {
    onTouchStart(e) {
      const t = e.touches[0]
      this.dragStartX = t.clientX
      this.dragStartY = t.clientY
      this.dragBtnX = this.btnX
      this.dragBtnY = this.btnY
      this.dragged = false
    },
    onTouchMove(e) {
      const t = e.touches[0]
      const dx = t.clientX - this.dragStartX
      const dy = t.clientY - this.dragStartY
      if (Math.abs(dx) > 6 || Math.abs(dy) > 6) {
        this.dragged = true
        const sw = uni.getSystemInfoSync().windowWidth
        const sh = uni.getSystemInfoSync().windowHeight
        const size = 44
        this.btnX = Math.max(0, Math.min(sw - size, this.dragBtnX + dx))
        this.btnY = Math.max(0, Math.min(sh - size, this.dragBtnY + dy))
      }
    },
    onTouchEnd(e) {
      if (this.dragged) return
      if (this.rippleAnimating) return
      const touch = (e.changedTouches || e.touches || [{}])[0]
      this.rippleX = touch.clientX || this.btnX + 22
      this.rippleY = touch.clientY || this.btnY + 22
      this.rippleShow = true
      this.rippleAnimating = true
      this.rotating = true
      // “最后更新”表示用户本次手动刷新的手机本地时间，和网络结果解耦。
      this.device.lastUpdate = formatLocalClock(new Date())
      this.fetchData()
      setTimeout(() => {
        this.rippleAnimating = false
        setTimeout(() => { this.rippleShow = false }, 300)
        this.rotating = false
        this.showToast('刷新成功')
      }, 600)
    },

    showToast(text) {
      this.toastText = text
      this.toastVisible = true
      setTimeout(() => { this.toastVisible = false }, 1200)
    },

    // ==================== OneNET 数据获取 ====================

    fetchData() {
      var self = this

      // 位置是用户指定的强制展示值；每轮刷新重新写入，防止任何异步响应覆盖。
      applyFixedDeviceLocation(self)

      // 1. 只有设备列表接口拥有连接状态；请求失败时保留上一次可信结果。
      //    设备状态请求最长 8 秒，未结束时跳过下一轮，避免旧响应覆盖新状态。
      if (!self.deviceStatusRequestPending) {
        self.deviceStatusRequestPending = true
        uni.request({
          url: ONENET_DEVICE_URL,
          method: 'GET',
          timeout: 8000,
          header: { 'Authorization': generateOneNetToken() },
          success: function(res) {
            try {
              var json = (typeof res.data === 'string') ? JSON.parse(res.data) : res.data
              if (json.errno === 0 && json.data && json.data.devices) {
                var devs = json.data.devices
                for (var i = 0; i < devs.length; i++) {
                  if (devs[i].title === ONENET_DN || devs[i].auth_info === ONENET_DN) {
                    self.device.online = normalizeOnlineStatus(devs[i].online)
                    break
                  }
                }
              }
            } catch(e) {}
          },
          complete: function() {
            self.deviceStatusRequestPending = false
          }
        })
      }

      // 2. 查询物模型属性。GPS/WiFi 坐标字段仅保留在返回体中供其他属性诊断，
      //    绝不写入 App 的强制展示位置；FALL_ALERT 仍由此接口处理。
      uni.request({
        url: ONENET_PROPS_URL,
        method: 'GET',
        timeout: 5000,
        header: { 'Authorization': generateOneNetToken() },
        success: function(res) {
          try {
            var json = (typeof res.data === 'string') ? JSON.parse(res.data) : res.data
            if (json.code === 0 && Array.isArray(json.data)) {
              var d = propertyListToMap(json.data)
              // 摔倒告警 — 板端通过 MQTT 推送 FALL_ALERT 属性
              if (d.FALL_ALERT && d.FALL_ALERT.value) {
                try {
                  var fallData = JSON.parse(d.FALL_ALERT.value)
                  var fallAlert = {
                    time: cleanAlertText(fallData.time, '', 40),
                    type: cleanAlertText(fallData.type, '摔倒告警', 40),
                    lat: boundedCoordinate(fallData.lat, -90, 90),
                    lng: boundedCoordinate(fallData.lng, -180, 180),
                  }
                  // 去重：同一时间和类型已在列表中的跳过
                  var dup = self.alerts.some(function(a) {
                    return a.time === fallAlert.time && a.type === fallAlert.type
                  })
                  if (!dup) {
                    self.alerts.unshift(fallAlert)
                    // 最多保留20条
                    if (self.alerts.length > 20) self.alerts.pop()
                    // APP-PLUS 本地通知
                    self.localNotify(fallAlert)
                  }
                } catch(e) {}
              }
            }
          } catch(e) {}
        }
      })
    },

    // ==================== 本地通知（仅 APP-PLUS） ====================

    localNotify(alert) {
      // #ifdef APP-PLUS
      plus.push.createMessage(alert.type, alert.type + '\n' + alert.time, {
        title: 'K1 守护',
        cover: false,
      })
      // #endif
    },
  },
}
</script>

<style>
page {
  background-color: #F2F9F5;
  font-family: -apple-system, BlinkMacSystemFont, 'Helvetica Neue', system-ui, sans-serif;
  -webkit-font-smoothing: antialiased;
}

.page {
  display: flex;
  flex-direction: column;
  height: 100vh;
}

/* ===== 自定义标题栏 ===== */
.nav-bar {
  background: linear-gradient(to bottom, #E8F5F0, #F2F9F5);
  display: flex;
  align-items: center;
  justify-content: center;
  height: 72rpx;
  flex-shrink: 0;
}

.nav-title {
  font-size: 36rpx;
  color: #3D5A4F;
  font-weight: 600;
  font-family: -apple-system, BlinkMacSystemFont, 'Helvetica Neue', system-ui, sans-serif;
  letter-spacing: 1rpx;
}

/* ===== 地图卡片 ===== */
.map-card {
  background: #FFFFFF;
  border-radius: 24rpx;
  margin: 24rpx 24rpx 24rpx 24rpx;
  padding: 16rpx;
  border: 1px solid #D6E6DE;
  box-shadow: 0 2rpx 16rpx rgba(61, 90, 79, 0.06);
  flex-shrink: 0;
}

.map-frame {
  width: 100%;
  height: 28vh;
  border-radius: 16rpx;
  overflow: hidden;
}

.map {
  width: 100%;
  height: 100%;
}

/* ===== 悬浮刷新按钮 ===== */
.float-btn {
  position: fixed;
  width: 88rpx;
  height: 88rpx;
  border-radius: 50%;
  background: #E2F0E9;
  display: flex;
  align-items: center;
  justify-content: center;
  box-shadow: 0 4rpx 20rpx rgba(61, 90, 79, 0.10);
  z-index: 999;
  transition: transform 0.15s ease;
}
.float-btn:active {
  transform: scale(0.88);
}
.float-btn .rotating {
  animation: spin 0.7s ease-in-out;
}
@keyframes spin {
  from { transform: rotate(0deg); }
  to { transform: rotate(360deg); }
}

/* ===== 刷新波纹扩散 ===== */
.ripple-overlay {
  position: fixed;
  left: 0;
  top: 0;
  width: 100vw;
  height: 100vh;
  z-index: 1000;
  pointer-events: none;
  background: rgba(255, 255, 255, 0.35);
  animation: fadeIn 0.2s ease;
}

.ripple-circle {
  position: absolute;
  width: 20rpx;
  height: 20rpx;
  margin-left: -10rpx;
  margin-top: -10rpx;
  border-radius: 50%;
  background: rgba(255, 255, 255, 0.45);
}

.ripple-expand {
  animation: rippleOut 0.55s ease-out forwards;
}

@keyframes rippleOut {
  0%   { transform: scale(1); opacity: 0.7; }
  100% { transform: scale(250); opacity: 0; }
}

@keyframes fadeIn {
  from { opacity: 0; }
  to   { opacity: 1; }
}

/* ===== 按钮锁定态 ===== */
.btn-locked {
  pointer-events: none;
  opacity: 0.55;
}

/* ===== Toast ===== */
.toast {
  position: fixed;
  left: 50%;
  bottom: 180rpx;
  transform: translateX(-50%);
  z-index: 2000;
  background: rgba(45, 59, 73, 0.88);
  color: #FFFFFF;
  font-size: 26rpx;
  padding: 18rpx 40rpx;
  border-radius: 40rpx;
  font-family: -apple-system, BlinkMacSystemFont, 'Helvetica Neue', system-ui, sans-serif;
  animation: toastIn 0.3s ease;
}

@keyframes toastIn {
  from { opacity: 0; transform: translateX(-50%) translateY(20rpx); }
  to   { opacity: 1; transform: translateX(-50%) translateY(0); }
}

/* ===== 卡片滚动区 ===== */
.cards-area {
  flex: 1;
  padding: 24rpx 0;
}

/* ===== 卡片 ===== */
.card {
  background: #FFFFFF;
  border-radius: 24rpx;
  padding: 32rpx;
  margin: 0 24rpx 24rpx 24rpx;
  border: 1px solid #E2F0E9;
  box-sizing: border-box;
}

.card-header {
  display: flex;
  align-items: center;
  margin-bottom: 20rpx;
  padding-bottom: 20rpx;
  border-bottom: 1rpx solid #D6E6DE;
}

.card-icon {
  width: 40rpx;
  height: 40rpx;
  margin-right: 14rpx;
}

.card-title {
  font-size: 30rpx;
  font-weight: 400;
  color: #1A1A1A;
}

.card-row {
  display: flex;
  justify-content: space-between;
  align-items: center;
  padding: 14rpx 0;
}

.label {
  font-size: 28rpx;
  color: #666666;
}

.value {
  font-size: 28rpx;
  color: #1A1A1A;
  font-weight: 500;
}

.online {
  color: #4CAF50;
}

.offline {
  color: #999999;
}

.placeholder {
  color: #999999;
  font-weight: 400;
}

/* ===== 告警列表 ===== */
.alert-item {
  padding: 18rpx 0;
  border-top: 1rpx solid #D6E6DE;
}

.alert-item:first-of-type {
  border-top: none;
}

.alert-time {
  font-size: 22rpx;
  color: #999999;
  margin-bottom: 5rpx;
}

.alert-text {
  font-size: 28rpx;
  color: #FF7A45;
  font-weight: 600;
}

.alert-loc {
  font-size: 24rpx;
  color: #666666;
  margin-top: 5rpx;
}
</style>
