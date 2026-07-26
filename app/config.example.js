// Copy this file to config.js for a local build. config.js is ignored by Git.
// A production mobile app should call a trusted backend instead of embedding
// a long-lived OneNET product key in the APK.
export default Object.freeze({
  onenetProductId: 'REPLACE_WITH_ONENET_PRODUCT_ID',
  onenetDeviceName: 'REPLACE_WITH_ONENET_DEVICE_NAME',
  onenetProductKeyB64: 'REPLACE_WITH_ONENET_PRODUCT_KEY_BASE64',
  fixedDeviceLocation: Object.freeze({
    lng: 0,
    lat: 0,
    locType: 'WiFi定位',
  }),
})
