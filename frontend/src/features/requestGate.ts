export type RequestToken<Key> = Readonly<{ sequence: number; key: Key }>

export class LatestRequestGate<Key> {
  private sequence = 0

  begin(key: Key): RequestToken<Key> {
    return { sequence: ++this.sequence, key }
  }

  isCurrent(token: RequestToken<Key>, currentKey: Key): boolean {
    return token.sequence === this.sequence && token.key === currentKey
  }
}
