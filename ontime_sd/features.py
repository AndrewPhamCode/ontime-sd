"""Feature extraction for the Phase 5 model, with one cutoff and one gate.

Every predictor here forecasts from an **anchor**: the last stop on the trip whose
arrival was observed at or before the prediction cutoff. That is the only position
information actually available at prediction time, and using it consistently is
what keeps the three predictors comparable to each other and to MTS.

Leakage is the failure mode that matters. A feature drawn from after the cutoff
would make the model appear to beat MTS while being worthless, and the result would
look entirely plausible. Two structural defences:

  1. Both queries below take the cutoff as a parameter and filter every arrival on
     it. There is no code path that reads an arrival without a cutoff bound.
  2. Segment statistics arrive pre-fit with a `fit_through_date` that the caller
     must check against the test window. See ontime_sd/segments.py.

Deliberately excluded, though each would help: the target stop's own arrival (the
label), any arrival at or after the cutoff, and MTS's prediction for the same stop.
The last would likely be the strongest single feature and would make the comparison
circular rather than a head-to-head. See DESIGN.md ADR-0038.
"""

from __future__ import annotations

import itertools
import logging
from dataclasses import dataclass
from datetime import date, datetime, timedelta

import asyncpg
import numpy as np

from ontime_sd.config import SERVICE_TZ
from ontime_sd.network_state import RouteConditions
from ontime_sd.segments import SegmentMeans

log = logging.getLogger(__name__)

FEATURE_NAMES = (
    "stops_ahead",
    "scheduled_remaining_seconds",
    "anchor_delay_seconds",
    "segment_sum_seconds",
    "hour_bin",
    "is_weekend",
    "coarse_segment_share",
    # How late the rest of the fleet on this route was observed to be just before
    # the cutoff. The only fresh signal available without reading GPS. See
    # ontime_sd/network_state.py for why it is not a leak.
    "route_recent_delay_seconds",
    "route_recent_n",
    "trip_progress",
    "minute_of_day",
    # Route identity, as a LightGBM categorical rather than a number, because
    # route ids are labels and 992 is not 887 larger than 105.
    "route_index",
)

CATEGORICAL_FEATURES = ("route_index",)

# How far ahead of the anchor a training pair may reach.
#
# Was 12, which quietly broke the evaluation. The anchor at serving time sits a
# median of 11 stops back at the 10 minute horizon and 18 at the 20 minute one, so
# 31.9% and 78.7% of scored rows asked the model about distances it had never been
# trained on. Boosted trees do not extrapolate: past the last split the prediction
# flattens, which is exactly where the model stopped beating MTS.
#
# 30 covers the p95 of what is actually served. The original 12 was chosen to hold
# the row count down, since pairs grow with the square of the stop count, so the
# row count is now held down by sampling the far pairs instead of refusing them.
MAX_STOPS_AHEAD = 30

# Percentage of pairs beyond the old cap to keep. Deterministic, so a rerun
# reproduces the same training set.
FAR_PAIR_SAMPLE_PCT = 35
NEAR_PAIR_LIMIT = 12


@dataclass(frozen=True, slots=True)
class Context:
    """Everything known at the cutoff about one (trip, target stop) prediction."""

    start_date: date
    trip_id: str
    target_sequence: int
    target_stop_id: str
    anchor_sequence: int
    anchor_stop_id: str
    anchor_arrived_at: datetime
    anchor_scheduled_seconds: int
    target_scheduled_seconds: int
    hour_bin: int
    is_weekend: bool
    # The instant the anchor's arrival became knowable: its arrival plus the gap to
    # the later bracketing ping. Any prediction made before this could not have
    # used the anchor at all.
    # Optional so a test can build a context without inventing a ping gap. Both
    # production queries always supply it.
    anchor_known_at: datetime | None = None
    route_id: str | None = None
    # Present for evaluation rows, absent for training rows.
    horizon_minutes: int | None = None
    actual_arrived_at: datetime | None = None
    ping_gap_seconds: int | None = None

    @property
    def stops_ahead(self) -> int:
        return self.target_sequence - self.anchor_sequence

    @property
    def prediction_instant(self) -> datetime:
        """When this prediction is being made.

        For an evaluation row that is the scored cutoff, `arrival - horizon`. For a
        training row there is no horizon, so it is the moment the anchor became
        knowable, which is the earliest instant the row could have been predicted.

        Used only to time-align features that describe conditions, never to derive
        anything about the target. The distinction matters: `prediction_instant -
        anchor_arrived_at` would be a legal-looking feature that in this evaluation
        equals `label - horizon`, and is therefore banned. See ADR-0044.
        """
        if self.horizon_minutes is not None and self.actual_arrived_at is not None:
            return self.actual_arrived_at - timedelta(minutes=self.horizon_minutes)
        return self.anchor_known_at or self.anchor_arrived_at

    @property
    def minute_of_day(self) -> int:
        local = self.anchor_arrived_at.astimezone(SERVICE_TZ)
        return local.hour * 60 + local.minute

    @property
    def scheduled_remaining_seconds(self) -> int:
        return self.target_scheduled_seconds - self.anchor_scheduled_seconds

    @property
    def anchor_delay_seconds(self) -> float:
        """How late the vehicle was at the anchor, in seconds.

        The single most informative thing available at the cutoff, and the whole
        basis of the persist-delay baseline.
        """
        scheduled = datetime.combine(self.start_date, datetime.min.time()).replace(tzinfo=None)
        observed_local = self.anchor_arrived_at.astimezone(SERVICE_TZ).replace(tzinfo=None)
        return (observed_local - scheduled).total_seconds() - self.anchor_scheduled_seconds

    @property
    def label_seconds(self) -> float | None:
        """Actual travel time from the anchor to the target. The training label."""
        if self.actual_arrived_at is None:
            return None
        return (self.actual_arrived_at - self.anchor_arrived_at).total_seconds()


# Training rows: every (anchor, target) pair within a trip where both arrivals were
# observed. The cutoff for such a row is the anchor's own arrival time, so the
# label varies naturally with how far ahead the target is.
#
# Deliberately NOT built on the evaluation horizon grid. There, the cutoff is
# defined as the arrival minus the horizon, so the remaining time would always
# equal the horizon exactly and the model would learn nothing.
_TRAINING_SQL = """
select
    a.start_date,
    a.trip_id,
    b.stop_sequence                                  as target_sequence,
    b.stop_id                                        as target_stop_id,
    a.stop_sequence                                  as anchor_sequence,
    a.stop_id                                        as anchor_stop_id,
    a.arrived_at                                     as anchor_arrived_at,
    sa.arrival_seconds                               as anchor_scheduled_seconds,
    sb.arrival_seconds                               as target_scheduled_seconds,
    ((extract(epoch from (a.arrived_at at time zone 'America/Los_Angeles')
              - a.start_date::timestamp) / 3600)::int) as hour_bin,
    extract(isodow from a.start_date) in (6, 7)      as is_weekend,
    a.arrived_at + (coalesce(a.ping_gap_seconds, 0) * interval '1 second')
                                                     as anchor_known_at,
    b.arrived_at                                     as actual_arrived_at,
    b.ping_gap_seconds                               as ping_gap_seconds,
    tr.route_id                                      as route_id,
    null::int                                        as horizon_minutes
from arrivals a
join arrivals b
  on b.start_date = a.start_date and b.trip_id = a.trip_id
 and b.stop_sequence > a.stop_sequence
 and b.stop_sequence <= a.stop_sequence + $4
join stop_times sa
  on sa.feed_version = a.feed_version and sa.trip_id = a.trip_id
 and sa.stop_sequence = a.stop_sequence
join stop_times sb
  on sb.feed_version = b.feed_version and sb.trip_id = b.trip_id
 and sb.stop_sequence = b.stop_sequence
join trips tr
  on tr.feed_version = a.feed_version and tr.trip_id = a.trip_id
where a.start_date between $1 and $2
  -- Both endpoints well observed, so the label is a measurement rather than
  -- mostly interpolation error.
  and a.ping_gap_seconds <= $3
  and b.ping_gap_seconds <= $3
  and sa.arrival_seconds is not null
  and sb.arrival_seconds is not null
  and b.arrived_at > a.arrived_at
  and extract(epoch from b.arrived_at - a.arrived_at) <= 7200
  -- Keep every near pair and a deterministic sample of the far ones. Raising the
  -- cap without this multiplies the row count by roughly four, because pairs grow
  -- with the square of the stop count. hashtext keeps the sample reproducible,
  -- which a random() would not.
  and (
        b.stop_sequence - a.stop_sequence <= $5
        or mod(abs(hashtext(a.trip_id || ':' || a.stop_sequence
                            || ':' || b.stop_sequence)), 100) < $6
      )
"""

# Evaluation rows: for every (arrival, horizon) pair MTS was scored on, the anchor
# as of that cutoff. Scoring the same population is what makes the head-to-head
# airtight.
_CONTEXT_SQL = """
select
    pe.start_date,
    pe.trip_id,
    pe.stop_sequence                                 as target_sequence,
    pe.stop_id                                       as target_stop_id,
    anchor.stop_sequence                             as anchor_sequence,
    anchor.stop_id                                   as anchor_stop_id,
    anchor.arrived_at                                as anchor_arrived_at,
    anchor.known_at                                  as anchor_known_at,
    sa.arrival_seconds                               as anchor_scheduled_seconds,
    sb.arrival_seconds                               as target_scheduled_seconds,
    ((extract(epoch from (anchor.arrived_at at time zone 'America/Los_Angeles')
              - pe.start_date::timestamp) / 3600)::int) as hour_bin,
    extract(isodow from pe.start_date) in (6, 7)     as is_weekend,
    pe.arrived_at                                    as actual_arrived_at,
    pe.ping_gap_seconds,
    pe.route_id,
    pe.horizon_minutes
from prediction_errors pe
join lateral (
    select a.stop_sequence, a.stop_id, a.arrived_at,
           a.arrived_at + (coalesce(a.ping_gap_seconds, 0) * interval '1 second')
               as known_at
    from arrivals a
    where a.start_date = pe.start_date
      and a.trip_id = pe.trip_id
      and a.stop_sequence < pe.stop_sequence
      -- The cutoff, and the reason it is not simply `arrived_at <= cutoff`.
      --
      -- An arrival time is INFERRED by interpolating between the two pings that
      -- bracket the stop, so it only becomes knowable once the later ping has
      -- arrived. Selecting an anchor on arrived_at alone lets a prediction use a
      -- timestamp that was itself computed from GPS received after the cutoff.
      --
      -- That is not hypothetical. With the loose condition the model appeared to
      -- beat MTS at every horizon (0.63 against 0.93 minutes at one minute out);
      -- under this condition it does not. The apparent win was the leak. See
      -- ADR-0038.
      and a.arrived_at + (coalesce(a.ping_gap_seconds, 0) * interval '1 second')
          <= pe.arrived_at - (pe.horizon_minutes * interval '1 minute')
    order by a.stop_sequence desc
    limit 1
) anchor on true
join stop_times sa
  on sa.feed_version = pe.feed_version and sa.trip_id = pe.trip_id
 and sa.stop_sequence = anchor.stop_sequence
join stop_times sb
  on sb.feed_version = pe.feed_version and sb.trip_id = pe.trip_id
 and sb.stop_sequence = pe.stop_sequence
where pe.source = 'mts'
  and pe.start_date between $1 and $2
  and sa.arrival_seconds is not null
  and sb.arrival_seconds is not null
"""


def _to_context(row: asyncpg.Record) -> Context:
    return Context(
        start_date=row["start_date"],
        trip_id=row["trip_id"],
        target_sequence=row["target_sequence"],
        target_stop_id=row["target_stop_id"],
        anchor_sequence=row["anchor_sequence"],
        anchor_stop_id=row["anchor_stop_id"],
        anchor_arrived_at=row["anchor_arrived_at"],
        anchor_scheduled_seconds=row["anchor_scheduled_seconds"],
        target_scheduled_seconds=row["target_scheduled_seconds"],
        hour_bin=row["hour_bin"],
        is_weekend=row["is_weekend"],
        anchor_known_at=row["anchor_known_at"],
        horizon_minutes=row["horizon_minutes"],
        actual_arrived_at=row["actual_arrived_at"],
        ping_gap_seconds=row["ping_gap_seconds"],
        route_id=row["route_id"],
    )


async def load_training_contexts(
    conn: asyncpg.Connection,
    first_day: date,
    last_day: date,
    *,
    max_endpoint_ping_gap: int = 180,
    max_stops_ahead: int = MAX_STOPS_AHEAD,
    near_pair_limit: int = NEAR_PAIR_LIMIT,
    far_pair_sample_pct: int = FAR_PAIR_SAMPLE_PCT,
) -> list[Context]:
    rows = await conn.fetch(
        _TRAINING_SQL,
        first_day,
        last_day,
        max_endpoint_ping_gap,
        max_stops_ahead,
        near_pair_limit,
        far_pair_sample_pct,
    )
    return [_to_context(row) for row in rows]


async def load_evaluation_contexts(
    conn: asyncpg.Connection, first_day: date, last_day: date
) -> list[Context]:
    rows = await conn.fetch(_CONTEXT_SQL, first_day, last_day)
    return [_to_context(row) for row in rows]


def segment_sum(
    context: Context,
    trip_stops: list[tuple[int, str]],
    means: SegmentMeans,
) -> tuple[float, float]:
    """Sum the historical means for the segments between anchor and target.

    Returns the total seconds and the share of segments that fell back to a coarser
    estimate, so a prediction resting mostly on the global mean is identifiable
    rather than indistinguishable from a well supported one.
    """
    relevant = [
        (sequence, stop_id)
        for sequence, stop_id in trip_stops
        if context.anchor_sequence <= sequence <= context.target_sequence
    ]
    if len(relevant) < 2:
        return 0.0, 1.0

    total = 0.0
    coarse = 0
    pairs = 0
    for (_, from_stop), (_, to_stop) in itertools.pairwise(relevant):
        seconds, level = means.lookup(from_stop, to_stop, context.hour_bin, context.is_weekend)
        total += seconds
        pairs += 1
        if level != "exact":
            coarse += 1

    return total, (coarse / pairs if pairs else 1.0)


def build_route_index(contexts: list[Context]) -> dict[str, int]:
    """Stable integer codes for route ids, sorted so a rerun reproduces them.

    Built from the training contexts and then reused for the test matrix. A route
    appearing only at test time maps to -1, which LightGBM treats as an unseen
    category rather than silently folding it into whichever route happened to get
    code 0.
    """
    routes = sorted({c.route_id for c in contexts if c.route_id is not None})
    return {route_id: index for index, route_id in enumerate(routes)}


def build_matrix(
    contexts: list[Context],
    trip_stops: dict[tuple[date, str], list[tuple[int, str]]],
    means: SegmentMeans,
    conditions: RouteConditions | None = None,
    route_index: dict[str, int] | None = None,
) -> tuple[np.ndarray, np.ndarray]:
    """Turn contexts into a feature matrix and label vector.

    Labels are travel time in seconds from the anchor to the target. Contexts with
    no label produce a NaN, which the caller filters.
    """
    features = np.zeros((len(contexts), len(FEATURE_NAMES)), dtype=np.float64)
    labels = np.full(len(contexts), np.nan, dtype=np.float64)

    for index, context in enumerate(contexts):
        stops = trip_stops.get((context.start_date, context.trip_id), [])
        total, coarse_share = segment_sum(context, stops, means)

        if conditions is not None:
            recent_delay, recent_n = conditions.lookup(context.route_id, context.prediction_instant)
        else:
            recent_delay, recent_n = 0.0, 0

        # Where the anchor sits in the trip. A bus three stops from the end behaves
        # differently from one three stops in, even at the same stops_ahead.
        progress = context.anchor_sequence / len(stops) if stops else 0.0

        if route_index is not None and context.route_id is not None:
            route_code = float(route_index.get(context.route_id, -1))
        else:
            route_code = -1.0

        features[index] = (
            context.stops_ahead,
            context.scheduled_remaining_seconds,
            context.anchor_delay_seconds,
            total,
            context.hour_bin,
            1.0 if context.is_weekend else 0.0,
            coarse_share,
            recent_delay,
            recent_n,
            progress,
            context.minute_of_day,
            route_code,
        )
        label = context.label_seconds
        if label is not None:
            labels[index] = label

    return features, labels
