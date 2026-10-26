import { createContext, useCallback, useContext, useEffect, useMemo, useRef, useState } from 'react'
import { get, post } from '../api'
import { validateConfig } from '../lib/schema'

// One draft of the main settings for the whole console.
//
// The old console had three pages that each POSTed only their own part of the
// settings to /api/config; the server fills every missing key with its default,
// so saving one page silently reset the others (target groups, voice key…).
// Here every save posts the complete draft, so nothing is reset.

const ConfigContext = createContext(null)

const same = (a, b) => JSON.stringify(a) === JSON.stringify(b)

// Group / contact names (WECHAT_GROUPS, PRIVATE_CHATS) are kept as lists in
// the draft ([] = all). .env stores each name URL-encoded and comma-joined, so a
// name may itself contain a comma; load-config also returns them as lists.
const NAME_LISTS = { wechat_groups: 'wechat_groups_list', private_chats: 'private_chats_list' }

const encodeNames = (list) => {
  const names = (Array.isArray(list) ? list : []).map((x) => String(x).trim()).filter(Boolean)
  return names.length ? names.map((n) => encodeURIComponent(n)).join(',') : '*'
}

function fromServer(cfg) {
  const out = { ...cfg }
  for (const [key, listKey] of Object.entries(NAME_LISTS)) {
    out[key] = Array.isArray(cfg[listKey])
      ? cfg[listKey]
      : String(cfg[key] ?? '').split(',').map((x) => x.trim()).filter((x) => x && x !== '*')
    delete out[listKey]
  }
  return out
}

// Keys that POST /api/config leaves alone when they are missing from the
// payload (it reads them with a plain config.get()). Sending them only when
// they changed keeps values that live in the machine-local .env.local (the
// data dir) out of the shared .env.
const OMIT_IF_UNCHANGED = ['bot_display_name', 'wechat_backend', 'log_level', 'wechat_data_dir']

export function toPayload(draft, base = null) {
  const out = {
    ...draft,
    wechat_groups: encodeNames(draft.wechat_groups),
    private_chats: encodeNames(draft.private_chats),
  }
  if (base) {
    for (const k of Object.keys(out)) {
      if (OMIT_IF_UNCHANGED.includes(k) && same(draft[k], base[k])) delete out[k]
    }
  }
  return out
}

export function ConfigProvider({ children }) {
  const [state, setState] = useState({ status: 'loading', base: null, draft: null, error: '', detectedDataDir: '' })
  const [saving, setSaving] = useState(false)
  const loadSeq = useRef(0)

  const load = useCallback(async () => {
    const seq = ++loadSeq.current
    setState((s) => ({ ...s, status: s.base ? 'reloading' : 'loading', error: '' }))
    try {
      const data = await get('/api/load-config')
      if (seq !== loadSeq.current) return
      const cfg = fromServer(data.config || {})
      setState({ status: 'ready', base: cfg, draft: cfg, error: '', detectedDataDir: data.detected_data_dir || '' })
    } catch (e) {
      if (seq !== loadSeq.current) return
      setState((s) => ({ ...s, status: s.base ? 'ready' : 'error', error: e.message }))
    }
  }, [])

  useEffect(() => { load() }, [load])

  const set = useCallback((key, value) => {
    setState((s) => (s.draft ? { ...s, draft: { ...s.draft, [key]: value } } : s))
  }, [])
  const setMany = useCallback((patch) => {
    setState((s) => (s.draft ? { ...s, draft: { ...s.draft, ...patch } } : s))
  }, [])
  const discard = useCallback(() => {
    setState((s) => ({ ...s, draft: s.base }))
  }, [])

  const dirtyKeys = useMemo(() => {
    const { base, draft } = state
    if (!base || !draft) return []
    return Object.keys(draft).filter((k) => !same(draft[k], base[k]))
  }, [state])

  const errors = useMemo(() => validateConfig(state.draft), [state.draft])

  const save = useCallback(async () => {
    if (!state.draft) throw new Error('设置还没加载完')
    setSaving(true)
    try {
      const res = await post('/api/config', toPayload(state.draft, state.base))
      await load()
      return res
    } finally {
      setSaving(false)
    }
  }, [state.draft, state.base, load])

  // Leaving the desktop window with unsaved settings: ask first.
  useEffect(() => {
    if (!dirtyKeys.length) return undefined
    const onBeforeUnload = (e) => { e.preventDefault(); e.returnValue = '' }
    window.addEventListener('beforeunload', onBeforeUnload)
    return () => window.removeEventListener('beforeunload', onBeforeUnload)
  }, [dirtyKeys.length])

  const value = useMemo(() => ({
    ...state,
    ready: !!state.draft,
    saving,
    dirtyKeys,
    errors,
    set,
    setMany,
    discard,
    save,
    reload: load,
  }), [state, saving, dirtyKeys, errors, set, setMany, discard, save, load])

  return <ConfigContext.Provider value={value}>{children}</ConfigContext.Provider>
}

export function useConfig() {
  const ctx = useContext(ConfigContext)
  if (!ctx) throw new Error('useConfig outside ConfigProvider')
  return ctx
}

/** [value, setValue, error] for one key of the draft. */
export function useField(key) {
  const { draft, set, errors } = useConfig()
  const setter = useCallback((v) => set(key, v), [set, key])
  return [draft ? draft[key] : undefined, setter, errors[key] || '']
}
