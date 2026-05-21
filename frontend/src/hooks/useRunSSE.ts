import { useEffect, useState } from 'react';

import type { RunEvent } from '../lib/api';

export function useRunSSE(runId: string | undefined) {
  const [events, setEvents] = useState<RunEvent[]>([]);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    setEvents([]);
    setError(null);
    if (!runId) {
      return;
    }

    const source = new EventSource(`/api/runs/${runId}/events`);
    const handleEvent = (event: MessageEvent<string>) => {
      // Stage 3.E follow-up: any successful payload means the
      // stream reconnected. Clear the disconnect banner so users
      // don't see "正在重连…" stuck on screen forever after a
      // single transient drop.
      setError(null);
      try {
        const parsed = JSON.parse(event.data) as RunEvent;
        setEvents((current) => {
          if (current.some((item) => item.id === parsed.id)) {
            return current;
          }
          return [...current, parsed].slice(-20);
        });
      } catch {
        setError('Invalid run event received');
      }
    };

    // Browsers' native EventSource ``onopen`` fires both on initial
    // connect and on each successful auto-reconnect. Use it as the
    // authoritative "we're live again" signal.
    source.onopen = () => setError(null);

    source.addEventListener('run_event', handleEvent as EventListener);
    source.addEventListener('message', handleEvent as EventListener);
    // Only set error if the stream is genuinely down (readyState=CLOSED)
    // — transient blips during reconnect leave it CONNECTING.
    source.onerror = () => {
      if (source.readyState === EventSource.CLOSED) {
        setError('Run event stream disconnected');
      }
    };

    return () => {
      source.removeEventListener('run_event', handleEvent as EventListener);
      source.removeEventListener('message', handleEvent as EventListener);
      source.close();
    };
  }, [runId]);

  return { events, error };
}
