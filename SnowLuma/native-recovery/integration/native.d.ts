/** Recovered interface. Loading paths remain unverified; see README.zh-CN.md. */
export interface ManualMapHandle {
  base: bigint;
  entry: bigint;
  exceptionTable: bigint;
  prepare: bigint;
  size: number;
}
export interface NativeError {status: 'error'; operation: string; code: number}
export interface Integrity {
  pid: number;
  executablePath: string | null;
  process: {status: 'ok'; rid: number} | NativeError;
  file: {status: 'ok'; label: null | {rid: number; inherited: boolean; policy: number}} | NativeError;
}
export function getAllMainProcessAsync(): Promise<number[]>;
export function getRuntimeIntegrityAsync(): Promise<Integrity>;
export function getFileHoldersAsync(path: string): Promise<{pid: number; name: string}[]>;
export function loadModuleAsync(pid: number, dylibPath: string): Promise<number>;
export function unloadModuleAsync(pid: number, dylibPath: string): Promise<number>;
export function loadModuleManualAsync(pid: number, dylibPath: string): Promise<ManualMapHandle>;
export function unloadModuleManualAsync(pid: number, handle: ManualMapHandle): Promise<void>;
