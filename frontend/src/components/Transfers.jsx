import { money, signed } from '../api.js'

// Every transfer, with the numbers that drove it side by side. Collapsed by
// default: the decision is the headline, the evidence is one tap away.

const Fixtures = ({ rows }) => (
  <div className="fixture-strip">
    {rows?.length
      ? rows.flatMap((row) =>
          row.fixtures.length
            ? row.fixtures.map((f, index) => (
                <span key={`${row.gameweek}-${index}`} className={`fdr fdr-${f.difficulty}`}>
                  {f.opponent}
                  {f.home ? '' : ' (a)'}
                </span>
              ))
            : [
                <span key={row.gameweek} className="fdr fdr-5">
                  blank
                </span>,
              ],
        )
      : '—'}
  </div>
)

const Row = ({ label, out, in: incoming, format = (v) => v }) => (
  <tr>
    <td>{label}</td>
    <td>{format(out)}</td>
    <td>{format(incoming)}</td>
  </tr>
)

export default function Transfers({ view }) {
  const transfers = view.transfers || []

  return (
    <div className="panel">
      <h2>Transfers</h2>

      {transfers.length === 0 && (
        <div className="panel-body">
          <p className="note">
            {view.roll_reason || 'No transfers made this gameweek.'}
          </p>
        </div>
      )}

      {transfers.map((transfer) => {
        const { out, in: incoming } = transfer.comparison
        const gain = transfer.comparison.projected_gain

        return (
          <details className="transfer" key={`${transfer.out}-${transfer.in}`}>
            <summary>
              <span className="swap-out">{out.name}</span>
              <span className="swap-arrow">→</span>
              <span className="swap-in">{incoming.name}</span>
              {transfer.was_hit && <span className="swap-arrow">(−4)</span>}
            </summary>

            <p className="note" style={{ padding: '0 16px' }}>{transfer.reason}</p>

            <table className="compare">
              <thead>
                <tr>
                  <th />
                  <th>{out.name}</th>
                  <th>{incoming.name}</th>
                </tr>
              </thead>
              <tbody>
                <Row label="Projected (horizon)" out={out.projected_points}
                     in={incoming.projected_points} />
                <Row label="Next gameweek" out={out.next_gameweek_points}
                     in={incoming.next_gameweek_points} />
                <tr>
                  <td>Projected gain</td>
                  <td />
                  <td className={gain >= 0 ? 'gain' : 'loss'}>{signed(gain)}</td>
                </tr>
                <Row label="Price" out={out.price} in={incoming.price} format={money} />
                <Row label="Season points" out={out.season_points}
                     in={incoming.season_points} />
                <Row label="Minutes" out={out.minutes} in={incoming.minutes} />
                <Row label="Starts" out={out.starts} in={incoming.starts} />
                <Row label="Goals" out={out.goals} in={incoming.goals} />
                <Row label="Assists" out={out.assists} in={incoming.assists} />
                <Row label="xG" out={out.expected_goals} in={incoming.expected_goals} />
                <Row label="xA" out={out.expected_assists} in={incoming.expected_assists} />
                <Row label="DefCon rate" out={out.defcon_rate} in={incoming.defcon_rate} />
                <tr>
                  <td>Fixtures</td>
                  <td><Fixtures rows={out.fixtures} /></td>
                  <td><Fixtures rows={incoming.fixtures} /></td>
                </tr>
                {(out.availability || incoming.availability) && (
                  <tr>
                    <td>Availability</td>
                    <td>{out.availability || 'Fit'}</td>
                    <td>{incoming.availability || 'Fit'}</td>
                  </tr>
                )}
              </tbody>
            </table>
          </details>
        )
      })}
    </div>
  )
}
