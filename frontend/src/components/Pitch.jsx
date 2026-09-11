import Player from './Player.jsx'

// A half pitch, read from the goal line up: keeper at the bottom, then
// defenders, midfielders and forwards. The rows come from whoever was
// selected, so the formation draws itself -- nothing here knows what 3-4-3 is.

const ROWS = ['GKP', 'DEF', 'MID', 'FWD']

export default function Pitch({ starters, bench, chip, onSelect }) {
  const benchBoosted = chip === 'bboost'

  return (
    <>
      <div className="pitch">
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
