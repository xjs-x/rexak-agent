import { posix } from 'node:path';
export default posix;
export const resolve = posix.resolve;
export const dirname = posix.dirname;
export function resolveToCwd(value, cwd) {
  if (value.startsWith('~') || value.includes('\\') || /^[A-Za-z]:/.test(value)) throw new Error('Use Kali POSIX workspace paths');
  return posix.resolve(cwd, value.startsWith('@') ? value.slice(1) : value);
}
export const resolveReadPathAsync = async (value, cwd) => resolveToCwd(value, cwd);
export const splitBom = value => value.startsWith('\uFEFF') ? { bom: '\uFEFF', text: value.slice(1) } : { bom: '', text: value };
export const getShellEnv = () => ({});
// Kept for the original factory import; custom Operations are mandatory and this fallback is unreachable.
export const pathExists = async () => { throw new Error('Local filesystem fallback is disabled'); };
