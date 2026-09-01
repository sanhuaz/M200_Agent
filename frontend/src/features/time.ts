import type { ApiTimestamp, TimeZone } from '../types/time'

export const DEFAULT_TIMEZONE = 'Asia/Shanghai'

type LocalDateParts = {
  year: string
  month: string
  day: string
  hour: string
  minute: string
  second: string
}

const warnedValues = new Set<string>()

function warnOnce(key: string, message: string) {
  if (warnedValues.has(key)) return
  warnedValues.add(key)
  console.warn(message)
}

export function normalizeTimeZone(timeZone?: TimeZone | null): string {
  const candidate = timeZone?.trim() || DEFAULT_TIMEZONE
  try {
    new Intl.DateTimeFormat('en-US', { timeZone: candidate }).format()
    return candidate
  } catch {
    warnOnce(`timezone:${candidate}`, `[time] 无法使用时区 ${candidate}，已回退到 ${DEFAULT_TIMEZONE}`)
    return DEFAULT_TIMEZONE
  }
}

function parseTimestamp(value: ApiTimestamp): Date | null {
  if (!value?.trim()) return null
  const date = new Date(value)
  return Number.isNaN(date.getTime()) ? null : date
}

function invalidTimestamp(value: ApiTimestamp, fallback: string): string {
  if (!value?.trim()) return fallback
  warnOnce(`timestamp:${value}`, `[time] 无法解析 API 时间 ${value}`)
  return value
}

function localDateParts(value: ApiTimestamp, timeZone: TimeZone): LocalDateParts | null {
  const date = parseTimestamp(value)
  if (!date) return null
  const parts = new Intl.DateTimeFormat('en-CA', {
    timeZone: normalizeTimeZone(timeZone),
    year: 'numeric',
    month: '2-digit',
    day: '2-digit',
    hour: '2-digit',
    minute: '2-digit',
    second: '2-digit',
    hourCycle: 'h23',
  }).formatToParts(date)
  const result = Object.fromEntries(parts.map((part) => [part.type, part.value])) as Partial<LocalDateParts>
  return {
    year: result.year || '',
    month: result.month || '',
    day: result.day || '',
    hour: result.hour || '',
    minute: result.minute || '',
    second: result.second || '',
  }
}

export function formatLocalDate(value: ApiTimestamp, timeZone: TimeZone = DEFAULT_TIMEZONE): string {
  const parts = localDateParts(value, timeZone)
  if (!parts) return invalidTimestamp(value, '日期未知')
  return `${parts.year}年${Number(parts.month)}月${Number(parts.day)}日`
}

export function formatLocalDateKey(value: ApiTimestamp, timeZone: TimeZone = DEFAULT_TIMEZONE): string | null {
  const parts = localDateParts(value, timeZone)
  return parts ? `${parts.year}-${parts.month}-${parts.day}` : null
}

export function sameLocalDay(
  first: ApiTimestamp,
  second: ApiTimestamp,
  timeZone: TimeZone = DEFAULT_TIMEZONE,
): boolean {
  const firstKey = formatLocalDateKey(first, timeZone)
  const secondKey = formatLocalDateKey(second, timeZone)
  return firstKey !== null && firstKey === secondKey
}

export function formatLocalTime(value: ApiTimestamp, timeZone: TimeZone = DEFAULT_TIMEZONE): string {
  const parts = localDateParts(value, timeZone)
  if (!parts) return invalidTimestamp(value, '时间未知')
  return `${parts.hour}:${parts.minute}`
}

export function formatLocalDateTime(value: ApiTimestamp, timeZone: TimeZone = DEFAULT_TIMEZONE): string {
  const parts = localDateParts(value, timeZone)
  if (!parts) return invalidTimestamp(value, '时间未知')
  const zone = normalizeTimeZone(timeZone)
  return `${parts.year}-${parts.month}-${parts.day} ${parts.hour}:${parts.minute}:${parts.second}（${zone}）`
}

function isValidCalendarDate(year: number, month: number, day: number): boolean {
  const date = new Date(Date.UTC(year, month - 1, day))
  return date.getUTCFullYear() === year && date.getUTCMonth() === month - 1 && date.getUTCDate() === day
}

export function formatCalendarDate(value: string | null | undefined): string {
  if (!value?.trim()) return '未精确记录'
  const match = /^(\d{4})-(\d{2})-(\d{2})$/.exec(value.trim())
  if (!match || !isValidCalendarDate(Number(match[1]), Number(match[2]), Number(match[3]))) {
    warnOnce(`calendar:${value}`, `[time] 无法解析 API 日期 ${value}`)
    return value
  }
  return value
}
