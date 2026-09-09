import {
  createBrowserRouter,
  Navigate,
  Outlet,
  ScrollRestoration,
  useLocation,
  useParams,
} from "react-router"

import { AppShell } from "@/components/layout/app-shell"
import { AppShellSkeleton } from "@/components/layout/app-shell-skeleton"
import { RouteProgress } from "@/components/layout/route-progress"
import { Spinner } from "@/components/ui/spinner"
import { hadSession, useAuth } from "@/hooks/use-auth"
import {
  historyChunk,
  newRunChunk,
  newsroomChunk,
  profileChunk,
  resetPasswordChunk,
  runDetailChunk,
} from "@/lib/route-chunks"
import { LoginRoute } from "@/routes/login"
import { NotFoundRoute } from "@/routes/not-found"

/**
 * The frame every screen renders inside.
 *
 * It owns the two things that are about NAVIGATION rather than about any one
 * page: the bar that appears when a screen is taking a moment to arrive, and
 * scroll restoration. Both have to sit inside the data router to see its
 * state, and both have to be rendered exactly once - which is what a pathless
 * root route is for.
 */
function RootLayout() {
  return (
    <>
      <RouteProgress />
      {/* Going back to a long task list used to land at the top of it, so the
          row you were reading had to be found again. */}
      <ScrollRestoration />
      <Outlet />
    </>
  )
}

/**
 * What is on screen while the first chunk of the app is still arriving.
 *
 * Screens are downloaded on demand now, which means the very first navigation
 * has one thing to fetch before it can render anything. React Router shows the
 * nearest ancestor's HydrateFallback while that happens; without one it
 * renders null, and a white page is indistinguishable from a broken app.
 *
 * One continuous wait, not two different ones. A browser that has been signed
 * in here before goes straight to the console laid out for THE SCREEN IN THE
 * URL, and stays in that shape through session confirmation and the chunk
 * download until the real screen replaces it. Before, this was a spinner, then
 * a task list, then the screen's own placeholder - three shapes for one wait.
 *
 * `window.location` rather than `useLocation`: a HydrateFallback renders
 * before the router has committed a location, and the address bar is already
 * correct at that point anyway.
 *
 * A first-time visitor still gets the spinner, for the same reason
 * `RequireAuth` gives them one - a console they are about to be redirected
 * away from is a worse thing to show them than nothing.
 */
function BootSplash() {
  if (!hadSession()) {
    return (
      <div className="grid min-h-dvh place-items-center">
        <Spinner label="Loading" />
      </div>
    )
  }
  return (
    <AppShellSkeleton
      pathname={window.location.pathname}
      search={window.location.search}
    />
  )
}

/**
 * Guard every screen behind a signed-in session.
 *
 * A layout route rather than a check inside each page: one place to get right,
 * and a new route is protected by where it sits in the tree rather than by
 * someone remembering to add a line.
 */
function RequireAuth() {
  const { status } = useAuth()
  const location = useLocation()

  if (status === "pending") {
    // Not the login screen. Rendering it here would flash sign-in on every
    // reload before the existing session finishes loading.
    //
    // Which placeholder depends on what this browser has done before.
    // Confirming the session is a round trip, and for someone who was signed
    // in a minute ago it will almost certainly come back "yes" - so they get
    // the console's own layout, drawn empty, and the wait reads as the app
    // laying itself out instead of as a blank page with a spinner on it.
    // Someone arriving for the first time gets the spinner, because a console
    // they are about to be redirected away from is a worse thing to show them
    // than nothing.
    return hadSession() ? (
      <AppShellSkeleton pathname={location.pathname} search={location.search} />
    ) : (
      <div className="grid min-h-dvh place-items-center">
        <Spinner label="Loading" />
      </div>
    )
  }
  if (status === "out") {
    return <Navigate to="/login" state={{ from: location.pathname }} replace />
  }
  return (
    <AppShell>
      <Outlet />
    </AppShell>
  )
}


/**
 * Send an old link to its current equivalent.
 *
 * Two moves happened here: the screens were called "runs" before "tasks", and
 * the review stopped being its own screen - it is a tab on the task now. Both
 * shapes are still out there in bookmarks and in anything already shared, so
 * both land where they should instead of on a 404 that looks like the app is
 * broken.
 */
function LegacyRunRedirect({ review = false }: { review?: boolean }) {
  const { runId } = useParams()
  return <Navigate to={`/tasks/${runId}${review ? "?tab=review" : ""}`} replace />
}

export const router = createBrowserRouter([
  {
    id: "root",
    Component: RootLayout,
    // Every lazy route below resolves under this, so this is the one fallback
    // that has to exist.
    HydrateFallback: BootSplash,
    children: [
      // PUBLIC. Every landing page named in an auth email must be listed here -
      // Oreag's proxy.ts documents what happens otherwise: the links in every
      // password-reset mail dead-end on the sign-in screen.
      //
      // Sign-in is the ONE screen loaded eagerly, in the main bundle. It is
      // small, and it is the screen a signed-out visitor is guaranteed to
      // need - putting it behind its own download would trade a smaller
      // bundle for a second round trip on the very first paint.
      { path: "/login", element: <LoginRoute /> },
      {
        path: "/reset-password",
        lazy: async () => ({
          Component: (await resetPasswordChunk()).ResetPasswordRoute,
        }),
      },

      {
        element: <RequireAuth />,
        children: [
          { path: "/", element: <Navigate to="/new" replace /> },
          {
            path: "/new",
            lazy: async () => ({ Component: (await newRunChunk()).NewRunRoute }),
          },
          {
            path: "/newsroom",
            lazy: async () => ({
              Component: (await newsroomChunk()).NewsroomRoute,
            }),
          },
          {
            path: "/tasks",
            lazy: async () => ({ Component: (await historyChunk()).HistoryRoute }),
          },
          {
            path: "/profile",
            lazy: async () => ({ Component: (await profileChunk()).ProfileRoute }),
          },
          // Legacy paths. These screens were called "runs" before, and links to
          // them exist in browser history and in anything already shared. Keeping
          // the redirects costs three lines and means an old bookmark lands where
          // it should instead of on a 404 that looks like the app is broken.
          { path: "/runs", element: <Navigate to="/tasks" replace /> },
          { path: "/runs/:runId", element: <LegacyRunRedirect /> },
          { path: "/runs/:runId/review", element: <LegacyRunRedirect review /> },
          {
            path: "/tasks/:runId",
            lazy: async () => ({
              Component: (await runDetailChunk()).RunDetailRoute,
            }),
          },
          { path: "/tasks/:runId/review", element: <LegacyRunRedirect review /> },
        ],
      },
      { path: "*", element: <NotFoundRoute /> },
    ],
  },
])
