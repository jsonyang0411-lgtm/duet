import { useEffect, useState } from 'react'
import { api } from './api'
import { useRoute } from './lib/router'
import { ToastProvider } from './store/toast'
import { StatusProvider } from './store/status'
import { ConfigProvider } from './store/config'
import { TopBar, Rail, SaveBar } from './components/Shell'
import { LogoGlyph } from './components/art'
import Dashboard from './components/Dashboard'
import DecisionsPanel from './components/DecisionsPanel'
import ConfigPanel from './components/ConfigPanel'
import ModulesPanel from './components/ModulesPanel'
import AdvancedPanel from './components/AdvancedPanel'
import NicknameEditor from './components/NicknameEditor'
import TodoManager from './components/TodoManager'
import InfluencerPanel from './components/InfluencerPanel'
import LogViewer from './components/LogViewer'
import Onboarding from './components/Onboarding'

export default function App() {
  // null = still asking the server; then true (console) or false (first-run setup)
  const [onboarded, setOnboarded] = useState(null)
  const [slow, setSlow] = useState(false)

  useEffect(() => {
    let alive = true
    let timer
    const ask = async () => {
      try {
        const d = await api('/api/onboarding/status')
        if (alive) setOnboarded(!!d.onboarding_done)
      } catch {
        if (alive) timer = setTimeout(ask, 1000)
      }
    }
    ask()
    const slowTimer = setTimeout(() => setSlow(true), 6000)
    return () => { alive = false; clearTimeout(timer); clearTimeout(slowTimer) }
  }, [])

  if (onboarded === null) return <Splash slow={slow} />
  return (
    <ToastProvider>
      {onboarded
        ? <StatusProvider><ConfigProvider><Console /></ConfigProvider></StatusProvider>
        : <Onboarding onDone={() => setOnboarded(true)} />}
    </ToastProvider>
  )
}

function Console() {
  const route = useRoute()
  let page
  switch (route.page) {
    case 'trace': page = <DecisionsPanel route={route} />; break
    case 'config': page = <ConfigPanel section={route.sub || 'ai'} />; break
    case 'modules': page = <ModulesPanel selected={route.sub} />; break
    case 'advanced': page = <AdvancedPanel />; break
    case 'members': page = <NicknameEditor />; break
    case 'todo': page = <TodoManager />; break
    case 'influencer': page = <InfluencerPanel sub={route.sub} />; break
    case 'logs': page = <LogViewer />; break
    default: page = <Dashboard />
  }
  return (
    <div className="app">
      <TopBar route={route} />
      <Rail route={route} />
      <main className="main" id="main">
        {page}
        <SaveBar />
      </main>
    </div>
  )
}

function Splash({ slow }) {
  return (
    <div className="splash">
      <div className="splash-box">
        <span className="brand-mark"><LogoGlyph /></span>
        <div className="stack" style={{ gap: 6 }}>
          <b className="latin" style={{ fontSize: 20, letterSpacing: '.16em' }}>WEBOT</b>
          <span>正在加载...</span>
        </div>
      </div>
      <div className="indet" style={{ width: 240 }} />
      {slow && <p className="dim">还连不上控制台服务。请确认 webot 正在运行，稍等会自动重试。</p>}
    </div>
  )
}
