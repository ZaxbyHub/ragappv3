import { useCallback, useEffect, useRef, useState } from 'react';

export interface UseCoalescedAppendOptions {
  fallbackMs?: number;
}

export interface UseCoalescedAppendResult {
  append: (chunk: string) => void;
  flush: () => void;
  reset: () => void;
  content: string;
  isPending: boolean;
}

export default function useCoalescedAppend(
  options?: UseCoalescedAppendOptions,
): UseCoalescedAppendResult {
  const { fallbackMs = 50 } = options ?? {};

  const bufferRef = useRef<string[]>([]);
  const rafHandleRef = useRef<number | null>(null);
  const timeoutHandleRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const mountedRef = useRef(true);

  const [content, setContent] = useState('');
  const [isPending, setIsPending] = useState(false);

  const flush = useCallback(() => {
    if (!mountedRef.current) return;
    if (bufferRef.current.length === 0) return;

    const bufferedChunks = bufferRef.current;
    bufferRef.current = [];

    setContent((prev) => prev + bufferedChunks.join(''));
    setIsPending(false);

    rafHandleRef.current = null;
    timeoutHandleRef.current = null;
  }, []);

  const scheduleFlush = useCallback(() => {
    if (rafHandleRef.current !== null || timeoutHandleRef.current !== null) {
      return;
    }

    if (typeof requestAnimationFrame !== 'undefined') {
      rafHandleRef.current = requestAnimationFrame(() => {
        if (!mountedRef.current) return;
        flush();
      });
    } else {
      timeoutHandleRef.current = setTimeout(() => {
        if (!mountedRef.current) return;
        flush();
      }, fallbackMs);
    }
  }, [flush, fallbackMs]);

  const append = useCallback(
    (chunk: string) => {
      bufferRef.current.push(chunk);
      setIsPending(true);
      scheduleFlush();
    },
    [scheduleFlush],
  );

  const reset = useCallback(() => {
    if (rafHandleRef.current !== null) {
      cancelAnimationFrame(rafHandleRef.current);
      rafHandleRef.current = null;
    }
    if (timeoutHandleRef.current !== null) {
      clearTimeout(timeoutHandleRef.current);
      timeoutHandleRef.current = null;
    }
    bufferRef.current = [];
    setContent('');
    setIsPending(false);
  }, []);

  // Cleanup on unmount: cancel scheduled flush and flush remaining content
  useEffect(() => {
    mountedRef.current = true;
    return () => {
      mountedRef.current = false;
      if (rafHandleRef.current !== null) {
        cancelAnimationFrame(rafHandleRef.current);
        rafHandleRef.current = null;
      }
      if (timeoutHandleRef.current !== null) {
        clearTimeout(timeoutHandleRef.current);
        timeoutHandleRef.current = null;
      }
      // Flush any remaining buffered chunks
      if (bufferRef.current.length > 0) {
        const bufferedChunks = bufferRef.current;
        bufferRef.current = [];
        setContent((prev) => prev + bufferedChunks.join(''));
        setIsPending(false);
      }
    };
  }, []);

  return { append, flush, reset, content, isPending };
}
