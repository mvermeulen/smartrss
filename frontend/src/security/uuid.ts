/**
 * Canonical lowercase RFC 4122 UUIDv4 generator safe on insecure HTTP origins (U-05, Q-2).
 *
 * `crypto.randomUUID()` is `[SecureContext]`-only and undefined on plain HTTP LAN origins
 * (e.g. `http://smartrss.lan:8080`). Falls back to `crypto.getRandomValues()` which is
 * available in all contexts.
 */

const HEX_TABLE: readonly string[] = Array.from({ length: 256 }, (_, i) =>
  i.toString(16).padStart(2, "0"),
);

export function uuidv4(): string {
  const cryptoObj = globalThis.crypto;
  if (cryptoObj && typeof cryptoObj.randomUUID === "function") {
    return cryptoObj.randomUUID().toLowerCase();
  }

  const bytes = new Uint8Array(16);
  if (cryptoObj && typeof cryptoObj.getRandomValues === "function") {
    cryptoObj.getRandomValues(bytes);
  } else {
    throw new Error("Web Crypto API unavailable for UUID generation");
  }

  // RFC 4122 section 4.4: set version (4) and variant (10xx) bits
  bytes[6] = ((bytes[6] as number) & 0x0f) | 0x40;
  bytes[8] = ((bytes[8] as number) & 0x3f) | 0x80;

  let out = "";
  for (let i = 0; i < 16; i += 1) {
    if (i === 4 || i === 6 || i === 8 || i === 10) {
      out += "-";
    }
    out += HEX_TABLE[bytes[i] as number] as string;
  }
  return out;
}
