// Season totals, and a cumulative line against the official average.
//
// The chart is a hand-drawn SVG rather than a charting library: two series and
// a few dozen points do not justify the dependency, and an inline SVG scales
// to any width without a resize observer.

const Chart = ({ cumulative }) => {
  if (cumulative.length < 2) return null

  const width = 300
  const height = 110
  const padding = { top: 6, right: 4, bottom: 16, left: 4 }
  const maximum = Math.max(
    ...cumulative.map((row) => Math.max(row.points, row.average || 0)),
    1,
  )

  const x = (index) =>
    padding.left +
    (index / (cumulative.length - 1)) * (width - padding.left - padding.right)
  const y = (value) =>
    height - padding.bottom - (value / maximum) * (height - padding.top - padding.bottom)

  const line = (key) =>
    cumulative
      .map((row, index) => `${index ? 'L' : 'M'}${x(index).toFixed(1)} ${y(row[key] || 0).toFixed(1)}`)
      .join(' ')

  const last = cumulative[cumulative.length - 1]

  return (
    <div className="chart">
      <div className="legend">
        <span>
          <i className="swatch" style={{ background: 'var(--accent)' }} />
          Model {last.points}
        </span>
        <span>
          <i className="swatch" style={{ background: 'var(--text-faint)' }} />
          Average {Math.round(last.average || 0)}
        </span>
      </div>
      <svg
        viewBox={`0 0 ${width} ${height}`}
        width="100%"
        height={height}
        role="img"
        aria-label={`Cumulative points ${last.points} against an average of ${Math.round(last.average || 0)}`}
      >
        <path d={line('average')} fill="none" stroke="var(--text-faint)"
              strokeWidth="1.5" strokeDasharray="3 3" />
        <path d={line('points')} fill="none" stroke="var(--accent)"
              strokeWidth="2" strokeLinejoin="round" />
        {cumulative.map((row, index) => (
          <circle key={row.gameweek} cx={x(index)} cy={y(row.points)} r="2"
                  fill="var(--accent)" />
        ))}
      </svg>
    </div>
  )
}

export default function Season({ season }) {
  if (!season || !season.gameweeks_played) return null

  return (
    <div className="panel">
      <h2>Season</h2>

      <div className="stats">
        <div className="stat">
          <div className="stat-value">{season.total_points}</div>
          <div className="stat-label">Total points</div>
        </div>
        <div className="stat">
          <div className="stat-value">
            {season.gameweeks_beating_average}
            <span style={{ color: 'var(--text-faint)', fontSize: 15 }}>
              /{season.gameweeks_played}
            </span>
          </div>
          <div className="stat-label">Beat average</div>
        </div>
        <div className="stat">
          <div className="stat-value">
            {season.total_transfer_cost ? `−${season.total_transfer_cost}` : '0'}
          </div>
          <div className="stat-label">Hits</div>
        </div>
      </div>

      <Chart cumulative={season.cumulative} />

      {season.chips_used?.length > 0 && (
        <div className="panel-body">
          {season.chips_used.map((chip) => (
            <p className="note" key={`${chip.chip}-${chip.gameweek}`}>
              <strong style={{ color: 'var(--captain)' }}>{chip.label}</strong>
              {' — '}
              {chip.reason || `played in GW${chip.gameweek}`}
            </p>
          ))}
        </div>
      )}
    </div>
  )
}
