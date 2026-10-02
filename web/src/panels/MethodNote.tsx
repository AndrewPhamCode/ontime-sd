import { api, type ModelRun } from '../api/client';
import { formatCount } from '../lib/format';
import { useApi } from '../lib/useApi';
import { Card } from './Card';

/** How the number was produced, and the mistake that was caught making it.
 *
 *  The leak is on the page deliberately. A pipeline that infers its own ground
 *  truth makes "what was knowable at time T" a subtle question, and the first
 *  version of this model got it wrong in the flattering direction. */
export function MethodNote() {
  const { data } = useApi<ModelRun | null>(() => api.modelRun());

  return (
    <Card
      title="Method"
      note="How these figures are defined, and where the first attempt went wrong."
    >
      <h3 style={{ fontSize: 14, margin: '0 0 6px' }}>What is being measured</h3>
      <p className="note" style={{ maxWidth: '76ch' }}>
        For every arrival we reconstructed from GPS, we look up what each predictor was
        saying 1, 5, 10 and 20 minutes <em>before the bus actually arrived</em>, then take
        the absolute difference. Measuring back from the real arrival rather than the
        predicted one matters: measuring from the prediction is self-referential, because
        the worse a prediction is, the further the evaluation point drifts from the event.
      </p>

      <h3 style={{ fontSize: 14, margin: '18px 0 6px' }}>
        An apparent 32% win that turned out to be a leak
      </h3>
      <p className="note" style={{ maxWidth: '76ch' }}>
        The first run of this model beat MTS at every lead time, by 32% at one minute out.
        That was implausible: one minute before arrival MTS can see the vehicle in real
        time, while the model only sees the previous stop. The cause was that arrival times
        here are <em>inferred</em>, by interpolating between the two GPS fixes either side
        of a stop, so an arrival only becomes knowable once the later fix lands. Selecting
        the model's starting point on arrival time alone let it use a timestamp computed
        from GPS received after the cutoff. Requiring that interpolation window to close
        before the cutoff removed the entire advantage, and the honest result is the parity
        shown above.
      </p>

      <h3 style={{ fontSize: 14, margin: '18px 0 6px' }}>Provenance</h3>
      {data ? (
        <table style={{ maxWidth: 620 }}>
          <tbody>
            <tr>
              <td>Trained on</td>
              <td>
                {data.train_from} to {data.train_to} · {formatCount(data.train_rows)} rows
              </td>
            </tr>
            <tr>
              <td>Tested on</td>
              <td>
                {data.test_from} to {data.test_to} · {formatCount(data.test_rows)} rows
              </td>
            </tr>
            <tr>
              <td>Split</td>
              <td>Time-based, never random</td>
            </tr>
            <tr>
              <td>Features</td>
              <td className="mono">{data.features?.join(', ') ?? '—'}</td>
            </tr>
            <tr>
              <td>Notes</td>
              <td>{data.notes ?? '—'}</td>
            </tr>
          </tbody>
        </table>
      ) : (
        <p className="empty">No model run recorded yet.</p>
      )}
      <p className="note">
        Travel-time statistics used by the model are fitted on the training days only, with
        the cutoff date stored alongside every statistic, so a leak would be visible in the
        data rather than implicit in the code.
      </p>
    </Card>
  );
}
