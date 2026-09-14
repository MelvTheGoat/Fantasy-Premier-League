// Every call the frontend makes. Kept in one file so the shape of the API is
// visible in one place, and so a change to it fails loudly rather than in a
// component.

//: Where the API lives, relative to wherever this page is served from. On a
//: static host the "API" is a tree of files sitting next to index.html, and on
//: the real API it is that same path handled by a route. Both are substituted
//: at build time, so neither case is special below.
//:
//: The suffix is empty against the real API and `.json` against a static
//: export, where a response has to be a file with an extension -- see the
//: export job for why it cannot be left off.
const base = import.meta.env.BASE_URL
const suffix = import.meta.env.VITE_API_SUFFIX || ''
const api = (path) => `${base}api${path}${suffix}`

const json = async (path) => {
  const response = await fetch(api(path))
  if (!response.ok) {
    const body = await response.json().catch(() => ({}))
    throw new Error(body.detail || `${response.status} ${response.statusText}`)
  }
  return response.json()
}

export const fetchGameweeks = () => json('/gameweeks')
export const fetchSummary = () => json('/summary')
export const fetchGameweek = (model, gameweek) =>
  json(`/${model}/gameweek/${gameweek}`)
export const fetchSeason = (model) => json(`/${model}/season`)
export const fetchPlayer = (model, gameweek, element) =>
  json(`/${model}/gameweek/${gameweek}/player/${element}`)

export const money = (tenths) =>
  tenths == null ? '—' : `£${(tenths / 10).toFixed(1)}m`

export const signed = (value) =>
  value == null ? '—' : `${value > 0 ? '+' : ''}${value}`

export const fetchSetup = () => json('/setup')
