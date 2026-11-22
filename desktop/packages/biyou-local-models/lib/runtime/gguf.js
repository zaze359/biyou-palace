/**
 * GGUF 头部解析：只读文件前 32MB，不加载权重。
 *
 * 用途：导入已有 `.gguf` 时读出架构、量化、层数、训练上下文；模型目录里的
 * 元数据优先，文件头只作为兜底和校验。解析失败不阻断导入——调用方会退回
 * 目录/文件名信息并标注 unverified。
 */
import { open } from 'node:fs/promises'

const GGUF_MAGIC = 0x46554747
const MAX_HEADER_BYTES = 32 * 1024 * 1024
const MAX_ARRAY_DEPTH = 2

/** general.file_type → 常见量化名。 */
const FILE_TYPES = {
  0: 'F32',
  1: 'F16',
  2: 'Q4_0',
  3: 'Q4_1',
  7: 'Q8_0',
  8: 'Q5_0',
  9: 'Q5_1',
  10: 'Q2_K',
  11: 'Q3_K_S',
  12: 'Q3_K_M',
  13: 'Q3_K_L',
  14: 'Q4_K_S',
  15: 'Q4_K_M',
  16: 'Q5_K_S',
  17: 'Q5_K_M',
  18: 'Q6_K',
  19: 'IQ2_XXS',
  20: 'IQ2_XS',
  21: 'Q2_K_S',
  22: 'IQ3_XS',
  23: 'IQ3_XXS',
  24: 'IQ1_S',
  25: 'IQ4_NL',
  26: 'IQ3_S',
  27: 'IQ3_M',
  28: 'IQ2_S',
  29: 'IQ2_M',
  30: 'IQ4_XS',
  31: 'IQ1_M',
  32: 'BF16',
  36: 'TQ1_0',
  37: 'TQ2_0',
  38: 'MXFP4',
}

const TYPE_SIZES = { 0: 1, 1: 1, 2: 2, 3: 2, 4: 4, 5: 4, 6: 4, 7: 1, 10: 8, 11: 8, 12: 8 }

/** 解析 GGUF 头部；任何结构问题都抛出带 code 的错误。 */
export async function readGgufHeader(file) {
  const handle = await open(file, 'r')
  try {
    const buffer = Buffer.alloc(MAX_HEADER_BYTES)
    const { bytesRead } = await handle.read(buffer, 0, MAX_HEADER_BYTES, 0)
    const cursor = new Cursor(buffer.subarray(0, bytesRead))
    const magic = cursor.u32()
    if (magic !== GGUF_MAGIC) throw ggufError('INVALID_MAGIC', `不是 GGUF 文件：${file}`)
    const version = cursor.u32()
    if (version < 2 || version > 3) throw ggufError('UNSUPPORTED_VERSION', `不支持的 GGUF 版本：${version}`)
    const tensorCount = Number(cursor.u64())
    const metadataCount = Number(cursor.u64())
    const metadata = new Map()

    for (let index = 0; index < metadataCount; index += 1) {
      let key
      try {
        key = cursor.string()
      } catch (error) {
        if (error.code === 'EOF') break
        throw error
      }
      const type = cursor.u32()
      if (isInteresting(key)) {
        try {
          metadata.set(key, readValue(cursor, type, 0))
        } catch (error) {
          if (error.code === 'EOF') break
          // 单个元数据项坏了不拖垮整份文件；保留已读到的部分。
          break
        }
      } else {
        try {
          skipValue(cursor, type, 0)
        } catch (error) {
          if (error.code === 'EOF') break
          throw error
        }
      }
    }

    const architecture = asString(metadata.get('general.architecture')) ?? null
    const name = asString(metadata.get('general.name')) ?? null
    const contextLength = architecture ? asNumber(metadata.get(`${architecture}.context_length`)) : null
    const blockCount = architecture ? asNumber(metadata.get(`${architecture}.block_count`)) : null
    const embeddingLength = architecture ? asNumber(metadata.get(`${architecture}.embedding_length`)) : null
    const fileType = asNumber(metadata.get('general.file_type'))
    const quantization = FILE_TYPES[fileType] ?? null

    return {
      version,
      tensorCount,
      metadataCount,
      architecture,
      name,
      contextLength,
      blockCount,
      embeddingLength,
      fileType,
      quantization,
      createdBy: asString(metadata.get('general.creator')) ?? null,
      raw: Object.fromEntries(metadata),
    }
  } finally {
    await handle.close()
  }
}

function isInteresting(key) {
  return (
    key === 'general.architecture' ||
    key === 'general.name' ||
    key === 'general.file_type' ||
    key === 'general.creator' ||
    key.endsWith('.context_length') ||
    key.endsWith('.block_count') ||
    key.endsWith('.embedding_length')
  )
}

function readValue(cursor, type, depth) {
  switch (type) {
    case 0: return cursor.u8()
    case 1: return cursor.i8()
    case 2: return cursor.u16()
    case 3: return cursor.i16()
    case 4: return cursor.u32()
    case 5: return cursor.i32()
    case 6: return cursor.f32()
    case 7: return cursor.u8() !== 0
    case 8: return cursor.string()
    case 10: return Number(cursor.u64())
    case 11: return Number(cursor.i64())
    case 12: return cursor.f64()
    case 9: {
      if (depth > MAX_ARRAY_DEPTH) throw ggufError('TOO_DEEP', 'GGUF 数组嵌套过深')
      const elementType = cursor.u32()
      const count = Number(cursor.u64())
      skipArray(cursor, elementType, count, depth + 1)
      return null
    }
    default:
      throw ggufError('UNKNOWN_TYPE', `未知 GGUF 元数据类型：${type}`)
  }
}

function skipValue(cursor, type, depth) {
  if (type === 8) {
    cursor.skip(cursor.u64())
    return
  }
  if (type === 9) {
    if (depth > MAX_ARRAY_DEPTH) throw ggufError('TOO_DEEP', 'GGUF 数组嵌套过深')
    const elementType = cursor.u32()
    const count = Number(cursor.u64())
    skipArray(cursor, elementType, count, depth + 1)
    return
  }
  const size = TYPE_SIZES[type]
  if (!size) throw ggufError('UNKNOWN_TYPE', `未知 GGUF 元数据类型：${type}`)
  cursor.skip(size)
}

function skipArray(cursor, elementType, count, depth) {
  if (elementType === 8) {
    for (let index = 0; index < count; index += 1) cursor.skip(cursor.u64())
    return
  }
  if (elementType === 9) {
    for (let index = 0; index < count; index += 1) {
      const nestedType = cursor.u32()
      const nestedCount = Number(cursor.u64())
      skipArray(cursor, nestedType, nestedCount, depth + 1)
    }
    return
  }
  const size = TYPE_SIZES[elementType]
  if (!size) throw ggufError('UNKNOWN_TYPE', `未知 GGUF 数组元素类型：${elementType}`)
  cursor.skip(size * count)
}

function asString(value) {
  return typeof value === 'string' ? value : null
}

function asNumber(value) {
  return typeof value === 'number' && Number.isFinite(value) ? value : null
}

/** 字节游标；越界统一抛 EOF，让上层决定跳过还是中止。 */
class Cursor {
  #buffer
  #offset = 0

  constructor(buffer) {
    this.#buffer = buffer
  }

  get offset() {
    return this.#offset
  }

  skip(count) {
    const size = Number(count)
    if (!Number.isSafeInteger(size) || size < 0) throw ggufError('INVALID_LENGTH', 'GGUF 长度字段无效')
    if (this.#offset + size > this.#buffer.length) throw ggufError('EOF', 'GGUF 头部读取越界')
    this.#offset += size
  }

  u8() {
    this.#need(1)
    return this.#buffer[this.#offset++]
  }

  i8() {
    this.#need(1)
    const value = this.#buffer.readInt8(this.#offset)
    this.#offset += 1
    return value
  }

  u16() {
    this.#need(2)
    const value = this.#buffer.readUInt16LE(this.#offset)
    this.#offset += 2
    return value
  }

  i16() {
    this.#need(2)
    const value = this.#buffer.readInt16LE(this.#offset)
    this.#offset += 2
    return value
  }

  u32() {
    this.#need(4)
    const value = this.#buffer.readUInt32LE(this.#offset)
    this.#offset += 4
    return value
  }

  i32() {
    this.#need(4)
    const value = this.#buffer.readInt32LE(this.#offset)
    this.#offset += 4
    return value
  }

  u64() {
    this.#need(8)
    const value = this.#buffer.readBigUInt64LE(this.#offset)
    this.#offset += 8
    return value
  }

  i64() {
    this.#need(8)
    const value = this.#buffer.readBigInt64LE(this.#offset)
    this.#offset += 8
    return value
  }

  f32() {
    this.#need(4)
    const value = this.#buffer.readFloatLE(this.#offset)
    this.#offset += 4
    return value
  }

  f64() {
    this.#need(8)
    const value = this.#buffer.readDoubleLE(this.#offset)
    this.#offset += 8
    return value
  }

  string() {
    const length = this.u64()
    if (length > BigInt(Number.MAX_SAFE_INTEGER)) throw ggufError('INVALID_LENGTH', 'GGUF 字符串过长')
    const size = Number(length)
    this.#need(size)
    const value = this.#buffer.toString('utf8', this.#offset, this.#offset + size)
    this.#offset += size
    return value
  }

  #need(size) {
    if (this.#offset + size > this.#buffer.length) throw ggufError('EOF', 'GGUF 头部读取越界')
  }
}

function ggufError(code, message) {
  const error = new Error(message)
  error.code = code
  return error
}
