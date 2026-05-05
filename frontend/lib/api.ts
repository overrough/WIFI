const API_URL = process.env.NEXT_PUBLIC_API_URL || 'http://localhost:8000'
const API_KEY = process.env.NEXT_PUBLIC_API_KEY || 'local-dev-key'

const headers = () => ({
  'Content-Type': 'application/json',
  'X-API-Key': API_KEY,
})

export interface Message {
  id: string
  role: 'user' | 'assistant'
  content: string
  created_at?: string
}

export interface Conversation {
  id: string
  mode: string
  started_at: string
  summary?: string
}

export async function startConversation(mode: string = 'work'): Promise<{ conversation_id: string; mode: string }> {
  const res = await fetch(`${API_URL}/chat/start`, {
    method: 'POST',
    headers: headers(),
    body: JSON.stringify({ mode }),
  })
  if (!res.ok) throw new Error(`Failed to start conversation: ${res.status}`)
  return res.json()
}

export async function* streamMessage(
  conversationId: string,
  content: string
): AsyncGenerator<{ type: string; content: string }> {
  const res = await fetch(`${API_URL}/chat/${conversationId}/message`, {
    method: 'POST',
    headers: headers(),
    body: JSON.stringify({ content }),
  })

  if (!res.ok) throw new Error(`Request failed: ${res.status}`)
  if (!res.body) throw new Error('No response body')

  const reader = res.body.getReader()
  const decoder = new TextDecoder()
  let buffer = ''

  while (true) {
    const { done, value } = await reader.read()
    if (done) break

    buffer += decoder.decode(value, { stream: true })
    const lines = buffer.split('\n')
    buffer = lines.pop() || ''

    for (const line of lines) {
      if (line.startsWith('data: ')) {
        try {
          const data = JSON.parse(line.slice(6))
          yield data
        } catch {
          // ignore malformed SSE lines
        }
      }
    }
  }
}

export async function endConversation(conversationId: string): Promise<void> {
  await fetch(`${API_URL}/chat/${conversationId}/end`, {
    method: 'POST',
    headers: headers(),
  })
}

export async function getConversationMessages(conversationId: string): Promise<Message[]> {
  const res = await fetch(`${API_URL}/chat/${conversationId}/messages`, {
    headers: headers(),
  })
  if (!res.ok) throw new Error('Failed to load messages')
  return res.json()
}

export async function listConversations(): Promise<Conversation[]> {
  const res = await fetch(`${API_URL}/chat/history`, {
    headers: headers(),
  })
  if (!res.ok) return []
  return res.json()
}

export async function listTasks(status = 'pending') {
  const res = await fetch(`${API_URL}/tasks/?status=${status}`, {
    headers: headers(),
  })
  if (!res.ok) return []
  return res.json()
}

export async function searchMemory(query: string) {
  const res = await fetch(`${API_URL}/memory/search`, {
    method: 'POST',
    headers: headers(),
    body: JSON.stringify({ query, top_k: 5 }),
  })
  if (!res.ok) return []
  const data = await res.json()
  return data.results || []
}
