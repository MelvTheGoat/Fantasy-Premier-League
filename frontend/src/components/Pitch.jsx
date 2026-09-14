import Player from './Player.jsx'

// A half pitch, read from the goal line up: keeper at the bottom, then
// defenders, midfielders and forwards. The rows come from whoever was
// selected, so the formation draws itself -- nothing here knows what 3-4-3 is.

const ROWS = ['GKP', 'DEF', 'MID', 'FWD']

// The markings, drawn to real proportions: half of a 105x68m pitch, with the
// goal line along the bottom. Units are decimetres, so 165 is the 16.5m
// penalty area and 91.5 the radius of the centre circle and the D.
//
// `preserveAspectRatio="none"` lets it stretch to whatever shape the pitch
// ends up, which is the point -- the squad decides the height, and the
// markings should follow it rather than dictate it.
function Markings() {
  return (
    <svg
      className="pitch-lines"
      viewBox="0 0 680 525"
      preserveAspectRatio="none"
      aria-hidden="true"
    >
      {/* Halfway line at the top, with the near half of the centre circle. */}
      <line x1="0" y1="1" x2="680" y2="1" />
      <path d="M 248.5 1 A 91.5 91.5 0 0 0 431.5 1" />

      {/* The D, then the penalty area it sits on top of. */}
      <path d="M 266.9 360 A 91.5 91.5 0 0 1 413.1 360" />
      <rect x="138.5" y="360" width="403" height="167" />
      <rect x="248.5" y="470" width="183" height="57" />
      <circle cx="340" cy="415" r="4" className="spot" />
    </svg>
  )
}

export default function Pitch({ starters, bench, chip, onSelect }) {
  const benchBoosted = chip === 'bboost'

  return (
    <>
      <div className="pitch">
        <Markings />
        {ROWS.map((position) => {
          const row = starters.filter((p) => p.position === position)
          if (!row.length) return null
          return (
            <div className="row" key={position}>
              {row.map((player) => (
                <Player key={player.element} player={player} onSelect={onSelect} />
              ))}
            </div>
          )
        }).reverse()}
      </div>

      <div className={`bench${benchBoosted ? ' boosted' : ''}`}>
        <div className="bench-label">
          {benchBoosted ? 'Bench — Bench Boost active, these count' : 'Bench'}
        </div>
        <div className="row">
          {bench.map((player) => (
            <Player
              key={player.element}
              player={player}
              onSelect={onSelect}
              benchBoosted={benchBoosted}
            />
          ))}
        </div>
      </div>
    </>
  )
}
