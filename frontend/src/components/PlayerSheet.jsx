import { useEffect } from 'react'
import { money } from '../api.js'

// The detail sheet, opened by tapping a player. The selection reason is hidden
// until then by design: the pitch should read as a team, not as a wall of
// justification.

const percent = (value) => (value == null ? '—' : `${Math.round(value * 100)}%`)

export default function PlayerSheet({ player, onClose }) {
  useEffect(() => {
    const onKey = (event) => event.key === 'Escape' && onClose()
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [onClose])

  if (!player) return null
  const projection = player.projection

  return (
    <div className="sheet-backdrop" onClick={onClose} role="presentation">
      <div
        className="sheet"
        onClick={(event) => event.stopPropagation()}
        role="dialog"
        aria-modal="true"
        aria-label={player.name}
      >
        <div className="sheet-handle" />

        <div className="sheet-head">
          <img src={player.shirt} alt="" />
          <div>
            <h3>{player.name}</h3>
            <p>
              {player.position} · {player.team} · {money(player.price)}
              {player.is_captain && ' · Captain'}
            </p>
          </div>
          <div style={{ marginLeft: 'auto', textAlign: 'right' }}>
            <div style={{ fontSize: 26, fontWeight: 700 }}>{player.total}</div>
            <div style={{ fontSize: 11, color: 'var(--text-faint)' }}>
              {player.multiplier > 1 ? `${player.points} × ${player.multiplier}` : 'points'}
            </div>
          </div>
        </div>

        {player.reason && <p className="sheet-reason">{player.reason}</p>}

        {(player.subbed_on || player.subbed_off) && (
          <p className="sheet-reason" style={{ color: player.subbed_on ? 'var(--accent)' : 'var(--bad)' }}>
            {player.subbed_on
              ? 'Substituted on automatically, so these points count.'
              : 'Played no minutes, so was substituted out automatically.'}
          </p>
        )}

        {projection && (
          <dl className="sheet-grid">
            <div>
              <dt>Projected</dt>
              <dd>{projection.expected_points}</dd>
            </div>
            <div>
              <dt>Actual</dt>
              <dd>{player.points}</dd>
            </div>
            <div>
              <dt>Chance of starting</dt>
              <dd>{percent(projection.probability_of_start)}</dd>
            </div>
            <div>
              <dt>Minutes played</dt>
              <dd>{player.minutes}</dd>
            </div>
            <div>
              <dt>xG</dt>
              <dd>{projection.expected_goals}</dd>
            </div>
            <div>
              <dt>xA</dt>
              <dd>{projection.expected_assists}</dd>
            </div>
            <div>
              <dt>Clean sheet</dt>
              <dd>{percent(projection.clean_sheet_probability)}</dd>
            </div>
            <div>
              <dt>DefCon</dt>
              <dd>{percent(projection.defcon_probability)}</dd>
            </div>
          </dl>
        )}
      </div>
    </div>
  )
}
