'use client'

import clsx from 'clsx'
import { Message } from '@/lib/api'

interface Props {
  message: Message
  isStreaming?: boolean
}

function renderContent(content: string): string {
  // Very basic markdown-like rendering (no lib dependency)
  return content
    .replace(/\*\*(.*?)\*\*/g, '<strong>$1</strong>')
    .replace(/`([^`]+)`/g, '<code>$1</code>')
    .replace(/\n/g, '<br />')
}

export default function MessageBubble({ message, isStreaming }: Props) {
  const isUser = message.role === 'user'

  return (
    <div
      className={clsx(
        'flex gap-3 animate-slide-up',
        isUser ? 'flex-row-reverse' : 'flex-row'
      )}
    >
      {/* Avatar */}
      <div className={clsx(
        'w-8 h-8 rounded-full flex items-center justify-center flex-shrink-0 text-xs font-bold',
        isUser
          ? 'bg-jarvis-gold/20 text-jarvis-gold border border-jarvis-gold/30'
          : 'bg-jarvis-accent/10 text-jarvis-accent border border-jarvis-accent/30'
      )}>
        {isUser ? 'YOU' : 'J'}
      </div>

      {/* Bubble */}
      <div className={clsx(
        'max-w-[80%] rounded-2xl px-4 py-3 text-sm leading-relaxed',
        isUser
          ? 'bg-jarvis-gold/10 border border-jarvis-gold/20 text-jarvis-text rounded-tr-sm'
          : 'bg-jarvis-surface border border-jarvis-border text-jarvis-text rounded-tl-sm'
      )}>
        {isUser ? (
          <p className="whitespace-pre-wrap">{message.content}</p>
        ) : (
          <div
            className={clsx(
              'prose-jarvis',
              isStreaming && !message.content && 'cursor-blink',
              isStreaming && message.content && 'cursor-blink'
            )}
            dangerouslySetInnerHTML={{
              __html: renderContent(message.content || ''),
            }}
          />
        )}
      </div>
    </div>
  )
}
