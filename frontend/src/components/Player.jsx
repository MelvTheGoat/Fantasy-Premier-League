import { useState } from 'react'

// One player on the pitch: shirt, name, fixture, points.
//
// The badges carry what the rules did to them -- captained, tripled, or moved
// by an automatic substitution -- because that is the part a scoreline alone
// never explains.

export default function Player({ player, onSelect, benchBoosted }) {
  const [shirtFailed, setShirtFailed] = useState(false)
  const counts = player.multiplier > 0
  const fixture = player.fixtures?.length
    ? player.fixtures
        .map((f) => `${f.opponent} (${f.home ? 'H' : 'A'})`)
        .join(', ')
    : 'No fixture'

  return (
    <button
      className={`player${player.subbed_off ? ' out' : ''}`}
      onClick={() => onSelect(player)}
      aria-label={`${player.name}, ${player.position}, ${player.points} points`}
    >
      {player.is_captain && (
        <span className="badge" title={player.triple_captain ? 'Triple Captain' : 'Captain'}>
          {player.triple_captain ? 'TC' : 'C'}
        </span>
      )}
      {!player.is_captain && player.is_vice_captain && (
        <span className="badge vice" title="Vice-captain">V</span>
      )}
      {player.subbed_on && (
        <span className="badge sub-on" title="Substituted on">↑</span>
      )}
      {player.subbed_off && (
        <span className="badge sub-off" title="Substituted off">↓</span>
      )}

      {/* If the shirt CDN is unreachable, fall back to the club's initials
          rather than leaving a hole where the shirt should be. */}
      {shirtFailed ? (
        <div className="player-shirt shirt-fallback" aria-hidden="true">
          {player.team}
        </div>
      ) : (
        <img
          className="player-shirt"
          src={player.shirt}
          alt=""
          loading="lazy"
          onError={() => setShirtFailed(true)}
        />
      )}
      <div className="player-name">{player.name}</div>
      <div className="player-fixture">{fixture}</div>
      <div className={`player-points${counts || benchBoosted ? '' : ' zero'}`}>
        {player.total}
        {player.multiplier > 1 && <span> ×{player.multiplier}</span>}
      </div>
    </button>
  )
}
