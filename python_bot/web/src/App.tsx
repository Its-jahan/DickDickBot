import { useCallback, useEffect, useRef, useState } from 'react'
import { api, authHeaders, readLogin, ApiError } from '@/lib/api'
import { TG, waitForTelegram } from '@/lib/tg'
import { num } from '@/lib/format'
import { BottomNav, ThemeToggle, type TabKey } from '@/components/Shell'
import { useToast } from '@/components/Toast'
import { Card, CardContent } from '@/components/ui/card'
import { Skeleton } from '@/components/ui/skeleton'
import { Button } from '@/components/ui/button'
import { Home } from '@/screens/Home'
import { Feed } from '@/screens/Feed'
import { Crypto } from '@/screens/Crypto'
import { Bank } from '@/screens/Bank'
import { Shop } from '@/screens/Shop'
import { Bag } from '@/screens/Bag'
import { Transfer } from '@/screens/Transfer'
import { ActionSheet, type ActionKind } from '@/screens/Actions'
import { GroupSheet, type GroupKind } from '@/screens/Group'
import { HeistSheet } from '@/screens/Heist'
import { Login } from '@/screens/Login'
import { ToneSettings } from '@/components/ToneSettings'
import { politeText, tonePayload, type ToneMode } from '@/lib/tone'

const ENDPOINT: Record<TabKey, string> = {
  home: '/api/home', feed: '/api/feed', crypto: '/api/crypto',
  bank: '/api/bank', shop: '/api/shop', bag: '/api/inventory',
}

type Phase = 'boot' | 'login' | 'groups' | 'empty' | 'play' | 'error'

export default function App() {
  const [phase, setPhase] = useState<Phase>('boot')
  const [error, setError] = useState('')
  const [name, setName] = useState('')
  const [groups, setGroups] = useState<any[]>([])
  const [chat, setChat] = useState<number | null>(null)
  const [tab, setTab] = useState<TabKey>('home')
  const [data, setData] = useState<{ tab: TabKey; payload: any } | null>(null)
  // Monotonic request id: a slow response for a tab you have already left must not
  // overwrite the one you are looking at now.
  const reqRef = useRef(0)
  const [loading, setLoading] = useState(false)
  const [xfer, setXfer] = useState(false)
  const [action, setAction] = useState<ActionKind | null>(null)
  const [groupKind, setGroupKind] = useState<GroupKind | null>(null)
  const [heist, setHeist] = useState(false)
  const [growing, setGrowing] = useState(false)
  // The bell's badge. `seen` is the newest id the player has actually looked at, so the
  // count survives switching tabs and does not reset just because the app re-rendered.
  const [seen, setSeen] = useState(0)
  const [unread, setUnread] = useState(0)
  const toast = useToast()
  const tone = (groups.find((g) => g.chat_id === chat)?.tone ?? 'adult') as ToneMode

  const logout = useCallback(() => {
    try { localStorage.removeItem('login'); localStorage.removeItem('chat') } catch { /* private window */ }
    setChat(null); setData(null); setPhase('login')
  }, [])

  const boot = useCallback(async () => {
    // telegram-web-app.js is async, so at this point it may not have landed yet. Wait a
    // bounded moment for it rather than deciding early and showing a Telegram user the
    // browser login screen.
    await waitForTelegram()
    if (!TG?.initData && !readLogin()) return setPhase('login')
    try {
      const r = await fetch('/api/groups', { headers: authHeaders() })
      const j = await r.json()
      if (!j.ok) {
        // A stored login the server no longer accepts (expired, or the token rotated)
        // must drop the player back to the widget rather than stranding them on an
        // error they cannot act on.
        if (r.status === 403 && readLogin()) return logout()
        throw new ApiError(j.error || 'خطا', r.status)
      }
      setName(j.name)
      setGroups(j.groups)
      if (!j.groups.length) return setPhase('empty')
      let saved: number | null = null
      try { saved = Number(localStorage.getItem('chat')) || null } catch { /* private window */ }
      const pick = j.groups.length === 1 ? j.groups[0].chat_id
        : (saved && j.groups.some((g: any) => g.chat_id === saved) ? saved : null)
      if (pick) { setChat(pick); setPhase('play') } else setPhase('groups')
    } catch (e: any) {
      setError(e.message || 'خطا در اتصال')
      setPhase('error')
    }
  }, [logout])

  useEffect(() => {
    waitForTelegram().then(() => {
      try {
        TG?.ready()
        TG?.expand()
        // No saved choice means follow the client. The pre-paint script in index.html
        // could not read this, because Telegram had not loaded yet.
        let saved: string | null = null
        try { saved = localStorage.getItem('theme') } catch { /* private window */ }
        if (!saved && TG?.colorScheme) {
          document.documentElement.classList.toggle('dark', TG.colorScheme === 'dark')
        }
      } catch { /* not in Telegram */ }
    })
    ;(window as any).onTelegramAuth = (user: any) => {
      try { localStorage.setItem('login', JSON.stringify(user)) } catch { /* private window */ }
      setPhase('boot'); boot()
    }
    boot()
  }, [boot])

  const reload = useCallback(async () => {
    if (!chat) return
    const want = tab
    const seq = ++reqRef.current
    setLoading(true)
    try {
      const d = await api<any>(ENDPOINT[want], undefined, chat)
      // Tag the payload with the tab it belongs to. Rendering is gated on that tag, so a
      // screen can never be handed another screen's data - which is what made switching
      // tabs crash: setTab re-renders IMMEDIATELY, long before the new data arrives, so
      // <Top> got the home payload and read .rows.length off undefined.
      if (seq === reqRef.current) setData({ tab: want, payload: tonePayload(d, tone) })
    } catch (e: any) {
      // A response that lost the race must not clear the screen the player is now on.
      if (seq === reqRef.current) {
        setData(null)
        toast(e.message, true)
      }
    } finally {
      if (seq === reqRef.current) setLoading(false)
    }
  }, [chat, tab, toast, tone])

  // /d, from the home screen. It is a plain button rather than a sheet because there is
  // nothing to choose: the roll is the whole interaction.
  const grow = useCallback(async () => {
    if (!chat || growing) return
    setGrowing(true)
    try {
      const r = await api<any>('/api/grow', {}, chat)
      toast(r.message?.split('\n')[0] ?? 'رشد کردی')
      await reload()
    } catch (e: any) {
      toast(e.message, true)
    } finally {
      setGrowing(false)
    }
  }, [chat, growing, reload, toast])

  useEffect(() => { if (phase === 'play') reload() }, [phase, tab, chat, reload])

  // Poll the feed for the badge while the player is on another tab. Cheap: it returns a
  // count, not the list.
  useEffect(() => {
    if (phase !== 'play' || !chat) return
    let alive = true
    const check = async () => {
      try {
        const r = await api<any>(`/api/feed?since=${seen}`, undefined, chat)
        if (!alive) return
        setUnread(r.unread)
        if (tab === 'feed') { setSeen(r.latest); setUnread(0) }
      } catch { /* the badge is not worth a toast */ }
    }
    check()
    const t = window.setInterval(check, 45000)
    return () => { alive = false; window.clearInterval(t) }
  }, [phase, chat, tab, seen])

  // Only ever the data for the tab being drawn. Anything else is a mismatch, and the
  // skeleton is the honest thing to show while the right data is on its way.
  const d = data && data.tab === tab ? data.payload : null

  const choose = (id: number) => {
    try { localStorage.setItem('chat', String(id)) } catch { /* private window */ }
    setChat(id); setData(null); setTab('home'); setPhase('play')
  }

  const changeTone = (mode: ToneMode) => {
    setGroups((all) => all.map((g) => g.chat_id === chat ? { ...g, tone: mode } : g))
    setData(null)
  }

  const header = (
    <div className="mb-3 flex items-center justify-between">
      <div className="text-sm font-semibold text-muted-foreground">
        {groups.find((g) => g.chat_id === chat)?.title ??
          (tone === 'polite' ? politeText('دودول') : 'دودول')}
      </div>
      <div className="flex items-center gap-1">
        {chat && <ToneSettings chat={chat} tone={tone} onTone={changeTone} />}
        <ThemeToggle />
      </div>
    </div>
  )

  if (phase === 'boot') {
    return <Wrap><Skeleton className="h-32 w-full" /><div className="h-3" /><Skeleton className="h-24 w-full" /></Wrap>
  }
  if (phase === 'login') return <Wrap><Login /></Wrap>
  if (phase === 'error') {
    return <Wrap><div className="py-16 text-center text-sm text-muted-foreground">{error}</div></Wrap>
  }
  if (phase === 'empty') {
    return (
      <Wrap>
        <div className="py-16 text-center text-sm leading-loose text-muted-foreground">
          سلام {name} 👋<br /><br />
          هنوز توی هیچ گروهی بازی نکردی.<br />
          اول توی گروه یه بار <b>/d</b> بزن، بعد برگرد.
        </div>
      </Wrap>
    )
  }
  if (phase === 'groups') {
    return (
      <Wrap>
        <div className="mb-3 flex items-center justify-between">
          <h1 className="text-lg font-bold">سلام {name} 👋</h1>
          <div className="flex items-center gap-1">
            <ThemeToggle />
            {!TG?.initData && <Button variant="ghost" size="sm" onClick={logout}>خروج</Button>}
          </div>
        </div>
        <p className="mb-3 text-sm text-muted-foreground">کدوم گروه؟ هر گروه لیگ جداگانهٔ خودشه.</p>
        <div className="space-y-2.5">
          {groups.map((g) => (
            <Card key={g.chat_id} className="cursor-pointer transition-colors hover:bg-accent"
                  onClick={() => choose(g.chat_id)}>
              <CardContent className="flex items-center justify-between py-4">
                <b className="truncate">{g.title}</b>
                <b className="shrink-0 tnum">{num(g.size)} سانت</b>
              </CardContent>
            </Card>
          ))}
        </div>
      </Wrap>
    )
  }

  return (
    <>
      <Wrap>
        {header}
        {!d ? (
          loading ? (
            <><Skeleton className="h-32 w-full" /><div className="h-3" /><Skeleton className="h-28 w-full" /></>
          ) : (
            <div className="py-16 text-center text-sm text-muted-foreground">
              چیزی برای نمایش نیست
            </div>
          )
        ) : tab === 'home' ? (
          <Home d={d} onPickGroup={() => setPhase('groups')} onTransfer={() => setXfer(true)}
                onLogout={logout} onAction={setAction} onGroup={setGroupKind} onGrow={grow}
                onHeist={() => setHeist(true)} />
        ) : tab === 'feed' ? <Feed d={d} />
          : tab === 'crypto' ? <Crypto d={d} chat={chat!} reload={reload} />
          : tab === 'bank' ? <Bank d={d} chat={chat!} reload={reload} />
          : tab === 'shop' ? <Shop d={d} chat={chat!} reload={reload} />
          : <Bag d={d} chat={chat!} reload={reload} />}
      </Wrap>
      <Transfer chat={chat!} open={xfer} onClose={() => setXfer(false)} reload={reload} />
      <ActionSheet kind={action} chat={chat!} onClose={() => setAction(null)} reload={reload} />
      <GroupSheet kind={groupKind} chat={chat!} onClose={() => setGroupKind(null)} reload={reload} />
      <HeistSheet chat={chat!} open={heist} onClose={() => setHeist(false)} reload={reload} />
      <BottomNav tab={tab} onTab={setTab} unread={unread} />
    </>
  )
}

function Wrap({ children }: { children: React.ReactNode }) {
  return <div className="mx-auto max-w-2xl px-4 py-4">{children}</div>
}
