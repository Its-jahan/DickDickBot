import { useState } from 'react'
import { Sheet, SheetContent } from '@/components/ui/sheet'
import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import { PlayerPicker } from '@/components/PlayerPicker'
import { api } from '@/lib/api'
import { useToast } from '@/components/Toast'

export type ActionKind = 'steal' | 'donate'

const META: Record<ActionKind, { title: string; verb: string; note: string; amount: boolean }> = {
  steal: {
    title: '🥷 دزدی',
    verb: 'بدزد',
    note: 'هرچی طرف بزرگ‌تر باشه سخت‌تره. اگه گیر بیفتی غرامت می‌دی — و نتیجه هرچی باشه توی گروه اعلام می‌شه.',
    amount: false,
  },
  donate: {
    title: '🎁 اهدای سایز',
    verb: 'اهدا کن',
    note: 'سایز از جیب تو کم و به او اضافه می‌شه. توی گروه اعلام می‌شه.',
    amount: true,
  },
}

/**
 * The group actions that fit in a browser: one target, one outcome, one announcement.
 *
 * A challenge, an /ejma or a heist deliberately stay in the chat - their whole point is
 * other people reacting to a message, and half of them need somebody else to tap a
 * button that does not exist here.
 */
export function ActionSheet({ kind, chat, onClose, reload }: {
  kind: ActionKind | null
  chat: number
  onClose: () => void
  reload: () => Promise<void>
}) {
  const [target, setTarget] = useState<number | null>(null)
  const [amount, setAmount] = useState('')
  const [busy, setBusy] = useState(false)
  const toast = useToast()

  if (!kind) return null
  const meta = META[kind]

  const go = async () => {
    if (!target) return toast('اول یه نفر رو انتخاب کن', true)
    const v = Number(amount)
    if (meta.amount && !v) return toast('مقدار رو بنویس', true)
    setBusy(true)
    try {
      const r = await api<any>(`/api/${kind}`, meta.amount ? { target, amount: v } : { target }, chat)
      onClose()
      setTarget(null)
      setAmount('')
      toast(r.message)
      await reload()
    } catch (e: any) {
      toast(e.message, true)
    } finally {
      setBusy(false)
    }
  }

  return (
    <Sheet open onOpenChange={(o) => !o && onClose()}>
      <SheetContent title={meta.title}>
        <div className="mb-3 text-lg font-bold">{meta.title}</div>
        <div className="space-y-3">
          <PlayerPicker chat={chat} value={target} onPick={(id) => setTarget(id)} />
          {meta.amount && (
            <Input type="number" inputMode="numeric" placeholder="مقدار سانت"
                   value={amount} onChange={(e) => setAmount(e.target.value)} />
          )}
          <Button size="lg" disabled={busy} onClick={go}>{meta.verb}</Button>
          <p className="text-center text-[11px] leading-relaxed text-muted-foreground">
            📣 {meta.note}
          </p>
        </div>
      </SheetContent>
    </Sheet>
  )
}
