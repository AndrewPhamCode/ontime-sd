import { useEffect, useState } from 'react';
import { Analysis } from './views/Analysis';
import { MapApp } from './views/MapApp';

type Tab = 'map' | 'analysis';
type Theme = 'light' | 'dark';

function useTheme(): [Theme, () => void] {
  const [theme, setTheme] = useState<Theme>(() =>
    window.matchMedia?.('(prefers-color-scheme: dark)').matches ? 'dark' : 'light',
  );
  useEffect(() => {
    document.documentElement.dataset['theme'] = theme;
  }, [theme]);
  return [theme, () => setTheme((current) => (current === 'dark' ? 'light' : 'dark'))];
}

export function App() {
  const [tab, setTab] = useState<Tab>('map');
  const [theme, toggleTheme] = useTheme();

  return (
    <div className="app">
      <nav className="appbar">
        <div className="brand">
          <span className="brand-mark" aria-hidden="true" />
          <strong>OnTime SD</strong>
          <span className="brand-sub">San Diego transit, with the optimism removed</span>
        </div>
        <div className="appbar-actions">
          <div className="tabs" role="tablist">
            <button
              role="tab"
              aria-selected={tab === 'map'}
              className={tab === 'map' ? 'tab active' : 'tab'}
              onClick={() => setTab('map')}
            >
              Map
            </button>
            <button
              role="tab"
              aria-selected={tab === 'analysis'}
              className={tab === 'analysis' ? 'tab active' : 'tab'}
              onClick={() => setTab('analysis')}
            >
              How accurate is this?
            </button>
          </div>
          <button className="toggle" onClick={toggleTheme}>
            {theme === 'dark' ? 'Light' : 'Dark'}
          </button>
        </div>
      </nav>

      {tab === 'map' ? (
        <MapApp onShowEvidence={() => setTab('analysis')} />
      ) : (
        <div className="scroll-area">
          <Analysis />
        </div>
      )}
    </div>
  );
}
