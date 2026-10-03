import { DataQuality } from '../panels/DataQuality';
import { DeltaPanel } from '../panels/DeltaPanel';
import { ErrorDistribution } from '../panels/ErrorDistribution';
import { Headline } from '../panels/Headline';
import { HorizonChart } from '../panels/HorizonChart';
import { MethodNote } from '../panels/MethodNote';
import { RouteTable } from '../panels/RouteTable';
import { api, type DataQuality as DataQualityData } from '../api/client';
import { formatCount } from '../lib/format';
import { useApi } from '../lib/useApi';
import { useSettings } from '../settings/SettingsContext';
import { useParams } from '../settings/useParams';
import { weakenedBy } from '../settings/settings';

/** The evidence behind the corrections the map shows. Unchanged from the
 *  dashboard it was before; riders get the map, anyone judging the work gets
 *  this. */
export function Analysis({ onOpenSettings }: { onOpenSettings: () => void }) {
  const params = useParams();
  const { settings } = useSettings();
  const weakened = weakenedBy(settings);
  // The arrival count is read from the API rather than written into the copy,
  // because it grows every day the collector runs and changes with the label
  // filter. A hardcoded figure would be wrong by tomorrow.
  const quality = useApi<DataQualityData>(() => api.dataQuality(params), [params]);

  return (
    <div className="page">
      <div className="masthead">
        <div>
          <h1>How accurate is this?</h1>
          <p className="subtitle">
            Every corrected estimate on the map comes from measuring MTS against what
            actually happened. This is that measurement: the agency's own predictions scored
            on {quality.data ? formatCount(quality.data.arrivals_total) : 'all'} arrivals
            reconstructed from raw GPS, with a model beside them. The honest result is
            parity, not a win.
          </p>
          <p className="subtitle">
            Showing the {settings.horizon} minute horizon.{' '}
            <button className="linklike" onClick={onOpenSettings}>
              Change the horizon, the evidence threshold or the predictor
            </button>
            .
          </p>
          {weakened.length > 0 ? (
            <p className="settings-warn" role="status">
              These are not the project's headline figures: {weakened.join('; ')}.
            </p>
          ) : null}
        </div>
      </div>

      <Headline />
      <HorizonChart />
      <DeltaPanel />

      <ErrorDistribution />
      <RouteTable />
      <DataQuality />
      <MethodNote />
    </div>
  );
}
