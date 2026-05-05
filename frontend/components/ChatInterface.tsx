'use client'

import { useCallback, useEffect, useRef, useState } from 'react'
import { Send, Square, Zap } from 'lucide-react'
import clsx from 'clsx'
import MessageBubble from './MessageBubble'
import ModeSelector from './ModeSelector'
import {
  endConversation,
  startConversation,
  streamMessage,
  type Message,
} from '@/lib/api'

type Mode = 'work' | 'personal' | 'strategic'

const SUGGESTED_PROMPTS = [
  "What should I focus on today?",
  "Create a task: review the proposal by Friday",
  "What's the most important thing I haven't done yet?",
  "Switch to strategic mode",
  "Remember that I work best in the mornings",
]

export default function ChatInterface() {
  const [messages, setMessages] = useState<Message[]>([])
  const [input, setInput] = useState('')
  const [mode, setMode] = useState<Mode>('work')
  const [conversationId, setConversationId] = useState<string | null>(null)
  const [isStreaming, setIsStreaming] = useState(false)
  const [streamingId, setStreamingId] = useState<string | null>(null)
  const [error, setError] = useState<string | null>(null)

  const bottomRef = useRef<HTMLDivElement>(null)
  const textareaRef = useRef<HTMLTextAreaElement>(null)
  const abortRef = useRef<boolean>(false)

  // Auto-scroll to bottom
  useEffect(() => {
    bottomRef.current?.scrollIntoView({ behavior: 'smooth' })
  }, [messages])

  // Auto-resize textarea
  useEffect(() => {
    const el = textareaRef.current
    if (!el) return
    el.style.height = 'auto'
    el.style.height = `${Math.min(el.scrollHeight, 160)}px`
  }, [input])

  const ensureConversation = useCallback(async (): Promise<string> => {
    if (conversationId) return conversationId
    const res = await startConversation(mode)
    setConversationId(res.conversation_id)
    return res.conversation_id
  }, [conversationId, mode])

  const handleModeChange = useCallback(async (newMode: Mode) => {
    setMode(newMode)
    // End old conversation, new one picks up the mode on next message
    if (conversationId) {
      try { await endConversation(conversationId) } catch { /* ignore */ }
      setConversationId(null)
    }
    // Visual feedback
    const sysMsg: Message = {
      id: `sys-${Date.now()}`,
      role: 'assistant',
      content: `Switched to ${newMode.charAt(0).toUpperCase() + newMode.slice(1)} mode.`,
    }
    setMessages(prev => [...prev, sysMsg])
  }, [conversationId])

  const sendMessage = useCallback(async (text: string) => {
    const trimmed = text.trim()
    if (!trimmed || isStreaming) return

    setError(null)
    abortRef.current = false

    // Detect mode switch commands
    const modeMatch = trimmed.toLowerCase().match(/switch to (work|personal|strategic) mode/)
    if (modeMatch) {
      await handleModeChange(modeMatch[1] as Mode)
      setInput('')
      return
    }

    const userMsg: Message = {
      id: `user-${Date.now()}`,
      role: 'user',
      content: trimmed,
    }
    setMessages(prev => [...prev, userMsg])
    setInput('')

    const assistantId = `asst-${Date.now()}`
    const placeholder: Message = { id: assistantId, role: 'assistant', content: '' }
    setMessages(prev => [...prev, placeholder])
    setStreamingId(assistantId)
    setIsStreaming(true)

    try {
      const convId = await ensureConversation()
      let fullContent = ''

      for await (const event of streamMessage(convId, trimmed)) {
        if (abortRef.current) break
        if (event.type === 'token') {
          fullContent += event.content
          setMessages(prev =>
            prev.map(m => m.id === assistantId ? { ...m, content: fullContent } : m)
          )
        } else if (event.type === 'done') {
          break
        } else if (event.type === 'error') {
          throw new Error(event.content)
        }
      }
    } catch (err) {
      const msg = err instanceof Error ? err.message : 'Something went wrong'
      setError(msg)
      setMessages(prev =>
        prev.map(m =>
          m.id === assistantId
            ? { ...m, content: `The request failed: ${msg}` }
            : m
        )
      )
    } finally {
      setIsStreaming(false)
      setStreamingId(null)
    }
  }, [isStreaming, ensureConversation, handleModeChange])

  const handleKeyDown = useCallback((e: React.KeyboardEvent) => {
    if (e.key === 'Enter' && !e.shiftKey) {
      e.preventDefault()
      sendMessage(input)
    }
  }, [input, sendMessage])

  const handleStop = useCallback(() => {
    abortRef.current = true
    setIsStreaming(false)
    setStreamingId(null)
  }, [])

  const isEmpty = messages.length === 0

  return (
    <div className="flex flex-col h-screen bg-jarvis-bg">
      {/* ── Header ─────────────────────────────────────────── */}
      <header className="flex items-center justify-between px-6 py-4 border-b border-jarvis-border bg-jarvis-surface/50 backdrop-blur-sm">
        <div className="flex items-center gap-3">
          <div className="w-8 h-8 rounded-full bg-jarvis-accent/20 border border-jarvis-accent/40 flex items-center justify-center">
            <Zap size={14} className="text-jarvis-accent" />
          </div>
          <div>
            <h1 className="text-sm font-semibold text-white tracking-wide">JARVIS</h1>
            <p className="text-xs text-jarvis-muted">Digital Chief of Staff</p>
          </div>
        </div>
        <ModeSelector current={mode} onChange={handleModeChange} disabled={isStreaming} />
      </header>

      {/* ── Messages ───────────────────────────────────────── */}
      <main className="flex-1 overflow-y-auto px-4 py-6 space-y-5">
        {isEmpty ? (
          <div className="flex flex-col items-center justify-center h-full gap-8 text-center px-4">
            <div className="space-y-2">
              <div className="w-16 h-16 mx-auto rounded-full bg-jarvis-accent/10 border border-jarvis-accent/30 flex items-center justify-center">
                <Zap size={28} className="text-jarvis-accent" />
              </div>
              <h2 className="text-2xl font-bold text-white">Good to see you.</h2>
              <p className="text-jarvis-muted text-sm max-w-sm">
                Tell me what you need. I remember, I plan, I execute.
              </p>
            </div>
            <div className="flex flex-wrap gap-2 justify-center max-w-lg">
              {SUGGESTED_PROMPTS.map(prompt => (
                <button
                  key={prompt}
                  onClick={() => sendMessage(prompt)}
                  className="px-3 py-2 text-xs rounded-lg border border-jarvis-border bg-jarvis-surface
                             text-jarvis-muted hover:text-jarvis-text hover:border-jarvis-accent/40
                             transition-all duration-150"
                >
                  {prompt}
                </button>
              ))}
            </div>
          </div>
        ) : (
          messages.map(msg => (
            <MessageBubble
              key={msg.id}
              message={msg}
              isStreaming={isStreaming && msg.id === streamingId}
            />
          ))
        )}
        {error && (
          <div className="text-xs text-jarvis-danger bg-jarvis-danger/10 border border-jarvis-danger/20 rounded-lg px-4 py-3">
            {error}
          </div>
        )}
        <div ref={bottomRef} />
      </main>

      {/* ── Input ──────────────────────────────────────────── */}
      <footer className="px-4 pb-4 pt-2 border-t border-jarvis-border bg-jarvis-surface/30">
        <div className="max-w-3xl mx-auto">
          <div className={clsx(
            'flex items-end gap-3 rounded-2xl border px-4 py-3 transition-colors duration-150',
            'bg-jarvis-surface',
            isStreaming
              ? 'border-jarvis-accent/50'
              : 'border-jarvis-border focus-within:border-jarvis-accent/50'
          )}>
            <textarea
              ref={textareaRef}
              value={input}
              onChange={e => setInput(e.target.value)}
              onKeyDown={handleKeyDown}
              placeholder="Talk to Jarvis..."
              rows={1}
              disabled={isStreaming}
              className={clsx(
                'flex-1 bg-transparent resize-none outline-none text-sm text-jarvis-text',
                'placeholder:text-jarvis-muted leading-relaxed',
                'disabled:opacity-50 disabled:cursor-not-allowed'
              )}
            />
            {isStreaming ? (
              <button
                onClick={handleStop}
                className="flex-shrink-0 w-8 h-8 rounded-lg bg-jarvis-danger/20 border border-jarvis-danger/40
                           text-jarvis-danger flex items-center justify-center hover:bg-jarvis-danger/30
                           transition-colors duration-150"
                title="Stop"
              >
                <Square size={12} fill="currentColor" />
              </button>
            ) : (
              <button
                onClick={() => sendMessage(input)}
                disabled={!input.trim()}
                className={clsx(
                  'flex-shrink-0 w-8 h-8 rounded-lg flex items-center justify-center transition-all duration-150',
                  input.trim()
                    ? 'bg-jarvis-accent text-jarvis-bg hover:bg-jarvis-accent/90'
                    : 'bg-jarvis-border text-jarvis-muted cursor-not-allowed'
                )}
                title="Send (Enter)"
              >
                <Send size={13} />
              </button>
            )}
          </div>
          <p className="text-center text-[10px] text-jarvis-muted/50 mt-2">
            Enter to send · Shift+Enter for newline · Mode: {mode}
          </p>
        </div>
      </footer>
    </div>
  )
}
