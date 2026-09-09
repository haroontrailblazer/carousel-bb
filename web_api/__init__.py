"""HTTP surface for the web console: auth, run control, and the SPA.

Split from ``review_api`` (which serves the Telegram review pages) because the
two have different audiences and different auth rules: everything here requires
a signed-in user, while the review pages must stay reachable without one.
"""
