// Node 25 ships a built-in `localStorage` global that, without a backing file
// passed via `--localstorage-file=...`, is a stub missing `setItem`. That stub
// shadows the real jsdom Storage implementation, so any code touching
// `localStorage` (e.g. Zustand's `persist` middleware) throws
// `storage.setItem is not a function` inside vitest's jsdom environment.
//
// We force the jsdom window's Storage objects onto the global namespace before
// any test code runs, so module-level evaluation of stores that call
// `createJSONStorage(() => localStorage)` picks up the real one.

// Build an in-memory Storage-compatible polyfill rather than relying on the
// Node/jsdom Storage objects. Both are present but neither has working
// `setItem` under Node 25 + jsdom 25, so any persist middleware that calls
// `localStorage.setItem(...)` throws TypeError.

function makeMemoryStorage(): Storage {
  const store = new Map<string, string>();
  return {
    get length(): number {
      return store.size;
    },
    clear(): void {
      store.clear();
    },
    getItem(key: string): string | null {
      return store.has(key) ? (store.get(key) as string) : null;
    },
    setItem(key: string, value: string): void {
      store.set(key, String(value));
    },
    removeItem(key: string): void {
      store.delete(key);
    },
    key(index: number): string | null {
      return Array.from(store.keys())[index] ?? null;
    },
  };
}

for (const name of ["localStorage", "sessionStorage"] as const) {
  Object.defineProperty(globalThis, name, {
    configurable: true,
    writable: true,
    value: makeMemoryStorage(),
  });
  if (typeof window !== "undefined") {
    Object.defineProperty(window, name, {
      configurable: true,
      writable: true,
      value: (globalThis as unknown as Record<string, Storage>)[name],
    });
  }
}
