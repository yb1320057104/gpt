import { describe, expect, it } from 'vitest'
import { parseAccountImport, parseEmailImport, parseProxyImport, proxyKey } from './parsers'

describe('parseAccountImport', () => {
  it('auto-detects all four formats in one mixed batch', () => {
    const secret = 'JBSWY3DPEHPK3PXP'
    const result = parseAccountImport([
      `first@example.com----Password-1----${secret}`,
      'second@example.com----Password-2----https://mail.test/second',
      'third@example.com----https://mail.test/third',
      `fourth@example.com----https://mail.test/fourth----${secret}`,
    ].join('\n'))

    expect(result.errors).toHaveLength(0)
    expect(result.accepted).toEqual([
      { email: 'first@example.com', chatgptPassword: 'Password-1', totpSecret: secret, emailAccessUrl: '' },
      { email: 'second@example.com', chatgptPassword: 'Password-2', totpSecret: '', emailAccessUrl: 'https://mail.test/second' },
      { email: 'third@example.com', chatgptPassword: '', totpSecret: '', emailAccessUrl: 'https://mail.test/third' },
      { email: 'fourth@example.com', chatgptPassword: '', totpSecret: secret, emailAccessUrl: 'https://mail.test/fourth' },
    ])
  })

  it('reports duplicate and malformed rows without exposing credentials', () => {
    const result = parseAccountImport(
      'known@example.com----Password----JBSWY3DPEHPK3PXP\n' +
      'bad@example.com----not-a-url',
      ['known@example.com'],
    )

    expect(result.duplicates).toHaveLength(1)
    expect(result.errors).toHaveLength(1)
    expect(JSON.stringify([...result.duplicates, ...result.errors])).not.toContain('Password')
  })
})

describe('parseEmailImport', () => {
  it('cleans BOM and blank lines and accepts the documented format', () => {
    const result = parseEmailImport(
      '\uFEFFdemo@example.com----https://example.com/inbox/token\n\n  second@example.com----https://example.com/s/2  ',
    )

    expect(result.total).toBe(2)
    expect(result.accepted).toEqual([
      { email: 'demo@example.com', accessUrl: 'https://example.com/inbox/token' },
      { email: 'second@example.com', accessUrl: 'https://example.com/s/2' },
    ])
  })

  it('skips existing and in-batch duplicate emails case-insensitively', () => {
    const result = parseEmailImport(
      'DUP@example.com----https://example.com/1\nnew@example.com----https://example.com/2\nNEW@example.com----https://example.com/3',
      ['dup@example.com'],
    )

    expect(result.accepted).toHaveLength(1)
    expect(result.duplicates).toHaveLength(2)
  })

  it('accepts mail.com branded mailboxes with an IMAP password', () => {
    const result = parseEmailImport(
      'person@gardener.com----mail-password-1\nsecond@fireman.net----mail-password-2',
    )

    expect(result.errors).toHaveLength(0)
    expect(result.accepted).toEqual([
      { email: 'person@gardener.com', accessUrl: 'mail-password-1' },
      { email: 'second@fireman.net', accessUrl: 'mail-password-2' },
    ])
  })

  it('rejects invalid email and unsupported URL without exposing the URL token', () => {
    const result = parseEmailImport(
      'invalid----https://example.com/s/super-secret-token\nok@example.com----file:///private/token',
    )

    expect(result.errors).toHaveLength(2)
    expect(JSON.stringify(result.errors)).not.toContain('super-secret-token')
    expect(JSON.stringify(result.errors)).not.toContain('/private/token')
  })
})

describe('parseProxyImport', () => {
  it('accepts a password containing colons', () => {
    const result = parseProxyImport('proxy.example.com:10000:user:pass:with:colons')
    expect(result.accepted).toEqual([
      { host: 'proxy.example.com', port: 10000, username: 'user', password: 'pass:with:colons', scheme: 'http' },
    ])
  })

  it('accepts whitespace-separated SOCKS5 URLs and preserves the scheme', () => {
    const result = parseProxyImport(
      'socks5://user-region-GB-one:pass@proxy.example.com:3000 socks5://user-region-GB-two:pass@proxy.example.com:3000',
    )
    expect(result.total).toBe(2)
    expect(result.errors).toHaveLength(0)
    expect(result.accepted.map((item) => item.scheme)).toEqual(['socks5', 'socks5'])
  })

  it('accepts common scheme-less HTTP proxy formats', () => {
    const result = parseProxyImport([
      'plain.proxy.test:8080',
      'fields.proxy.test:8081:user-one:pass-one',
      'user-two:pass-two@at.proxy.test:8082',
      'user-three:pass-three:tail.proxy.test:8083',
    ].join('\n'))

    expect(result.errors).toHaveLength(0)
    expect(result.accepted).toEqual([
      { host: 'plain.proxy.test', port: 8080, username: '', password: '', scheme: 'http' },
      { host: 'fields.proxy.test', port: 8081, username: 'user-one', password: 'pass-one', scheme: 'http' },
      { host: 'at.proxy.test', port: 8082, username: 'user-two', password: 'pass-two', scheme: 'http' },
      { host: 'tail.proxy.test', port: 8083, username: 'user-three', password: 'pass-three', scheme: 'http' },
    ])
  })

  it.each(['http', 'https', 'socks5', 'socks5h'] as const)(
    'preserves an explicit %s scheme',
    (scheme) => {
      const result = parseProxyImport(`${scheme}://user:pass@proxy.example.com:8080`)
      expect(result.errors).toHaveLength(0)
      expect(result.accepted[0]?.scheme).toBe(scheme)
    },
  )

  it.each(['host:0:user:pass', 'host:65536:user:pass', 'host:nope:user:pass'])(
    'rejects invalid port in %s',
    (input) => {
      expect(parseProxyImport(input).errors).toHaveLength(1)
    },
  )

  it('skips exact existing proxy records', () => {
    const parsed = { host: 'proxy.example.com', port: 10000, username: 'user', password: 'pass', scheme: 'http' as const }
    const result = parseProxyImport(
      'proxy.example.com:10000:user:pass',
      [proxyKey(parsed)],
    )
    expect(result.accepted).toHaveLength(0)
    expect(result.duplicates).toHaveLength(1)
  })
})
