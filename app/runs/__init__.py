"""Driving pipeline runs from a long-lived process, and streaming them out.

``bus`` fans live events out to connected browsers, ``stream`` turns ADK events
into the timeline both a fresh run and a review resume record, ``service``
starts and supervises runs, and ``recovery`` deals with runs that a restart
interrupted.
"""
