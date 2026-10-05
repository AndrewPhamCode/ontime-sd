import { useEffect, useRef } from 'react';
import { PLACES } from '../map/MapView';
import { SOURCE_LABELS } from '../lib/format';
import {
  DEFAULTS,
  HORIZON_CHOICES,
  LABEL_FILTER_CHOICES,
  PLACE_KEYS,
  REFRESH_CHOICES,
  SAMPLE_CHOICES,
  TIME_BAND_KEYS,
  TIME_BAND_LABELS,
  isModified,
  weakenedBy,
} from './settings';
import { useSettings } from './SettingsContext';

const REFRESH_LABELS: Record<number, string> = {
  5000: 'Every 5s',
  10000: 'Every 10s',
  30000: 'Every 30s',
  0: 'Off',
};

/** The settings drawer: predictions and model on top, map and arrivals below.
 *
 *  Each prediction setting states what moving it costs. The point of exposing
 *  them is to let someone interrogate the result, not to let the result be
 *  configured into looking good, so the costs are part of the control rather
 *  than a footnote.
 */
export function SettingsPanel({ onClose }: { onClose: () => void }) {
  const { settings, set, reset } = useSettings();
  const panel = useRef<HTMLDivElement | null>(null);
  const weakened = weakenedBy(settings);

  useEffect(() => {
    const onKey = (event: KeyboardEvent) => {
      if (event.key === 'Escape') onClose();
    };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, [onClose]);

  return (
    <div className="settings-scrim" onMouseDown={onClose}>
      <div
        className="settings-panel"
        ref={panel}
        role="dialog"
        aria-label="Settings"
        onMouseDown={(event) => event.stopPropagation()}
      >
        <header className="settings-head">
          <h2>Settings</h2>
          <div className="settings-head-actions">
            {isModified(settings) ? (
              <button className="toggle" onClick={reset}>
                Reset to defaults
              </button>
            ) : null}
            <button className="toggle" onClick={onClose} aria-label="Close settings">
              Close
            </button>
          </div>
        </header>

        <section className="settings-group">
          <h3>Predictions and model</h3>
          <p className="settings-blurb">
            These change how every figure on the evidence page is computed, and how the map
            corrects live arrivals. Defaults are the values the project reports.
          </p>

          <Row
            label="Horizon driving the correction"
            cost="Shorter horizons are easier to predict, so there is less bias to correct."
            isDefault={settings.horizon === DEFAULTS.horizon}
          >
            <select
              value={settings.horizon}
              onChange={(event) => set('horizon', Number(event.target.value))}
            >
              {HORIZON_CHOICES.map((value) => (
                <option key={value} value={value}>
                  {value} min ahead
                </option>
              ))}
            </select>
          </Row>

          <Row
            label="Minimum arrivals before correcting"
            cost="Below 10 the correction is fitted to noise rather than to a pattern."
            isDefault={settings.minSample === DEFAULTS.minSample}
          >
            <select
              value={settings.minSample}
              onChange={(event) => set('minSample', Number(event.target.value))}
            >
              {SAMPLE_CHOICES.map((value) => (
                <option key={value} value={value}>
                  {value} arrival{value === 1 ? '' : 's'}
                </option>
              ))}
            </select>
          </Row>

          <Row
            label="Label quality filter"
            cost="Relaxing it mixes our own GPS interpolation error into every predictor's score."
            isDefault={settings.labelFilter === DEFAULTS.labelFilter}
          >
            <select
              value={settings.labelFilter}
              onChange={(event) => set('labelFilter', Number(event.target.value))}
            >
              {LABEL_FILTER_CHOICES.map((value) => (
                <option key={value} value={value}>
                  Ping gap under {value < 120 ? `${value}s` : `${value / 60} min`}
                </option>
              ))}
            </select>
          </Row>

          <Row
            label="Compare against"
            cost="The two baselines are weaker than the model, so the comparison flatters us more."
            isDefault={settings.compare === DEFAULTS.compare}
          >
            <select
              value={settings.compare}
              onChange={(event) =>
                set('compare', event.target.value as typeof settings.compare)
              }
            >
              {(['lgbm', 'segment_mean', 'persist_delay'] as const).map((value) => (
                <option key={value} value={value}>
                  {SOURCE_LABELS[value]}
                </option>
              ))}
            </select>
          </Row>

          <Row
            label="Time of day"
            cost="A band is a slice of the day, not the whole result. PM rush is the hardest slice for both predictors."
            isDefault={settings.timeBand === DEFAULTS.timeBand}
          >
            <select
              value={settings.timeBand}
              onChange={(event) =>
                set('timeBand', event.target.value as typeof settings.timeBand)
              }
            >
              {TIME_BAND_KEYS.map((value) => (
                <option key={value} value={value}>
                  {TIME_BAND_LABELS[value]}
                </option>
              ))}
            </select>
          </Row>

          {weakened.length > 0 ? (
            <p className="settings-warn" role="status">
              Showing a weakened measurement: {weakened.join('; ')}.
            </p>
          ) : null}
        </section>

        <section className="settings-group">
          <h3>Map and arrivals</h3>
          <p className="settings-blurb">
            Presentation only. Nothing here changes a prediction.
          </p>

          <Row label="Default area" isDefault={settings.place === DEFAULTS.place}>
            <select
              value={settings.place}
              onChange={(event) =>
                set('place', event.target.value as typeof settings.place)
              }
            >
              {PLACE_KEYS.map((key) => (
                <option key={key} value={key}>
                  {PLACES[key]?.label ?? key}
                </option>
              ))}
            </select>
          </Row>

          <Row
            label="Refresh vehicles"
            cost="The upstream feed only updates every 30s, so faster polling buys nothing."
            isDefault={settings.refreshMs === DEFAULTS.refreshMs}
          >
            <select
              value={settings.refreshMs}
              onChange={(event) => set('refreshMs', Number(event.target.value))}
            >
              {REFRESH_CHOICES.map((value) => (
                <option key={value} value={value}>
                  {REFRESH_LABELS[value]}
                </option>
              ))}
            </select>
          </Row>

          <Row
            label="Show vehicles"
            isDefault={settings.showVehicles === DEFAULTS.showVehicles}
          >
            <Switch
              checked={settings.showVehicles}
              onChange={(value) => set('showVehicles', value)}
              label="Show vehicles"
            />
          </Row>

          <Row
            label="Show stop labels"
            isDefault={settings.showStopLabels === DEFAULTS.showStopLabels}
          >
            <Switch
              checked={settings.showStopLabels}
              onChange={(value) => set('showStopLabels', value)}
              label="Show stop labels"
            />
          </Row>

          <Row label="Clock" isDefault={settings.clock24 === DEFAULTS.clock24}>
            <select
              value={settings.clock24 ? '24' : '12'}
              onChange={(event) => set('clock24', event.target.value === '24')}
            >
              <option value="12">12 hour</option>
              <option value="24">24 hour</option>
            </select>
          </Row>
        </section>

        <p className="settings-foot">
          Stored in this browser only. Collector and pipeline settings are operational and
          live in the environment, not here.
        </p>
      </div>
    </div>
  );
}

function Row({
  label,
  cost,
  isDefault,
  children,
}: {
  label: string;
  cost?: string;
  isDefault: boolean;
  children: React.ReactNode;
}) {
  return (
    <div className="settings-row">
      <div className="settings-row-text">
        <label>
          {label}
          {isDefault ? null : <span className="settings-changed">changed</span>}
        </label>
        {cost ? <p className="settings-cost">{cost}</p> : null}
      </div>
      <div className="settings-control">{children}</div>
    </div>
  );
}

function Switch({
  checked,
  onChange,
  label,
}: {
  checked: boolean;
  onChange: (value: boolean) => void;
  label: string;
}) {
  return (
    <button
      role="switch"
      aria-checked={checked}
      aria-label={label}
      className={checked ? 'switch on' : 'switch'}
      onClick={() => onChange(!checked)}
    >
      <span className="switch-knob" />
      <span className="switch-text">{checked ? 'On' : 'Off'}</span>
    </button>
  );
}
