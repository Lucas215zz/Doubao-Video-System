const fs = require('node:fs')
const path = require('node:path')
const NodeXMLHttpRequest = require('xhr2')
const util = require('node:util')
const vm = require('node:vm')
const crypto = require('node:crypto')

let doubaoCookieHeader = ''
let doubaoStsToken = null
const objectUrlStore = new Map()
let objectUrlSeq = 0

function bufferFromPart(part) {
  if (part == null) {
    return Buffer.alloc(0)
  }
  if (Buffer.isBuffer(part)) {
    return part
  }
  if (part instanceof BrowserBlob) {
    return part._buffer
  }
  if (part instanceof ArrayBuffer) {
    return Buffer.from(part)
  }
  if (ArrayBuffer.isView(part)) {
    return Buffer.from(part.buffer, part.byteOffset, part.byteLength)
  }
  return Buffer.from(String(part))
}

class BrowserBlob {
  constructor(parts = [], options = {}) {
    this.type = String(options.type || '').toLowerCase()
    this._buffer = Buffer.concat(parts.map(bufferFromPart))
    this.size = this._buffer.length
  }

  arrayBuffer() {
    const buffer = this._buffer
    return Promise.resolve(buffer.buffer.slice(buffer.byteOffset, buffer.byteOffset + buffer.byteLength))
  }

  text() {
    return Promise.resolve(this._buffer.toString('utf8'))
  }

  slice(start = 0, end = this.size, type = this.type) {
    return new BrowserBlob([this._buffer.slice(start, end)], { type })
  }

  get [Symbol.toStringTag]() {
    return 'Blob'
  }
}

class BrowserFile extends BrowserBlob {
  constructor(parts = [], name = 'file', options = {}) {
    super(parts, options)
    this.name = String(name)
    this.lastModified = Number(options.lastModified || Date.now())
  }

  get [Symbol.toStringTag]() {
    return 'File'
  }
}

function escapeMultipartValue(value) {
  return String(value).replace(/\\/g, '\\\\').replace(/"/g, '\\"').replace(/\r?\n/g, ' ')
}

class BrowserFormData {
  constructor() {
    this._entries = []
  }

  append(name, value, filename) {
    this._entries.push([String(name), value, filename])
  }

  set(name, value, filename) {
    const key = String(name)
    this._entries = this._entries.filter((entry) => entry[0] !== key)
    this.append(key, value, filename)
  }

  delete(name) {
    const key = String(name)
    this._entries = this._entries.filter((entry) => entry[0] !== key)
  }

  get(name) {
    const key = String(name)
    const entry = this._entries.find((item) => item[0] === key)
    return entry ? entry[1] : null
  }

  getAll(name) {
    const key = String(name)
    return this._entries.filter((entry) => entry[0] === key).map((entry) => entry[1])
  }

  has(name) {
    const key = String(name)
    return this._entries.some((entry) => entry[0] === key)
  }

  entries() {
    return this._entries.map(([name, value]) => [name, value])[Symbol.iterator]()
  }

  keys() {
    return this._entries.map(([name]) => name)[Symbol.iterator]()
  }

  values() {
    return this._entries.map(([, value]) => value)[Symbol.iterator]()
  }

  [Symbol.iterator]() {
    return this.entries()
  }

  get [Symbol.toStringTag]() {
    return 'FormData'
  }
}

function isBrowserFormData(data) {
  return data instanceof BrowserFormData
}

function serializeFormData(formData) {
  const boundary = `----doubao-node-formdata-${Date.now().toString(16)}-${Math.random().toString(16).slice(2)}`
  const chunks = []

  for (const [name, value, filename] of formData._entries) {
    chunks.push(Buffer.from(`--${boundary}\r\n`))
    if (value instanceof BrowserBlob) {
      const fileName = filename || value.name || 'blob'
      chunks.push(
        Buffer.from(
          `Content-Disposition: form-data; name="${escapeMultipartValue(name)}"; filename="${escapeMultipartValue(fileName)}"\r\n` +
            `Content-Type: ${value.type || 'application/octet-stream'}\r\n\r\n`,
        ),
      )
      chunks.push(value._buffer)
      chunks.push(Buffer.from('\r\n'))
    } else {
      chunks.push(Buffer.from(`Content-Disposition: form-data; name="${escapeMultipartValue(name)}"\r\n\r\n`))
      chunks.push(Buffer.from(String(value)))
      chunks.push(Buffer.from('\r\n'))
    }
  }

  chunks.push(Buffer.from(`--${boundary}--\r\n`))
  return {
    body: Buffer.concat(chunks),
    contentType: `multipart/form-data; boundary=${boundary}`,
  }
}

function awsEncode(value) {
  return encodeURIComponent(String(value))
    .replace(/[!'()*]/g, (char) => `%${char.charCodeAt(0).toString(16).toUpperCase()}`)
}

function canonicalQuery(params) {
  const parts = []
  for (const key of Array.from(params.keys()).sort()) {
    const values = params.getAll(key).sort()
    for (const value of values) {
      if (value != null) {
        parts.push(`${awsEncode(key)}=${awsEncode(value)}`)
      }
    }
  }
  return parts.join('&')
}

function hmac(key, value) {
  return crypto.createHmac('sha256', key).update(value).digest()
}

function sha256Hex(value) {
  return crypto.createHash('sha256').update(value).digest('hex')
}

function signingKey(secret, date, region, service) {
  let key = Buffer.from(`AWS4${secret}`)
  for (const part of [date, region, service, 'aws4_request']) {
    key = hmac(key, part)
  }
  return key
}

function headerValue(headers, lowerName) {
  const actualName = headers._loweredHeaders && headers._loweredHeaders[lowerName]
  return actualName ? headers._headers[actualName] : undefined
}

function setHeader(headers, name, value) {
  headers._headers[name] = String(value)
  headers._loweredHeaders[name.toLowerCase()] = name
}

function signDoubaoImagexTopRequest(xhr, body) {
  if (!doubaoStsToken || !xhr._doubaoUrl) {
    return
  }
  let parsed
  try {
    parsed = new URL(xhr._doubaoUrl)
  } catch (_error) {
    return
  }
  const action = parsed.searchParams.get('Action')
  if (!['ApplyImageUpload', 'CommitImageUpload'].includes(action || '')) {
    return
  }
  if (!['imagex.bytedanceapi.com', 'www.doubao.com'].includes(parsed.hostname)) {
    return
  }

  const now = new Date()
  const amzDate = now.toISOString().replace(/[:-]|\.\d{3}/g, '')
  const shortDate = amzDate.slice(0, 8)
  const region = xhr._doubaoRegion || 'cn-north-1'
  const service = xhr._doubaoService || 'imagex'
  const payload = body == null ? '' : Buffer.isBuffer(body) ? body : typeof body === 'string' ? body : JSON.stringify(body)
  const payloadHash = sha256Hex(payload)

  setHeader(xhr, 'X-Amz-Date', amzDate)
  setHeader(xhr, 'X-Amz-Security-Token', doubaoStsToken.SessionToken)
  if (body != null && !headerValue(xhr, 'x-amz-content-sha256')) {
    setHeader(xhr, 'X-Amz-Content-Sha256', payloadHash)
  }

  const signedNames = Object.keys(xhr._headers)
    .map((name) => name.toLowerCase())
    .filter((name) => !['authorization', 'content-length', 'user-agent'].includes(name))
    .sort()
  const canonicalHeaders = signedNames
    .map((name) => `${name}:${String(headerValue(xhr, name) || '').replace(/\s+/g, ' ').trim()}\n`)
    .join('')
  const signedHeaders = signedNames.join(';')
  const canonical = [
    xhr._doubaoMethod || 'GET',
    parsed.pathname || '/',
    canonicalQuery(parsed.searchParams),
    canonicalHeaders,
    signedHeaders,
    payloadHash,
  ].join('\n')
  const scope = `${shortDate}/${region}/${service}/aws4_request`
  const stringToSign = ['AWS4-HMAC-SHA256', amzDate, scope, sha256Hex(canonical)].join('\n')
  const signature = crypto.createHmac('sha256', signingKey(doubaoStsToken.SecretAccessKey, shortDate, region, service)).update(stringToSign).digest('hex')
  setHeader(
    xhr,
    'Authorization',
    `AWS4-HMAC-SHA256 Credential=${doubaoStsToken.AccessKeyId || doubaoStsToken.AccessKeyID}/${scope}, SignedHeaders=${signedHeaders}, Signature=${signature}`,
  )
}

class CookieXMLHttpRequest extends NodeXMLHttpRequest {
  constructor(...args) {
    super(...args)
    this._restrictedHeaders = { ...this._restrictedHeaders }
    delete this._restrictedHeaders.cookie
    delete this._restrictedHeaders.origin
    delete this._restrictedHeaders.referer
  }

  open(method, url, ...args) {
    this._doubaoUrl = String(url || '')
    this._doubaoMethod = String(method || 'GET').toUpperCase()
    return super.open(method, url, ...args)
  }

  send(data) {
    if (isBrowserFormData(data)) {
      if (this._method === 'GET' || this._method === 'HEAD') {
        data = null
      } else {
        const multipart = serializeFormData(data)
        this._headers['Content-Type'] = multipart.contentType
        this._loweredHeaders['content-type'] = 'Content-Type'
        data = multipart.body
      }
    } else if (data instanceof BrowserBlob) {
      if (!this._headers['Content-Type'] && !this._headers['content-type'] && data.type) {
        this._headers['Content-Type'] = data.type
        this._loweredHeaders['content-type'] = 'Content-Type'
      }
      data = data._buffer
    }
    if (doubaoCookieHeader && this._doubaoUrl.startsWith('https://www.doubao.com/')) {
      try {
        this.setRequestHeader('Cookie', doubaoCookieHeader)
        this.setRequestHeader('Origin', 'https://www.doubao.com')
        this.setRequestHeader('Referer', 'https://www.doubao.com/chat/')
      } catch (_error) {
        // xhr2 can reject some browser-forbidden headers; keep the original upload error.
      }
    }
    signDoubaoImagexTopRequest(this, data)
    return super.send(data)
  }
}

global.XMLHttpRequest = CookieXMLHttpRequest
global.Blob = BrowserBlob
global.File = BrowserFile
global.FormData = BrowserFormData

function dispatchMessage(target, event) {
  if (typeof target.onmessage === 'function') {
    target.onmessage(event)
  }
  if (target._messageListeners) {
    for (const listener of target._messageListeners) {
      listener(event)
    }
  }
}

function dispatchError(target, error) {
  if (typeof target.onerror === 'function') {
    target.onerror(error)
    return
  }
  if (target._errorListeners && target._errorListeners.size) {
    for (const listener of target._errorListeners) {
      listener(error)
    }
  }
}

function addEventListenerPolyfill(target, type, listener) {
  if (typeof listener !== 'function') {
    return
  }
  if (type === 'message') {
    target._messageListeners = target._messageListeners || new Set()
    target._messageListeners.add(listener)
  } else if (type === 'error') {
    target._errorListeners = target._errorListeners || new Set()
    target._errorListeners.add(listener)
  }
}

function removeEventListenerPolyfill(target, type, listener) {
  const key = type === 'message' ? '_messageListeners' : type === 'error' ? '_errorListeners' : ''
  if (key && target[key]) {
    target[key].delete(listener)
  }
}

function inlineWorkerCodeFromUrl(url) {
  const value = String(url || '')
  if (objectUrlStore.has(value)) {
    return objectUrlStore.get(value)
  }
  if (value.startsWith('data:application/javascript,')) {
    return decodeURIComponent(value.slice('data:application/javascript,'.length))
  }
  if (value.startsWith('data:text/javascript,')) {
    return decodeURIComponent(value.slice('data:text/javascript,'.length))
  }
  throw new Error(`Unsupported worker URL: ${value}`)
}

class InlineWorker {
  constructor(url) {
    this.onmessage = null
    this.onerror = null
    this._messageListeners = new Set()
    this._errorListeners = new Set()
    this._terminated = false

    const code = inlineWorkerCodeFromUrl(url)
    const workerScope = {
      onmessage: null,
      onerror: null,
      _messageListeners: new Set(),
      _errorListeners: new Set(),
      console,
      setTimeout,
      clearTimeout,
      setInterval,
      clearInterval,
      ArrayBuffer,
      DataView,
      Int8Array,
      Uint8Array,
      Uint8ClampedArray,
      Int16Array,
      Uint16Array,
      Int32Array,
      Uint32Array,
      Float32Array,
      Float64Array,
      Blob: BrowserBlob,
      File: BrowserFile,
      FormData: BrowserFormData,
      FileReader: global.FileReader,
      postMessage: (data) => {
        if (!this._terminated) {
          setTimeout(() => dispatchMessage(this, { data }), 0)
        }
      },
      close: () => {
        this.terminate()
      },
    }
    workerScope.self = workerScope
    workerScope.globalThis = workerScope
    workerScope.addEventListener = (type, listener) => addEventListenerPolyfill(workerScope, type, listener)
    workerScope.removeEventListener = (type, listener) => removeEventListenerPolyfill(workerScope, type, listener)
    this._workerScope = workerScope
    this._context = vm.createContext(workerScope)

    try {
      vm.runInContext(code, this._context, { filename: 'doubao-inline-worker.js' })
    } catch (error) {
      setTimeout(() => dispatchError(this, error), 0)
    }
  }

  postMessage(data) {
    if (this._terminated) {
      return
    }
    setTimeout(() => {
      if (this._terminated) {
        return
      }
      try {
        dispatchMessage(this._workerScope, { data })
      } catch (error) {
        dispatchError(this, error)
      }
    }, 0)
  }

  addEventListener(type, listener) {
    addEventListenerPolyfill(this, type, listener)
  }

  removeEventListener(type, listener) {
    removeEventListenerPolyfill(this, type, listener)
  }

  terminate() {
    this._terminated = true
    this._messageListeners.clear()
    this._errorListeners.clear()
  }
}

const NativeURL = global.URL || require('node:url').URL
const nativeCreateObjectURL = typeof NativeURL.createObjectURL === 'function' ? NativeURL.createObjectURL.bind(NativeURL) : null
NativeURL.createObjectURL = function createObjectURL(blob) {
  if (blob instanceof BrowserBlob) {
    const key = `blob:doubao-node-worker-${++objectUrlSeq}`
    objectUrlStore.set(key, blob._buffer.toString('utf8'))
    return key
  }
  if (nativeCreateObjectURL) {
    return nativeCreateObjectURL(blob)
  }
  throw new Error('URL.createObjectURL only supports BrowserBlob in this helper')
}
NativeURL.revokeObjectURL = function revokeObjectURL(url) {
  objectUrlStore.delete(String(url || ''))
}
global.URL = NativeURL
global.Worker = InlineWorker

if (typeof global.window === 'undefined') {
  global.window = global
}

if (typeof global.self === 'undefined') {
  global.self = global
}

try {
  Object.defineProperty(global, 'navigator', {
    value: {
      userAgent:
        'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36',
    },
    configurable: true,
  })
} catch (_error) {
  global.navigator = global.navigator || { userAgent: 'Mozilla/5.0' }
}

if (typeof global.localStorage === 'undefined') {
  const store = new Map()
  global.localStorage = {
    getItem(key) {
      return store.has(key) ? store.get(key) : null
    },
    setItem(key, value) {
      store.set(key, String(value))
    },
    removeItem(key) {
      store.delete(key)
    },
    clear() {
      store.clear()
    },
  }
}

if (typeof global.FileReader === 'undefined') {
  global.FileReader = class FileReader {
    constructor() {
      this.result = null
      this.error = null
      this.onload = null
      this.onerror = null
    }

    readAsArrayBuffer(blob) {
      Promise.resolve()
        .then(() => blob.arrayBuffer())
        .then((buffer) => {
          this.result = buffer
          if (typeof this.onload === 'function') {
            this.onload({ target: this })
          }
        })
        .catch((error) => {
          this.error = error
          if (typeof this.onerror === 'function') {
            this.onerror({ target: this })
          }
        })
    }

    readAsDataURL(blob) {
      Promise.resolve()
        .then(() => blob.arrayBuffer())
        .then((buffer) => {
          const base64 = Buffer.from(buffer).toString('base64')
          this.result = `data:${blob.type || 'application/octet-stream'};base64,${base64}`
          if (typeof this.onload === 'function') {
            this.onload({ target: this })
          }
        })
        .catch((error) => {
          this.error = error
          if (typeof this.onerror === 'function') {
            this.onerror({ target: this })
          }
        })
    }
  }
}

function readStdin() {
  return new Promise((resolve, reject) => {
    let data = ''
    process.stdin.setEncoding('utf8')
    process.stdin.on('data', (chunk) => {
      data += chunk
    })
    process.stdin.on('end', () => resolve(data))
    process.stdin.on('error', reject)
  })
}

function normalizeToken(token) {
  return {
    AccessKeyId: token.AccessKeyId || token.access_key_id || token.access_key || '',
    AccessKeyID: token.AccessKeyID || token.AccessKeyId || token.access_key_id || token.access_key || '',
    SecretAccessKey: token.SecretAccessKey || token.secret_access_key || token.secret_key || '',
    SessionToken: token.SessionToken || token.session_token || '',
    ExpiredTime: token.ExpiredTime || token.expired_time || '',
    CurrentTime: token.CurrentTime || token.current_time || '',
  }
}

function loadBundledUploader(bundlePath) {
  if (!bundlePath || !fs.existsSync(bundlePath)) {
    return null
  }
  const previousChunks = global.self.__LOADABLE_LOADED_CHUNKS__
  global.self.__LOADABLE_LOADED_CHUNKS__ = []
  const code = fs.readFileSync(bundlePath, 'utf8')
  const loader = new Function('require', 'global', 'self', 'window', code)
  loader(require, global, global.self, global.window)
  const chunk = global.self.__LOADABLE_LOADED_CHUNKS__[0]
  global.self.__LOADABLE_LOADED_CHUNKS__ = previousChunks || []
  const moduleFactory = chunk && chunk[1] && chunk[1][517826]
  if (typeof moduleFactory !== 'function') {
    throw new Error(`Doubao uploader bundle is missing module 517826: ${bundlePath}`)
  }
  const mod = { exports: {} }
  moduleFactory(mod, mod.exports, () => {
    throw new Error('unexpected webpack require from Doubao uploader bundle')
  })
  return mod.exports.default || mod.exports
}

function loadUploader(bundlePath) {
  return loadBundledUploader(bundlePath) || require('tt-uploader')
}

function fail(error) {
  const message = error && error.stack ? error.stack : String(error)
  process.stderr.write(message + '\n')
  process.exit(1)
}

function summarizeUploadInfo(info) {
  if (!info || typeof info !== 'object') {
    return info
  }
  return {
    type: info.type,
    stage: info.stage,
    status: info.status,
    percent: info.percent,
    key: info.key,
    oid: info.oid,
    errorCode: info.errorCode || (info.extra && info.extra.errorCode),
    message: info.message || (info.extra && info.extra.message),
    error: info.error || (info.extra && info.extra.error),
    requestId: info.requestId || (info.extra && info.extra.requestId),
    req: info.req,
    res: info.res,
  }
}

function sanitizeUploadResult(info) {
  if (!info || typeof info !== 'object') {
    return info
  }
  const uploadResult = info.uploadResult && typeof info.uploadResult === 'object' ? { ...info.uploadResult } : null
  return {
    type: info.type,
    stage: info.stage,
    status: info.status,
    percent: info.percent,
    key: info.key,
    oid: info.oid,
    fileName: info.fileName,
    fileSize: info.fileSize,
    ImageWidth: info.ImageWidth,
    ImageHeight: info.ImageHeight,
    ImageMd5: info.ImageMd5,
    uploadResult,
  }
}

async function main() {
  const input = JSON.parse(await readStdin())
  doubaoCookieHeader = input.cookieHeader || ''
  doubaoStsToken = normalizeToken(input.stsToken || {})
  const filePath = input.filePath
  if (!filePath || !fs.existsSync(filePath)) {
    throw new Error(`file not found: ${filePath || ''}`)
  }

  const buffer = fs.readFileSync(filePath)
  const fileName = input.fileName || path.basename(filePath)
  const contentType = input.contentType || 'application/octet-stream'
  const file = new File([buffer], fileName, { type: contentType })
  const token = doubaoStsToken
  const serviceId = input.serviceId
  if (!serviceId) {
    throw new Error('serviceId is required')
  }

  const TTUploader = loadUploader(input.bundlePath)
  const uploader = new TTUploader({
    appId: Number(input.appId || 497858),
    userId: String(input.userId || '0'),
    useLocalStorage: false,
    useFileExtension: true,
    retryUploadTime: Number(input.retryUploadTime || 2),
    useServerCurrentTime: input.useServerCurrentTime !== false,
    noLog: input.noLog !== false,
  })
  if (typeof uploader.refreshSTSToken === 'function') {
    uploader.refreshSTSToken(token)
  } else if (typeof uploader.refreshStsToken === 'function') {
    uploader.refreshStsToken(token)
  }
  if (typeof uploader.setOption === 'function') {
    uploader.setOption({
      imageHost: input.imageHost || 'https://www.doubao.com/top/v1',
      imageConfig: { serviceId },
    })
  }

  const result = await new Promise((resolve, reject) => {
    const timer = setTimeout(() => reject(new Error('ImageX upload timeout')), Number(input.timeoutMs || 120000))

    uploader.once('complete', (info) => {
      clearTimeout(timer)
      resolve(info)
    })
    uploader.once('error', (info) => {
      clearTimeout(timer)
      reject(new Error(util.inspect(summarizeUploadInfo(info), { depth: 5, breakLength: 140 })))
    })

    const key = uploader.addImageFile({
      file,
      stsToken: token,
      type: input.uploadType || 'image',
      ...(input.serviceIdInTask ? { serviceId } : {}),
      ...(input.storeKey ? { storeKey: input.storeKey } : {}),
      ...(input.prefix ? { prefix: input.prefix } : {}),
      headers: input.headers || undefined,
      skipMeta: Boolean(input.skipMeta),
      forceMeta: Boolean(input.forceMeta),
      useDirectUpload: Boolean(input.useDirectUpload),
      callbackArgs: input.callbackArgs,
    })
    if (!key) {
      clearTimeout(timer)
      reject(new Error('failed to add image file'))
      return
    }
    uploader.start(key)
  })

  process.stdout.write(JSON.stringify(sanitizeUploadResult(result)))
}

main().catch(fail)
