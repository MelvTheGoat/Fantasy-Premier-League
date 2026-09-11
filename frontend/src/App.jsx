import { useCallback, useEffect, useMemo, useState } from 'react'
import {
  fetchGameweek,
  fetchGameweeks,
  fetchPlayer,
  fetchSeason,
} from './api.js'
import Pitch from './components/Pitch.jsx'
import PlayerSheet from './components/PlayerSheet.jsx'
import ScoreCard from './components/ScoreCard.jsx'
import Season from './components/Season.jsx'
import Transfers from './components/Transfers.jsx'

const MODELS = [
  { id: 'manager', label: 'The Manager' },
  { id: 'best_xi', label: 'Best XI' },
]

//: How often to re-read the gameweek while it is still provisional. Matches
//: settle over a day or two, so a minute is responsive without being noisy.
const LIVE_POLL_MS = 60_000

export default function App() {
  const [model, setModel] = useState('manager')
  const [gameweeks, setGameweeks] = useState([])
  const [gameweek, setGameweek] = useState(null)
  const [view, setView] = useState(null)
  const [season, setSeason] = useState(null)
  const [selected, setSelected] = useState(null)
  const [error, setError] = useState(null)
  const [loading, setLoading] = useState(true)

  // Land on the most recent gameweek that has picks.
  useEffect(() => {
    fetchGameweeks()
      .then((data) => {
        const playable = data.gameweeks.filter((row) => row.has_picks)
        setGameweeks(playable)
        setGameweek((current) => current ?? data.current ?? playable.at(-1)?.id ?? null)
      })
      .catch((problem) => setError(problem.message))
  }, [])

  const load = useCallback(() => {
    if (gameweek == null) return
    return Promise.all([fetchGameweek(model, gameweek), fetchSeason(model)])
      .then(([gameweekView, seasonView]) => {
        setView(gameweekView)
        setSeason(seasonView)
        setError(null)
      })
      .catch((problem) => {
        setView(null)
        setError(problem.message)
      })
      .finally(() => setLoading(false))
  }, [model, gameweek])

  useEffect(() => {
    setLoading(true)
    load()
  }, [load])

  // While a gameweek is provisional its points are still moving, so keep
  // pulling. Once it is final there is nothing left to change.
  useEffect(() => {
    if (!view || view.final) return undefined
    const timer = setInterval(load, LIVE_POLL_MS)
    return () => clearInterval(timer)
  }, [view, load])

  const index = useMemo(
    () => gameweeks.findIndex((row) => row.id === gameweek),
    [gameweeks, gameweek],
  )

  const openPlayer = useCallback(
    (player) => {
      setSelected(player)
      fetchPlayer(model, gameweek, player.element)
        .then(setSelected)
        .catch(() => {})
    },
    [model, gameweek],
  )

  return (
    <div className="app">
      <header className="masthead">
        <h1>FPL AI Manager</h1>
        <p>Two models playing the 2026/27 season, scored on real points.</p>
      </header>

      <div className="tabs" role="tablist">
        {MODELS.map((entry) => (
          <button
            key={entry.id}
            role="tab"
            aria-selected={model === entry.id}
            onClick={() => setModel(entry.id)}
          >
            {entry.label}
          </button>
        ))}
      </div>

      <div className="gw-bar">
        <button
          onClick={() => setGameweek(gameweeks[index - 1].id)}
          disabled={index <= 0}
          aria-label="Previous gameweek"
        >
          ‹
        </button>
        <select
          value={gameweek ?? ''}
          onChange={(event) => setGameweek(Number(event.target.value))}
          aria-label="Gameweek"
        >
          {gameweeks.map((row) => (
            <option key={row.id} value={row.id}>
              Gameweek {row.id}
            </option>
          ))}
        </select>
        <button
          onClick={() => setGameweek(gameweeks[index + 1].id)}
          disabled={index < 0 || index >= gameweeks.length - 1}
          aria-label="Next gameweek"
        >
          ›
        </button>
      </div>

      {error && <p className="message error">{error}</p>}

      {loading && !view && (
        <>
          <div className="skeleton" />
          <div className="skeleton" style={{ height: 320 }} />
        </>
      )}

      {view && (
        <>
          <ScoreCard view={view} />

          <Pitch
            starters={view.starters}
            bench={view.bench}
            chip={view.chip}
            onSelect={openPlayer}
          />

          {view.chip_reason && (
            <div className="panel">
              <h2>{view.chip_label}</h2>
              <div className="panel-body">
                <p className="note">{view.chip_reason}</p>
              </div>
            </div>
          )}

          {model === 'manager' && <Transfers view={view} />}

          <Season season={season} />
        </>
      )}

      {!loading && !view && !error && (
        <p className="message">No picks stored for this gameweek yet.</p>
      )}

      {selected && (
        <PlayerSheet player={selected} onClose={() => setSelected(null)} />
      )}
    </div>
  )
}
