/// <reference types="vitest/globals" />
import '@testing-library/jest-dom';
import { vi } from 'vitest';

// Mock localStorage BEFORE any modules access it - must be at the very top
Object.defineProperty(global, 'localStorage', {
  value: {
    getItem: vi.fn(() => null),
    setItem: vi.fn(),
    removeItem: vi.fn(),
    clear: vi.fn(),
    length: 0,
    key: vi.fn(),
  },
  writable: true,
});

// Mock window.confirm
Object.defineProperty(window, 'confirm', {
  value: vi.fn(() => true),
  writable: true,
});

// Mock Element.prototype.scrollTo — JSDOM does not implement it
if (!Element.prototype.scrollTo) {
  Element.prototype.scrollTo = vi.fn();
}

// Mock window.matchMedia — JSDOM does not implement it. Defaults to "not
// matching" (e.g. light OS preference); individual tests can override
// window.matchMedia to simulate a dark OS preference.
if (!window.matchMedia) {
  Object.defineProperty(window, 'matchMedia', {
    writable: true,
    value: (query: string) => ({
      matches: false,
      media: query,
      onchange: null,
      addEventListener: vi.fn(),
      removeEventListener: vi.fn(),
      addListener: vi.fn(),
      removeListener: vi.fn(),
      dispatchEvent: vi.fn(),
    }),
  });
}
