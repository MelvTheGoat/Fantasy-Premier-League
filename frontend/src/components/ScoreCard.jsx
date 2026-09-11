import { signed } from '../api.js'

// The headline: points, whether they are final yet, and how they compare with
// the official FPL average for that gameweek.

export default function ScoreCard({ view }) {
  const hasPoints = view.points != null
  const margin = view.margin

  return (
    <div className="score">
      <div className="score-top">
        <span className="score-points">{hasPoints ? view.points : '–'}</span>
        <span className="score-unit">pts</span>
        <span className={`status${view.final ? ' final' : ''}`}>{view.status}</span>
      </div>

      <div className="score-meta">
        {view.average != null && (
          <span>
            Average {view.average}
            {margin != null && (
              <>
                {' '}
                <span className={`delta ${margin > 0 ? 'up' : margin < 0 ? 'down' : ''}`}>
                  {signed(margin)}
                </span>
              </>
            )}
          </span>
        )}
        {view.formation && <span>{view.formation}</span>}
        {view.transfer_cost > 0 && <span>Hits −{view.transfer_cost}</span>}
        {view.bench_points != null && <span>Bench {view.bench_points}</span>}
      </div>

      {view.chip_label && (
        <div className="chip-badge">{view.chip_label}</div>
      )}
    </div>
  )
}
