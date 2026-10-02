import type { ReactNode } from 'react';

interface Props {
  title: string;
  note?: string;
  children: ReactNode;
  aside?: ReactNode;
}

export function Card({ title, note, children, aside }: Props) {
  return (
    <section className="card">
      <header>
        <div
          style={{
            display: 'flex',
            justifyContent: 'space-between',
            alignItems: 'baseline',
            gap: 12,
            flexWrap: 'wrap',
          }}
        >
          <h2>{title}</h2>
          {aside}
        </div>
        {note ? <p className="note">{note}</p> : null}
      </header>
      {children}
    </section>
  );
}

export function Pending({ what }: { what: string }) {
  return <p className="empty">Loading {what}…</p>;
}

export function Failed({ error }: { error: string }) {
  return (
    <p className="empty">
      Could not load this panel: {error}. Is the API running on port 8000?
    </p>
  );
}

export function Empty({ what }: { what: string }) {
  return <p className="empty">No {what} yet. Run the pipeline to populate it.</p>;
}
