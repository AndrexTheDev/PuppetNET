import { LIMITS } from './graph-data';
export function apiBase(value: string): string {
  if (!value.trim()) return '';
  const url = new URL(value);
  if (url.protocol !== 'https:' || url.username || url.password || url.search || url.hash) throw new Error('API-Basis muss eine HTTPS-URL ohne Zugangsdaten, Query oder Fragment sein.');
  return url.href.replace(/\/$/, '');
}
export async function readResponse(response: Response): Promise<string> {
  if (!response.ok) throw new Error(`API antwortet mit HTTP ${response.status}.`);
  if (!response.body) throw new Error('Leere API-Antwort.');
  const reader = response.body.getReader(), decoder = new TextDecoder();
  let bytes = 0, text = '';
  try {
    for (;;) {
      const { done, value } = await reader.read();
      if (done) break;
      bytes += value.byteLength;
      if (bytes > LIMITS.bytes) throw new Error('API-Antwort überschreitet 64 MiB.');
      text += decoder.decode(value, { stream: true });
    }
    return text + decoder.decode();
  } catch (error) { await reader.cancel(); throw error; }
  finally { reader.releaseLock(); }
}
