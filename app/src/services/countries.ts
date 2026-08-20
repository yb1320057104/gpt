export const COMMON_REGISTRATION_COUNTRIES = [
  { value: 'JP', label: '日本' },
  { value: 'TR', label: '土耳其' },
  { value: 'DE', label: '德国' },
  { value: 'US', label: '美国' },
  { value: 'GB', label: '英国' },
  { value: 'BR', label: '巴西' },
  { value: 'CA', label: '加拿大' },
  { value: 'SG', label: '新加坡' },
  { value: 'HK', label: '中国香港' },
  { value: 'TW', label: '中国台湾' },
  { value: 'ID', label: '印度尼西亚' },
  { value: 'PH', label: '菲律宾' },
  { value: 'NL', label: '荷兰' },
  { value: 'IN', label: '印度' },
  { value: 'PL', label: '波兰' },
  { value: 'CH', label: '瑞士' },
  { value: 'KR', label: '韩国' },
  { value: 'VN', label: '越南' },
  { value: 'TH', label: '泰国' },
  { value: 'BA', label: '波黑' },
  { value: 'AE', label: '阿联酋' },
  { value: 'DK', label: '丹麦' },
  { value: 'ES', label: '西班牙' },
  { value: 'FI', label: '芬兰' },
  { value: 'FR', label: '法国' },
  { value: 'MX', label: '墨西哥' },
] as const

const COUNTRY_NAMES = new Map<string, string>(
  COMMON_REGISTRATION_COUNTRIES.map((item) => [item.value, item.label]),
)

export function normalizeCountryCode(value: string | null | undefined) {
  const normalized = String(value || '').trim().toUpperCase()
  return /^[A-Z]{2}$/.test(normalized) ? normalized : 'ZZ'
}

export function countryLabel(value: string | null | undefined) {
  const code = normalizeCountryCode(value)
  if (code === 'ZZ') return '未分类'
  return `${COUNTRY_NAMES.get(code) || code} · ${code}`
}

export function countryName(value: string | null | undefined) {
  const code = normalizeCountryCode(value)
  if (code === 'ZZ') return '未分类'
  return COUNTRY_NAMES.get(code) || `其他国家（${code}）`
}
