import { useEffect, useRef, useState } from 'react';
import { api, type StopSearchResult } from '../api/client';

/** Find a stop by name. Busiest matches first, because those are the ones someone
 *  is most likely looking for. */
export function StopSearch({ onPick }: { onPick: (stop: StopSearchResult) => void }) {
  const [query, setQuery] = useState('');
  const [results, setResults] = useState<StopSearchResult[]>([]);
  const [open, setOpen] = useState(false);
  const box = useRef<HTMLDivElement | null>(null);

  useEffect(() => {
    if (query.trim().length < 2) {
      setResults([]);
      return;
    }
    let active = true;
    // Debounced, so typing does not fire a request per keystroke.
    const timer = setTimeout(() => {
      api
        .searchStops(query.trim())
        .then((found) => {
          if (active) {
            setResults(found);
            setOpen(true);
          }
        })
        .catch(() => {
          if (active) setResults([]);
        });
    }, 220);
    return () => {
      active = false;
      clearTimeout(timer);
    };
  }, [query]);

  useEffect(() => {
    const onDocumentClick = (event: MouseEvent) => {
      if (box.current && !box.current.contains(event.target as Node)) setOpen(false);
    };
    document.addEventListener('mousedown', onDocumentClick);
    return () => document.removeEventListener('mousedown', onDocumentClick);
  }, []);

  return (
    <div className="search" ref={box}>
      <input
        type="search"
        value={query}
        placeholder="Search stops, e.g. Gilman"
        aria-label="Search stops by name"
        onChange={(event) => setQuery(event.target.value)}
        onFocus={() => results.length && setOpen(true)}
        onKeyDown={(event) => {
          if (event.key === 'Escape') setOpen(false);
          if (event.key === 'Enter') {
            const first = results[0];
            if (first) {
              onPick(first);
              setOpen(false);
              return;
            }
            // Enter pressed before the debounce landed. Search now rather than
            // doing nothing, which is what a fast typist would otherwise see.
            const text = query.trim();
            if (text.length >= 2) {
              void api.searchStops(text).then((found) => {
                const best = found[0];
                if (best) {
                  onPick(best);
                  setOpen(false);
                } else {
                  setResults([]);
                }
              });
            }
          }
        }}
      />
      {open && results.length > 0 ? (
        <ul className="search-results">
          {results.map((stop) => (
            <li key={stop.stop_id}>
              <button
                onClick={() => {
                  onPick(stop);
                  setOpen(false);
                }}
              >
                <span>{stop.stop_name ?? stop.stop_id}</span>
                <span className="muted">
                  {stop.arrivals.toLocaleString('en-US')} arrivals observed
                </span>
              </button>
            </li>
          ))}
        </ul>
      ) : null}
    </div>
  );
}
