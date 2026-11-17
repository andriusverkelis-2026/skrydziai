// Šifravimas: gzip + AES-GCM, raktas iš slaptažodžio per PBKDF2-SHA256.
// Veikia ir naršyklėje, ir Node 20+ (abu turi WebCrypto ir CompressionStream).
const ITER = 200000;
const enc = new TextEncoder(), dec = new TextDecoder();

function b64(bytes) {
  let s = "";
  for (let i = 0; i < bytes.length; i += 0x8000) s += String.fromCharCode(...bytes.subarray(i, i + 0x8000));
  return btoa(s);
}
function unb64(str) {
  const s = atob(str), out = new Uint8Array(s.length);
  for (let i = 0; i < s.length; i++) out[i] = s.charCodeAt(i);
  return out;
}
async function pipe(bytes, stream) {
  const buf = await new Response(new Blob([bytes]).stream().pipeThrough(stream)).arrayBuffer();
  return new Uint8Array(buf);
}

const keyCache = new Map();
async function deriveKey(password, salt) {
  const ck = password + "|" + b64(salt);
  if (keyCache.has(ck)) return keyCache.get(ck);
  const base = await crypto.subtle.importKey("raw", enc.encode(password), "PBKDF2", false, ["deriveKey"]);
  const key = await crypto.subtle.deriveKey({ name: "PBKDF2", salt, iterations: ITER, hash: "SHA-256" },
    base, { name: "AES-GCM", length: 256 }, false, ["encrypt", "decrypt"]);
  keyCache.set(ck, key);
  return key;
}

export async function encryptJSON(obj, password) {
  const salt = crypto.getRandomValues(new Uint8Array(16)), iv = crypto.getRandomValues(new Uint8Array(12));
  const packed = await pipe(enc.encode(JSON.stringify(obj)), new CompressionStream("gzip"));
  const ct = new Uint8Array(await crypto.subtle.encrypt({ name: "AES-GCM", iv }, await deriveKey(password, salt), packed));
  return JSON.stringify({ v: 1, salt: b64(salt), iv: b64(iv), ct: b64(ct) });
}

// Meta klaidą "BAD_PASSWORD", jei slaptažodis neteisingas
export async function decryptJSON(text, password) {
  const env = typeof text === "string" ? JSON.parse(text) : text;
  const salt = unb64(env.salt), iv = unb64(env.iv);
  let packed;
  try {
    packed = await crypto.subtle.decrypt({ name: "AES-GCM", iv }, await deriveKey(password, salt), unb64(env.ct));
  } catch {
    throw new Error("BAD_PASSWORD");
  }
  return JSON.parse(dec.decode(await pipe(new Uint8Array(packed), new DecompressionStream("gzip"))));
}
