// Every call the frontend makes. Kept in one file so the shape of the API is
// visible in one place, and so a change to it fails loudly rather than in a
// component.

const json = async (path) => {
  const response = await fetch(path)
  if (!response.ok) {
    const body = await response.json().catch(() => ({}))
    throw new Error(body.detail || `${response.status} ${response.statusText}`)
  }
  return response.json()
}

export const fetchGameweeks = () => json('/api/gameweeks')
export const fetchSummary = () => json('/api/summary')
export const fetchGameweek = (model, gameweek) =>
  json(`/api/${model}/gameweek/${gameweek}`)
export const fetchSeason = (model) => json(`/api/${model}/season`)
export const fetchPlayer = (model, gameweek, element) =>
  json(`/api/${model}/gameweek/${gameweek}/player/${element}`)

export const money = (tenths) =>
  tenths == null ? '—' : `£${(tenths / 10).toFixed(1)}m`

export const signed = (value) =>
  value == null ? '—' : `${value > 0 ? '+' : ''}${value}`

export const fetchSetup = () => json('/api/setup')
