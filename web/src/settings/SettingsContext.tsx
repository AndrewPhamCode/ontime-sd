import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useState,
  type ReactNode,
} from 'react';
import { DEFAULTS, STORAGE_KEY, readStored, type Settings } from './settings';

interface SettingsApi {
  settings: Settings;
  set: <K extends keyof Settings>(key: K, value: Settings[K]) => void;
  reset: () => void;
}

const Context = createContext<SettingsApi | null>(null);

export function SettingsProvider({ children }: { children: ReactNode }) {
  const [settings, setSettings] = useState<Settings>(() =>
    readStored(typeof window === 'undefined' ? undefined : window.localStorage),
  );

  useEffect(() => {
    try {
      window.localStorage.setItem(STORAGE_KEY, JSON.stringify(settings));
    } catch {
      // Storage can be unavailable or full. The app still works for this
      // session; only persistence is lost, which is not worth an error state.
    }
  }, [settings]);

  const set = useCallback(<K extends keyof Settings>(key: K, value: Settings[K]) => {
    setSettings((current) => ({ ...current, [key]: value }));
  }, []);

  const reset = useCallback(() => setSettings({ ...DEFAULTS }), []);

  const value = useMemo<SettingsApi>(
    () => ({ settings, set, reset }),
    [settings, set, reset],
  );
  return <Context.Provider value={value}>{children}</Context.Provider>;
}

export function useSettings(): SettingsApi {
  const found = useContext(Context);
  if (!found) throw new Error('useSettings used outside SettingsProvider');
  return found;
}
