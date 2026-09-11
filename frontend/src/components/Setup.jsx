//: What a fresh deployment shows while it fills its own database. The first
//: run pulls a season's worth of data and replays every gameweek, which takes
//: minutes -- long enough that without this the site looks broken rather than
//: busy.

const STAGES = [
  { id: 'reference', label: 'Players, teams and fixtures' },
  { id: 'history', label: 'Price history' },
  { id: 'backfill', label: 'Replaying the season' },
]

export default function Setup({ setup }) {
  if (!setup) return <div className="skeleton" />

  const reached = STAGES.findIndex((stage) => stage.id === setup.stage)

  return (
    <div className="panel setup">
      <h2>Setting up</h2>
      <div className="panel-body">
        <p className="note">
          This is the first run. The season is being downloaded and replayed
          from gameweek one, which takes a few minutes. Nothing needs doing —
          the page refreshes itself.
        </p>

        <ol className="setup-stages">
          {STAGES.map((stage, index) => (
            <li
              key={stage.id}
              className={
                index < reached ? 'done' : index === reached ? 'active' : ''
              }
            >
              <span className="setup-mark" aria-hidden="true" />
              {stage.label}
            </li>
          ))}
        </ol>

        {setup.players > 0 && (
          <p className="note">
            {setup.players} players in,{' '}
            {setup.gameweeks > 0
              ? `${setup.gameweeks} gameweeks replayed`
              : 'no gameweeks replayed yet'}
            .
          </p>
        )}

        {!setup.scheduler && (
          <p className="note">
            Nothing is filling this in automatically. Run{' '}
            <code>fplai seed</code>, or start the app with{' '}
            <code>FPLAI_SCHEDULER=1</code>.
          </p>
        )}
      </div>
    </div>
  )
}
