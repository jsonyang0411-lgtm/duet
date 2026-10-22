// What the main settings (/api/load-config → /api/config) may contain, and the
// ranges src/config.py::_validate_config enforces. A value outside these ranges
// stops the bot from starting, so the console refuses to save it.

export const NUMBER_RULES = {
  chat_max_chars: { min: 8, max: 200, int: true },
  chat_context_window_sec: { min: 60, max: 7200, int: true },
  chat_context_count: { min: 1, max: 120, int: true },
  proactive_rate_window_sec: { min: 30, int: true },
  proactive_rate_quiet: { gt: 0 },
  proactive_rate_casual: { gt: 0 },
  proactive_rate_lively: { gt: 0 },
  proactive_rate_burst: { gt: 0 },
  sticky_mention_ttl_sec: { min: 10, max: 300, int: true },
  followup_listen_ttl_sec: { min: 10, max: 300, int: true },
  followup_listen_context_count: { min: 1, max: 20, int: true },
  followup_listen_max_message_chars: { min: 1, max: 80, int: true },
  message_burst_window_sec: { min: 0.2, max: 8 },
  message_burst_max_messages: { min: 1, max: 10, int: true },
  message_burst_max_chars: { min: 20, max: 1000, int: true },
  web_search_max_results: { min: 1, max: 8, int: true },
  web_search_timeout_sec: { min: 1, max: 20 },
  web_search_reply_max_chars: { min: 40, max: 300, int: true },
  feishu_export_window_hours: { min: 1, max: 168, int: true },
  feishu_auto_sync_min_messages: { min: 1, max: 500, int: true },
  feishu_auto_sync_cooldown_sec: { min: 60, max: 86400, int: true },
  todo_max_per_group: { min: 1, max: 200, int: true },
  todo_completed_retention_days: { min: 0, int: true },
  todo_deleted_retention_days: { min: 0, int: true },
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
  const rates = ['proactive_rate_quiet', 'proactive_rate_casual', 'proactive_rate_lively', 'proactive_rate_burst']
  const vals = rates.map((k) => Number(c[k]))
  if (vals.every((v) => !Number.isNaN(v)) && !(vals[0] < vals[1] && vals[1] < vals[2] && vals[2] < vals[3])) {
    errors.proactive_rate_order = '四档要从小到大：安静 < 随口 < 活跃 < 爆发'
  }
  if (c.chat_persona_profile === 'yang_yujing' && !String(c.yang_yujing_group_name || '').trim()) {
    errors.yang_yujing_group_name = '杨宇靖人设必须填逐条读取的群'
  }
  return errors
}

// Where each key lives in the console; used by the save bar to say what changed.
export const KEY_PAGE = [
  [/^(wechat_groups|wechat_backend|private_|wechat_data_dir|log_level)/, '微信连接'],
  [/^(ai_backend|deepseek_|openai_|anthropic_|summarize_model)/, 'AI 服务'],
  [/^(bot_display_name|trigger_keywords)/, '身份与群聊'],
  [/^(chat_|yang_yujing_)/, '人设'],
  [/^(wechat_data_dir|log_level)/, '数据与日志'],
  [/^fun_/, '趣味抽签'],
  [/^proactive_/, '主动发言'],
  [/^sticky_/, '粘性提及'],
  [/^followup_/, '跟聊'],
  [/^message_burst_/, '连发合并'],
  [/^welcome_/, '欢迎新人'],
  [/^todo_/, '群聊待办'],
  [/^feishu_/, '飞书同步'],
  [/^voice_/, '语音识别'],
  [/^web_search_/, '联网搜索'],
  [/^private_/, '私聊'],
]

export function pageOfKey(key) {
  for (const [re, name] of KEY_PAGE) if (re.test(key)) return name
  return '其他'
}

// Masked secrets come back from the server as "abcd***wxyz" (or "***").
export const isMasked = (v) => typeof v === 'string' && v.includes('***')
