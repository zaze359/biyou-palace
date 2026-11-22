/**
 * `biyou-local` provider：把 dsh 的请求转成 OpenAI chat/completions，再把
 * llama-server 的 SSE 还原成 dsh 的 StreamChunk。
 *
 * 约定：
 * - 模型按需加载，失败信息带模型 id 和本地服务状态；
 * - tool calls 保持原始 JSON 字符串，不在这里解析/重排；
 * - usage 在 finish 之前发出，finish 之后不再产出任何 chunk；
 * - API key 只在 supervisor 内部使用，provider 拿不到。
 */
export const PROVIDER_ID = 'biyou-local'

export function createLocalAdapter({ models, supervisor, logger = console }) {
  return {
    providerInfo(provider) {
      return { id: provider, name: '碧游宫 · 本地模型' }
    },

    /** 使用 dsh 的默认重试策略；本地服务失败不在 adapter 内重试。 */
    providerRetryPolicy() {
      return undefined
    },

    /** 首版只声明文本模态；图片由 dsh 在请求投影阶段替换成占位文本。 */
    imageRequestPricing() {
      return undefined
    },

    /** 不做缩图；保持归一化后的尺寸。 */
    imageRequestTarget(_provider, _model, image) {
      return { width: image.width, height: image.height, maxBytes: image.bytes }
    },

    async listModels(provider) {
      const status = await models.status()
      return status.models
        .filter((model) => model.servable)
        .map((model) => ({
          provider,
          id: model.id,
          name: model.display_name,
          description: `${model.context?.default ?? model.context?.train ?? 0} ctx · ${model.license ?? 'unknown'} · 本地`,
          inputModalities: ['text'],
          badge: { text: '本地', tone: 'info' },
        }))
    },

    async resolveModel(provider, modelId) {
      const model = await models.catalogModel(modelId)
      const entry = await models.registryEntry(modelId)
      if (!model && !entry) throw providerError('MODEL_NOT_FOUND', `本地模型不存在：${modelId}`)
      if (!entry) throw providerError('MODEL_NOT_INSTALLED', `本地模型尚未安装：${modelId}`)
      const contextWindow = Number(model?.context?.default ?? entry.context?.default ?? entry.context?.train) || 40960
      return {
        provider,
        id: modelId,
        name: model?.display_name ?? entry.display_name ?? modelId,
        description: model?.description ?? entry.description ?? '本地模型',
        inputModalities: ['text'],
        context: { contextWindow },
        defaultMaxTokens: Math.min(4096, Math.max(512, Math.floor(contextWindow / 4))),
      }
    },

    async prepareCall(provider, modelId, signal) {
      const model = await this.resolveModel(provider, modelId, signal)
      return {
        model,
        stream: (options) => this.stream(options),
      }
    },

    async *stream(options) {
      const modelId = options.model
      const resolved = await this.resolveModel(options.provider, modelId)
      if (!(await models.isInstalled(modelId))) {
        throw providerError('MODEL_NOT_INSTALLED', `本地模型尚未安装：${modelId}`)
      }

      let response
      try {
        response = await supervisor.chat(buildRequestBody(options, resolved), options.signal)
      } catch (error) {
        if (error.name === 'AbortError') throw error
        throw providerError(error.code ?? 'PROVIDER_UNAVAILABLE', `本地服务请求失败（${modelId}）：${error.message}`)
      }

      if (!response.ok) {
        const text = await response.text().catch(() => '')
        const code = response.status === 400 && /context|too many tokens/i.test(text)
          ? 'CONTEXT_WINDOW_EXCEEDED'
          : `HTTP_${response.status}`
        throw providerError(code, `本地模型 ${modelId} 返回 HTTP ${response.status}：${text.slice(0, 400)}`)
      }
      if (!response.body) throw providerError('EMPTY_STREAM', `本地模型 ${modelId} 没有返回流`)

      yield* streamChunks(response.body, options.signal, logger)
    },
  }
}

/** dsh messages/tools → OpenAI chat/completions body。 */
export function buildRequestBody(options, resolved) {
  const body = {
    model: options.model,
    messages: toOpenAiMessages(options.messages, options.system),
    stream: true,
    stream_options: { include_usage: true },
    max_tokens: options.maxTokens ?? resolved.defaultMaxTokens ?? 4096,
  }
  if (typeof options.temperature === 'number') body.temperature = options.temperature
  if (Array.isArray(options.stop) && options.stop.length > 0) body.stop = options.stop
  if (Array.isArray(options.tools) && options.tools.length > 0) {
    body.tools = options.tools.map((tool) => ({
      type: 'function',
      function: {
        name: tool.name,
        description: tool.description,
        parameters: tool.parameters ?? { type: 'object', properties: {} },
      },
    }))
    body.tool_choice = 'auto'
  }
  return body
}

/** 把 dsh 的 content blocks 压成 OpenAI 消息；reasoning 不进历史。 */
export function toOpenAiMessages(messages, system) {
  const output = []
  if (system !== undefined && system !== '') output.push({ role: 'system', content: system })
  for (const message of messages ?? []) {
    const blocks = Array.isArray(message.content)
      ? message.content
      : typeof message.content === 'string'
        ? [{ type: 'text', text: message.content }]
        : []
    switch (message.role) {
      case 'system': {
        const text = textOf(blocks)
        if (text) output.push({ role: 'system', content: text })
        break
      }
      case 'developer': {
        // tool-addition / tool-removal 是 dsh 的投影元数据，llama-server 不认；
        // 普通 developer 文本按 system 处理，保住“工具说明”这类上下文。
        const text = textOf(blocks)
        if (text) output.push({ role: 'system', content: text })
        break
      }
      case 'user': {
        const text = textOf(blocks)
        output.push({ role: 'user', content: text })
        break
      }
      case 'assistant': {
        const text = textOf(blocks)
        const toolCalls = blocks
          .filter((block) => block.type === 'tool-call')
          .map((block) => ({
            id: block.id,
            type: 'function',
            function: { name: block.name, arguments: block.arguments || '{}' },
          }))
        output.push({
          role: 'assistant',
          content: text || '',
          ...toolCalls.length > 0 ? { tool_calls: toolCalls } : {},
        })
        break
      }
      case 'tool': {
        output.push({
          role: 'tool',
          tool_call_id: message.toolCallId,
          content: textOf(message.content),
        })
        break
      }
      default:
        break
    }
  }
  return output
}

function textOf(blocks) {
  if (typeof blocks === 'string') return blocks
  return (blocks ?? [])
    .filter((block) => block?.type === 'text')
    .map((block) => block.text)
    .join('')
}

/** 解析 OpenAI SSE；所有块在 finish 之前闭合。 */
export async function* streamChunks(body, signal, logger = console) {
  const reader = body.getReader()
  const decoder = new TextDecoder()
  let buffer = ''
  let textIndex = null
  let text = ''
  let reasoningIndex = null
  let reasoning = ''
  let nextIndex = 0
  let finishReason = null
  let usage = null
  const toolCalls = new Map()
  let sawDone = false

  const handleEvent = function* (event) {
    for (const line of event.split('\n')) {
      const trimmed = line.trim()
      if (!trimmed || trimmed.startsWith(':') || !trimmed.startsWith('data:')) continue
      const data = trimmed.slice(5).trim()
      if (data === '[DONE]') {
        sawDone = true
        return
      }
      let payload
      try {
        payload = JSON.parse(data)
      } catch {
        logger.warn?.(`[biyou-local-models] 忽略无法解析的 SSE 行：${data.slice(0, 120)}`)
        continue
      }
      if (payload?.error) {
        const message = payload.error.message ?? JSON.stringify(payload.error)
        throw providerError(payload.error.code ?? 'STREAM_ERROR', String(message))
      }
      if (payload?.usage) usage = payload.usage
      const choice = payload?.choices?.[0]
      if (!choice) continue
      if (choice.finish_reason) finishReason = choice.finish_reason
      const delta = choice.delta ?? {}
      if (typeof delta.reasoning_content === 'string' && delta.reasoning_content.length > 0) {
        if (reasoningIndex === null) {
          reasoningIndex = nextIndex++
          yield { type: 'block-start', index: reasoningIndex, blockType: 'reasoning' }
        }
        reasoning += delta.reasoning_content
        yield { type: 'reasoning-delta', index: reasoningIndex, text: delta.reasoning_content }
      }
      if (typeof delta.content === 'string' && delta.content.length > 0) {
        if (textIndex === null) {
          textIndex = nextIndex++
          yield { type: 'block-start', index: textIndex, blockType: 'text' }
        }
        text += delta.content
        yield { type: 'text-delta', index: textIndex, text: delta.content }
      }
      for (const call of delta.tool_calls ?? []) {
        const key = call.index ?? 0
        let state = toolCalls.get(key)
        if (!state) {
          state = {
            index: nextIndex++,
            id: call.id ?? `call_${key}_${Date.now()}`,
            name: call.function?.name ?? '',
            arguments: '',
          }
          toolCalls.set(key, state)
          yield { type: 'block-start', index: state.index, blockType: 'tool-call' }
        }
        if (call.id) state.id = call.id
        if (call.function?.name) state.name = call.function.name
        const deltaArguments = call.function?.arguments ?? ''
        if (deltaArguments) state.arguments += deltaArguments
        yield {
          type: 'tool-call-delta',
          index: state.index,
          id: state.id,
          ...state.name ? { name: state.name } : {},
          argumentsDelta: deltaArguments,
        }
      }
    }
  }

  try {
    while (true) {
      if (signal?.aborted) throw abortError()
      const { done, value } = await reader.read()
      if (done) break
      buffer += decoder.decode(value, { stream: true })
      let boundary
      while (true) {
        const lf = buffer.indexOf('\n\n')
        const crlf = buffer.indexOf('\r\n\r\n')
        if (lf < 0 && crlf < 0) break
        const useCrlf = crlf >= 0 && (lf < 0 || crlf < lf)
        boundary = useCrlf ? crlf : lf
        const separator = useCrlf ? 4 : 2
        const event = buffer.slice(0, boundary)
        buffer = buffer.slice(boundary + separator)
        yield* handleEvent(event)
        if (sawDone) break
      }
      if (sawDone) break
    }
    if (!sawDone && buffer.trim()) yield* handleEvent(buffer)
  } finally {
    reader.releaseLock()
  }

  if (textIndex !== null) yield { type: 'block-end', index: textIndex, block: { type: 'text', text } }
  if (reasoningIndex !== null) yield { type: 'block-end', index: reasoningIndex, block: { type: 'reasoning', text: reasoning } }
  for (const state of toolCalls.values()) {
    yield {
      type: 'block-end',
      index: state.index,
      block: { type: 'tool-call', id: state.id, name: state.name, arguments: state.arguments || '{}' },
    }
  }
  if (usage) yield { type: 'usage', usage: normalizeUsage(usage) }
  yield {
    type: 'finish',
    reason: { kind: mapFinishReason(finishReason, toolCalls.size > 0) },
  }
}

function normalizeUsage(usage) {
  const cached = Number(usage.prompt_tokens_details?.cached_tokens) || 0
  const prompt = Number(usage.prompt_tokens) || 0
  return {
    inputTokens: Math.max(0, prompt - cached),
    outputTokens: Number(usage.completion_tokens) || 0,
    ...Number.isFinite(usage.total_tokens) ? { totalTokens: Number(usage.total_tokens) } : {},
    ...cached > 0 ? { cacheReadTokens: cached } : {},
  }
}

function mapFinishReason(reason, hasToolCalls) {
  if (reason === 'length') return 'max-tokens'
  if (hasToolCalls || reason === 'tool_calls') return 'tool-calls'
  return 'stop'
}

function abortError() {
  const error = new Error('已取消')
  error.name = 'AbortError'
  return error
}

export function providerError(code, message) {
  const error = new Error(message)
  error.code = code
  return error
}
