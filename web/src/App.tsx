import { useEffect, useState } from 'react';
import { Analysis } from './views/Analysis';
import { MapApp } from './views/MapApp';
import { SettingsProvider, useSettings } from './settings/SettingsContext';
import { SettingsPanel } from './settings/SettingsPanel';
import { isModified } from './settings/settings';

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
  return (
    <SettingsProvider>
      <Shell />
    </SettingsProvider>
  );
}

function Shell() {
  const [tab, setTab] = useState<Tab>('map');
  const [theme, toggleTheme] = useTheme();
  const [settingsOpen, setSettingsOpen] = useState(false);
  const { settings } = useSettings();
  // A screenshot of a configured app should not be mistakable for a screenshot
  // of the project's actual result, so a moved setting is visible in the bar.
  const modified = isModified(settings);

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
          <button
            className={modified ? 'toggle modified' : 'toggle'}
            onClick={() => setSettingsOpen(true)}
            aria-label="Settings"
          >
            <span aria-hidden="true">&#9881;</span> Settings
            {modified ? <span className="modified-dot" aria-hidden="true" /> : null}
          </button>
        </div>
      </nav>

      {modified ? (
        <div className="modified-banner">
          Settings have been changed from the project defaults, so these are not the figures
          the write-up quotes.{' '}
          <button className="linklike" onClick={() => setSettingsOpen(true)}>
            Review or reset them
          </button>
          .
        </div>
      ) : null}

      {tab === 'map' ? (
        <MapApp onShowEvidence={() => setTab('analysis')} />
      ) : (
        <div className="scroll-area">
          <Analysis onOpenSettings={() => setSettingsOpen(true)} />
        </div>
      )}

      {settingsOpen ? <SettingsPanel onClose={() => setSettingsOpen(false)} /> : null}
    </div>
  );
}
