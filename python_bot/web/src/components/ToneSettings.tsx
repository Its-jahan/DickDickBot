import { useState } from 'react'
import { Settings2, ShieldCheck } from 'lucide-react'
import { api } from '@/lib/api'
import { tap } from '@/lib/tg'
import type { ToneMode } from '@/lib/tone'
import { Button } from '@/components/ui/button'
import { Sheet, SheetContent, SheetTrigger } from '@/components/ui/sheet'
import { useToast } from '@/components/Toast'

export function ToneSettings({ chat, tone, onTone }: {
  chat: number; tone: ToneMode; onTone: (tone: ToneMode) => void
}) {
  const [busy, setBusy] = useState(false)
  const toast = useToast()

  const change = async (mode: ToneMode) => {
    setBusy(true); tap()
    try {
      const result = await api<any>('/api/settings/tone', { mode }, chat)
      onTone(result.tone)
      toast(result.message)
    } catch (e: any) {
      toast(e.message, true)
    } finally {
      setBusy(false)
    }
  }

  return (
    <Sheet>
      <SheetTrigger asChild>
        <Button variant="ghost" size="icon" aria-label="تنظیمات گروه">
          <Settings2 className="h-4 w-4" />
        </Button>
      </SheetTrigger>
      <SheetContent title="تنظیمات گروه">
        <div className="mb-5 flex items-center gap-2">
          <ShieldCheck className="h-5 w-5 text-primary" />
          <h2 className="font-bold">لحن بازی این گروه</h2>
        </div>
        <p className="mb-4 text-sm leading-7 text-muted-foreground">
          این انتخاب فقط روی متن‌های همین گروه اثر می‌گذارد. فقط ادمین گروه می‌تواند آن را عوض کند.
        </p>
        <div className="grid grid-cols-2 gap-2">
          <Button variant={tone === 'adult' ? 'default' : 'outline'} disabled={busy}
                  onClick={() => change('adult')}>لحن +۱۸</Button>
          <Button variant={tone === 'polite' ? 'default' : 'outline'} disabled={busy}
                  onClick={() => change('polite')}>لحن محترمانه</Button>
        </div>
      </SheetContent>
    </Sheet>
  )
}
