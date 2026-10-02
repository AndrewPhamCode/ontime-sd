import { DataQuality } from '../panels/DataQuality';
import { DeltaPanel } from '../panels/DeltaPanel';
import { ErrorDistribution } from '../panels/ErrorDistribution';
import { Headline } from '../panels/Headline';
import { HorizonChart } from '../panels/HorizonChart';
import { MethodNote } from '../panels/MethodNote';
import { RouteTable } from '../panels/RouteTable';
import { HORIZONS } from '../lib/format';
import { useState } from 'react';

/** The evidence behind the corrections the map shows. Unchanged from the
 *  dashboard it was before; riders get the map, anyone judging the work gets
 *  this. */
export function Analysis() {
  const [horizon, setHorizon] = useState<number>(10);

  return (
    <div className="page">
      <div className="masthead">
        <div>
          <h1>How accurate is this?</h1>
          <p className="subtitle">
            Every corrected estimate on the map comes from measuring MTS against what
            actually happened. This is that measurement: the agency's own predictions scored
            on 613,615 arrivals reconstructed from raw GPS, with a model beside them. The
            honest result is parity, not a win.
          </p>
        </div>
      </div>

      <Headline />
      <HorizonChart />
      <DeltaPanel />

      <div className="controls" style={{ marginTop: 20 }}>
        <label htmlFor="horizon">Lead time for the panels below</label>
        <select
          id="horizon"
          value={horizon}
          onChange={(event) => setHorizon(Number(event.target.value))}
        >
          {HORIZONS.map((value) => (
            <option key={value} value={value}>
              {value} minutes ahead
            </option>
          ))}
        </select>
      </div>

      <ErrorDistribution horizon={horizon} />
      <RouteTable horizon={horizon} />
      <DataQuality />
      <MethodNote />
    </div>
  );
}
