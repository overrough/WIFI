'use client'

import clsx from 'clsx'

const MODES = [
  { id: 'work',      label: 'Work',     desc: 'Execution-focused' },
  { id: 'personal',  label: 'Personal', desc: 'Coach-style' },
  { id: 'strategic', label: 'Strategic', desc: 'Co-founder lens' },
] as const

type Mode = 'work' | 'personal' | 'strategic'

interface Props {
  current: Mode
  onChange: (mode: Mode) => void
  disabled?: boolean
}

export default function ModeSelector({ current, onChange, disabled }: Props) {
  return (
    <div className="flex gap-1.5">
      {MODES.map(m => (
        <button
          key={m.id}
          onClick={() => onChange(m.id)}
          disabled={disabled}
          title={m.desc}
          className={clsx(
            'px-3 py-1.5 rounded-md text-xs font-medium transition-all duration-150',
            'border focus:outline-none focus:ring-1 focus:ring-jarvis-accent',
            current === m.id
              ? 'bg-jarvis-accent/10 border-jarvis-accent text-jarvis-accent'
              : 'bg-transparent border-jarvis-border text-jarvis-muted hover:border-jarvis-accent/50 hover:text-jarvis-text',
            disabled && 'opacity-40 cursor-not-allowed'
          )}
        >
          {m.label}
        </button>
      ))}
    </div>
  )
}
