import { StrictMode } from "react"
import { createRoot } from "react-dom/client"

import { App } from "@/App"
import { loadAuthConfig } from "@/lib/supabase"
import "@/index.css"

// Start fetching the Supabase configuration NOW, in parallel with React
// mounting, rather than when the first component happens to ask for it.
//
// The client cannot be constructed without it - the project URL and anon key
// come from the backend so one build works against any deployment - and the
// first thing that needs the client is the sidebar asking who is signed in.
// Left alone, that made the boot sequence a chain: download the bundle, mount,
// render, ask for the profile, THEN start a round trip that could have been
// running the whole time.
//
// `loadAuthConfig` caches its promise, so this is a head start and not a
// second request. It is deliberately not awaited: a failure here is handled
// where it matters, on the sign-in screen, which explains that the console is
// not configured. Blocking the render on it would mean a blank page instead.
void loadAuthConfig()

createRoot(document.getElementById("root")!).render(
  <StrictMode>
    <App />
  </StrictMode>,
)
