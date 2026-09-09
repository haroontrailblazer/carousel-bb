import * as React from "react"

const STORAGE_KEY = "carousel-theme"

/** --background for each theme. Mirrors index.css and the boot script in
 *  index.html; all three have to agree or the browser chrome and the page
 *  disagree by a shade. */
const BAR_COLOUR = { dark: "#0F1210", light: "#F7F7F5" } as const

/**
 * Repaint the phone's browser chrome and the page's own frame.
 *
 * Two separate things the CSS cannot reach. `theme-color` is what colours the
 * address bar and the area behind the status bar on a phone - left alone, a
 * dark console sits in a white bar. `color-scheme` is what tells the browser
 * to draw scrollbars, form controls and the canvas behind the page dark,
 * which is why a dark page used to flash white during an overscroll.
 */
function paintBrowserChrome(dark: boolean): void {
  const meta = document.querySelector('meta[name="theme-color"]')
  meta?.setAttribute("content", dark ? BAR_COLOUR.dark : BAR_COLOUR.light)
  document.documentElement.style.colorScheme = dark ? "dark" : "light"
}

/**
 * Light / dark for this browser.
 *
 * The class on <html> is the source of truth, set before first paint by the
 * inline script in index.html - so reading it here is reading what is already
 * on screen, with no flash and no second opinion.
 *
 * Lifted out of the sidebar when the profile page gained a proper appearance
 * control: two components driving the same setting from two private copies of
 * this state would disagree the moment either one changed it.
 */
export function useTheme() {
  const [dark, setDarkState] = React.useState(() =>
    document.documentElement.classList.contains("dark"),
  )

  const setDark = React.useCallback((next: boolean) => {
    document.documentElement.classList.toggle("dark", next)
    paintBrowserChrome(next)
    try {
      localStorage.setItem(STORAGE_KEY, next ? "dark" : "light")
    } catch {
      /* blocked storage: the class is what matters for this session */
    }
    setDarkState(next)
  }, [])

  // Another surface (the sidebar, the profile page) may have changed it while
  // this component was mounted; re-read when the DOM says so.
  React.useEffect(() => {
    const observer = new MutationObserver(() => {
      const dark = document.documentElement.classList.contains("dark")
      setDarkState(dark)
      // Whoever toggled the class may not have gone through setDark - the
      // boot script does not, and neither would anything added later.
      paintBrowserChrome(dark)
    })
    observer.observe(document.documentElement, {
      attributes: true,
      attributeFilter: ["class"],
    })
    return () => observer.disconnect()
  }, [])

  const toggle = React.useCallback(() => setDark(!dark), [dark, setDark])
  return { dark, setDark, toggle }
}
