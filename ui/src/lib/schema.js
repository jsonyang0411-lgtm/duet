// What the main settings (/api/load-config → /api/config) may contain, and the
// ranges src/config.py::_validate_config enforces. A value outside these ranges
// stops the bot from starting, so the console refuses to save it.

export const NUMBER_RULES = {
  message_burst_window_sec: { min: 0.2, max: 8 },
  message_burst_max_messages: { min: 1, max: 10, int: true },
  message_burst_max_chars: { min: 20, max: 1000, int: true },
}

export function ruleText(rule) {
  if (!rule) return ''
  const parts = []
  if (rule.gt !== undefined) parts.push(`大于 ${rule.gt}`)
  if (rule.min !== undefined && rule.max !== undefined) parts.push(`${rule.min}–${rule.max}`)
  else if (rule.min !== undefined) parts.push(`至少 ${rule.min}`)
  else if (rule.max !== undefined) parts.push(`最多 ${rule.max}`)
  if (rule.int) parts.push('整数')
  return parts.join('，')
}

export function checkNumber(value, rule) {
  if (value === '' || value === null || value === undefined || Number.isNaN(Number(value))) return '请填一个数字'
  const n = Number(value)
  if (!Number.isFinite(n) || Math.abs(n) > 1e9) return '这个数不对'
  if (!rule) return ''
  if (rule.int && !Number.isSafeInteger(n)) return '要填整数'
  if (rule.gt !== undefined && !(n > rule.gt)) return `要大于 ${rule.gt}`
  if (rule.min !== undefined && n < rule.min) return `范围是 ${ruleText(rule)}`
  if (rule.max !== undefined && n > rule.max) return `范围是 ${ruleText(rule)}`
  return ''
}

/** Field errors for the whole draft: { key: message }. */
export function validateConfig(c) {
  const errors = {}
  if (!c) return errors
  for (const [key, rule] of Object.entries(NUMBER_RULES)) {
    if (key in c) {
      const msg = checkNumber(c[key], rule)
      if (msg) errors[key] = msg
    }
  }
  return errors
}

// Where each key lives in the console; used by the save bar to say what changed.
export const KEY_PAGE = [
  [/^(wechat_|private_|bot_display_name|log_level|message_burst_)/, '微信连接'],
  [/^voice_/, '语音识别'],
]

export function pageOfKey(key) {
  for (const [re, name] of KEY_PAGE) if (re.test(key)) return name
  return '其他'
}

// Masked secrets come back from the server as "abcd***wxyz" (or "***").
export const isMasked = (v) => typeof v === 'string' && v.includes('***')
